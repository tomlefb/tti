"""HTF transferability pre-flight (P0 of STRATEGY_RESEARCH_PROTOCOL.md §7).

Validates the premise that decisions taken on closed H4 candles
converge across data sources, even though wick-level (M5)
detectors do not.

Strategy under test: trivial MA50-cross on close H4.
- Long signal: close > MA50(close, n=50)
- Short signal: close < MA50(close, n=50)
- Trigger: H4 candle where sign(close - MA50) flips vs previous bar

We DO NOT measure an edge here. We compare the *series of trigger
timestamps* between sources, pair by pair. Mismatch is computed as
1 - |intersection| / |union|. Strict same-bar match (no ±1 candle
tolerance).

Output: console report + JSON file under
calibration/runs/<date>_htf_transferability_preflight.json.

Caveats:
- H4 candles do not naturally align across sources (MT5 = broker
  Athens tz, DBN = UTC midnight, Duk = whatever we resample to).
  We re-resample all three from M5 to H4 with UTC origin
  (00:00, 04:00, 08:00, 12:00, 16:00, 20:00) so a "same-bar" match
  is even meaningful.
- DBN is futures back-adjusted (Panama). Level offset vs MT5/Duk
  is large but irrelevant: the MA50-cross signal is invariant
  under additive shifts.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
DUK_ROOT = REPO_ROOT / "tests" / "fixtures" / "dukascopy"
MT5_ROOT = REPO_ROOT / "tests" / "fixtures" / "historical"
DBN_ROOT = REPO_ROOT / "tests" / "fixtures" / "historical_extended" / "processed_adjusted"

INSTRUMENTS = ["NDX100", "XAUUSD", "SPX500"]
MA_PERIOD = 50

# Common window driven by MT5 coverage (the shortest source).
# NDX/SPX MT5 starts 2022-10-20; DBN ends 2026-04-29.
WINDOW_START = pd.Timestamp("2022-10-21", tz="UTC")
WINDOW_END = pd.Timestamp("2026-04-29", tz="UTC")


def load_mt5_m5(instrument: str) -> pd.DataFrame:
    df = pd.read_parquet(MT5_ROOT / f"{instrument}_M5.parquet")
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df.set_index("time")[["open", "high", "low", "close"]]


def load_dbn_m5(instrument: str) -> pd.DataFrame:
    df = pd.read_parquet(DBN_ROOT / f"{instrument}_M5.parquet")
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df.set_index("time")[["open", "high", "low", "close"]]


def load_duk_m5(instrument: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Load and concatenate Dukascopy monthly M5 parquets within window."""
    instrument_dir = DUK_ROOT / instrument
    files = sorted(instrument_dir.glob("*_bid.parquet"))
    frames = []
    for f in files:
        # filename pattern: YYYY-MM_bid.parquet
        ym = f.stem.split("_")[0]
        try:
            month_start = pd.Timestamp(f"{ym}-01", tz="UTC")
        except Exception:
            continue
        # quick filter: skip files entirely outside the window
        if month_start > end + pd.Timedelta(days=31):
            continue
        if month_start + pd.Timedelta(days=31) < start:
            continue
        frames.append(pd.read_parquet(f))
    if not frames:
        raise FileNotFoundError(f"No Dukascopy M5 parquets found for {instrument}")
    df = pd.concat(frames)
    df = df.sort_index()
    df = df[~df.index.duplicated(keep="first")]
    return df[["open", "high", "low", "close"]]


def resample_m5_to_h4_utc(m5: pd.DataFrame) -> pd.DataFrame:
    """Resample M5 to H4 anchored at UTC midnight (00, 04, 08, 12, 16, 20).

    Drops bars with no underlying M5 data (avoids spurious signals
    on weekends / market closes).
    """
    h4 = (
        m5.resample("4h", origin="epoch", label="left", closed="left")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
        .dropna(subset=["close"])
    )
    return h4


def compute_cross_triggers(h4: pd.DataFrame, ma_period: int = MA_PERIOD) -> pd.DataFrame:
    """Return a DataFrame of trigger events.

    Trigger = bar where sign(close - MA50) flips vs the previous bar.
    Direction = +1 (long) / -1 (short).
    """
    df = h4.copy()
    df["ma"] = df["close"].rolling(ma_period, min_periods=ma_period).mean()
    df = df.dropna(subset=["ma"])
    df["sign"] = (df["close"] > df["ma"]).astype(int) * 2 - 1  # +1 / -1
    df["sign_prev"] = df["sign"].shift(1)
    df = df.dropna(subset=["sign_prev"])
    df["sign_prev"] = df["sign_prev"].astype(int)
    triggers = df[df["sign"] != df["sign_prev"]].copy()
    return triggers[["sign"]].rename(columns={"sign": "direction"})


def compute_pair_mismatch(a: set[pd.Timestamp], b: set[pd.Timestamp]) -> dict:
    inter = a & b
    union = a | b
    n_a = len(a)
    n_b = len(b)
    n_inter = len(inter)
    n_union = len(union)
    mismatch = 1.0 - (n_inter / n_union) if n_union else 0.0
    return {
        "n_a": n_a,
        "n_b": n_b,
        "n_intersection": n_inter,
        "n_union": n_union,
        "mismatch_pct": round(mismatch * 100, 2),
        "agreement_pct": round((n_inter / n_union * 100) if n_union else 0.0, 2),
    }


def classify_verdict(mismatch_pct: float) -> str:
    if mismatch_pct < 10:
        return "Excellente"
    if mismatch_pct < 20:
        return "Bonne"
    if mismatch_pct < 40:
        return "Borderline"
    return "Cassee"


