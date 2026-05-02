"""Pre-flight validation before re-running the parameter sweep on
MT5 fixtures. Three independent diagnostics:

Q1 — Timestamp alignment (~15 min)
    Sample 10 random timestamps from the NDX100 common window, read
    the MT5 and Databento M5 candle at exactly that timestamp, and
    report close-price delta + body-shape consistency. A systematic
    > 1% offset coupled with a body-shape match at T±5 min would
    indicate a timezone bug; a > 1% offset with body shape matched
    at the same T is just the CFD-vs-futures price-level offset
    (already documented).

Q2 — Mismatch detail on 5 NDX MT5 setups (~30 min)
    For 5 MT5 NDX setups (random sample, seed=42) from the prior
    run's full-setup JSONL, show what Databento did on the same
    date+killzone+direction: was a setup emitted at all, what was
    the DBN OHLC at the MT5 setup's MSS-confirm minute, what was
    the closest DBN setup if any. Identifies the dominant
    "why didn't DBN fire here" cause from the data side.

Q3 — Explicit N + CI per cell (~10 min)
    Re-tabulate the 6 BacktestResult JSONs from the prior run with
    n_total / n_closed / mean_r / CI 95% / win rate / setups per
    month, plus a "CI-defensible edge" verdict per cell.

Outputs (under ``calibration/runs/``):
- ``timestamp_alignment_check_<TS>.md``
- ``mismatch_detail_<TS>.md``
- ``cell_stats_explicit_<TS>.md``

Usage::

    python calibration/run_mt5_vs_databento_preflight.py \\
        --prior-run calibration/runs/mt5_vs_databento_tick_2026-05-02T11-43-04Z
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.backtest.result import BacktestResult  # noqa: E402

_PARIS = ZoneInfo("Europe/Paris")
_MT5_DIR = _REPO_ROOT / "tests" / "fixtures" / "historical"
_DBN_DIR = _REPO_ROOT / "tests" / "fixtures" / "historical_extended" / "processed_adjusted"


def _ts() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")


def _load_m5(source_dir: Path, symbol: str) -> pd.DataFrame:
    df = pd.read_parquet(source_dir / f"{symbol}_M5.parquet")
    if df["time"].dt.tz is None:
        df["time"] = df["time"].dt.tz_localize("UTC")
    return df.sort_values("time").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Q1 — Timestamp alignment.
# ---------------------------------------------------------------------------
def _q1_timestamp_alignment(out_dir: Path) -> Path:
    symbol = "NDX100"
    rng = random.Random(42)
    mt5 = _load_m5(_MT5_DIR, symbol)
    dbn = _load_m5(_DBN_DIR, symbol)
    common = sorted(set(mt5["time"]) & set(dbn["time"]))
    if len(common) < 100:
        raise SystemExit(f"too few common timestamps: {len(common)}")
    sample = sorted(rng.sample(common, k=10))

    rows: list[dict] = []
    mt5_ix = {t: i for i, t in enumerate(mt5["time"])}
    dbn_ix = {t: i for i, t in enumerate(dbn["time"])}
    for ts in sample:
        m = mt5.iloc[mt5_ix[ts]]
        d = dbn.iloc[dbn_ix[ts]]
        rows.append(
            {
                "ts_utc": pd.Timestamp(ts).isoformat(),
                "mt5_o": float(m["open"]),
                "mt5_h": float(m["high"]),
                "mt5_l": float(m["low"]),
                "mt5_c": float(m["close"]),
                "dbn_o": float(d["open"]),
                "dbn_h": float(d["high"]),
                "dbn_l": float(d["low"]),
                "dbn_c": float(d["close"]),
                "delta_close": float(d["close"] - m["close"]),
                "delta_close_pct": float((d["close"] - m["close"]) / m["close"]),
                "mt5_body": float(m["close"] - m["open"]),
                "dbn_body": float(d["close"] - d["open"]),
                "same_dir": (
                    int(np.sign(m["close"] - m["open"]) == np.sign(d["close"] - d["open"]))
                ),
            }
        )

    # The right diagnostic for a timezone offset is **cross-correlation
    # of close-to-close returns across lags**, not |Δclose| (which is
    # dominated by the documented price-level offset). If the two
    # sources are aligned on UTC, returns at lag 0 should correlate
    # positively. A timezone bug would surface as a peak correlation
    # at lag ≠ 0.
    common_ts = pd.DatetimeIndex(common)
    mt5_idx = mt5.set_index("time").reindex(common_ts)
    dbn_idx = dbn.set_index("time").reindex(common_ts)
    mt5_ret = mt5_idx["close"].pct_change().dropna().to_numpy()
    dbn_ret = dbn_idx["close"].pct_change().dropna().to_numpy()
    n_common = min(len(mt5_ret), len(dbn_ret))
    mt5_ret = mt5_ret[:n_common]
    dbn_ret = dbn_ret[:n_common]
    # Compute cross-correlation of returns at lags ∈ [-12, +12] M5 bars
    # (i.e. ±60 min). A non-zero peak suggests a timezone offset.
    lags = list(range(-12, 13))
    corrs: list[tuple[int, float]] = []
    for lag in lags:
        if lag >= 0:
            a = mt5_ret[: n_common - lag]
            b = dbn_ret[lag:n_common]
        else:
            a = mt5_ret[-lag:n_common]
            b = dbn_ret[: n_common + lag]
        if len(a) < 100:
            continue
        # nan-safe Pearson
        a_arr = np.asarray(a, dtype="float64")
        b_arr = np.asarray(b, dtype="float64")
        mask = np.isfinite(a_arr) & np.isfinite(b_arr)
        if mask.sum() < 100:
            continue
        c = float(np.corrcoef(a_arr[mask], b_arr[mask])[0, 1])
        corrs.append((lag, c))
    best_lag, best_corr = max(corrs, key=lambda kv: kv[1])
    lag_zero = next(c for l, c in corrs if l == 0)

    lines: list[str] = []
    lines.append(f"# Timestamp alignment check — MT5 vs Databento — {_ts()}")
    lines.append("")
    lines.append(
        "Tests whether MT5 and Databento M5 candles are aligned on the same "
        "UTC minute, or whether a broker-time vs exchange-time offset is "
        "polluting the comparison. 10 random NDX100 timestamps from the "
        "common window (seed=42)."
    )
    lines.append("")
    lines.append("## Per-sample candle comparison")
    lines.append("")
    lines.append(
        "| Timestamp UTC | MT5 close | DBN close | Δ close | Δ % | "
        "MT5 body | DBN body | same dir |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|:---:|")
    for r in rows:
        lines.append(
            f"| {r['ts_utc']} | {r['mt5_c']:.2f} | {r['dbn_c']:.2f} | "
            f"{r['delta_close']:+.2f} | {r['delta_close_pct']:+.3%} | "
            f"{r['mt5_body']:+.2f} | {r['dbn_body']:+.2f} | "
            f"{'✓' if r['same_dir'] else '✗'} |"
        )
    lines.append("")

    # Aggregate
    deltas = [r["delta_close"] for r in rows]
    pcts = [r["delta_close_pct"] for r in rows]
    same_dir = sum(r["same_dir"] for r in rows) / len(rows)
    lines.append("## Aggregate")
    lines.append("")
    lines.append(f"- Mean Δ close: **{np.mean(deltas):+.2f}** | median: {np.median(deltas):+.2f} | stdev: {np.std(deltas):.2f}")
    lines.append(f"- Mean Δ %    : **{np.mean(pcts):+.3%}** | median: {np.median(pcts):+.3%}")
    lines.append(f"- Direction agreement on same UTC minute: **{same_dir:.0%}**")
    lines.append("")
    lines.append("## Cross-correlation of close-to-close returns across lags")
    lines.append("")
    lines.append(
        f"On {n_common} common UTC minutes (full overlap window, not just "
        "the 10-sample probe). A timezone offset would surface as a "
        "correlation peak at lag ≠ 0; a peak at lag 0 confirms the two "
        "sources are aligned on UTC and the Δ close magnitude is a "
        "level-only offset."
    )
    lines.append("")
    lines.append("| Lag (M5 bars) | Lag (min) | Pearson r |")
    lines.append("|---:|---:|---:|")
    for lag, c in corrs:
        lines.append(f"| {lag} | {lag*5:+d} | {c:+.4f} |")
    lines.append("")
    lines.append(f"- Lag 0 correlation: **{lag_zero:+.4f}**")
    lines.append(f"- Peak lag: **{best_lag}** ({best_lag*5:+d} min) | peak r = {best_corr:+.4f}")
    lines.append("")
    if best_lag == 0 and lag_zero > 0.3:
        lines.append(
            "**Verdict — alignment OK**: peak return-correlation is at "
            "lag 0 with r > 0.3. The two sources are aligned on the UTC "
            "minute axis; the multi-hundred-point Δ close is a "
            "**price-level offset** between CFD and back-adjusted futures, "
            "already documented in `phase1` and `deep_diagnosis`."
        )
    elif best_lag == 0 and lag_zero <= 0.3:
        lines.append(
            f"**Verdict — alignment OK but coupling is weak**: lag 0 is "
            f"the peak but its r ({lag_zero:+.3f}) is low. The two sources "
            "are aligned in time but their micro-structure is genuinely "
            "different. Consistent with the deep_diagnosis report's body "
            "correlation ≈ 0 finding."
        )
    else:
        lines.append(
            f"**Verdict — alignment SUSPICIOUS**: peak return-correlation "
            f"is at lag {best_lag} ({best_lag*5:+d} min), not lag 0. "
            "A timezone offset may be polluting the comparison. Investigate "
            "whether MT5 fixtures are stored in broker time rather than UTC."
        )
    lines.append("")
    path = out_dir / f"timestamp_alignment_check_{_ts()}.md"
    path.write_text("\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------------------
# Q2 — Mismatch detail on 5 MT5 NDX setups.
# ---------------------------------------------------------------------------
def _q2_mismatch_detail(prior_run: Path, out_dir: Path) -> Path:
    sym = "NDX100"
    mt5_setups = [
        json.loads(line)
        for line in (prior_run / f"mt5_{sym}_setups.jsonl").read_text().splitlines()
        if line.strip()
    ]
    dbn_setups = [
        json.loads(line)
        for line in (prior_run / f"dbn_{sym}_setups.jsonl").read_text().splitlines()
        if line.strip()
    ]
    rng = random.Random(42)
    sample = sorted(rng.sample(mt5_setups, k=min(5, len(mt5_setups))), key=lambda s: s["timestamp_utc"])

    mt5_m5 = _load_m5(_MT5_DIR, sym)
    dbn_m5 = _load_m5(_DBN_DIR, sym)
    mt5_h1 = pd.read_parquet(_MT5_DIR / f"{sym}_H1.parquet")
    dbn_h1 = pd.read_parquet(_DBN_DIR / f"{sym}_H1.parquet")
    if mt5_h1["time"].dt.tz is None:
        mt5_h1["time"] = mt5_h1["time"].dt.tz_localize("UTC")
    if dbn_h1["time"].dt.tz is None:
        dbn_h1["time"] = dbn_h1["time"].dt.tz_localize("UTC")

    lines: list[str] = []
    lines.append(f"# Mismatch detail — 5 MT5 NDX setups vs Databento — {_ts()}")
    lines.append("")
    lines.append(
        "For each sampled MT5 NDX setup, what did Databento see at the same "
        "timestamp? Goal: identify whether the 100% mismatch is data-driven "
        "(DBN's chain of bias / sweep / MSS / FVG simply did not fire on "
        "this date because the price path was different) or analysis-driven."
    )
    lines.append("")

    for idx, m in enumerate(sample, 1):
        ts_utc = pd.Timestamp(m["timestamp_utc"])
        ts_paris = ts_utc.astimezone(_PARIS)
        date_iso = ts_paris.date().isoformat()
        lines.append(f"## Case {idx} — {date_iso} {m['killzone']} {m['direction']}")
        lines.append("")
        lines.append(
            f"**MT5 setup**: ts={m['timestamp_utc']} | direction={m['direction']} | "
            f"quality={m['quality']} | swept={m['swept_level_price']:.2f} | "
            f"entry={m['entry_price']:.2f} | SL={m['stop_loss']:.2f} | "
            f"TP1={m['tp1_price']:.2f} | TPr={m['tp_runner_price']:.2f} | "
            f"R={m['realized_r']:+.2f} | outcome={m['outcome']}"
        )
        lines.append("")

        # DBN candle at exact MT5 timestamp.
        mask = dbn_m5["time"] == ts_utc
        if mask.any():
            d = dbn_m5[mask].iloc[0]
            lines.append(
                f"**DBN candle at same UTC minute**: O={d['open']:.2f} H={d['high']:.2f} "
                f"L={d['low']:.2f} C={d['close']:.2f}"
            )
            try:
                m5_mt5 = mt5_m5[mt5_m5["time"] == ts_utc].iloc[0]
                dx_close = float(d["close"]) - float(m5_mt5["close"])
                lines.append(
                    f"  - Δ close vs MT5 same minute: {dx_close:+.2f} pts ({dx_close/float(m5_mt5['close']):+.3%})"
                )
            except (IndexError, KeyError):
                pass
        else:
            lines.append("**DBN candle at same UTC minute**: ABSENT")
        lines.append("")

        # DBN setups same day+killzone (any direction).
        same_day_dbn = [
            s for s in dbn_setups
            if pd.Timestamp(s["timestamp_utc"]).astimezone(_PARIS).date().isoformat() == date_iso
            and s["killzone"] == m["killzone"]
        ]
        if same_day_dbn:
            lines.append(
                f"**DBN setups on {date_iso} {m['killzone']}**: {len(same_day_dbn)} found"
            )
            for s in same_day_dbn:
                lines.append(
                    f"  - {s['timestamp_utc']} {s['direction']} q={s['quality']} "
                    f"entry={s['entry_price']:.2f} swept={s['swept_level_price']:.2f} "
                    f"R={s['realized_r']:+.2f}"
                )
        else:
            lines.append(
                f"**DBN setups on {date_iso} {m['killzone']}**: NONE — "
                "DBN's bias/sweep/MSS chain did not fire any setup in this "
                "killzone on this date."
            )
        lines.append("")

        # H1 bias proxy: closes around MT5 setup time.
        win_start = ts_utc - pd.Timedelta(hours=24)
        mt5_h1_window = mt5_h1[(mt5_h1["time"] >= win_start) & (mt5_h1["time"] <= ts_utc)]
        dbn_h1_window = dbn_h1[(dbn_h1["time"] >= win_start) & (dbn_h1["time"] <= ts_utc)]
        if len(mt5_h1_window) > 1 and len(dbn_h1_window) > 1:
            mt5_drift = float(mt5_h1_window["close"].iloc[-1] - mt5_h1_window["close"].iloc[0])
            dbn_drift = float(dbn_h1_window["close"].iloc[-1] - dbn_h1_window["close"].iloc[0])
            lines.append(
                f"**24h H1 close drift before setup**: MT5 {mt5_drift:+.2f} | "
                f"DBN {dbn_drift:+.2f} | same direction: "
                f"{'✓' if (mt5_drift > 0) == (dbn_drift > 0) else '✗'}"
            )
        lines.append("")

        # Cause classification heuristic.
        cause = []
        if not same_day_dbn:
            cause.append("DBN emitted no setup in this killzone — pre-MSS chain didn't form")
        elif not any(s["direction"] == m["direction"] for s in same_day_dbn):
            cause.append("DBN emitted setup in opposite direction — bias diverges")
        else:
            cause.append("DBN emitted same-direction setup but at different timestamp/levels")
        if mask.any():
            try:
                m5_mt5 = mt5_m5[mt5_m5["time"] == ts_utc].iloc[0]
                d = dbn_m5[mask].iloc[0]
                if abs(float(d["close"]) - float(m5_mt5["close"])) / float(m5_mt5["close"]) > 0.005:
                    cause.append(
                        f"large price-level gap at minute T ({float(d['close']) - float(m5_mt5['close']):+.0f} pts)"
                    )
            except (IndexError, KeyError):
                pass
        lines.append("**Cause probable**: " + " · ".join(cause))
        lines.append("")

    path = out_dir / f"mismatch_detail_{_ts()}.md"
    path.write_text("\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------------------
# Q3 — Explicit cell stats.
# ---------------------------------------------------------------------------
def _q3_cell_stats(prior_run: Path, out_dir: Path) -> Path:
    cells = []
    for src in ("mt5", "dbn"):
        for inst in ("XAUUSD", "NDX100", "SPX500"):
            p = prior_run / f"{src}_{inst}.json"
            if not p.exists():
                continue
            r = BacktestResult.from_json(p)
            n_closed = sum(
                1 for s in r.setups if s.outcome not in ("entry_not_hit", "open_at_horizon")
            )
            ci_lo, ci_hi = r.mean_r_ci_95
            ci_defensible = (n_closed >= 20) and (ci_lo > 0)
            ci_inconclusive = n_closed < 20
            cells.append(
                {
                    "source": src,
                    "instrument": inst,
                    "period_start": r.period_start,
                    "period_end": r.period_end,
                    "n_total": r.n_setups,
                    "n_closed": n_closed,
                    "mean_r": r.mean_r,
                    "ci_lo": ci_lo,
                    "ci_hi": ci_hi,
                    "win_rate": r.win_rate,
                    "setups_per_month": r.setups_per_month,
                    "ci_defensible": ci_defensible,
                    "ci_inconclusive": ci_inconclusive,
                }
            )

    lines: list[str] = []
    lines.append(f"# Cell stats — explicit N + CI per (source, instrument) — {_ts()}")
    lines.append("")
    lines.append(
        f"Re-aggregated from prior run `{prior_run.name}` BacktestResult "
        "JSONs without re-running the backtest. CI is bootstrap 95% "
        "percentile-method, 10k resamples, seed=42 (taken from the JSON "
        "field `mean_r_ci_95`)."
    )
    lines.append("")
    lines.append(
        "**Edge-defensibility rule** (from operator spec): a cell shows "
        "a CI-defensible edge if and only if `n_closed >= 20` AND CI "
        "lower bound > 0. Below n=20 the bootstrap CI is wide and "
        "uninformative — those cells are flagged `inconclusive`."
    )
    lines.append("")
    lines.append(
        "| Source | Inst | Period | n total | n closed | mean R | CI 95% | "
        "win rate | setups/mo | edge? |"
    )
    lines.append("|---|---|---|---:|---:|---:|---|---:|---:|---|")
    for c in cells:
        ci = f"[{c['ci_lo']:+.3f}, {c['ci_hi']:+.3f}]"
        if c["ci_defensible"]:
            edge = "✅ CI-defensible"
        elif c["ci_inconclusive"]:
            edge = "🚧 inconclusive (n<20)"
        else:
            edge = "❌ not edge"
        lines.append(
            f"| {c['source']} | {c['instrument']} | "
            f"{c['period_start']}→{c['period_end']} | {c['n_total']} | "
            f"{c['n_closed']} | {c['mean_r']:+.3f} | {ci} | "
            f"{c['win_rate']:.1%} | {c['setups_per_month']:.2f} | {edge} |"
        )
    lines.append("")

    n_defensible = sum(1 for c in cells if c["ci_defensible"])
    n_inconclusive = sum(1 for c in cells if c["ci_inconclusive"])
    lines.append("## Verdict on edge defensibility")
    lines.append("")
    lines.append(f"- CI-defensible cells: **{n_defensible} / {len(cells)}**")
    lines.append(f"- Inconclusive (n<20) cells: **{n_inconclusive} / {len(cells)}**")
    lines.append("")
    if n_defensible == 0 and n_inconclusive == len(cells):
        lines.append(
            "**No cell reaches n=20.** The +1.225 / +0.539 mean R numbers "
            "for MT5 NDX / XAU under the tick simulator are point estimates "
            "with wide bootstrap CIs (lower bound below zero). They are "
            "**suggestive** of a surviving edge (89-94% retention vs the "
            "Sprint 6.5 legacy mean R) but **not statistically defensible** "
            "on this sample. Larger n is required to convert the suggestion "
            "into a proven edge."
        )
        lines.append("")
        lines.append(
            "Two ways to grow n: (a) extend the MT5 fixture window beyond "
            "the current ~10–17 months — most retail brokers retain at "
            "least 1y of M5 history, possibly more; (b) run the parameter "
            "sweep (`baseline_tjr_variants.py`) on MT5 fixtures and "
            "aggregate across variants if and only if the variants are "
            "interpretable as parameter-perturbation neighbours of the "
            "operator-validated baseline (in which case pooled n grows "
            "but the strategy under test is the variant family, not a "
            "single setting)."
        )
    lines.append("")
    path = out_dir / f"cell_stats_explicit_{_ts()}.md"
    path.write_text("\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prior-run",
        default="calibration/runs/mt5_vs_databento_tick_2026-05-02T11-43-04Z",
        help="Run directory with the BacktestResult JSONs and full-setup JSONLs.",
    )
    parser.add_argument(
        "--output-dir",
        default="calibration/runs",
        help="Where to write the three .md outputs.",
    )
    parser.add_argument(
        "--skip",
        default="",
        help="Comma-separated subset of {q1,q2,q3} to skip.",
    )
    args = parser.parse_args()

    prior_run = Path(args.prior_run)
    if not prior_run.is_dir():
        raise SystemExit(f"prior-run not a directory: {prior_run}")
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    skip = {s.strip().lower() for s in args.skip.split(",") if s.strip()}

    if "q1" not in skip:
        p = _q1_timestamp_alignment(out_dir)
        print(f"Q1 → {p}")
    if "q2" not in skip:
        p = _q2_mismatch_detail(prior_run, out_dir)
        print(f"Q2 → {p}")
    if "q3" not in skip:
        p = _q3_cell_stats(prior_run, out_dir)
        print(f"Q3 → {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
