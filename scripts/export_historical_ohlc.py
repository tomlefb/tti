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

import argparse
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


# Single-call MT5 history fetch is capped server-side around 99_999 candles.
# In max-history mode we paginate M5 explicitly; D1/H4/H1 fit comfortably in
# one call even for 3 years (1095 D1, ~6500 H4, ~26_300 H1 candles).
_MT5_MAX_CANDLES_PER_CALL = 99_999


# When a requested symbol isn't directly available on the broker, try these
# common alternative spellings before giving up. FundedNext (and other prop
# firms) often suffix symbols (.r, .cash) or rename indices. The matching is
# case-sensitive — we test each variant via mt5.symbol_info().
_SYMBOL_ALTERNATIVES: dict[str, list[str]] = {
    "SPX500": ["SPX500.r", "US500", "US500.r", "SP500", "S&P500"],
    "US30": ["US30.r", "DJ30", "DJ30.r", "US30Cash", "US30.cash"],
    "GER30": ["GER30.r", "DE30", "DE30.r", "DAX30", "DE40", "DE40.r", "GER40", "GER40.r"],
    "USOIL": ["USOUSD", "USOIL.r", "WTI", "WTI.r", "OIL", "OIL.r", "XTIUSD", "XTIUSD.r"],
    "XAGUSD": ["XAGUSD.r", "SILVER"],
    "BTCUSD": ["BTCUSD.r", "BITCOIN", "BTC/USD"],
    "ETHUSD": ["ETHUSD.r", "ETHEREUM", "ETH/USD"],
    "USDJPY": ["USDJPY.r"],
    "XAUUSD": ["XAUUSD.r", "GOLD"],
    "NDX100": ["NDX100.r", "USTEC", "NAS100", "NQ100", "NASDAQ100"],
    "EURUSD": ["EURUSD.r"],
    "GBPUSD": ["GBPUSD.r"],
}


def _resolve_symbol(mt5, requested: str) -> str | None:
    """Return the actual broker symbol name, or ``None`` if unavailable.

    Tries the requested name first, then the built-in alternatives list.
    A symbol is considered available if ``mt5.symbol_info()`` returns a
    non-None info object.
    """
    candidates = [requested] + _SYMBOL_ALTERNATIVES.get(requested, [])
    for name in candidates:
        info = mt5.symbol_info(name)
        if info is not None:
            return name
    return None


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


def _read_existing_stats(symbol: str, tf_name: str) -> dict | None:
    """Read an existing parquet fixture and return summary stats.

    Used in max-history mode to print before/after comparisons. Returns
    ``None`` if the file does not exist or cannot be read.
    """
    path = _OUTPUT_DIR / f"{symbol}_{tf_name}.parquet"
    if not path.exists():
        return None
    try:
        import pandas as pd  # local import; pandas is verified in main()

        df = pd.read_parquet(path, engine="pyarrow")
        if len(df) == 0:
            return {"rows": 0, "date_min": None, "date_max": None}
        return {
            "rows": len(df),
            "date_min": df["time"].iloc[0].isoformat(),
            "date_max": df["time"].iloc[-1].isoformat(),
        }
    except Exception:  # noqa: BLE001 — best-effort; just skip the comparison
        return None


def _fetch_max_history(
    mt5, np_, symbol: str, tf_name: str, tf_const: int, max_days: int
):
    """Fetch up to ``max_days`` of candles for ``symbol`` at ``tf_const``.

    Uses position-based pagination via ``copy_rates_from_pos`` because it is
    the only MT5 API that paginates deterministically without ambiguity
    around date_from semantics (which goes backward, not forward, despite
    the parameter name suggesting otherwise).

    For D1/H4/H1 a single call is enough — 3 years × 24 hourly bars ≈
    26_280, well under the ~99_999 per-call cap. For M5 we walk backward
    in batches: positions 0..N-1, then N..2N-1, etc., until either the
    broker stops returning data or we've collected ``max_days`` worth.

    Returns a numpy structured array (concatenated across pages) or ``None``
    when the broker serves nothing.
    """
    if tf_name != "M5":
        # Single call. Pad heavily; the broker caps server-side anyway.
        per_day = {"D1": 1, "H4": 6, "H1": 24}.get(tf_name, 24)
        n_target = min(max_days * per_day + 200, _MT5_MAX_CANDLES_PER_CALL)
        return mt5.copy_rates_from_pos(symbol, tf_const, 0, n_target)

    # M5 paginated by position, walking backward from the most recent bar.
    # 24/7 crypto: 1095d × 24h × 12 = 315_360 candles ≈ 4 calls.
    # Weekday-only instruments (FX/indices): ~5/7 of that ≈ 3 calls.
    # We overshoot the target slightly to absorb any session-coverage
    # variation, then concatenate and trim later via dedup + sort.
    target_count = max_days * 24 * 12 + 5_000
    chunks = []
    collected = 0
    start_pos = 0
    iterations = 0
    while collected < target_count and iterations < 50:
        iterations += 1
        request_n = min(_MT5_MAX_CANDLES_PER_CALL, target_count - collected)
        if request_n <= 0:
            break
        chunk = mt5.copy_rates_from_pos(symbol, tf_const, start_pos, request_n)
        if chunk is None or len(chunk) == 0:
            break
        chunks.append(chunk)
        collected += len(chunk)
        # If the broker returned fewer rows than requested, it has nothing
        # older to give — stop paginating.
        if len(chunk) < request_n:
            break
        start_pos += len(chunk)

    if not chunks:
        return None
    return np_.concatenate(chunks) if len(chunks) > 1 else chunks[0]


