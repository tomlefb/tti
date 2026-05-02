"""Deep diagnosis MT5 vs Databento — five structural tests.

Goal: determine whether MT5 broker fixtures and Panama-adjusted Databento
fixtures track the same market or two structurally different time series.
Phase 1 already showed near-zero detection match; this deeper view
inspects the underlying candles directly to localize the divergence.

Tests (all on the overlap window, XAUUSD + NDX100 + SPX500):

    1. Timestamp alignment — what fraction of candles share a timestamp?
       Where do mismatches concentrate (session boundaries, weekends)?
    2. Candle-shape correlation — on co-timed candles, do body% and
       wick% correlate (>0.95 = same microstructure)?
    3. Direction agreement — do close>open match per candle?
    4. ATR(14) — do the two sources have comparable volatility?
    5. Concrete sweep examples — pick candles that look like sweeps on
       MT5; does the DB candle on the same timestamp look like a sweep?

Read-only. Output: ``calibration/runs/{TS}_mt5_vs_databento_deep_diagnosis.md``.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MT5_DIR = _REPO_ROOT / "tests" / "fixtures" / "historical"
_DB_ADJ_DIR = _REPO_ROOT / "tests" / "fixtures" / "historical_extended" / "processed_adjusted"
_RUNS_DIR = _REPO_ROOT / "calibration" / "runs"
_PAIRS = ["XAUUSD", "NDX100", "SPX500"]
_TIMESTAMP = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")

# SPX500 is not in MT5 fixtures (MT5 dropped it Sprint 6.5); keep
# diagnosis focused on XAU and NDX where both sources exist. SPX gets
# a single-source row.


def _load(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    if df["time"].dt.tz is None:
        df["time"] = df["time"].dt.tz_localize("UTC")
    df = df.set_index("time").sort_index()
    # Drop duplicates that would break pandas alignment in joins/comparisons.
    df = df[~df.index.duplicated(keep="first")]
    return df


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.rolling(period).mean()


def _candle_metrics(df: pd.DataFrame) -> pd.DataFrame:
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    body = (df["close"] - df["open"]) / rng
    upper = (df["high"] - df[["open", "close"]].max(axis=1)) / rng
    lower = (df[["open", "close"]].min(axis=1) - df["low"]) / rng
    return pd.DataFrame({"body_pct": body, "upper_wick": upper, "lower_wick": lower})


def _sweep_score(df: pd.DataFrame, lookback: int = 12) -> pd.Series:
    """Heuristic: a candle "looks like a sweep" if its high exceeds the
    rolling N-bar high with a meaningful upper wick, OR its low pierces
    the rolling N-bar low with a meaningful lower wick. Magnitude scaled
    by ATR. Returns a signed magnitude per candle (+ for upper sweep,
    - for lower sweep, 0 for neither)."""
    rolling_high = df["high"].rolling(lookback).max().shift(1)
    rolling_low = df["low"].rolling(lookback).min().shift(1)
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    upper_wick = (df["high"] - df[["open", "close"]].max(axis=1)) / rng
    lower_wick = (df[["open", "close"]].min(axis=1) - df["low"]) / rng
    upper_pierce = df["high"] - rolling_high
    lower_pierce = rolling_low - df["low"]
    out = pd.Series(0.0, index=df.index)
    upper_mask = (upper_pierce > 0) & (upper_wick > 0.5)
    lower_mask = (lower_pierce > 0) & (lower_wick > 0.5)
    out.loc[upper_mask] = upper_pierce[upper_mask]
    out.loc[lower_mask] = -lower_pierce[lower_mask]
    return out


def _section_for_pair(pair: str, lines: list[str]) -> dict | None:
    mt5 = _load(_MT5_DIR / f"{pair}_M5.parquet")
    db = _load(_DB_ADJ_DIR / f"{pair}_M5.parquet")
    if mt5 is None or db is None:
        lines.append(f"### {pair}")
        lines.append("")
        if mt5 is None:
            lines.append(f"- MT5 fixture not available — skipping {pair}.")
        else:
            lines.append(f"- Databento adjusted fixture not available — skipping {pair}.")
        lines.append("")
        return None

    overlap_start = max(mt5.index.min(), db.index.min())
    overlap_end = min(mt5.index.max(), db.index.max())
    mt5_o = mt5.loc[overlap_start:overlap_end]
    db_o = db.loc[overlap_start:overlap_end]

    lines.append(f"### {pair}")
    lines.append("")
    lines.append(
        f"- Overlap window: {overlap_start.isoformat()} → {overlap_end.isoformat()}"
    )
    lines.append(f"- MT5 timezone: {mt5.index.tz}, Databento timezone: {db.index.tz}")
    lines.append(f"- MT5 candles in overlap: {len(mt5_o):,}")
    lines.append(f"- DB candles in overlap: {len(db_o):,}")

    common = mt5_o.index.intersection(db_o.index)
    mt5_only = mt5_o.index.difference(db_o.index)
    db_only = db_o.index.difference(mt5_o.index)

    pct_common_mt5 = 100.0 * len(common) / len(mt5_o) if len(mt5_o) else 0.0
    pct_common_db = 100.0 * len(common) / len(db_o) if len(db_o) else 0.0

    lines.append(f"- Common timestamps: {len(common):,}")
    lines.append(
        f"- MT5-only timestamps: {len(mt5_only):,} ({100.0*len(mt5_only)/max(len(mt5_o),1):.2f}%)"
    )
    lines.append(
        f"- DB-only timestamps: {len(db_only):,} ({100.0*len(db_only)/max(len(db_o),1):.2f}%)"
    )
    lines.append(f"- Coverage: MT5 ∩ DB = {pct_common_mt5:.1f}% of MT5, {pct_common_db:.1f}% of DB")

    # Pattern of mismatches — look at hour-of-day and weekday distribution.
    if len(mt5_only):
        sample = mt5_only[:8].tolist()
        lines.append(f"- Sample MT5-only timestamps: {[t.isoformat() for t in sample]}")
        # Hour distribution
        hours_mt5_only = mt5_only.hour.value_counts().sort_index()
        top = hours_mt5_only.nlargest(5)
        lines.append(
            f"- MT5-only by hour (top 5 UTC): "
            f"{dict(zip(top.index.tolist(), top.values.tolist()))}"
        )
    if len(db_only):
        sample = db_only[:8].tolist()
        lines.append(f"- Sample DB-only timestamps: {[t.isoformat() for t in sample]}")
        hours_db_only = db_only.hour.value_counts().sort_index()
        top = hours_db_only.nlargest(5)
        lines.append(
            f"- DB-only by hour (top 5 UTC): "
            f"{dict(zip(top.index.tolist(), top.values.tolist()))}"
        )
    lines.append("")

    if len(common) == 0:
        lines.append("- No common timestamps; cannot run shape/direction/ATR tests.")
        lines.append("")
        return None

    # Test 2 — candle shape correlation on common timestamps.
    mt5_c = _candle_metrics(mt5.loc[common])
    db_c = _candle_metrics(db.loc[common])
    mt5_c.columns = [c + "_mt5" for c in mt5_c.columns]
    db_c.columns = [c + "_db" for c in db_c.columns]
    j = mt5_c.join(db_c, how="inner").dropna()

    body_corr = j["body_pct_mt5"].corr(j["body_pct_db"])
    upper_corr = j["upper_wick_mt5"].corr(j["upper_wick_db"])
    lower_corr = j["lower_wick_mt5"].corr(j["lower_wick_db"])

    lines.append("**Test 2 — Candle shape correlations (Pearson r, common timestamps):**")
    lines.append("")
    lines.append(f"- Body %: {body_corr:.4f}")
    lines.append(f"- Upper wick %: {upper_corr:.4f}")
    lines.append(f"- Lower wick %: {lower_corr:.4f}")
    lines.append("")

    # Test 3 — direction agreement.
    common_sorted = common.sort_values()
    mt5_aligned = mt5.reindex(common_sorted)
    db_aligned = db.reindex(common_sorted)
    mt5_bull = (mt5_aligned["close"] > mt5_aligned["open"]).to_numpy()
    db_bull = (db_aligned["close"] > db_aligned["open"]).to_numpy()
    agree = mt5_bull == db_bull
    agree_rate = float(agree.mean())
    rng_mt5 = (mt5_aligned["high"] - mt5_aligned["low"]).to_numpy()
    median_rng = float(np.nanmedian(rng_mt5))
    nontrivial_mask = rng_mt5 >= median_rng
    agree_nontrivial = (
        float(agree[nontrivial_mask].mean()) if nontrivial_mask.any() else float("nan")
    )

    lines.append("**Test 3 — Direction agreement on common timestamps:**")
    lines.append("")
    lines.append(f"- Overall: {agree_rate:.4f} ({len(common):,} candles)")
    lines.append(
        f"- On non-trivial candles (range ≥ median {median_rng:.4f}): {agree_nontrivial:.4f}"
    )
    lines.append("")

    # Test 4 — ATR(14) over the FULL overlap series of each source (not just common).
    mt5_atr_series = _atr(mt5_o, 14).dropna()
    db_atr_series = _atr(db_o, 14).dropna()
    mt5_atr_mean = float(mt5_atr_series.mean())
    db_atr_mean = float(db_atr_series.mean())
    mt5_atr_med = float(mt5_atr_series.median())
    db_atr_med = float(db_atr_series.median())
    ratio_mean = db_atr_mean / mt5_atr_mean if mt5_atr_mean else float("nan")
    ratio_med = db_atr_med / mt5_atr_med if mt5_atr_med else float("nan")

    # ATR correlation on common timestamps (point-by-point)
    mt5_atr_c = _atr(mt5.loc[common], 14)
    db_atr_c = _atr(db.loc[common], 14)
    atr_corr = mt5_atr_c.corr(db_atr_c)

    lines.append("**Test 4 — ATR(14) comparison:**")
    lines.append("")
    lines.append(f"- Mean ATR MT5: {mt5_atr_mean:.4f} | Median: {mt5_atr_med:.4f}")
    lines.append(f"- Mean ATR DB:  {db_atr_mean:.4f} | Median: {db_atr_med:.4f}")
    lines.append(f"- DB/MT5 ratio: mean {ratio_mean:.3f}, median {ratio_med:.3f}")
    lines.append(f"- Per-bar ATR(14) Pearson correlation on common ts: {atr_corr:.4f}")
    lines.append("")

    # Test 5 — concrete sweep examples.
    sweeps_mt5 = _sweep_score(mt5_o, lookback=12)
    sweeps_mt5_strong = sweeps_mt5[sweeps_mt5.abs() > 0]
    sample_n = 8
    if len(sweeps_mt5_strong) > sample_n:
        # Pick spread out samples — every Nth strong sweep.
        step = max(1, len(sweeps_mt5_strong) // sample_n)
        examples_idx = sweeps_mt5_strong.index[::step][:sample_n]
    else:
        examples_idx = sweeps_mt5_strong.index[:sample_n]

    sweeps_db = _sweep_score(db_o, lookback=12)

    lines.append("**Test 5 — Concrete sweep examples (MT5-detected, DB at same timestamp):**")
    lines.append("")
    lines.append(
        "| Timestamp | MT5 OHLC | MT5 sweep mag | DB OHLC | DB sweep mag | Same direction? |"
    )
    lines.append("|---|---|---:|---|---:|:---:|")
    for ts in examples_idx:
        if ts not in db_o.index:
            continue
        m_row = mt5_o.loc[ts]
        d_row = db_o.loc[ts]
        ms = sweeps_mt5.get(ts, 0.0)
        ds = sweeps_db.get(ts, 0.0)
        m_ohlc = (
            f"O={m_row['open']:.2f} H={m_row['high']:.2f} "
            f"L={m_row['low']:.2f} C={m_row['close']:.2f}"
        )
        d_ohlc = (
            f"O={d_row['open']:.2f} H={d_row['high']:.2f} "
            f"L={d_row['low']:.2f} C={d_row['close']:.2f}"
        )
        same = "✓" if (ms > 0 and ds > 0) or (ms < 0 and ds < 0) else "✗"
        lines.append(
            f"| {ts.isoformat()} | {m_ohlc} | {ms:+.4f} | {d_ohlc} | {ds:+.4f} | {same} |"
        )
    lines.append("")

    # Quantitative sweep agreement (use numpy alignment to avoid duplicate-index issues).
    sweeps_mt5_c = _sweep_score(mt5_aligned, lookback=12).to_numpy()
    sweeps_db_c = _sweep_score(db_aligned, lookback=12).to_numpy()
    mt5_strong = np.abs(sweeps_mt5_c) > 0
    db_strong = np.abs(sweeps_db_c) > 0
    both_strong = mt5_strong & db_strong
    if both_strong.any():
        same_dir = (sweeps_mt5_c[both_strong] > 0) == (sweeps_db_c[both_strong] > 0)
        sweep_agree_rate = float(same_dir.mean())
    else:
        sweep_agree_rate = float("nan")

    lines.append(
        f"- Per-candle sweep events (heuristic): MT5={int(mt5_strong.sum()):,}, "
        f"DB={int(db_strong.sum()):,}, both={int(both_strong.sum()):,}"
    )
    lines.append(
        f"- When BOTH flag a sweep on the same candle, direction "
        f"agreement: {sweep_agree_rate:.4f}"
    )
    lines.append("")

    return {
        "pair": pair,
        "common": len(common),
        "mt5_only": len(mt5_only),
        "db_only": len(db_only),
        "body_corr": body_corr,
        "upper_corr": upper_corr,
        "lower_corr": lower_corr,
        "agree_rate": float(agree_rate),
        "agree_nontrivial": float(agree_nontrivial)
        if not pd.isna(agree_nontrivial)
        else None,
        "atr_ratio_mean": ratio_mean,
        "atr_ratio_median": ratio_med,
        "atr_corr": float(atr_corr) if not pd.isna(atr_corr) else None,
        "sweep_agree_rate": float(sweep_agree_rate)
        if not pd.isna(sweep_agree_rate)
        else None,
    }


def main() -> int:
    print(f"=== Deep diagnosis MT5 vs Databento — {_TIMESTAMP} ===", flush=True)
    lines: list[str] = []
    lines.append(f"# MT5 vs Databento — deep structural diagnosis — {_TIMESTAMP}")
    lines.append("")
    lines.append(
        "Five tests inspecting the raw candle data of the operator's MT5 "
        "broker fixtures vs the Panama-adjusted Databento fixtures. "
        "Goal: determine whether the two sources track the same market or "
        "structurally different time series. All tests are read-only."
    )
    lines.append("")
    lines.append("## Tests 1-5 per instrument")
    lines.append("")

    summaries: dict[str, dict] = {}
    for pair in _PAIRS:
        s = _section_for_pair(pair, lines)
        if s is not None:
            summaries[pair] = s

    # Cross-pair summary.
    lines.append("## Cross-pair summary")
    lines.append("")
    if summaries:
        lines.append(
            "| Pair | Common ts | Body corr | Upper wick corr | Direction agree | "
            "ATR ratio (DB/MT5) | ATR corr | Sweep agree |"
        )
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        for p, s in summaries.items():
            sweep_str = f"{s['sweep_agree_rate']:.3f}" if s["sweep_agree_rate"] is not None else "—"
            lines.append(
                f"| {p} | {s['common']:,} | {s['body_corr']:.3f} | "
                f"{s['upper_corr']:.3f} | {s['agree_rate']:.3f} | "
                f"{s['atr_ratio_median']:.2f} | "
                f"{s['atr_corr']:.3f} | {sweep_str} |"
            )
        lines.append("")
    else:
        lines.append("- No paired data available.")
        lines.append("")

    # Conclusion logic.
    lines.append("## Conclusion")
    lines.append("")
    if summaries:
        # Aggregated diagnosis.
        weak_shape = [p for p, s in summaries.items() if s["body_corr"] < 0.7]
        weak_dir = [p for p, s in summaries.items() if s["agree_rate"] < 0.95]
        big_atr = [
            p
            for p, s in summaries.items()
            if abs(s["atr_ratio_median"] - 1.0) > 0.5
        ]

        if weak_shape:
            lines.append(
                "- ⚠️ **Microstructure mismatch**: candle body correlation < 0.70 on "
                f"{', '.join(weak_shape)}. The same minute window has different "
                "body/wick shapes — i.e., the bid/ask flows that print into the "
                "candle are genuinely different between sources."
            )
        else:
            lines.append(
                "- ✅ Candle body correlation ≥ 0.70 on all paired instruments — "
                "microstructure is broadly aligned."
            )
        if weak_dir:
            lines.append(
                f"- ⚠️ **Direction disagreement**: per-candle close>open match < 95% "
                f"on {', '.join(weak_dir)}. The two sources disagree on whether a "
                "candle was bullish or bearish."
            )
        else:
            lines.append(
                "- ✅ Direction agreement ≥ 95% on all paired instruments — "
                "candles broadly agree on close>open."
            )
        if big_atr:
            lines.append(
                f"- ⚠️ **Volatility scale mismatch**: ATR(14) ratio outside [0.5, 1.5] "
                f"on {', '.join(big_atr)}. The detector's sweep_buffer / FVG_MIN_SIZE "
                "thresholds are calibrated for one volatility regime — mismatched "
                "ATR means thresholds will fire differently."
            )
        else:
            lines.append(
                "- ✅ ATR ratio in [0.5, 1.5] on all paired instruments — "
                "volatility is broadly comparable."
            )
        lines.append("")

        # Final verdict on the question "same market?".
        # Heuristic: if body_corr >= 0.7 AND direction >= 0.95 AND ATR ratio close to 1, "same market".
        same_market = all(
            s["body_corr"] >= 0.7
            and s["agree_rate"] >= 0.95
            and abs(s["atr_ratio_median"] - 1.0) <= 0.5
            for s in summaries.values()
        )
        if same_market:
            lines.append(
                "**Verdict: SAME MARKET** — the two sources reflect compatible "
                "microstructure. The detection divergence (97% mismatch in Phase 1) "
                "must come from absolute price-level offsets that shift sweep "
                "buffers / equal-HL tolerances around levels, not from "
                "structural data incompatibility. Re-detuning the per-instrument "
                "sweep_buffer in absolute terms (or switching to ATR-relative "
                "thresholds) should reconcile detection."
            )
        else:
            lines.append(
                "**Verdict: STRUCTURALLY DIFFERENT MARKETS** — even at the candle "
                "level, the two sources disagree on shape, direction, or volatility. "
                "The Sprint 6.5 MT5 backtest and the 10-year Databento backtest "
                "are measuring different markets. The operator's choice of broker "
                "(which fixture format MT5 uses) determines which signal set the "
                "detector will see in production. Going forward, **only fixtures "
                "from the live broker can be used to validate parameters**; "
                "Databento futures data is unsuitable as a proxy."
            )
        lines.append("")

    _RUNS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _RUNS_DIR / f"{_TIMESTAMP}_mt5_vs_databento_deep_diagnosis.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")

    # Stdout summary.
    print()
    print("=== Summary ===")
    for p, s in summaries.items():
        sweep_str = f"{s['sweep_agree_rate']:.3f}" if s["sweep_agree_rate"] is not None else "—"
        print(
            f"  {p}: common ts={s['common']:,}, "
            f"body corr={s['body_corr']:.3f}, "
            f"dir agree={s['agree_rate']:.3f}, "
            f"ATR ratio={s['atr_ratio_median']:.2f}, "
            f"ATR corr={s['atr_corr']:.3f}, "
            f"sweep agree={sweep_str}"
        )
    print(f"  Report: {out_path.relative_to(_REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
