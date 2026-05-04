"""Inventory alternative-source coverage for the v1.1 universe.

For each of the 15 FundedNext-tradable assets, query Yahoo Finance
for the longest available D1 history and report:
- first/last date
- bar count
- gaps > 10 days
- whether ≥ 15 y / ≥ 20 y available

Used as the gate before launching the full 20-y walk-forward.

Run
---
    python -m calibration._inventory_extended_sources
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pandas as pd  # noqa: E402
import yfinance as yf  # noqa: E402

# Map FundedNext label → primary Yahoo ticker, backup ticker
TICKER_MAP: dict[str, list[str]] = {
    # US equity indices
    "NDX100": ["^NDX"],
    "SPX500": ["^GSPC"],
    "US30": ["^DJI"],
    "US2000": ["^RUT"],
    # International indices
    "GER30": ["^GDAXI", "EWG"],   # DAX, fall back to ETF
    "UK100": ["^FTSE", "EWU"],
    "JP225": ["^N225", "EWJ"],
    # FX majors
    "EURUSD": ["EURUSD=X"],
    "GBPUSD": ["GBPUSD=X"],
    "USDJPY": ["USDJPY=X"],
    "AUDUSD": ["AUDUSD=X"],
    # Metals
    "XAUUSD": ["GC=F", "GLD"],
    "XAGUSD": ["SI=F", "SLV"],
    # Energy
    "USOUSD": ["CL=F", "USO"],
    # Crypto
    "BTCUSD": ["BTC-USD"],
}


def query_one(symbol: str) -> dict:
    try:
        t = yf.Ticker(symbol)
        df = t.history(period="max", interval="1d", auto_adjust=False)
        if df is None or len(df) == 0:
            return {"symbol": symbol, "ok": False, "reason": "empty"}
        df.index = pd.to_datetime(df.index, utc=True).normalize()
        df = df[~df.index.duplicated(keep="first")].sort_index()
        first = df.index.min()
        last = df.index.max()
        years = (last - first).days / 365.25
        diffs = df.index.to_series().diff().dt.days
        gaps_10 = int((diffs > 10).sum())
        return {
            "symbol": symbol,
            "ok": True,
            "first": first.date().isoformat(),
            "last": last.date().isoformat(),
            "n_bars": len(df),
            "years": round(years, 2),
            "gaps_gt_10d": gaps_10,
        }
    except Exception as e:
        return {"symbol": symbol, "ok": False, "reason": str(e)[:120]}


def main() -> int:
    print(f"Querying Yahoo Finance for {len(TICKER_MAP)} assets...\n", flush=True)
    rows: list[dict] = []
    for asset, candidates in TICKER_MAP.items():
        chosen = None
        for sym in candidates:
            res = query_one(sym)
            if res["ok"]:
                chosen = res
                chosen["asset"] = asset
                chosen["fallback_used"] = (sym != candidates[0])
                break
            else:
                print(f"  {asset:<8} via {sym:<10}: FAIL {res.get('reason')}", flush=True)
        if chosen is None:
            chosen = {"asset": asset, "ok": False, "reason": "all candidates failed"}
        rows.append(chosen)
        if chosen.get("ok"):
            yrs = chosen["years"]
            mark15 = "✓15y" if yrs >= 15 else "    "
            mark20 = "✓20y" if yrs >= 20 else "    "
            print(
                f"  {asset:<8} via {chosen['symbol']:<10}: "
                f"{chosen['first']} → {chosen['last']} "
                f"({yrs:.1f}y, n={chosen['n_bars']}, gaps={chosen['gaps_gt_10d']}) "
                f"{mark15} {mark20}"
                f"{'  [fallback]' if chosen.get('fallback_used') else ''}",
                flush=True,
            )

    print("\n=== Summary ===")
    n_ok = sum(1 for r in rows if r.get("ok"))
    n_15y = sum(1 for r in rows if r.get("ok") and r.get("years", 0) >= 15)
    n_20y = sum(1 for r in rows if r.get("ok") and r.get("years", 0) >= 20)
    print(f"  Available: {n_ok} / 15")
    print(f"  ≥ 15 years: {n_15y} / 15")
    print(f"  ≥ 20 years: {n_20y} / 15")

    return 0


if __name__ == "__main__":
    sys.exit(main())