def run_instrument(instrument: str) -> dict:
    print(f"\n=== {instrument} ===")

    duk_m5 = load_duk_m5(instrument, WINDOW_START, WINDOW_END)
    mt5_m5 = load_mt5_m5(instrument)
    dbn_m5 = load_dbn_m5(instrument)

    # Restrict every source to the common window.
    duk_m5 = duk_m5.loc[(duk_m5.index >= WINDOW_START) & (duk_m5.index <= WINDOW_END)]
    mt5_m5 = mt5_m5.loc[(mt5_m5.index >= WINDOW_START) & (mt5_m5.index <= WINDOW_END)]
    dbn_m5 = dbn_m5.loc[(dbn_m5.index >= WINDOW_START) & (dbn_m5.index <= WINDOW_END)]

    print(
        f"  M5 rows: Duk={len(duk_m5)}, MT5={len(mt5_m5)}, DBN={len(dbn_m5)}"
    )

    duk_h4 = resample_m5_to_h4_utc(duk_m5)
    mt5_h4 = resample_m5_to_h4_utc(mt5_m5)
    dbn_h4 = resample_m5_to_h4_utc(dbn_m5)

    print(
        f"  H4 rows post-resample: Duk={len(duk_h4)}, MT5={len(mt5_h4)}, DBN={len(dbn_h4)}"
    )

    duk_trig = compute_cross_triggers(duk_h4)
    mt5_trig = compute_cross_triggers(mt5_h4)
    dbn_trig = compute_cross_triggers(dbn_h4)

    print(
        f"  Triggers: Duk={len(duk_trig)}, MT5={len(mt5_trig)}, DBN={len(dbn_trig)}"
    )

    duk_set = set(duk_trig.index)
    mt5_set = set(mt5_trig.index)
    dbn_set = set(dbn_trig.index)

    pairs = {
        "duk_vs_mt5": compute_pair_mismatch(duk_set, mt5_set),
        "duk_vs_dbn": compute_pair_mismatch(duk_set, dbn_set),
        "mt5_vs_dbn": compute_pair_mismatch(mt5_set, dbn_set),
    }

    for k, v in pairs.items():
        print(
            f"  {k}: agreement={v['agreement_pct']}% mismatch={v['mismatch_pct']}% "
            f"({v['n_intersection']}/{v['n_union']})"
        )

    # Direction-aware comparison: timestamps in intersection but with
    # different long/short direction count as mismatch.
    direction_disagreements: dict[str, int] = {}
    for label, a, b in (
        ("duk_vs_mt5", duk_trig, mt5_trig),
        ("duk_vs_dbn", duk_trig, dbn_trig),
        ("mt5_vs_dbn", mt5_trig, dbn_trig),
    ):
        a_dir = a["direction"].to_dict()
        b_dir = b["direction"].to_dict()
        common = set(a_dir) & set(b_dir)
        diff = sum(1 for ts in common if a_dir[ts] != b_dir[ts])
        direction_disagreements[label] = diff
    print(f"  direction disagreements on common bars: {direction_disagreements}")

    return {
        "instrument": instrument,
        "n_triggers": {
            "duk": len(duk_trig),
            "mt5": len(mt5_trig),
            "dbn": len(dbn_trig),
        },
        "pairs": pairs,
        "direction_disagreements_on_common_bars": direction_disagreements,
        "verdict_per_pair": {k: classify_verdict(v["mismatch_pct"]) for k, v in pairs.items()},
    }


def main() -> None:
    print(f"HTF transferability pre-flight — MA{MA_PERIOD} cross on close H4")
    print(f"Window: {WINDOW_START.date()} -> {WINDOW_END.date()}")
    print(f"Resample alignment: UTC origin (00, 04, 08, 12, 16, 20)")

    results = [run_instrument(inst) for inst in INSTRUMENTS]

    print("\n=== Aggregate ===")
    # Aggregate verdict: median mismatch across (instrument, pair) cells.
    all_mismatches: list[float] = []
    duk_mt5_mismatches: list[float] = []
    for r in results:
        for v in r["pairs"].values():
            all_mismatches.append(v["mismatch_pct"])
        duk_mt5_mismatches.append(r["pairs"]["duk_vs_mt5"]["mismatch_pct"])
    median_all = sorted(all_mismatches)[len(all_mismatches) // 2]
    median_duk_mt5 = sorted(duk_mt5_mismatches)[len(duk_mt5_mismatches) // 2]
    print(f"Median mismatch across all (instrument, pair) cells: {median_all}%")
    print(f"Median mismatch Duk vs MT5: {median_duk_mt5}%")
    global_verdict = classify_verdict(median_duk_mt5)
    print(f"Global verdict (driven by Duk vs MT5 — the load-bearing pair): {global_verdict}")

    out_path = REPO_ROOT / "calibration" / "runs" / f"{date.today().isoformat()}_htf_transferability_preflight.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out = {
        "ma_period": MA_PERIOD,
        "window_start": WINDOW_START.isoformat(),
        "window_end": WINDOW_END.isoformat(),
        "resample_alignment": "utc_origin_4h",
        "instruments": results,
        "aggregate": {
            "median_mismatch_pct_all_pairs": median_all,
            "median_mismatch_pct_duk_vs_mt5": median_duk_mt5,
            "global_verdict": global_verdict,
        },
    }
    out_path.write_text(json.dumps(out, indent=2, default=str))
    print(f"\nWrote: {out_path.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
