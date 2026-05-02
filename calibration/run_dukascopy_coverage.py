"""Probe Dukascopy historical coverage on the project's target instruments.

For each instrument and each historical anchor (recent, 5y, 10y, 15y), fetch
a 5-day M5 window and report bar count / first-last timestamp / errors.

Run:
    python calibration/run_dukascopy_coverage.py
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import dukascopy_python as duka
from dukascopy_python import instruments as I

REPORT_DIR = Path(__file__).parent
TS = datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%SZ")
REPORT_PATH = REPORT_DIR / f"dukascopy_coverage_check_{TS}.md"

# Map project target -> Dukascopy instrument code
TARGETS: list[tuple[str, str]] = [
    ("XAUUSD", I.INSTRUMENT_FX_METALS_XAU_USD),
    ("NDX100", I.INSTRUMENT_IDX_AMERICA_E_NQ_100),
    ("SPX500", I.INSTRUMENT_IDX_AMERICA_E_SANDP_500),
    ("EURUSD", I.INSTRUMENT_FX_MAJORS_EUR_USD),
    ("GBPUSD", I.INSTRUMENT_FX_MAJORS_GBP_USD),
    ("US30", I.INSTRUMENT_IDX_AMERICA_E_D_J_IND),
    ("BTCUSD", I.INSTRUMENT_VCCY_BTC_USD),
]

# Anchor windows (5 days each).
WINDOWS: list[tuple[str, datetime, datetime]] = [
    ("recent", datetime(2026, 4, 20), datetime(2026, 4, 25)),
    ("5y",     datetime(2021, 4, 20), datetime(2021, 4, 25)),
    ("10y",    datetime(2016, 4, 20), datetime(2016, 4, 25)),
    ("15y",    datetime(2011, 4, 20), datetime(2011, 4, 25)),
]

# Per-fetch timeout: lib has internal retries; we wall-clock-cap externally.
PER_FETCH_TIMEOUT_S = 120


def fetch_window(code: str, start: datetime, end: datetime) -> dict:
    """Fetch one window. Return summary dict."""
    t0 = time.time()
    try:
        df = duka.fetch(
            instrument=code,
            interval=duka.INTERVAL_MIN_5,
            offer_side=duka.OFFER_SIDE_BID,
            start=start,
            end=end,
            max_retries=2,
        )
        dt = time.time() - t0
        if df is None or len(df) == 0:
            return {"ok": False, "bars": 0, "elapsed": dt, "error": "empty dataframe"}
        first = df.index[0] if hasattr(df, "index") else None
        last = df.index[-1] if hasattr(df, "index") else None
        cols = list(df.columns)
        return {
            "ok": True,
            "bars": len(df),
            "elapsed": dt,
            "first": str(first),
            "last": str(last),
            "cols": cols,
        }
    except Exception as e:  # noqa: BLE001
        dt = time.time() - t0
        return {"ok": False, "bars": 0, "elapsed": dt, "error": f"{type(e).__name__}: {e}"}


def main() -> None:
    print(f"=== Dukascopy coverage probe — {TS} ===")
    print(f"Lib: dukascopy_python {duka.__name__} (v4.0.1)")
    print(f"Targets: {[t[0] for t in TARGETS]}")
    print(f"Windows: {[w[0] for w in WINDOWS]}\n")

    rows: list[dict] = []
    sample_format: dict | None = None

    for label, code in TARGETS:
        print(f"--- {label} ({code}) ---")
        row = {"label": label, "code": code, "results": {}}
        for w_label, start, end in WINDOWS:
            print(f"  [{w_label}] {start.date()} -> {end.date()} ...", end=" ", flush=True)
            r = fetch_window(code, start, end)
            row["results"][w_label] = r
            if r["ok"]:
                print(f"OK {r['bars']} bars in {r['elapsed']:.1f}s")
                if sample_format is None:
                    sample_format = {
                        "label": label,
                        "cols": r.get("cols"),
                        "first": r.get("first"),
                        "last": r.get("last"),
                    }
            else:
                print(f"FAIL ({r['elapsed']:.1f}s) {r['error']}")
        rows.append(row)

    # Build report
    lines: list[str] = []
    lines.append(f"# Dukascopy coverage probe — {TS}")
    lines.append("")
    lines.append("## Setup")
    lines.append("")
    lines.append("- Library: `dukascopy_python==4.0.1` (PyPI, official-style fork)")
    lines.append("- Interval: M5 (`INTERVAL_MIN_5`)")
    lines.append("- Side: BID")
    lines.append("- Window per anchor: 5 calendar days")
    lines.append("- Per-fetch retries: 2 (lib-internal); wall-clock not externally capped per fetch")
    lines.append("")
    lines.append("## Result matrix")
    lines.append("")
    header = "| Target | Dukascopy code | Recent | 5y | 10y | 15y | Verdict |"
    sep = "|---|---|---|---|---|---|---|"
    lines.append(header)
    lines.append(sep)

    summary_counts = {"5y": 0, "10y": 0, "15y": 0}
    for row in rows:
        cells = [row["label"], f"`{row['code']}`"]
        deepest = None
        for w_label in ["recent", "5y", "10y", "15y"]:
            r = row["results"][w_label]
            if r["ok"]:
                cells.append(f"OK {r['bars']}")
                deepest = w_label
            else:
                err = r["error"][:40]
                cells.append(f"FAIL ({err})")
        if deepest in {"5y", "10y", "15y"}:
            summary_counts["5y"] += 1
        if deepest in {"10y", "15y"}:
            summary_counts["10y"] += 1
        if deepest == "15y":
            summary_counts["15y"] += 1

        if deepest == "15y":
            verdict = "depth >=15y"
        elif deepest == "10y":
            verdict = "depth >=10y"
        elif deepest == "5y":
            verdict = "depth >=5y"
        elif deepest == "recent":
            verdict = "recent only"
        else:
            verdict = "UNAVAILABLE"
        cells.append(verdict)
        lines.append("| " + " | ".join(cells) + " |")

    lines.append("")
    lines.append("## Detail per fetch")
    lines.append("")
    for row in rows:
        lines.append(f"### {row['label']} (`{row['code']}`)")
        lines.append("")
        lines.append("| Window | OK | Bars | Elapsed (s) | First | Last | Error |")
        lines.append("|---|---|---|---|---|---|---|")
        for w_label in ["recent", "5y", "10y", "15y"]:
            r = row["results"][w_label]
            ok = "yes" if r["ok"] else "no"
            bars = r["bars"]
            elapsed = f"{r['elapsed']:.1f}"
            first = r.get("first", "") if r["ok"] else ""
            last = r.get("last", "") if r["ok"] else ""
            err = r.get("error", "") if not r["ok"] else ""
            lines.append(f"| {w_label} | {ok} | {bars} | {elapsed} | {first} | {last} | {err} |")
        lines.append("")

    lines.append("## Data format sample")
    lines.append("")
    if sample_format is not None:
        lines.append(f"From `{sample_format['label']}`:")
        lines.append("")
        lines.append(f"- Columns: `{sample_format['cols']}`")
        lines.append(f"- First index: `{sample_format['first']}`")
        lines.append(f"- Last index:  `{sample_format['last']}`")
    else:
        lines.append("No successful fetch — no sample available.")
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    n = len(TARGETS)
    lines.append(f"- Targets probed: **{n}**")
    lines.append(f"- With depth >=5y:  **{summary_counts['5y']}/{n}**")
    lines.append(f"- With depth >=10y: **{summary_counts['10y']}/{n}**")
    lines.append(f"- With depth >=15y: **{summary_counts['15y']}/{n}**")
    lines.append("")
    lines.append("## Recommendation")
    lines.append("")
    if summary_counts["5y"] == n:
        lines.append("- All 7 instruments have at least 5-year M5 depth on Dukascopy. "
                     "Full integration is viable.")
    elif summary_counts["5y"] >= 5:
        lines.append("- Most instruments have >=5y depth. Partial integration: cover the "
                     "available ones, document the gaps.")
    else:
        lines.append("- Coverage is too thin for the project's needs. Consider another "
                     "source for the missing instruments (HistData, Tickdata, broker exports).")
    lines.append("")

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport written: {REPORT_PATH}")


if __name__ == "__main__":
    sys.exit(main())
