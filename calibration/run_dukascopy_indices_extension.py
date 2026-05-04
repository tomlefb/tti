"""Extend the 5 short-coverage index fixtures to 7 years (2019-01 →
2026-04), matching the 6-instrument extension committed at 7fc13b4.

Targets:

- NDX100  (Nasdaq 100, ``E_NQ-100``)         — extend from 2022-10
- SPX500  (S&P 500, ``E_SandP-500``)         — extend from 2022-10
- US30    (Dow Jones, ``E_D&J-Ind``)         — extend from 2022-10
- USOUSD  (WTI Light Crude, ``E_Light``)     — extend from 2022-10
- GER30   (DAX, ``E_DAAX``)                  — extend from 2022-06

The existing D1 fixtures for NDX100 / SPX500 / US30 use the
``21:00 UTC`` MT5-style label; USOUSD / GER30 use ``00:00 UTC``
calendar-day label. Replacement fixtures use **00:00 UTC** for
consistency with the 6-instrument extension already in tree —
documented deviation; the pre-measure / rotation logic is
date-normalised so the label change is invisible at the
cross-asset alignment stage.

Run
---
    python -m calibration.run_dukascopy_indices_extension
"""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import dukascopy_python  # noqa: E402
from dukascopy_python import instruments as duka_instruments  # noqa: E402

from calibration.run_dukascopy_h1_extension import (  # noqa: E402
    OUT_DIR,
    WINDOW_END,
    WINDOW_START,
    _fetch_year,
    _resample_h1_to_d1,
    _to_mt5_schema,
)

INSTRUMENT_CODES: dict[str, str] = {
    "NDX100": duka_instruments.INSTRUMENT_IDX_AMERICA_E_NQ_100,
    "SPX500": duka_instruments.INSTRUMENT_IDX_AMERICA_E_SANDP_500,
    "US30":   duka_instruments.INSTRUMENT_IDX_AMERICA_E_D_J_IND,
    "USOUSD": duka_instruments.INSTRUMENT_CMD_ENERGY_E_LIGHT,
    "GER30":  duka_instruments.INSTRUMENT_IDX_EUROPE_E_DAAX,
}


def download_one(instrument: str) -> dict:
    """Mirror of ``run_dukascopy_h1_extension.download_one`` with the
    indices instrument codes."""
    import pandas as pd

    code = INSTRUMENT_CODES[instrument]
    print(f"\n=== {instrument} ({code}) ===", flush=True)
    t0 = time.perf_counter()

    frames: list = []
    for year in range(WINDOW_START.year, WINDOW_END.year + 1):
        ty0 = time.perf_counter()
        df_year = _fetch_year(code, year)
        ty1 = time.perf_counter()
        n = len(df_year)
        print(f"  {year}: {n:>5} H1 bars ({ty1 - ty0:.1f}s)", flush=True)
        if n > 0:
            frames.append(df_year)
        time.sleep(0.5)

    if not frames:
        print(f"  ❌ {instrument}: no data fetched", flush=True)
        return {"instrument": instrument, "status": "error", "reason": "no_data"}

    raw = pd.concat(frames).sort_index()
    h1 = _to_mt5_schema(raw)
    h1 = h1[(h1["time"] >= WINDOW_START) & (h1["time"] < WINDOW_END)]
    h1 = h1.reset_index(drop=True)

    d1 = _resample_h1_to_d1(h1)
    d1 = d1[(d1["time"] >= WINDOW_START) & (d1["time"] < WINDOW_END)]
    d1 = d1.reset_index(drop=True)

    h1_path = OUT_DIR / f"{instrument}_H1.parquet"
    d1_path = OUT_DIR / f"{instrument}_D1.parquet"
    h1.to_parquet(h1_path)
    d1.to_parquet(d1_path)

    elapsed = time.perf_counter() - t0
    print(
        f"  → H1 {len(h1)} bars ({h1['time'].iloc[0]} → {h1['time'].iloc[-1]}); "
        f"D1 {len(d1)} bars ({d1['time'].iloc[0]} → {d1['time'].iloc[-1]}); "
        f"total {elapsed:.1f}s",
        flush=True,
    )
    return {
        "instrument": instrument,
        "status": "ok",
        "h1_bars": len(h1),
        "d1_bars": len(d1),
        "h1_first": h1["time"].iloc[0],
        "h1_last": h1["time"].iloc[-1],
        "d1_first": d1["time"].iloc[0],
        "d1_last": d1["time"].iloc[-1],
        "wallclock_s": elapsed,
    }


def main() -> int:
    overall_t0 = time.perf_counter()
    results: list[dict] = []
    for inst in INSTRUMENT_CODES:
        try:
            r = download_one(inst)
        except Exception as exc:  # noqa: BLE001
            print(
                f"  ❌ {inst}: unexpected error {type(exc).__name__}: {exc}",
                flush=True,
            )
            r = {"instrument": inst, "status": "error", "reason": str(exc)}
        results.append(r)

    elapsed = time.perf_counter() - overall_t0
    print(f"\nTotal wallclock: {elapsed:.1f}s ({elapsed / 60:.1f} min)")

    print("\n=== Summary ===")
    print(
        f"{'Instrument':<10} {'Status':<8} {'H1 bars':>8} "
        f"{'D1 bars':>8} {'First':<27} {'Last':<27}"
    )
    for r in results:
        if r["status"] == "ok":
            print(
                f"{r['instrument']:<10} OK       "
                f"{r['h1_bars']:>8} {r['d1_bars']:>8} "
                f"{str(r['h1_first']):<27} {str(r['h1_last']):<27}"
            )
        else:
            print(
                f"{r['instrument']:<10} ERROR    "
                f"reason={r.get('reason', '?')}"
            )
    return 0 if all(r["status"] == "ok" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
