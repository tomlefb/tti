"""3-way ground-truth alignment: Dukascopy vs MT5 vs Databento.

Quantifies, per instrument, how Dukascopy's free historical M5 OHLCV
aligns with MT5 (broker CFD export) and Databento (back-adjusted
futures, Panama processing). Three instruments overlap on all three
sources — XAUUSD, NDX100, SPX500 — and these are the basis of the
verdict. The other four canonical instruments (EURUSD, GBPUSD, US30,
BTCUSD) are not in the Databento fixture and are excluded from this
comparison without blocking the report.

For each 3-way instrument:

* Find the common window where all three sources have data.
* Sample 10 trading days at random (seed = 42) from that window.
* On the sampled days, intersect the M5 timestamps across the three
  sources. For each common timestamp, record OHLC differences pair-by-
  pair (Duk vs MT5, Duk vs DBN, MT5 vs DBN).
* Aggregate per pair: mean / p50 / p95 / p99 absolute close diff in
  both absolute price units and relative percentage; Pearson
  correlation of close-to-close returns; sign agreement of bar bodies.
* Verdict per instrument:
    - A: Duk aligns MT5 — corr(Duk, MT5) > 0.95 and exceeds
      corr(Duk, DBN) by ≥ 0.02.
    - B: Duk aligns DBN — corr(Duk, DBN) > 0.95 and exceeds
      corr(Duk, MT5) by ≥ 0.02.
    - C: Duk is a third distinct source — neither A nor B.

Output: ``calibration/runs/3way_alignment_<TS>/{raw_diff.md, verdict.md}``.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.data.dukascopy import DukascopyClient  # noqa: E402

_MT5_DIR = _REPO_ROOT / "tests" / "fixtures" / "historical"
_DBN_DIR = _REPO_ROOT / "tests" / "fixtures" / "historical_extended" / "processed_adjusted"
_RUNS_DIR = _REPO_ROOT / "calibration" / "runs"

# Instruments present on all three sources.
THREEWAY_INSTRUMENTS = ["XAUUSD", "NDX100", "SPX500"]

N_SAMPLE_DAYS = 10
RNG_SEED = 42

# Verdict thresholds.
CORR_PRIMARY = 0.95
CORR_GAP = 0.02


def _load_mt5(instrument: str) -> pd.DataFrame:
    path = _MT5_DIR / f"{instrument}_M5.parquet"
    df = pd.read_parquet(path)
    df = df.set_index("time").sort_index()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df[["open", "high", "low", "close"]].astype("float64")


def _load_dbn(instrument: str) -> pd.DataFrame:
    path = _DBN_DIR / f"{instrument}_M5.parquet"
    df = pd.read_parquet(path)
    df = df.set_index("time").sort_index()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df[["open", "high", "low", "close"]].astype("float64")


def _load_duk(client: DukascopyClient, instrument: str,
              start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    df = client.fetch_m5(
        instrument,
        start=start.to_pydatetime(),
        end=(end + pd.Timedelta(seconds=1)).to_pydatetime(),
        side="bid",
        use_cache=True,
    )
    return df[["open", "high", "low", "close"]].astype("float64")


def _common_window(frames: Iterable[pd.DataFrame]) -> tuple[pd.Timestamp, pd.Timestamp]:
    starts = [f.index.min() for f in frames]
    ends = [f.index.max() for f in frames]
    return max(starts), min(ends)


def _sample_trading_days(
    start: pd.Timestamp, end: pd.Timestamp, n: int, rng: np.random.Generator
) -> list[date]:
    """Return ``n`` distinct weekday dates uniformly sampled from ``[start, end]``."""
    candidates: list[date] = []
    cur = start.normalize()
    end_norm = end.normalize()
    while cur <= end_norm:
        # Mon..Fri (Python weekday: Mon=0 ... Sun=6)
        if cur.weekday() < 5:
            candidates.append(cur.date())
        cur += pd.Timedelta(days=1)
    if len(candidates) <= n:
        return candidates
    idx = rng.choice(len(candidates), size=n, replace=False)
    return sorted([candidates[i] for i in idx])


def _restrict_to_days(df: pd.DataFrame, days: list[date]) -> pd.DataFrame:
    if len(df) == 0:
        return df
    day_set = set(days)
    mask = pd.Series(df.index.date, index=df.index).isin(day_set)
    return df.loc[mask.values]


def _pair_metrics(df_a: pd.DataFrame, df_b: pd.DataFrame) -> dict:
    """Compute OHLC diff and return-correlation metrics on the common index."""
    common = df_a.index.intersection(df_b.index)
    a = df_a.loc[common]
    b = df_b.loc[common]
    n = len(common)
    if n == 0:
        return {"n": 0}

    out: dict[str, float | int] = {"n": n}
    for col in ("open", "high", "low", "close"):
        diff = (a[col] - b[col]).abs()
        # Use mean of the two as the price scale for the relative metric.
        scale = ((a[col] + b[col]).abs() / 2.0).replace(0, np.nan)
        rel = (diff / scale).dropna()
        out[f"{col}_mad_abs"] = float(diff.mean())
        out[f"{col}_p50_abs"] = float(diff.quantile(0.50))
        out[f"{col}_p95_abs"] = float(diff.quantile(0.95))
        out[f"{col}_p99_abs"] = float(diff.quantile(0.99))
        out[f"{col}_mad_rel_pct"] = float(rel.mean() * 100.0) if len(rel) else float("nan")
        out[f"{col}_p95_rel_pct"] = float(rel.quantile(0.95) * 100.0) if len(rel) else float("nan")

    ret_a = a["close"].pct_change().dropna()
    ret_b = b["close"].pct_change().dropna()
    common_ret = ret_a.index.intersection(ret_b.index)
    if len(common_ret) >= 2:
        out["return_pearson"] = float(
            ret_a.loc[common_ret].corr(ret_b.loc[common_ret])
        )
    else:
        out["return_pearson"] = float("nan")

    sign_a = np.sign(a["close"] - a["open"])
    sign_b = np.sign(b["close"] - b["open"])
    if n > 0:
        out["body_sign_agreement"] = float((sign_a == sign_b).mean())
    else:
        out["body_sign_agreement"] = float("nan")

    return out


def _verdict_for(corr_dm: float, corr_dd: float) -> tuple[str, str]:
    """Return (label, rationale) per the spec thresholds."""
    if np.isnan(corr_dm) or np.isnan(corr_dd):
        return "C", "insufficient data for both pairs"
    if corr_dm > CORR_PRIMARY and (corr_dm - corr_dd) >= CORR_GAP:
        return "A", (
            f"corr(Duk,MT5)={corr_dm:.3f} > {CORR_PRIMARY:.2f} "
            f"and exceeds corr(Duk,DBN)={corr_dd:.3f} by "
            f"{corr_dm - corr_dd:+.3f} (>= {CORR_GAP})"
        )
    if corr_dd > CORR_PRIMARY and (corr_dd - corr_dm) >= CORR_GAP:
        return "B", (
            f"corr(Duk,DBN)={corr_dd:.3f} > {CORR_PRIMARY:.2f} "
            f"and exceeds corr(Duk,MT5)={corr_dm:.3f} by "
            f"{corr_dd - corr_dm:+.3f} (>= {CORR_GAP})"
        )
    return "C", (
        f"corr(Duk,MT5)={corr_dm:.3f}, corr(Duk,DBN)={corr_dd:.3f} — "
        f"neither passes the {CORR_PRIMARY:.2f} threshold by a "
        f"{CORR_GAP:.2f} margin"
    )


def _format_pair_table(per_pair: dict[str, dict]) -> list[str]:
    """Build the markdown OHLC table for one instrument."""
    lines: list[str] = []
    lines.append("| Pair | N bars | Close MAD abs | Close MAD rel | "
                 "Close p95 abs | Return Pearson | Body sign agree |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for label, m in per_pair.items():
        if m.get("n", 0) == 0:
            lines.append(f"| {label} | 0 | — | — | — | — | — |")
            continue
        lines.append(
            f"| {label} | {m['n']} | "
            f"{m['close_mad_abs']:.4f} | "
            f"{m['close_mad_rel_pct']:.4f}% | "
            f"{m['close_p95_abs']:.4f} | "
            f"{m['return_pearson']:.4f} | "
            f"{m['body_sign_agreement']:.4f} |"
        )
    return lines


def main() -> int:
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    out_dir = _RUNS_DIR / f"3way_alignment_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_path = out_dir / "raw_diff.md"
    verdict_path = out_dir / "verdict.md"

    rng = np.random.default_rng(RNG_SEED)
    client = DukascopyClient()

    raw_lines: list[str] = []
    raw_lines.append(f"# 3-way alignment — raw diff per instrument ({ts})")
    raw_lines.append("")
    raw_lines.append(
        "Pair labels: **Duk** = Dukascopy bid M5 (parquet cache), "
        "**MT5** = MetaTrader 5 broker CFD export "
        "(`tests/fixtures/historical/`), "
        "**DBN** = Databento back-adjusted futures, Panama processing "
        "(`tests/fixtures/historical_extended/processed_adjusted/`)."
    )
    raw_lines.append("")
    raw_lines.append(
        f"Sample: {N_SAMPLE_DAYS} weekday dates drawn uniformly from each "
        f"instrument's 3-source common window (numpy seed={RNG_SEED}). "
        "All M5 timestamps where all three sources have a bar are kept; "
        "metrics are computed on that intersection."
    )
    raw_lines.append("")

    verdict_lines: list[str] = []
    verdict_lines.append(f"# 3-way alignment — verdict ({ts})")
    verdict_lines.append("")
    verdict_lines.append(
        "Verdict thresholds: corr(Duk, X) > "
        f"{CORR_PRIMARY:.2f} and corr exceeds the other pair by "
        f"≥ {CORR_GAP:.2f}. Returns are 5-min close-to-close on the "
        "common-timestamp intersection of the sampled days."
    )
    verdict_lines.append("")
    verdict_lines.append("## Verdict per instrument")
    verdict_lines.append("")
    verdict_lines.append(
        "| Instrument | Common window | N bars | corr(Duk,MT5) | "
        "corr(Duk,DBN) | corr(MT5,DBN) | Verdict |"
    )
    verdict_lines.append("|---|---|---:|---:|---:|---:|---|")

    per_inst_verdict: dict[str, str] = {}

    for instrument in THREEWAY_INSTRUMENTS:
        print(f"=== {instrument} ===")
        mt5 = _load_mt5(instrument)
        dbn = _load_dbn(instrument)
        # Common window from MT5+DBN bounds (MT5 is the limiting source).
        win_start, win_end = _common_window([mt5, dbn])
        # Load Duk over a slightly padded window to be safe on slicing.
        duk = _load_duk(
            client,
            instrument,
            win_start - pd.Timedelta(days=1),
            win_end + pd.Timedelta(days=1),
        )
        # Triple-intersected window.
        win_start, win_end = _common_window([mt5, dbn, duk])
        print(f"  common window: {win_start} -> {win_end}")

        days = _sample_trading_days(win_start, win_end, N_SAMPLE_DAYS, rng)
        print(f"  sampled days: {[d.isoformat() for d in days]}")

        mt5_d = _restrict_to_days(mt5, days)
        dbn_d = _restrict_to_days(dbn, days)
        duk_d = _restrict_to_days(duk, days)

        # Triple-intersected timestamps used for the report.
        common_idx = (
            duk_d.index.intersection(mt5_d.index).intersection(dbn_d.index)
        )
        n_common = len(common_idx)
        print(f"  common bars on sampled days: {n_common}")

        duk_c = duk_d.loc[common_idx]
        mt5_c = mt5_d.loc[common_idx]
        dbn_c = dbn_d.loc[common_idx]

        m_dm = _pair_metrics(duk_c, mt5_c)
        m_dd = _pair_metrics(duk_c, dbn_c)
        m_md = _pair_metrics(mt5_c, dbn_c)

        corr_dm = m_dm.get("return_pearson", float("nan"))
        corr_dd = m_dd.get("return_pearson", float("nan"))
        corr_md = m_md.get("return_pearson", float("nan"))
        v_label, v_rationale = _verdict_for(corr_dm, corr_dd)
        per_inst_verdict[instrument] = v_label
        print(
            f"  corr Duk-MT5={corr_dm:.4f}  Duk-DBN={corr_dd:.4f}  "
            f"MT5-DBN={corr_md:.4f}  =>  verdict {v_label}"
        )

        # Append to raw diff report.
        raw_lines.append(f"## {instrument}")
        raw_lines.append("")
        raw_lines.append(
            f"- Common window: `{win_start}` → `{win_end}` "
            f"({(win_end - win_start).days} days span)"
        )
        raw_lines.append(
            f"- Sampled days (n={len(days)}): "
            + ", ".join(d.isoformat() for d in days)
        )
        raw_lines.append(f"- Common M5 bars after intersection: **{n_common}**")
        raw_lines.append("")
        raw_lines.extend(
            _format_pair_table(
                {"Duk vs MT5": m_dm, "Duk vs DBN": m_dd, "MT5 vs DBN": m_md}
            )
        )
        raw_lines.append("")
        raw_lines.append("Detailed OHLC quantiles (absolute / relative %):")
        raw_lines.append("")
        raw_lines.append(
            "| Pair | open p99 abs | high p99 abs | low p99 abs | "
            "close p99 abs | open p95 rel% | close p95 rel% |"
        )
        raw_lines.append("|---|---:|---:|---:|---:|---:|---:|")
        for label, m in [
            ("Duk vs MT5", m_dm),
            ("Duk vs DBN", m_dd),
            ("MT5 vs DBN", m_md),
        ]:
            if m.get("n", 0) == 0:
                raw_lines.append(f"| {label} | — | — | — | — | — | — |")
                continue
            raw_lines.append(
                f"| {label} | "
                f"{m['open_p99_abs']:.4f} | "
                f"{m['high_p99_abs']:.4f} | "
                f"{m['low_p99_abs']:.4f} | "
                f"{m['close_p99_abs']:.4f} | "
                f"{m['open_p95_rel_pct']:.4f}% | "
                f"{m['close_p95_rel_pct']:.4f}% |"
            )
        raw_lines.append("")

        verdict_lines.append(
            f"| {instrument} | "
            f"{win_start.date()} → {win_end.date()} | "
            f"{n_common} | "
            f"{corr_dm:.4f} | "
            f"{corr_dd:.4f} | "
            f"{corr_md:.4f} | "
            f"**{v_label}** ({v_rationale.split(' — ')[0] if ' — ' in v_rationale else v_rationale[:60]}…) |"
        )

    # Recommendation section.
    n = len(THREEWAY_INSTRUMENTS)
    counts = {k: sum(1 for v in per_inst_verdict.values() if v == k)
              for k in ("A", "B", "C")}
    verdict_lines.append("")
    verdict_lines.append("## Aggregate")
    verdict_lines.append("")
    verdict_lines.append(
        f"- A (Duk ≈ MT5): **{counts['A']}/{n}** — "
        + ", ".join(i for i, v in per_inst_verdict.items() if v == "A")
    )
    verdict_lines.append(
        f"- B (Duk ≈ DBN): **{counts['B']}/{n}** — "
        + ", ".join(i for i, v in per_inst_verdict.items() if v == "B")
    )
    verdict_lines.append(
        f"- C (Duk distinct): **{counts['C']}/{n}** — "
        + ", ".join(i for i, v in per_inst_verdict.items() if v == "C")
    )
    verdict_lines.append("")
    verdict_lines.append("## Recommendation for the source hierarchy")
    verdict_lines.append("")
    if counts["A"] >= 2:
        verdict_lines.append(
            "**Majority A.** Dukascopy is a long-term proxy for MT5 on the "
            "tested instruments. Promote Dukascopy to **primary** source for "
            "calibration and historical backtests (14+ years, free, "
            "deterministic schema). Keep MT5 as the live-runtime source and "
            "Databento as a secondary cross-check on a different market "
            "structure (futures back-adjusted) — useful for sanity but not "
            "primary."
        )
    elif counts["B"] >= 2:
        verdict_lines.append(
            "**Majority B.** Dukascopy and Databento are two long-term datasets "
            "with similar structure (both diverge from broker CFD). MT5 remains "
            "the only source aligned with the production runtime; backtests on "
            "Duk + DBN measure futures-like behaviour and do not predict MT5 "
            "edge directly. The 'edge on 2+ sources' criterion under this "
            "regime would mean 'Duk and DBN' but the operator should treat MT5 "
            "as the ground truth for live decisions."
        )
    elif counts["C"] >= 2:
        verdict_lines.append(
            "**Majority C.** All three are distinct market structures. Adopt "
            "the **'edge on 2 of 3 sources'** rule as the standard for any "
            "strategy: a setup must hold on at least two of {Duk, MT5, DBN} "
            "to be considered robust. Cross-source robustness becomes a "
            "first-class screening criterion — strategies that only profit on "
            "one source are likely overfit to that source's quirks."
        )
    else:
        verdict_lines.append(
            "**Mixed.** Per-instrument verdicts diverge; no global "
            "recommendation. Apply per-instrument hierarchy as documented "
            "above and treat the choice of primary source as an instrument-level "
            "decision."
        )
    verdict_lines.append("")
    verdict_lines.append(
        "EURUSD, GBPUSD, US30, BTCUSD are not present in the Databento "
        "fixture and are excluded from this 3-way comparison. Their "
        "Dukascopy alignment with MT5 alone could be added later as a "
        "supplementary 2-way check."
    )
    verdict_lines.append("")

    raw_path.write_text("\n".join(raw_lines), encoding="utf-8")
    verdict_path.write_text("\n".join(verdict_lines), encoding="utf-8")
    print()
    print(f"Reports written under: {out_dir.relative_to(_REPO_ROOT)}")
    print("  - raw_diff.md")
    print("  - verdict.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
