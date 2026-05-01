"""Continuous spread recorder for the FundedNext MT5 terminal.

Polls ``mt5.symbol_info_tick`` every 10 seconds for each watched symbol
and appends ``(timestamp_utc, symbol, bid, ask, spread_points,
spread_native)`` to a per-symbol Parquet file. Designed to run for an
entire trading week (multiple killzones) in the background.

Usage (Windows host, MT5 terminal logged in):

    python -m calibration.broker_calibration.live_spread_recorder

Stop with Ctrl+C. The script flushes pending samples to disk on exit.

Output (gitignored):
    calibration/broker_calibration/spread_log_<symbol>.parquet
    calibration/broker_calibration/spread_log_<symbol>.csv  (fallback)

CSV is used as a fallback if pyarrow / fastparquet are not available.
The post-hoc analysis script (to be written) accepts both formats.
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

# --- Tunables -----------------------------------------------------------
POLL_INTERVAL_SECONDS = 10
FLUSH_INTERVAL_SECONDS = 60          # also when the in-memory buffer crosses this size
SUMMARY_INTERVAL_SECONDS = 60
WATCHED_SYMBOLS = ("XAUUSD", "NDX100")

OUTPUT_DIR = Path(__file__).resolve().parent


@dataclass
class Sample:
    timestamp_utc: datetime
    symbol: str
    bid: float
    ask: float
    spread_points: int
    spread_native: float  # raw price-units difference (cents for XAU, points for NDX)


# ----------------------------------------------------------------------
# Persistence
# ----------------------------------------------------------------------


def _output_path(symbol: str, *, fmt: str) -> Path:
    return OUTPUT_DIR / f"spread_log_{symbol}.{fmt}"


def _flush_buffer(symbol: str, buffer: list[Sample]) -> None:
    """Append a buffer of samples for ``symbol`` to disk.

    Tries Parquet first (append-by-rewrite — small file, daily-week
    scale is fine). Falls back to CSV if no Parquet engine is installed.
    """
    if not buffer:
        return

    new_df = pd.DataFrame([s.__dict__ for s in buffer])
    parquet_path = _output_path(symbol, fmt="parquet")
    csv_path = _output_path(symbol, fmt="csv")

    try:
        if parquet_path.exists():
            existing = pd.read_parquet(parquet_path)
            combined = pd.concat([existing, new_df], ignore_index=True)
        else:
            combined = new_df
        combined.to_parquet(parquet_path, index=False)
        logger.debug("Flushed %d samples → %s", len(buffer), parquet_path.name)
    except (ImportError, ValueError) as exc:
        # No parquet engine — append to CSV.
        logger.warning(
            "Parquet write failed (%s); falling back to CSV %s",
            exc,
            csv_path.name,
        )
        new_df.to_csv(
            csv_path,
            mode="a",
            header=not csv_path.exists(),
            index=False,
        )


def _summarise(buffer: list[Sample], window_seconds: int) -> dict[str, dict[str, float]]:
    """Per-symbol median spread over the last ``window_seconds`` of buffered samples."""
    if not buffer:
        return {}
    cutoff = buffer[-1].timestamp_utc.timestamp() - window_seconds
    recent = [s for s in buffer if s.timestamp_utc.timestamp() >= cutoff]
    by_sym: dict[str, list[Sample]] = {}
    for s in recent:
        by_sym.setdefault(s.symbol, []).append(s)
    out: dict[str, dict[str, float]] = {}
    for sym, items in by_sym.items():
        out[sym] = {
            "n": len(items),
            "median_spread_points": median(s.spread_points for s in items),
            "median_spread_native": median(s.spread_native for s in items),
            "min_spread_native": min(s.spread_native for s in items),
            "max_spread_native": max(s.spread_native for s in items),
        }
    return out


# ----------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------


def _connect_mt5():  # pragma: no cover — needs live terminal
    try:
        import MetaTrader5 as mt5  # type: ignore[import-not-found]
    except ImportError as exc:
        raise SystemExit(
            "[FAIL] MetaTrader5 package is not installed. "
            "Run on the Windows host."
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


def _ensure_visible(mt5: Any, symbol: str) -> bool:  # pragma: no cover
    """``symbol_info_tick`` returns None for symbols not in the Market Watch."""
    info = mt5.symbol_info(symbol)
    if info is None:
        logger.warning("Symbol %s unknown to broker — will be skipped.", symbol)
        return False
    if not info.visible:
        if not mt5.symbol_select(symbol, True):
            logger.warning("Failed to add %s to Market Watch — skipping.", symbol)
            return False
    return True


def _poll_once(mt5: Any, symbols: tuple[str, ...]) -> list[Sample]:  # pragma: no cover
    samples: list[Sample] = []
    now = datetime.now(tz=UTC)
    for sym in symbols:
        tick = mt5.symbol_info_tick(sym)
        if tick is None:
            continue
        bid = float(tick.bid)
        ask = float(tick.ask)
        spread_native = ask - bid
        info = mt5.symbol_info(sym)
        point = float(info.point) if info is not None else 0.0
        spread_points = int(round(spread_native / point)) if point else 0
        samples.append(
            Sample(
                timestamp_utc=now,
                symbol=sym,
                bid=bid,
                ask=ask,
                spread_points=spread_points,
                spread_native=spread_native,
            )
        )
    return samples


def main() -> int:  # pragma: no cover — entrypoint
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger.info(
        "live_spread_recorder starting — watching %s (poll=%ds, flush=%ds)",
        WATCHED_SYMBOLS,
        POLL_INTERVAL_SECONDS,
        FLUSH_INTERVAL_SECONDS,
    )

    mt5 = _connect_mt5()

    visible_symbols = tuple(s for s in WATCHED_SYMBOLS if _ensure_visible(mt5, s))
    if not visible_symbols:
        mt5.shutdown()
        raise SystemExit("[FAIL] None of the watched symbols are visible.")
    if visible_symbols != WATCHED_SYMBOLS:
        logger.warning(
            "Recording subset of symbols: %s (missing: %s)",
            visible_symbols,
            tuple(s for s in WATCHED_SYMBOLS if s not in visible_symbols),
        )

    stop_requested = False

    def _handle_sigint(signum, frame):  # noqa: ARG001
        nonlocal stop_requested
        logger.info("Stop signal received — flushing and exiting.")
        stop_requested = True

    signal.signal(signal.SIGINT, _handle_sigint)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_sigint)

    buffers: dict[str, list[Sample]] = {s: [] for s in visible_symbols}
    all_recent: list[Sample] = []  # rolling window for summaries
    last_flush = time.time()
    last_summary = time.time()

    try:
        while not stop_requested:
            try:
                samples = _poll_once(mt5, visible_symbols)
            except Exception:  # noqa: BLE001 — keep recorder alive
                logger.exception("poll error — sleeping and retrying")
                samples = []

            for s in samples:
                buffers[s.symbol].append(s)
                all_recent.append(s)

            # Trim recent buffer to roughly 1h to keep summaries cheap.
            cutoff = time.time() - 3600
            all_recent[:] = [s for s in all_recent if s.timestamp_utc.timestamp() >= cutoff]

            now_t = time.time()
            if now_t - last_summary >= SUMMARY_INTERVAL_SECONDS:
                summary = _summarise(all_recent, SUMMARY_INTERVAL_SECONDS)
                if summary:
                    logger.info("1-min spread medians:")
                    for sym, st in summary.items():
                        logger.info(
                            "  %s: n=%d median=%g native (%g points), "
                            "range native=[%g, %g]",
                            sym,
                            st["n"],
                            st["median_spread_native"],
                            st["median_spread_points"],
                            st["min_spread_native"],
                            st["max_spread_native"],
                        )
                last_summary = now_t

            if now_t - last_flush >= FLUSH_INTERVAL_SECONDS:
                for sym, buf in buffers.items():
                    _flush_buffer(sym, buf)
                    buf.clear()
                last_flush = now_t

            # Sleep in small slices so SIGINT is responsive.
            slept = 0.0
            while slept < POLL_INTERVAL_SECONDS and not stop_requested:
                time.sleep(0.5)
                slept += 0.5
    finally:
        for sym, buf in buffers.items():
            _flush_buffer(sym, buf)
        mt5.shutdown()
        logger.info("live_spread_recorder stopped.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
