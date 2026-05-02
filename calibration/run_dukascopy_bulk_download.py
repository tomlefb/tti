"""Bulk-download all available Dukascopy M5 history into the local parquet cache.

For each canonical instrument, walks month by month from the
empirically-determined data start (per the 2026-05-02 coverage probe
in ``calibration/dukascopy_coverage_check_*.md``) through the most
recent complete month, calling
:meth:`src.data.dukascopy.DukascopyClient.fetch_m5` with
``use_cache=True``. Cache hits are skipped immediately; misses fetch
the whole month and persist it on disk.

Usage::

    python calibration/run_dukascopy_bulk_download.py
    python calibration/run_dukascopy_bulk_download.py --instruments XAUUSD,NDX100
    python calibration/run_dukascopy_bulk_download.py --output-log path/to.log
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from src.data.dukascopy import DukascopyClient, canonical_instruments

# Earliest M5 month available per instrument (from the coverage probe).
INSTRUMENT_START: dict[str, tuple[int, int]] = {
    "XAUUSD": (2008, 6),
    "NDX100": (2012, 6),
    "SPX500": (2012, 6),
    "EURUSD": (2012, 1),
    "GBPUSD": (2012, 6),
    "US30":   (2012, 6),
    "BTCUSD": (2017, 6),
}

SIDE = "bid"
MAX_RETRIES = 3
RETRY_BACKOFF_S = (1.0, 2.0, 4.0)

CALIBRATION_RUNS_DIR = Path(__file__).resolve().parent / "runs"


def _iter_months(
    start: tuple[int, int], end: tuple[int, int]
) -> list[tuple[int, int]]:
    """Return ``(year, month)`` pairs from ``start`` to ``end`` inclusive."""
    out: list[tuple[int, int]] = []
    y, m = start
    end_y, end_m = end
    while (y, m) <= (end_y, end_m):
        out.append((y, m))
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1
    return out


def _last_complete_month() -> tuple[int, int]:
    """Return ``(year, month)`` of the most recent fully-complete UTC month."""
    today = datetime.now(UTC)
    if today.month == 1:
        return today.year - 1, 12
    return today.year, today.month - 1


def _month_path(
    client: DukascopyClient, instrument: str, year: int, month: int
) -> Path:
    if client.cache_dir is None:
        raise RuntimeError("client must have a cache_dir for bulk download")
    return (
        client.cache_dir
        / instrument
        / f"{year:04d}-{month:02d}_{SIDE}.parquet"
    )


def _process_month(
    client: DukascopyClient,
    instrument: str,
    year: int,
    month: int,
    logger: logging.Logger,
) -> dict:
    """Fetch one month and return a status dict.

    Cache state is observed by checking the parquet file's existence
    *before* the fetch_m5 call, since the client itself does not
    surface that information.
    """
    cache_path = _month_path(client, instrument, year, month)
    cache_state = "HIT" if cache_path.exists() else "MISS"

    month_start = datetime(year, month, 1, tzinfo=UTC)
    if month == 12:
        month_end = datetime(year + 1, 1, 1, tzinfo=UTC)
    else:
        month_end = datetime(year, month + 1, 1, tzinfo=UTC)

    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            t0 = time.time()
            df = client.fetch_m5(
                instrument,
                start=month_start,
                end=month_end,
                side=SIDE,
                use_cache=True,
            )
            elapsed = time.time() - t0
            n_bars = len(df)
            logger.info(
                "%s %04d-%02d: %d bars (%.2fs, cache %s)",
                instrument, year, month, n_bars, elapsed, cache_state,
            )
            return {
                "status": "ok",
                "bars": n_bars,
                "elapsed": elapsed,
                "cache": cache_state,
            }
        except Exception as exc:  # noqa: BLE001 — we explicitly retry & log
            last_exc = exc
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_BACKOFF_S[attempt]
                logger.warning(
                    "%s %04d-%02d: attempt %d failed (%s: %s) — retry in %.0fs",
                    instrument, year, month, attempt + 1,
                    type(exc).__name__, exc, wait,
                )
                time.sleep(wait)
            else:
                logger.error(
                    "%s %04d-%02d: failed after %d attempts: %s: %s",
                    instrument, year, month, MAX_RETRIES,
                    type(exc).__name__, exc,
                )
    return {
        "status": "error",
        "bars": 0,
        "elapsed": 0.0,
        "cache": cache_state,
        "error": f"{type(last_exc).__name__}: {last_exc}",
    }


def run_bulk_download(
    instruments: list[str], logger: logging.Logger
) -> tuple[dict, float]:
    client = DukascopyClient()
    end_month = _last_complete_month()
    summary: dict[str, dict] = {}
    overall_t0 = time.time()

    for instrument in instruments:
        start = INSTRUMENT_START[instrument]
        months = _iter_months(start, end_month)
        logger.info(
            "=== %s: %04d-%02d -> %04d-%02d (%d months) ===",
            instrument, start[0], start[1], end_month[0], end_month[1], len(months),
        )
        per_inst = {
            "start": f"{start[0]:04d}-{start[1]:02d}",
            "end": f"{end_month[0]:04d}-{end_month[1]:02d}",
            "months_total": 0,
            "months_hit": 0,
            "months_miss": 0,
            "months_error": 0,
            "bars_total": 0,
            "errors": [],
        }
        inst_t0 = time.time()
        for year, month in months:
            r = _process_month(client, instrument, year, month, logger)
            per_inst["months_total"] += 1
            if r["status"] == "error":
                per_inst["months_error"] += 1
                per_inst["errors"].append(
                    {"month": f"{year:04d}-{month:02d}", "error": r["error"]}
                )
            else:
                if r["cache"] == "HIT":
                    per_inst["months_hit"] += 1
                else:
                    per_inst["months_miss"] += 1
                per_inst["bars_total"] += r["bars"]
        per_inst["wall_clock_s"] = time.time() - inst_t0
        summary[instrument] = per_inst
        logger.info(
            "=== %s done: %d months (%d hit, %d miss, %d err), "
            "%d bars, %.1fs ===",
            instrument,
            per_inst["months_total"],
            per_inst["months_hit"],
            per_inst["months_miss"],
            per_inst["months_error"],
            per_inst["bars_total"],
            per_inst["wall_clock_s"],
        )

    return summary, time.time() - overall_t0


def write_report(
    summary: dict, overall_elapsed: float, report_path: Path
) -> None:
    lines: list[str] = []
    lines.append(f"# Dukascopy bulk download — {datetime.now(UTC).isoformat()}")
    lines.append("")
    lines.append(
        f"Wall-clock total: **{overall_elapsed:.1f}s** "
        f"({overall_elapsed / 60:.1f} min)"
    )
    lines.append("")
    lines.append("## Per-instrument summary")
    lines.append("")
    lines.append(
        "| Instrument | Window | Months | Hit | Miss | Err | Total bars | Wall-clock |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    for inst, s in summary.items():
        lines.append(
            f"| {inst} | {s['start']} -> {s['end']} | "
            f"{s['months_total']} | {s['months_hit']} | "
            f"{s['months_miss']} | {s['months_error']} | "
            f"{s['bars_total']:,} | {s['wall_clock_s']:.1f}s |"
        )
    lines.append("")

    has_errors = any(s["months_error"] > 0 for s in summary.values())
    lines.append("## Errors")
    lines.append("")
    if not has_errors:
        lines.append("None.")
    else:
        for inst, s in summary.items():
            if s["months_error"] == 0:
                continue
            lines.append(f"### {inst}")
            lines.append("")
            for e in s["errors"]:
                lines.append(f"- {e['month']}: `{e['error']}`")
            lines.append("")
    lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")


def _setup_logger(log_path: Path) -> logging.Logger:
    logger = logging.getLogger("dukascopy_bulk")
    logger.setLevel(logging.INFO)
    # Avoid duplicate handlers on re-import.
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bulk-download Dukascopy M5 into the local parquet cache."
    )
    parser.add_argument(
        "--instruments",
        default=",".join(canonical_instruments()),
        help="Comma-separated instrument list (default: all 7).",
    )
    parser.add_argument(
        "--output-log",
        default=None,
        help="Path to write the run log (default: calibration/runs/dukascopy_bulk_<TS>.log).",
    )
    parser.add_argument(
        "--report",
        default=None,
        help="Path to write the markdown report (default: alongside the log).",
    )
    args = parser.parse_args()

    requested = [
        s.strip().upper() for s in args.instruments.split(",") if s.strip()
    ]
    unknown = [s for s in requested if s not in canonical_instruments()]
    if unknown:
        print(
            f"Unknown instruments: {unknown}. "
            f"Supported: {canonical_instruments()}",
            file=sys.stderr,
        )
        return 2

    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    CALIBRATION_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = (
        Path(args.output_log)
        if args.output_log
        else CALIBRATION_RUNS_DIR / f"dukascopy_bulk_{ts}.log"
    )
    report_path = (
        Path(args.report)
        if args.report
        else CALIBRATION_RUNS_DIR / f"dukascopy_bulk_{ts}.md"
    )

    logger = _setup_logger(log_path)
    logger.info("Bulk download started: instruments=%s", requested)
    summary, overall_elapsed = run_bulk_download(requested, logger)
    logger.info("Done in %.1fs (%.1f min)", overall_elapsed, overall_elapsed / 60)

    write_report(summary, overall_elapsed, report_path)
    logger.info("Report: %s", report_path)
    logger.info("Log:    %s", log_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