def _fetch_and_save_max_history(
    mt5, pd, np_, symbol: str, tf_name: str, tf_const: int, max_days: int
) -> dict:
    """Like ``_fetch_and_save`` but uses paginated max-history fetching."""
    rates = _fetch_max_history(mt5, np_, symbol, tf_name, tf_const, max_days)
    if rates is None or len(rates) == 0:
        return {
            "ok": False,
            "symbol": symbol,
            "tf": tf_name,
            "rows": 0,
            "path": None,
            "date_min": None,
            "date_max": None,
            "error": f"no data returned; mt5.last_error()={mt5.last_error()!r}",
        }

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.sort_values("time").drop_duplicates(subset="time").reset_index(drop=True)

    out_path = _OUTPUT_DIR / f"{symbol}_{tf_name}.parquet"
    df.to_parquet(out_path, engine="pyarrow", index=False)

    return {
        "ok": True,
        "symbol": symbol,
        "tf": tf_name,
        "rows": len(df),
        "path": out_path,
        "date_min": df["time"].iloc[0].isoformat(),
        "date_max": df["time"].iloc[-1].isoformat(),
        "error": None,
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


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "One-shot historical OHLC export. Defaults to settings.WATCHED_PAIRS; "
            "pass --symbols to override."
        )
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=None,
        help=(
            "List of canonical symbol names to export (e.g. SPX500 US30). "
            "Broker-specific aliases (.r, .cash, etc.) are tried automatically. "
            "If omitted, settings.WATCHED_PAIRS is used."
        ),
    )
    parser.add_argument(
        "--max-history-days",
        type=int,
        default=None,
        help=(
            "If set, ignore the default _TIMEFRAME_REQUESTS counts and instead "
            "fetch up to this many days of history per timeframe. M5 is "
            "paginated forward from the start date because a single MT5 call "
            "is capped at ~99_999 candles (~1 year of M5). The broker may "
            "serve less than requested — the script reports what was returned."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to cp1252, which can't encode the ✓/✗ glyphs
    # used in progress lines. Force UTF-8 so the script is portable.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    args = _parse_args(argv)
    settings = load_settings()
    requested_symbols: list[str] = list(args.symbols) if args.symbols else list(settings.WATCHED_PAIRS)

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
        import numpy as np
    except ImportError:
        print("ERROR: numpy not installed — see requirements.txt.", file=sys.stderr)
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

    # In max-history mode we capture pre-fetch stats per (symbol, tf) so the
    # final summary can show old → new comparisons. This is read from disk
    # before any fetching happens.
    max_history_days: int | None = args.max_history_days
    pre_stats: dict[tuple[str, str], dict | None] = {}
    if max_history_days is not None:
        for sym in requested_symbols:
            for tf_name, _ in _TIMEFRAME_REQUESTS:
                pre_stats[(sym, tf_name)] = _read_existing_stats(sym, tf_name)
    pre_dir_size_mb = sum(
        p.stat().st_size for p in _OUTPUT_DIR.glob("*.parquet")
    ) / (1024 * 1024) if _OUTPUT_DIR.exists() else 0.0

    print(f"[{datetime.now(UTC).isoformat()}] Historical OHLC export starting")
    print(f"  output directory : {_OUTPUT_DIR.relative_to(_REPO_ROOT)}")
    print(f"  requested symbols: {', '.join(requested_symbols)}")
    print(f"  timeframes       : {', '.join(tf for tf, _ in _TIMEFRAME_REQUESTS)}")
    if max_history_days is not None:
        print(f"  max history days : {max_history_days} (paginated)")

    initialized = False
    results: list[dict] = []
    # Map of requested name -> resolved broker name (or None if unavailable).
    resolution: dict[str, str | None] = {}
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

        # ---- Symbol availability probe -----------------------------------
        # Resolve every requested symbol up-front, before any fetching, so
        # the operator can see which names mapped to which broker symbols.
        print()
        print("Symbol availability:")
        for requested in requested_symbols:
            resolved = _resolve_symbol(mt5, requested)
            resolution[requested] = resolved
            if resolved is None:
                print(f"  ✗ {requested}: NOT AVAILABLE on broker")
            elif resolved == requested:
                print(f"  ✓ {requested}: OK")
            else:
                print(f"  ✓ {requested}: OK (broker name: {resolved})")

        timeframes = _resolve_timeframes(mt5)

        for requested, resolved in resolution.items():
            if resolved is None:
                continue
            for tf_name, n_candles in _TIMEFRAME_REQUESTS:
                tf_const = timeframes[tf_name]
                try:
                    if max_history_days is not None:
                        res = _fetch_and_save_max_history(
                            mt5, pd, np, resolved, tf_name, tf_const, max_history_days
                        )
                    else:
                        res = _fetch_and_save(mt5, pd, resolved, tf_name, tf_const, n_candles)
                except Exception as exc:  # noqa: BLE001 — boundary, want to continue
                    res = {
                        "ok": False,
                        "symbol": resolved,
                        "tf": tf_name,
                        "rows": 0,
                        "path": None,
                        "date_min": None,
                        "date_max": None,
                        "error": f"{type(exc).__name__}: {exc}",
                    }

                res["requested"] = requested
                results.append(res)
                if res["ok"]:
                    print(
                        f"  ✓ {resolved} {tf_name}: {res['rows']} candles, "
                        f"{res['date_min']} → {res['date_max']}"
                    )
                else:
                    print(
                        f"  ✗ {resolved} {tf_name}: FAILED — {res['error']}",
                        file=sys.stderr,
                    )

    finally:
        if initialized:
            mt5.shutdown()

    # ---- Summary ----------------------------------------------------------
    successes = [r for r in results if r["ok"]]
    failures = [r for r in results if not r["ok"]]
    written_paths = [r["path"] for r in successes]
    available_symbols = [s for s, r in resolution.items() if r is not None]
    unavailable_symbols = [s for s, r in resolution.items() if r is None]
    total_files_expected = len(available_symbols) * len(_TIMEFRAME_REQUESTS)

    if successes:
        all_min = min(r["date_min"] for r in successes)
        all_max = max(r["date_max"] for r in successes)
    else:
        all_min = all_max = "n/a"

    print()
    print("Per-symbol status:")
    for requested in requested_symbols:
        resolved = resolution.get(requested)
        if resolved is None:
            print(f"  - {requested}: not available")
            continue
        sym_results = [r for r in results if r.get("requested") == requested]
        sym_ok = sum(1 for r in sym_results if r["ok"])
        sym_total = len(sym_results)
        broker_label = resolved if resolved == requested else f"{resolved} (alias of {requested})"
        if sym_ok == sym_total and sym_total > 0:
            print(f"  - {broker_label}: exported ({sym_ok}/{sym_total} timeframes)")
        else:
            print(f"  - {broker_label}: partial ({sym_ok}/{sym_total} timeframes)")

    print()
    print("Requested symbols:   " + ", ".join(requested_symbols))
    print("Timeframes:          " + ", ".join(tf for tf, _ in _TIMEFRAME_REQUESTS))
    print(f"Total files written: {len(successes)} / {total_files_expected}")
    print(f"Total disk usage:    {_format_size_mb(written_paths):.2f} MB")
    print(f"Date range covered:  {all_min} → {all_max}")
    if unavailable_symbols:
        print("Unavailable symbols: " + ", ".join(unavailable_symbols))
    if failures:
        print("Failures:")
        for r in failures:
            print(f"  - {r['symbol']} {r['tf']}: {r['error']}")
    else:
        print("Failures:           none")

    # ---- Max-history before/after comparison -----------------------------
    if max_history_days is not None:
        post_dir_size_mb = sum(
            p.stat().st_size for p in _OUTPUT_DIR.glob("*.parquet")
        ) / (1024 * 1024) if _OUTPUT_DIR.exists() else 0.0

        print()
        print("Before / after comparison (max-history mode):")
        for requested in requested_symbols:
            resolved = resolution.get(requested)
            if resolved is None:
                continue
            print(f"  {requested}:")
            for tf_name, _ in _TIMEFRAME_REQUESTS:
                old = pre_stats.get((requested, tf_name))
                new = next(
                    (
                        r
                        for r in results
                        if r.get("requested") == requested and r["tf"] == tf_name
                    ),
                    None,
                )
                old_rows = old["rows"] if old else 0
                old_min = old["date_min"] if old and old.get("date_min") else "—"
                old_max = old["date_max"] if old and old.get("date_max") else "—"
                if new and new["ok"]:
                    new_rows = new["rows"]
                    new_min = new["date_min"]
                    new_max = new["date_max"]
                else:
                    new_rows = 0
                    new_min = new_max = "FAILED"
                if tf_name == "M5":
                    print(
                        f"    {tf_name}: old {old_rows:>7} candles "
                        f"({old_min} → {old_max})"
                    )
                    print(
                        f"        new {new_rows:>7} candles "
                        f"({new_min} → {new_max})"
                    )
                else:
                    print(
                        f"    {tf_name}: old start {old_min}  →  new start {new_min}"
                    )
        print()
        print(
            f"Fixtures dir size: {pre_dir_size_mb:.2f} MB → "
            f"{post_dir_size_mb:.2f} MB (Δ {post_dir_size_mb - pre_dir_size_mb:+.2f} MB)"
        )

    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
