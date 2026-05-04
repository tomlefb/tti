"""Download H1 fixtures from Dukascopy for the 6-instrument extension
of the trend-rotation universe, then re-aggregate D1 from H1.

Targets (per the operator brief — no superset opportunism):

- AUDUSD H1 (FX major, missing)
- JP225 H1   (Nikkei 225 cash, missing)
- UK100 H1   (FTSE 100 cash, missing)
- US2000 H1  (Russell 2000 cash, missing)
- USDJPY H1  (FX major, present but only 1 y; extending to ~7 y)
- XAGUSD H1  (Silver, present but only 1 y; extending to ~7 y)

Output schema (matches existing MT5-style fixtures in
``tests/fixtures/historical/``): tz-aware ``time`` column +
``open / high / low / close / tick_volume / spread / real_volume``.
``tick_volume`` is populated from the Dukascopy ``volume`` field;
``spread`` and ``real_volume`` are zero-filled (Dukascopy data does
not carry MT5-style spread / real-volume metadata).

D1 re-aggregation: calendar day in UTC. Open = first H1 of the day,
close = last H1, high = max H1 high, low = min H1 low, tick_volume
= sum, spread = 0, real_volume = 0. Timestamp = ``00:00:00 UTC`` of
the calendar day (matches the most recent ``USDJPY_D1.parquet``
convention; older fixtures use 21:00/22:00 — documented deviation).

Run
---
    python -m calibration.run_dukascopy_h1_extension
"""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import dukascopy_python  # noqa: E402
from dukascopy_python import instruments as duka_instruments  # noqa: E402

OUT_DIR = REPO_ROOT / "tests" / "fixtures" / "historical"
RUNS_DIR = REPO_ROOT / "calibration" / "runs"

# Per-instrument Dukascopy code mapping. Symbols verified at
# package level — see calibration script docstring.
INSTRUMENT_CODES: dict[str, str] = {
    "AUDUSD": duka_instruments.INSTRUMENT_FX_MAJORS_AUD_USD,
    "JP225":  duka_instruments.INSTRUMENT_IDX_ASIA_E_N225JAP,
    "UK100":  duka_instruments.INSTRUMENT_IDX_EUROPE_E_FUTSEE_100,
    "US2000": duka_instruments.INSTRUMENT_IDX_AMERICA_USSC2000_IDX_USD,
    "USDJPY": duka_instruments.INSTRUMENT_FX_MAJORS_USD_JPY,
    "XAGUSD": duka_instruments.INSTRUMENT_FX_METALS_XAG_USD,
}

# Window: 2019-01-01 → 2026-04-30 inclusive, chunked by year.
WINDOW_START = datetime(2019, 1, 1, tzinfo=UTC)
WINDOW_END = datetime(2026, 5, 1, tzinfo=UTC)


def _fetch_year(code: str, year: int) -> pd.DataFrame:
    """Fetch one year of H1 bars from Dukascopy. Empty DataFrame on no data."""
    start = datetime(year, 1, 1, tzinfo=UTC)
    end = datetime(year + 1, 1, 1, tzinfo=UTC)
    try:
        df = dukascopy_python.fetch(
            instrument=code,
            interval=dukascopy_python.INTERVAL_HOUR_1,
            offer_side=dukascopy_python.OFFER_SIDE_BID,
            start=start,
            end=end,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"    {year}: ERROR {type(exc).__name__}: {exc}", flush=True)
        return pd.DataFrame()
    if df is None or len(df) == 0:
        return pd.DataFrame()
    return df


def _to_mt5_schema(df: pd.DataFrame) -> pd.DataFrame:
    """Rename + add tick_volume / spread / real_volume columns to match
    the existing ``tests/fixtures/historical/`` schema."""
    df = df.copy()
    df.columns = [str(c).lower() for c in df.columns]
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    out = pd.DataFrame(
        {
            "time": df.index.values,
            "open": df["open"].astype("float64").values,
            "high": df["high"].astype("float64").values,
            "low": df["low"].astype("float64").values,
            "close": df["close"].astype("float64").values,
            "tick_volume": df["volume"].fillna(0).astype("uint64").values
            if "volume" in df.columns
            else pd.Series([0] * len(df), dtype="uint64").values,
            "spread": pd.Series([0] * len(df), dtype="int32").values,
            "real_volume": pd.Series([0] * len(df), dtype="uint64").values,
        }
    )
    out["time"] = pd.to_datetime(out["time"], utc=True).astype(
        "datetime64[ms, UTC]"
    )
    out = out.sort_values("time").drop_duplicates(
        subset=["time"], keep="last"
    ).reset_index(drop=True)
    return out


def _resample_h1_to_d1(h1: pd.DataFrame) -> pd.DataFrame:
    """Re-aggregate H1 → D1 (calendar day in UTC, label at 00:00 UTC)."""
    df = h1.set_index("time").sort_index()
    d1 = df.resample("1D", origin="epoch", label="left", closed="left").agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "tick_volume": "sum",
            "spread": "max",
            "real_volume": "sum",
        }
    )
    d1 = d1.dropna(subset=["close"])
    d1 = d1.reset_index()
    d1["time"] = pd.to_datetime(d1["time"], utc=True).astype(
        "datetime64[ms, UTC]"
    )
    d1["tick_volume"] = d1["tick_volume"].astype("uint64")
    d1["spread"] = d1["spread"].astype("int32")
    d1["real_volume"] = d1["real_volume"].astype("uint64")
    return d1


def download_one(instrument: str) -> dict:
    """Fetch H1 for ``instrument`` over the configured window, save H1
    + D1 parquet, and return a status dict for the report."""
    code = INSTRUMENT_CODES[instrument]
    print(f"\n=== {instrument} ({code}) ===", flush=True)
    t0 = time.perf_counter()

    frames: list[pd.DataFrame] = []
    for year in range(WINDOW_START.year, WINDOW_END.year + 1):
        ty0 = time.perf_counter()
        df_year = _fetch_year(code, year)
        ty1 = time.perf_counter()
        n = len(df_year)
        print(f"  {year}: {n:>5} H1 bars ({ty1 - ty0:.1f}s)", flush=True)
        if n > 0:
            frames.append(df_year)
        # Polite pause between year chunks (Dukascopy back-end limits).
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
    h1_first = h1["time"].iloc[0]
    h1_last = h1["time"].iloc[-1]
    d1_first = d1["time"].iloc[0]
    d1_last = d1["time"].iloc[-1]
    print(
        f"  → H1 {len(h1)} bars ({h1_first} → {h1_last}); "
        f"D1 {len(d1)} bars ({d1_first} → {d1_last}); "
        f"total {elapsed:.1f}s",
        flush=True,
    )

    return {
        "instrument": instrument,
        "status": "ok",
        "h1_bars": len(h1),
        "d1_bars": len(d1),
        "h1_first": h1_first,
        "h1_last": h1_last,
        "d1_first": d1_first,
        "d1_last": d1_last,
        "wallclock_s": elapsed,
    }


def main() -> int:
    overall_t0 = time.perf_counter()
    results: list[dict] = []
    for inst in INSTRUMENT_CODES:
        try:
            r = download_one(inst)
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ {inst}: unexpected error {type(exc).__name__}: {exc}", flush=True)
            r = {"instrument": inst, "status": "error", "reason": str(exc)}
        results.append(r)

    elapsed = time.perf_counter() - overall_t0
    print(f"\nTotal wallclock: {elapsed:.1f}s ({elapsed / 60:.1f} min)")

    # Summary
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
