"""Measure how much M5 historical depth FundedNext exposes per instrument.

Read-only operation: queries ``mt5.copy_rates_range`` from 2010-01-01 to
``now`` for each candidate instrument, prints the earliest/latest
timestamps, the bar count, and the approximate coverage in years.

Run on the Windows host where the MT5 terminal is logged in:

    python -m calibration.broker_calibration.measure_data_depth

Writes a single markdown report to
``calibration/broker_calibration/mt5_data_depth_<UTC_TS>.md``.

For each candidate symbol we try a list of common broker aliases (e.g.
NDX100 ↔ US100/USTEC/NAS100) and report on whichever resolves first.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).resolve().parent

# Each entry: (display_name, [aliases tried in order]).
# The first alias that resolves via ``symbol_info`` wins.
CANDIDATES: list[tuple[str, list[str]]] = [
    ("XAUUSD", ["XAUUSD"]),
    ("NDX100", ["NDX100", "US100", "USTEC", "NAS100"]),
    ("SPX500", ["SPX500", "US500", "SPX"]),
    ("EURUSD", ["EURUSD"]),
    ("GBPUSD", ["GBPUSD"]),
    ("US30", ["US30", "DJ30", "DJI30", "DOW30", "WS30"]),
    ("BTCUSD", ["BTCUSD", "BTCUSDT", "BITCOIN"]),
]

# Far-back start date — MT5 returns whatever it has on the server.
FROM_DATE = datetime(2010, 1, 1, tzinfo=UTC)


def _connect_mt5():  # pragma: no cover — needs live terminal
    try:
        import MetaTrader5 as mt5  # type: ignore[import-not-found]
    except ImportError as exc:
        raise SystemExit(
            "[FAIL] MetaTrader5 package is not installed. Run on the Windows host."
        ) from exc

    from config.secrets import MT5_LOGIN, MT5_PASSWORD, MT5_SERVER

    ok = mt5.initialize(
        login=int(MT5_LOGIN),
        password=str(MT5_PASSWORD),
        server=str(MT5_SERVER),
    )
    if not ok:
        raise SystemExit(
            f"[FAIL] mt5.initialize() failed. last_error={mt5.last_error()!r}"
        )
    return mt5


def _resolve_symbol(mt5: Any, aliases: list[str]) -> str | None:
    """Return the first alias for which ``symbol_info`` is non-None.

    Calls ``symbol_select`` first to make sure the symbol is enabled in
    Market Watch — otherwise ``copy_rates_range`` may return nothing
    even if the symbol exists on the server.
    """
    for alias in aliases:
        info = mt5.symbol_info(alias)
        if info is None:
            continue
        # Ensure it's enabled in Market Watch so rate queries work.
        if not info.visible:
            mt5.symbol_select(alias, True)
        return alias
    return None


def _verdict(years: float, n_bars: int) -> str:
    if n_bars < 1000:
        return "[X] Insufficient (very limited or not available)"
    if years >= 3.0:
        return "[OK] Sufficient depth for backtest"
    if years >= 1.0:
        return "[!] Limited depth, needs complement"
    return "[X] Insufficient"


def _format_report(
    rows: list[dict[str, Any]],
    *,
    queried_at: datetime,
    from_date: datetime,
) -> str:
    lines: list[str] = []
    lines.append("# MT5 historical M5 depth — per instrument")
    lines.append("")
    lines.append(f"Queried at: **{queried_at.isoformat(timespec='seconds')}**")
    lines.append(
        f"Requested window: **{from_date.isoformat(timespec='seconds')}** "
        f"→ **{queried_at.isoformat(timespec='seconds')}**"
    )
    lines.append(
        "MT5 returns only what the server actually has, so the 'earliest' "
        "column is the true server-side floor."
    )
    lines.append("")
    lines.append("| Instrument | Resolved symbol | Earliest (UTC) | Latest (UTC) | Years | M5 candles |")
    lines.append("|---|---|---|---|---|---|")
    for r in rows:
        lines.append(
            f"| {r['display']} | {r['resolved'] or '—'} | "
            f"{r['earliest'] or '—'} | {r['latest'] or '—'} | "
            f"{r['years']:.2f} | {r['n_bars']} |"
        )
    lines.append("")
    lines.append("## Verdict per instrument")
    lines.append("")
    for r in rows:
        lines.append(f"- **{r['display']}** ({r['resolved'] or 'not resolved'}): {r['verdict']}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Caveat — terminal cap vs broker archive")
    lines.append("")
    lines.append(
        "If every instrument shows ~99999 bars, that is almost certainly "
        "the MT5 terminal's *Max bars in chart* setting (default 100000), "
        "not the true broker archive depth. To verify the broker's actual "
        "depth, raise the cap in the terminal:"
    )
    lines.append("")
    lines.append(
        "1. In MetaTrader 5: **Tools -> Options -> Charts -> Max bars in chart** "
        "-> set to a large value (e.g. 5000000 or 'Unlimited')."
    )
    lines.append("2. Restart the terminal so the new cap takes effect.")
    lines.append(
        "3. For each watched symbol, scroll the M5 chart all the way to the "
        "left (or press End / Home) so the terminal pulls the full history "
        "from the broker into local cache."
    )
    lines.append("4. Re-run this script.")
    lines.append("")
    lines.append(
        "Until that is done, the depth measured here is a **lower bound** "
        "on what FundedNext can actually provide."
    )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "_Generated by `calibration/broker_calibration/measure_data_depth.py`. "
        "Read-only query, no orders placed._"
    )
    return "\n".join(lines) + "\n"


# MT5 terminal caps a single rate query at ~65k bars by default; 50k
# leaves headroom and stays well below the ceiling on every broker we
# have tested.
CHUNK_SIZE = 50_000


def _walk_back(mt5: Any, symbol: str) -> tuple[int, int, int]:
    """Return ``(n_bars, earliest_unix, latest_unix)`` for ``symbol`` on M5.

    Walks the history backwards in ``CHUNK_SIZE`` steps using
    ``copy_rates_from(symbol, tf, end, count)``: each call returns up to
    ``count`` bars whose timestamps are <= ``end``. We stop when a chunk
    returns 0 bars (no older history) or when the same oldest timestamp
    repeats (broker truncation).
    """
    total = 0
    earliest: int | None = None
    latest: int | None = None

    # Start the walk just past 'now' so we capture the most recent bar.
    cursor = datetime.now() + timedelta(days=2)
    last_oldest: int | None = None

    while True:
        rates = mt5.copy_rates_from(symbol, mt5.TIMEFRAME_M5, cursor, CHUNK_SIZE)
        if rates is None or len(rates) == 0:
            break

        oldest = int(rates[0]["time"])
        newest = int(rates[-1]["time"])
        n = len(rates)

        if latest is None or newest > latest:
            latest = newest
        if earliest is None or oldest < earliest:
            earliest = oldest

        # If the chunk did not advance us further back, we have hit the
        # broker-side floor (or the chunk size is larger than what is
        # available). Either way, stop.
        if last_oldest is not None and oldest >= last_oldest:
            # We may still want to count this chunk's contribution, but
            # avoid double-counting on full overlap.
            if n > 0 and oldest == last_oldest:
                # No new bars older than what we already had — done.
                break
            break

        # Count bars on the FIRST chunk fully; for subsequent chunks,
        # subtract the overlap (the chunk ends at `cursor`, which is the
        # previous chunk's oldest bar — that bar is included in both).
        if total == 0:
            total += n
        else:
            total += max(0, n - 1)

        if n < CHUNK_SIZE:
            # Server returned fewer than asked — no older data.
            break

        last_oldest = oldest
        # Step the cursor to one second before the oldest bar so the
        # next chunk returns strictly older bars.
        cursor = datetime.fromtimestamp(oldest - 1)

    if earliest is None or latest is None:
        return 0, 0, 0
    return total, earliest, latest


def main() -> int:  # pragma: no cover — manual entrypoint
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    mt5 = _connect_mt5()
    try:
        queried_at = datetime.now(tz=UTC)
        rows: list[dict[str, Any]] = []

        for display, aliases in CANDIDATES:
            resolved = _resolve_symbol(mt5, aliases)
            if resolved is None:
                logger.info("[%s] none of %s resolves — skipping", display, aliases)
                rows.append(
                    {
                        "display": display,
                        "resolved": None,
                        "earliest": None,
                        "latest": None,
                        "years": 0.0,
                        "n_bars": 0,
                        "verdict": "[X] Insufficient (symbol not available on this broker)",
                    }
                )
                continue

            logger.info("[%s] resolved → %s, walking M5 history backwards", display, resolved)
            # The MT5 terminal caps a single rate query at ~65k bars
            # ("Max bars in chart"), so a 16-year request returns
            # ``Invalid params``. Walk backwards in CHUNK_SIZE steps
            # until the broker has nothing older to give us.
            n_bars, earliest_unix, latest_unix = _walk_back(mt5, resolved)
            if n_bars == 0:
                logger.warning(
                    "[%s] no candles returned — last_error=%r",
                    display, mt5.last_error(),
                )
                rows.append(
                    {
                        "display": display,
                        "resolved": resolved,
                        "earliest": None,
                        "latest": None,
                        "years": 0.0,
                        "n_bars": 0,
                        "verdict": "[X] Insufficient (no candles returned)",
                    }
                )
                continue

            earliest = datetime.fromtimestamp(earliest_unix, tz=UTC)
            latest = datetime.fromtimestamp(latest_unix, tz=UTC)
            span_seconds = latest_unix - earliest_unix
            years = span_seconds / (365.25 * 24 * 3600)

            verdict = _verdict(years, n_bars)
            rows.append(
                {
                    "display": display,
                    "resolved": resolved,
                    "earliest": earliest.isoformat(timespec="seconds"),
                    "latest": latest.isoformat(timespec="seconds"),
                    "years": years,
                    "n_bars": n_bars,
                    "verdict": verdict,
                }
            )

            print(
                f"  {display:>8} ({resolved:>8}): "
                f"earliest={earliest.isoformat(timespec='minutes')}  "
                f"latest={latest.isoformat(timespec='minutes')}  "
                f"years={years:5.2f}  bars={n_bars}  -> {verdict}"
            )

        report = _format_report(rows, queried_at=queried_at, from_date=FROM_DATE)
        ts = queried_at.strftime("%Y%m%dT%H%M%SZ")
        out_path = OUTPUT_DIR / f"mt5_data_depth_{ts}.md"
        out_path.write_text(report, encoding="utf-8")

        print()
        print(f"Report: {out_path}")
        return 0
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    sys.exit(main())
