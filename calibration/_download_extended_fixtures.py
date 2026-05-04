"""Download D1 OHLC for the v1.1 universe via Yahoo Finance.

Saves to ``tests/fixtures/historical_extended/yahoo/<ASSET>_D1.parquet``
with the format expected by the trend_rotation_d1 pipeline:
- columns: open, high, low, close (volume kept too)
- index: tz-aware UTC, normalised to calendar-day 00:00 UTC

Run
---
    python -m calibration._download_extended_fixtures
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pandas as pd  # noqa: E402
import yfinance as yf  # noqa: E402

# Use the chosen-symbol mapping from the inventory script (primary picks).
TICKER_MAP: dict[str, str] = {
    "NDX100": "^NDX",
    "SPX500": "^GSPC",
    "US30": "^DJI",
    "US2000": "^RUT",
    "GER30": "^GDAXI",
    "UK100": "^FTSE",
    "JP225": "^N225",
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X",
    "XAUUSD": "GC=F",
    "XAGUSD": "SI=F",
    "USOUSD": "CL=F",
    "BTCUSD": "BTC-USD",
}

OUT_DIR = REPO_ROOT / "tests" / "fixtures" / "historical_extended" / "yahoo"


def download_one(asset: str, symbol: str) -> dict:
    """Download max history; normalise; save parquet."""
    t = yf.Ticker(symbol)
    df = t.history(period="max", interval="1d", auto_adjust=False)
    if df is None or len(df) == 0:
        return {"asset": asset, "ok": False, "reason": "empty"}

    # Normalise index to UTC 00:00 calendar day
    idx = pd.to_datetime(df.index, utc=True).normalize()
    df = df.copy()
    df.index = idx
    df = df[~df.index.duplicated(keep="first")].sort_index()

    # Keep OHLC + volume; rename columns to lowercase
    cols_keep = {}
    for c in ["Open", "High", "Low", "Close", "Volume"]:
        if c in df.columns:
            cols_keep[c] = c.lower()
    df = df[list(cols_keep.keys())].rename(columns=cols_keep)

    # Drop any rows where close is NaN
    df = df.dropna(subset=["close"])

    # Reset index to a 'time' column for parquet compatibility with the
    # existing trend_rotation_d1 panel loader (which reads 'time' if
    # present).
    df_to_save = df.copy()
    df_to_save["time"] = df_to_save.index
    df_to_save = df_to_save.reset_index(drop=True)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{asset}_D1.parquet"
    df_to_save.to_parquet(path, index=False)

    diffs = df.index.to_series().diff().dt.days
    gaps = int((diffs > 10).sum())
    years = (df.index.max() - df.index.min()).days / 365.25
    return {
        "asset": asset,
        "ok": True,
        "symbol": symbol,
        "first": df.index.min().date().isoformat(),
        "last": df.index.max().date().isoformat(),
        "years": round(years, 2),
        "n_bars": len(df),
        "gaps_gt_10d": gaps,
        "path": str(path.relative_to(REPO_ROOT)),
    }


def main() -> int:
    print(f"Downloading {len(TICKER_MAP)} assets to {OUT_DIR}\n", flush=True)
    rows = []
    t0 = time.perf_counter()
    for asset, sym in TICKER_MAP.items():
        try:
            r = download_one(asset, sym)
        except Exception as e:
            r = {"asset": asset, "ok": False, "reason": str(e)[:120]}
        rows.append(r)
        if r.get("ok"):
            print(
                f"  {asset:<8} {sym:<10}: "
                f"{r['first']} → {r['last']} "
                f"({r['years']:.1f}y, n={r['n_bars']}, gaps={r['gaps_gt_10d']}) "
                f"→ {r['path']}",
                flush=True,
            )
        else:
            print(f"  {asset:<8} {sym:<10}: FAIL — {r.get('reason')}", flush=True)
    print(f"\nWallclock: {time.perf_counter() - t0:.1f}s")
    n_ok = sum(1 for r in rows if r.get("ok"))
    print(f"Success: {n_ok} / {len(TICKER_MAP)}")
    return 0 if n_ok == len(TICKER_MAP) else 1


if __name__ == "__main__":
    sys.exit(main())
