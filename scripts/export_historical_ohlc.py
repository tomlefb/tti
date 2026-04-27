"""One-shot historical OHLC export (Sprint 0 → Sprint 1 handoff).

Run this ONCE on the Windows host with the MT5 terminal open and logged in.
It dumps ~6 months of OHLC data for each watched pair across the four
timeframes used by the strategy (D1, H4, H1, M5) into:

    tests/fixtures/historical/{SYMBOL}_{TF}.parquet

These fixtures unblock Sprint 1+ detector development on the Mac, where the
``MetaTrader5`` package has no wheel. They are committed to the repo. Do NOT
regenerate them casually — a regeneration shifts the underlying data and
silently invalidates any test baselines computed against them. See
``tests/fixtures/README.md``.

Stored schema (one row per candle):
    time          UTC datetime64[ns]      candle open time, converted from broker time
    open          float64                 OHLC as returned by MT5
    high          float64
    low           float64
    close         float64
    tick_volume   int64                   tick count
    real_volume   int64                   exchange/contract volume (0 for many FX brokers)
    spread        int64                   in points

HARD CONSTRAINT — DO NOT REMOVE:
    This script must contain ZERO calls to any order placement /
    modification / closure function:
        mt5.order_send, mt5.order_modify, mt5.order_close, mt5.order_check,
        mt5.order_calc_margin, or any other ``mt5.order_*`` function.
    The TJR system is detection + notification only. The human places
    every trade manually. See CLAUDE.md rule #1 and
    docs/04_PROJECT_RULES.md "no auto-trading code".
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

from _bootstrap import load_settings

# Output directory, relative to repo root. The bootstrap inserts the repo
# root into sys.path, so we resolve via __file__ to stay robust to cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_OUTPUT_DIR = _REPO_ROOT / "tests" / "fixtures" / "historical"


# Per-timeframe candle-count requests. Numbers target ~6 months of history
# with safety margin; brokers cap server-side history independently, so
# actual returned rows may be lower (we report what we get).
#
# M5 note: a true 24h * 5d * 26w ≈ 15600 candles for 6 months; FX/metals
# trade ~5d/week, ~24h/day, but indices have shorter sessions. 60_000 is
# generous and lets the broker cap us as needed.
_TIMEFRAME_REQUESTS = [
    ("D1", 250),  # ~6 months of daily candles, with margin
    ("H4", 1500),  # ~6 months of 4h candles
    ("H1", 6000),  # ~6 months of 1h candles
    ("M5", 60000),  # ~5 months realistic given MT5 server-side history limits
]


def _resolve_timeframes(mt5) -> dict[str, int]:
    """Map our string names to ``mt5.TIMEFRAME_*`` integer constants.

    Done at runtime because ``MetaTrader5`` is a Windows-only import.
    """
    return {
        "D1": mt5.TIMEFRAME_D1,
        "H4": mt5.TIMEFRAME_H4,
        "H1": mt5.TIMEFRAME_H1,
        "M5": mt5.TIMEFRAME_M5,
    }


def _fetch_and_save(mt5, pd, symbol: str, tf_name: str, tf_const: int, n_candles: int) -> dict:
    """Fetch ``n_candles`` for ``(symbol, tf_const)`` and write a parquet.

    Returns a dict with the outcome:
        {"ok": bool, "symbol": ..., "tf": ..., "rows": int, "path": Path,
         "date_min": str | None, "date_max": str | None, "error": str | None}
    """
    rates = mt5.copy_rates_from_pos(symbol, tf_const, 0, n_candles)
    if rates is None or len(rates) == 0:
        return {
            "ok": False,
            "symbol": symbol,
            "tf": tf_name,
            "rows": 0,
            "path": None,
            "date_min": None,
            "date_max": None,
            "error": f"copy_rates_from_pos returned no data; mt5.last_error()={mt5.last_error()!r}",
        }

    df = pd.DataFrame(rates)

    # MT5 returns 'time' as Unix seconds in BROKER timezone. The seconds
    # value is the broker-local wall-clock interpreted as if it were UTC,
    # so this is technically a broker-time conversion masquerading as UTC.
    # TODO: refactor to use src/mt5_client time-conversion helpers once
    #       Sprint 1 implements them; until then, broker-server offset
    #       must be normalized at consumption time, not here.
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)

    # Sort + dedupe defensively. MT5 normally returns sorted, unique rows,
    # but a previous incomplete fetch can leave duplicates if the script
    # is re-run during an active candle.
    df = df.sort_values("time").drop_duplicates(subset="time").reset_index(drop=True)

    out_path = _OUTPUT_DIR / f"{symbol}_{tf_name}.parquet"
    df.to_parquet(out_path, engine="pyarrow", index=False)

    date_min = df["time"].iloc[0].isoformat()
    date_max = df["time"].iloc[-1].isoformat()
    return {
        "ok": True,
        "symbol": symbol,
        "tf": tf_name,
        "rows": len(df),
        "path": out_path,
        "date_min": date_min,
        "date_max": date_max,
        "error": None,
    }


def _format_size_mb(paths: list[Path]) -> float:
    total_bytes = sum(p.stat().st_size for p in paths if p is not None and p.exists())
    return total_bytes / (1024 * 1024)


def main() -> int:
    # Windows consoles default to cp1252, which can't encode the ✓/✗ glyphs
    # used in progress lines. Force UTF-8 so the script is portable.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    settings = load_settings()

    try:
        import MetaTrader5 as mt5  # type: ignore[import-not-found]
    except ImportError:
        print(
            "ERROR: MetaTrader5 package not installed. This script must run "
            "on the Windows host. Install with `pip install -r requirements.txt`.",
            file=sys.stderr,
        )
        return 2

    try:
        import pandas as pd
    except ImportError:
        print("ERROR: pandas not installed — see requirements.txt.", file=sys.stderr)
        return 2

    try:
        import pyarrow  # noqa: F401  — required by df.to_parquet(engine='pyarrow')
    except ImportError:
        print(
            "ERROR: pyarrow not installed — see requirements.txt. Run "
            "`pip install -r requirements.txt` and try again.",
            file=sys.stderr,
        )
        return 2

    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[{datetime.now(UTC).isoformat()}] Historical OHLC export starting")
    print(f"  output directory : {_OUTPUT_DIR.relative_to(_REPO_ROOT)}")
    print(f"  watched pairs    : {', '.join(settings.WATCHED_PAIRS)}")
    print(f"  timeframes       : {', '.join(tf for tf, _ in _TIMEFRAME_REQUESTS)}")

    initialized = False
    results: list[dict] = []
    try:
        initialized = mt5.initialize(
            login=int(settings.MT5_LOGIN),
            password=str(settings.MT5_PASSWORD),
            server=str(settings.MT5_SERVER),
        )
        if not initialized:
            err = mt5.last_error()
            print(
                "ERROR: mt5.initialize() failed. Check that the MT5 "
                "terminal is open and logged in, and that MT5_LOGIN / "
                "MT5_PASSWORD / MT5_SERVER in config/secrets.py are correct.",
                file=sys.stderr,
            )
            print(f"  mt5.last_error() = {err!r}", file=sys.stderr)
            return 1

        timeframes = _resolve_timeframes(mt5)

        for symbol in settings.WATCHED_PAIRS:
            for tf_name, n_candles in _TIMEFRAME_REQUESTS:
                tf_const = timeframes[tf_name]
                try:
                    res = _fetch_and_save(mt5, pd, symbol, tf_name, tf_const, n_candles)
                except Exception as exc:  # noqa: BLE001 — boundary, want to continue
                    res = {
                        "ok": False,
                        "symbol": symbol,
                        "tf": tf_name,
                        "rows": 0,
                        "path": None,
                        "date_min": None,
                        "date_max": None,
                        "error": f"{type(exc).__name__}: {exc}",
                    }

                results.append(res)
                if res["ok"]:
                    print(
                        f"  ✓ {symbol} {tf_name}: {res['rows']} candles, "
                        f"{res['date_min']} → {res['date_max']}"
                    )
                else:
                    print(
                        f"  ✗ {symbol} {tf_name}: FAILED — {res['error']}",
                        file=sys.stderr,
                    )

    finally:
        if initialized:
            mt5.shutdown()

    # ---- Summary ----------------------------------------------------------
    successes = [r for r in results if r["ok"]]
    failures = [r for r in results if not r["ok"]]
    written_paths = [r["path"] for r in successes]
    total_files_expected = len(settings.WATCHED_PAIRS) * len(_TIMEFRAME_REQUESTS)

    if successes:
        all_min = min(r["date_min"] for r in successes)
        all_max = max(r["date_max"] for r in successes)
    else:
        all_min = all_max = "n/a"

    print()
    print("Symbols:           " + ", ".join(settings.WATCHED_PAIRS))
    print("Timeframes:        " + ", ".join(tf for tf, _ in _TIMEFRAME_REQUESTS))
    print(f"Total files written: {len(successes)} / {total_files_expected}")
    print(f"Total disk usage:    {_format_size_mb(written_paths):.2f} MB")
    print(f"Date range covered:  {all_min} → {all_max}")
    if failures:
        print("Failures:")
        for r in failures:
            print(f"  - {r['symbol']} {r['tf']}: {r['error']}")
    else:
        print("Failures:           none")

    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
