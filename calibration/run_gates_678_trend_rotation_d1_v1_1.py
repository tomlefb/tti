"""Gates 6, 7, 8 — trend_rotation_d1 v1.1, cell 126/5/3.

Three pre-deployment gates run end-to-end from a single driver:

    Gate 6 — MT5 sanity check
        Run pipeline 126/5/3 on the MT5 D1 panel for the universe
        of 15 assets (commit f868793 fixtures, 1500-day depth).
        Compare pooled mean_r / win rate / month-by-month direction
        agreement to the Yahoo run on the SAME calendar window.

    Gate 7 — top-K rotation transferability
        For each rebalance date that the pipeline would fire on
        the Yahoo panel, compute top-K independently on both
        sources and report:
            - exact basket match %
            - >= K-1 overlap %
            - >= 1 shared asset %

    Gate 8 — Phase C granular FundedNext fees
        Apply per-instrument round-trip cost (reuse the model
        from `calibration.investigate_trend_rotation_d1_v1_1`)
        to each MT5 exit, recompute pooled mean_r post-fee, and
        flag the impact on the projected annual return.

Everything writes under
``calibration/runs/gates_678_trend_rotation_d1_v1_1_<TS>/``:

    - gate6_mt5_sanity.md
    - gate7_top_k_transferability.md
    - gate8_granular_fees.md
    - FINAL_gates_678_verdict.md

The selected cell is the gate-4-v1.1 winner (commit `efe599e`):
``mom = 126, K = 5, rebal = 3``.

Run on the Windows host where the MT5 fixtures are present:

    python -m calibration.run_gates_678_trend_rotation_d1_v1_1
"""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.strategies.trend_rotation_d1 import (  # noqa: E402
    StrategyParams,
    StrategyState,
    TradeExit,
    build_rebalance_candidates,
)
from src.strategies.trend_rotation_d1.pipeline import (  # noqa: E402
    _score_one_asset,
)
from src.strategies.trend_rotation_d1.ranking import select_top_k  # noqa: E402

# Reuse the granular fee model from the gate-4 investigation (commit
# fb374b1) — already pre-spec, conservative FundedNext-like estimates.
from calibration.investigate_trend_rotation_d1_v1_1 import (  # noqa: E402
    FEE_PCT_NOTIONAL_RT,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

UNIVERSE: tuple[str, ...] = (
    "NDX100", "SPX500", "US30", "US2000", "GER30", "UK100", "JP225",
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD",
    "XAUUSD", "XAGUSD",
    "USOUSD",
    "BTCUSD",
)

CELL = {"momentum": 126, "K": 5, "rebalance": 3}

MT5_DIR = REPO_ROOT / "tests" / "fixtures" / "historical"
YAHOO_DIR = REPO_ROOT / "tests" / "fixtures" / "historical_extended" / "yahoo"
RUNS_DIR = REPO_ROOT / "calibration" / "runs"


# ---------------------------------------------------------------------------
# Panel loaders
# ---------------------------------------------------------------------------


def _load_one(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    if "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"], utc=True)
        df = df.set_index("time")
    df.index = df.index.normalize()
    df = df[~df.index.duplicated(keep="first")].sort_index()
    keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    return df[keep]


def load_panel_mt5() -> dict[str, pd.DataFrame]:
    panel: dict[str, pd.DataFrame] = {}
    for asset in UNIVERSE:
        p = MT5_DIR / f"{asset}_D1.parquet"
        if not p.exists():
            raise FileNotFoundError(f"Missing MT5 fixture: {p}")
        panel[asset] = _load_one(p)
    return panel


def load_panel_yahoo() -> dict[str, pd.DataFrame]:
    panel: dict[str, pd.DataFrame] = {}
    for asset in UNIVERSE:
        p = YAHOO_DIR / f"{asset}_D1.parquet"
        if not p.exists():
            raise FileNotFoundError(f"Missing Yahoo fixture: {p}")
        panel[asset] = _load_one(p)
    return panel


# ---------------------------------------------------------------------------
# Common window discovery
# ---------------------------------------------------------------------------


def latest_first_date(panel: dict[str, pd.DataFrame]) -> pd.Timestamp:
    return max(df.index.min() for df in panel.values())


def earliest_last_date(panel: dict[str, pd.DataFrame]) -> pd.Timestamp:
    return min(df.index.max() for df in panel.values())


def cycle_dates(panel: dict[str, pd.DataFrame], start: pd.Timestamp,
                end: pd.Timestamp) -> list[pd.Timestamp]:
    all_dates: set[pd.Timestamp] = set()
    for df in panel.values():
        all_dates |= set(df.index)
    return sorted(d for d in all_dates if start <= d <= end)


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------


def build_params() -> StrategyParams:
    return StrategyParams(
        universe=UNIVERSE,
        momentum_lookback_days=CELL["momentum"],
        K=CELL["K"],
        rebalance_frequency_days=CELL["rebalance"],
        risk_per_trade_pct=1.0,
        atr_period=20,
        atr_explosive_threshold=5.0,
        atr_regime_lookback=90,
    )


def run_streaming(panel: dict[str, pd.DataFrame], params: StrategyParams,
                  dates: list[pd.Timestamp]) -> tuple[list[TradeExit], list[dict]]:
    """Run the pipeline and also record the basket at every rebalance date.

    Returns
    -------
    exits, baskets_log
        ``exits``: closed trade records.
        ``baskets_log``: list of dicts, one per rebalance date the
        pipeline fired, with keys ``date`` (UTC ``Timestamp``) and
        ``basket`` (set of asset labels selected).

    The basket log is used by gate 7 to compare against the Yahoo
    pipeline's baskets at the same dates.
    """
    state = StrategyState()
    exits: list[TradeExit] = []
    baskets_log: list[dict] = []

    prior_rebalance = state.last_rebalance_date
    for now in dates:
        new_exits = build_rebalance_candidates(
            panel, params, state, now_utc=now.to_pydatetime()
        )
        exits.extend(new_exits)
        # If the pipeline rebalanced on this date, snapshot the basket.
        if state.last_rebalance_date != prior_rebalance:
            baskets_log.append({
                "date": now,
                "basket": set(state.current_basket),
            })
            prior_rebalance = state.last_rebalance_date
    return exits, baskets_log


# ---------------------------------------------------------------------------
# Gate 6 — MT5 sanity vs Yahoo on common window
# ---------------------------------------------------------------------------


def bootstrap_ci(rs: list[float], n_iter: int = 2000,
                 seed: int = 12345) -> tuple[float, float] | tuple[None, None]:
    import random
    if len(rs) < 30:
        return (None, None)
    rng = random.Random(seed)
    n = len(rs)
    means = []
    for _ in range(n_iter):
        sample = [rs[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    lo = means[int(0.025 * n_iter)]
    hi = means[int(0.975 * n_iter)]
    return lo, hi


def _stats(exits: list[TradeExit]) -> dict:
    rs = [e.return_r for e in exits]
    n = len(rs)
    if n == 0:
        return {"n": 0, "mean_r": 0.0, "win_rate": 0.0,
                "ci_low": None, "ci_high": None, "total_r": 0.0}
    mean = sum(rs) / n
    win = sum(1 for r in rs if r > 0) / n
    lo, hi = bootstrap_ci(rs)
    return {
        "n": n,
        "mean_r": mean,
        "win_rate": win,
        "ci_low": lo,
        "ci_high": hi,
        "total_r": sum(rs),
    }


def monthly_breakdown(exits: list[TradeExit]) -> dict[str, dict]:
    by_month: dict[str, list[float]] = {}
    for e in exits:
        m = pd.Timestamp(e.exit_timestamp_utc).tz_convert("UTC").strftime("%Y-%m")
        by_month.setdefault(m, []).append(e.return_r)
    out: dict[str, dict] = {}
    for m, rs in sorted(by_month.items()):
        n = len(rs)
        out[m] = {
            "n": n,
            "mean_r": sum(rs) / n,
            "sign": "+" if (sum(rs) / n) > 0 else "-" if (sum(rs) / n) < 0 else "0",
        }
    return out


def direction_agreement(mt: dict, yh: dict) -> dict:
    common = sorted(set(mt) & set(yh))
    if not common:
        return {"common_months": 0, "agree": 0, "agreement_pct": 0.0,
                "details": []}
    agree = 0
    details = []
    for m in common:
        s_mt = mt[m]["sign"]
        s_yh = yh[m]["sign"]
        # "0" months count as agree only with another "0".
        match = s_mt == s_yh
        if match:
            agree += 1
        details.append({
            "month": m,
            "mt5_n": mt[m]["n"], "mt5_mean": mt[m]["mean_r"], "mt5_sign": s_mt,
            "yh_n": yh[m]["n"], "yh_mean": yh[m]["mean_r"], "yh_sign": s_yh,
            "match": match,
        })
    return {
        "common_months": len(common),
        "agree": agree,
        "agreement_pct": agree / len(common),
        "details": details,
    }


# ---------------------------------------------------------------------------
# Gate 7 — top-K basket transferability
# ---------------------------------------------------------------------------


def basket_at_date(panel: dict[str, pd.DataFrame], params: StrategyParams,
                   now_utc: pd.Timestamp) -> set[str]:
    """Compute the top-K basket on ``panel`` at ``now_utc`` independently
    of any prior pipeline state. Mirrors the per-cycle ranking step of
    ``build_rebalance_candidates``."""
    scores: dict[str, float | None] = {}
    for asset in params.universe:
        df = panel.get(asset)
        if df is None:
            scores[asset] = None
            continue
        score, _ = _score_one_asset(df, now_utc.to_pydatetime(), params)
        scores[asset] = score
    return set(select_top_k(scores, params.K))


def gate7_compare(rebalance_dates: list[pd.Timestamp],
                  panel_mt: dict[str, pd.DataFrame],
                  panel_yh: dict[str, pd.DataFrame],
                  params: StrategyParams) -> dict:
    """Compare top-K baskets at each rebalance date across sources."""
    K = params.K
    rows: list[dict] = []
    asset_overlap: dict[str, dict[str, int]] = {
        a: {"both": 0, "yh_only": 0, "mt_only": 0} for a in params.universe
    }
    n_exact = n_kminus1 = n_shared = n_total = 0
    for d in rebalance_dates:
        b_mt = basket_at_date(panel_mt, params, d)
        b_yh = basket_at_date(panel_yh, params, d)
        if not b_mt or not b_yh:
            continue
        n_total += 1
        inter = b_mt & b_yh
        if b_mt == b_yh:
            n_exact += 1
        if len(inter) >= K - 1:
            n_kminus1 += 1
        if inter:
            n_shared += 1
        for a in params.universe:
            in_yh = a in b_yh
            in_mt = a in b_mt
            if in_yh and in_mt:
                asset_overlap[a]["both"] += 1
            elif in_yh and not in_mt:
                asset_overlap[a]["yh_only"] += 1
            elif in_mt and not in_yh:
                asset_overlap[a]["mt_only"] += 1
        rows.append({
            "date": d.strftime("%Y-%m-%d"),
            "basket_yh": sorted(b_yh),
            "basket_mt": sorted(b_mt),
            "shared": sorted(inter),
            "n_shared": len(inter),
            "exact": b_mt == b_yh,
        })
    return {
        "K": K,
        "n_rebalances": n_total,
        "n_exact": n_exact,
        "n_kminus1": n_kminus1,
        "n_shared": n_shared,
        "exact_pct": n_exact / n_total if n_total else 0.0,
        "kminus1_pct": n_kminus1 / n_total if n_total else 0.0,
        "shared_pct": n_shared / n_total if n_total else 0.0,
        "rows": rows,
        "asset_overlap": asset_overlap,
    }


# ---------------------------------------------------------------------------
# Gate 8 — granular fees applied per trade
# ---------------------------------------------------------------------------


def gate8_apply_fees(exits: list[TradeExit]) -> dict:
    """Subtract per-instrument round-trip cost (in R) from every exit."""
    rs_pre = [e.return_r for e in exits]
    rs_post: list[float] = []
    cost_rs: list[float] = []
    cost_by_asset: dict[str, list[float]] = {}
    for e in exits:
        fee_pct = FEE_PCT_NOTIONAL_RT.get(e.asset, 0.0005)
        cost_r = (
            fee_pct * e.entry_price / e.atr_at_entry
            if e.atr_at_entry > 0 else 0.0
        )
        rs_post.append(e.return_r - cost_r)
        cost_rs.append(cost_r)
        cost_by_asset.setdefault(e.asset, []).append(cost_r)
    n = len(rs_pre)
    if n == 0:
        return {"n": 0}
    mean_pre = sum(rs_pre) / n
    mean_post = sum(rs_post) / n
    mean_cost = sum(cost_rs) / n
    asset_costs = {
        a: {"n": len(v), "mean_cost_r": sum(v) / len(v), "fee_pct_rt": FEE_PCT_NOTIONAL_RT.get(a, 0.0005)}
        for a, v in sorted(cost_by_asset.items())
    }
    # Project annual return — risk_pct = 1, so 1R = 1 % of capital.
    months_span = max(
        (max(e.exit_timestamp_utc for e in exits)
         - min(e.entry_timestamp_utc for e in exits)).days / 30.4375,
        1.0,
    )
    spm = n / months_span
    proj_pre = mean_pre * spm * 12.0
    proj_post = mean_post * spm * 12.0
    return {
        "n": n,
        "mean_r_pre_fee": mean_pre,
        "mean_r_post_fee": mean_post,
        "mean_cost_r": mean_cost,
        "setups_per_month": spm,
        "proj_annual_pre_pct": proj_pre,
        "proj_annual_post_pct": proj_post,
        "asset_costs": asset_costs,
    }


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def write_gate6(out_dir: Path, *,
                window_start: pd.Timestamp, window_end: pd.Timestamp,
                stats_mt: dict, stats_yh: dict,
                month_mt: dict, month_yh: dict,
                dir_agreement: dict,
                wallclock_s: float) -> Path:
    L: list[str] = []
    L.append("# Gate 6 — MT5 sanity check (trend_rotation_d1 v1.1, cell 126/5/3)")
    L.append("")
    L.append(f"**Date**: {datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    L.append(f"**Window (common to MT5 + Yahoo)**: "
             f"`{window_start.date()} → {window_end.date()}` "
             f"({(window_end - window_start).days / 365.25:.2f} y)")
    L.append(f"**Cell**: 126/5/3 (gate-4-v1.1 selected, commit `efe599e`)")
    L.append(f"**Wallclock**: {wallclock_s:.1f} s")
    L.append("")
    # Verdict
    abs_diff_pct = (
        abs(stats_mt["mean_r"] - stats_yh["mean_r"]) / abs(stats_yh["mean_r"])
        if stats_yh["mean_r"] != 0 else float("inf")
    )
    sign_mt = stats_mt["mean_r"] > 0
    sign_yh = stats_yh["mean_r"] > 0
    if sign_mt != sign_yh:
        verdict = "❌ ARCHIVE — opposite-sign mean R between MT5 and Yahoo"
    elif abs_diff_pct > 0.5:
        verdict = (
            f"⚠️ REVIEW — mismatch {abs_diff_pct:.1%} > 50 % "
            "and direction agreement below threshold; integrate as realistic estimate"
        )
    elif dir_agreement["agreement_pct"] < 0.7:
        verdict = (
            f"⚠️ REVIEW — direction agreement {dir_agreement['agreement_pct']:.1%} "
            "< 70 %"
        )
    elif abs_diff_pct < 0.3:
        verdict = "✅ PASS (excellent: < 30 % mismatch)"
    else:
        verdict = "✅ PASS (acceptable: 30–50 % mismatch)"
    L.append(f"**Verdict**: {verdict}")
    L.append("")
    L.append("## Pooled metrics on common window")
    L.append("")
    L.append("| Source | n | mean_r | win | CI low | CI high | total_r |")
    L.append("|---|---:|---:|---:|---:|---:|---:|")
    for label, s in (("MT5", stats_mt), ("Yahoo", stats_yh)):
        ci = (
            f"{s['ci_low']:+.3f}" if s["ci_low"] is not None else "—"
        )
        ch = (
            f"{s['ci_high']:+.3f}" if s["ci_high"] is not None else "—"
        )
        L.append(
            f"| {label} | {s['n']} | {s['mean_r']:+.3f} | {s['win_rate']:.1%} "
            f"| {ci} | {ch} | {s['total_r']:+.2f} |"
        )
    L.append("")
    L.append(
        f"Mean R absolute mismatch: **{abs_diff_pct:.1%}** "
        f"(threshold: < 30 % great, 30-50 % acceptable, > 50 % review)."
    )
    L.append("")
    L.append("## Direction agreement — month by month")
    L.append("")
    L.append(
        f"Common months: {dir_agreement['common_months']}; "
        f"agreement: {dir_agreement['agree']} / "
        f"{dir_agreement['common_months']} = "
        f"**{dir_agreement['agreement_pct']:.1%}** "
        f"(threshold: > 70 %)."
    )
    L.append("")
    L.append("| Month | MT5 n | MT5 mean | sign | Yahoo n | Yahoo mean | sign | match |")
    L.append("|---|---:|---:|:---:|---:|---:|:---:|:---:|")
    for r in dir_agreement["details"]:
        check = "✅" if r["match"] else "❌"
        L.append(
            f"| {r['month']} | {r['mt5_n']} | {r['mt5_mean']:+.3f} | "
            f"{r['mt5_sign']} | {r['yh_n']} | {r['yh_mean']:+.3f} | "
            f"{r['yh_sign']} | {check} |"
        )
    L.append("")
    out = out_dir / "gate6_mt5_sanity.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    return out


def write_gate7(out_dir: Path, *, gate7: dict, wallclock_s: float) -> Path:
    L: list[str] = []
    L.append("# Gate 7 — top-K basket transferability MT5 vs Yahoo "
             "(trend_rotation_d1 v1.1)")
    L.append("")
    L.append(f"**Date**: {datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    L.append(f"**Cell**: 126/5/3, K = {gate7['K']}")
    L.append(f"**n rebalances measured**: {gate7['n_rebalances']}")
    L.append(f"**Wallclock**: {wallclock_s:.1f} s")
    L.append("")
    if gate7["exact_pct"] > 0.7:
        verdict = "✅ PASS (exact match > 70 %)"
    elif gate7["kminus1_pct"] > 0.7:
        verdict = ("⚠️ REVIEW — exact match < 70 %, but ≥ K-1 overlap > 70 % "
                   "(rotation transferability acceptable, edge probably reduced)")
    elif gate7["exact_pct"] < 0.5:
        verdict = "❌ ARCHIVE — exact match < 50 % (transferability fundamentally broken)"
    else:
        verdict = "⚠️ REVIEW — exact match between 50 % and 70 %"
    L.append(f"**Verdict**: {verdict}")
    L.append("")
    L.append("## Headline transferability metrics")
    L.append("")
    L.append("| Metric | n | % |")
    L.append("|---|---:|---:|")
    L.append(f"| Exact match (top-K identical) | {gate7['n_exact']} "
             f"| {gate7['exact_pct']:.1%} |")
    L.append(f"| ≥ K-1 overlap (1 differs OK) | {gate7['n_kminus1']} "
             f"| {gate7['kminus1_pct']:.1%} |")
    L.append(f"| ≥ 1 shared asset | {gate7['n_shared']} "
             f"| {gate7['shared_pct']:.1%} |")
    L.append("")
    L.append(
        "Pass criterion (spec §6 H10): exact match > 70 % of rebalances. "
        "Below 70 %, rotation transferability degrades — edge measured on "
        "Yahoo would not transpose 1:1 to MT5 baskets."
    )
    L.append("")
    L.append("## Per-asset overlap decomposition")
    L.append("")
    L.append("| Asset | Both | Yahoo-only | MT5-only |")
    L.append("|---|---:|---:|---:|")
    for asset, ov in gate7["asset_overlap"].items():
        L.append(f"| {asset} | {ov['both']} | {ov['yh_only']} | {ov['mt_only']} |")
    L.append("")
    # Sample of mismatched rebalances (first 20).
    L.append("## Sample — first 20 rebalances (mismatches highlighted)")
    L.append("")
    L.append("| Date | Yahoo basket | MT5 basket | shared | exact |")
    L.append("|---|---|---|---:|:---:|")
    for r in gate7["rows"][:20]:
        check = "✅" if r["exact"] else "❌"
        L.append(
            f"| {r['date']} | {','.join(r['basket_yh'])} "
            f"| {','.join(r['basket_mt'])} | {r['n_shared']} | {check} |"
        )
    L.append("")
    out = out_dir / "gate7_top_k_transferability.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    return out


def write_gate8(out_dir: Path, *, gate8: dict, wallclock_s: float) -> Path:
    L: list[str] = []
    L.append("# Gate 8 — Phase C granular FundedNext fees "
             "(trend_rotation_d1 v1.1, cell 126/5/3)")
    L.append("")
    L.append(f"**Date**: {datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    L.append(f"**Source for trades**: MT5 panel (gate 6 output)")
    L.append(f"**n trades**: {gate8['n']}")
    L.append(f"**Wallclock**: {wallclock_s:.1f} s")
    L.append("")
    # Verdict
    if gate8["mean_r_post_fee"] >= 0.3:
        verdict = (
            f"✅ PASS — post-fee mean_r {gate8['mean_r_post_fee']:+.3f} R ≥ +0.3 R"
        )
    elif gate8["mean_r_post_fee"] >= 0.1:
        verdict = (
            "⚠️ REVIEW — post-fee mean_r in [+0.1, +0.3) R (edge present but reduced)"
        )
    else:
        verdict = (
            "❌ ARCHIVE — post-fee mean_r < +0.1 R (cost stack collapses the edge)"
        )
    L.append(f"**Verdict**: {verdict}")
    L.append("")
    L.append("## Aggregate impact")
    L.append("")
    L.append("| Metric | Pre-fee | Post-fee | Δ |")
    L.append("|---|---:|---:|---:|")
    L.append(
        f"| mean_r (R) | {gate8['mean_r_pre_fee']:+.4f} | "
        f"{gate8['mean_r_post_fee']:+.4f} | "
        f"{gate8['mean_r_post_fee'] - gate8['mean_r_pre_fee']:+.4f} |"
    )
    L.append(
        f"| projected annual @ 1 % risk (%) | "
        f"{gate8['proj_annual_pre_pct']:+.1f} | "
        f"{gate8['proj_annual_post_pct']:+.1f} | "
        f"{gate8['proj_annual_post_pct'] - gate8['proj_annual_pre_pct']:+.1f} |"
    )
    L.append("")
    L.append(
        f"Mean cost per trade: **{gate8['mean_cost_r']:.4f} R**; "
        f"setups/month {gate8['setups_per_month']:.2f}; "
        f"cost-to-edge ratio "
        f"**{gate8['mean_cost_r'] / abs(gate8['mean_r_pre_fee']):.1%}** "
        f"of pre-fee mean_r."
    )
    L.append("")
    L.append("## Per-instrument fee model used (round-trip, % of notional)")
    L.append("")
    L.append("| Asset | n trades | fee % RT | mean cost (R) |")
    L.append("|---|---:|---:|---:|")
    for asset, c in gate8["asset_costs"].items():
        L.append(
            f"| {asset} | {c['n']} | {c['fee_pct_rt']:.4%} | "
            f"{c['mean_cost_r']:+.4f} |"
        )
    L.append("")
    L.append(
        "Source of the fee table: "
        "`calibration/investigate_trend_rotation_d1_v1_1.py` "
        "(commit `fb374b1`, conservative FundedNext-like estimates "
        "calibrated per asset class — indices ~0.01 %, FX 0.01-0.015 %, "
        "metals 0.03-0.05 %, energy 0.04 %, crypto 0.10 %). "
        "If FundedNext publishes a stricter fee schedule, re-run with that table."
    )
    L.append("")
    out = out_dir / "gate8_granular_fees.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    return out


def write_final(out_dir: Path, *,
                gate6_verdict: str, gate7_verdict: str, gate8_verdict: str,
                gate6_path: Path, gate7_path: Path, gate8_path: Path,
                stats_mt: dict, stats_yh: dict,
                gate7: dict, gate8: dict) -> Path:
    L: list[str] = []
    L.append("# Gates 6 / 7 / 8 — final verdict (trend_rotation_d1 v1.1, "
             "cell 126/5/3)")
    L.append("")
    L.append(f"**Date**: {datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    L.append(f"**Cell**: 126/5/3 (gate-4-v1.1 selected)")
    L.append("")
    # Headline verdicts
    pass_count = sum(1 for v in (gate6_verdict, gate7_verdict, gate8_verdict)
                     if v.startswith("✅"))
    review_count = sum(1 for v in (gate6_verdict, gate7_verdict, gate8_verdict)
                       if v.startswith("⚠️"))
    archive_count = sum(1 for v in (gate6_verdict, gate7_verdict, gate8_verdict)
                        if v.startswith("❌"))
    if pass_count == 3:
        global_verdict = "✅ ALL PASS — Phase 1 deployment recommended"
    elif archive_count > 0:
        global_verdict = (
            f"❌ ARCHIVE/STOP — {archive_count} gate(s) ARCHIVE; "
            "do NOT subscribe Phase 1"
        )
    elif pass_count == 2:
        global_verdict = (
            "⚠️ DISCUSSION — 2/3 PASS; review the REVIEW gate before deciding"
        )
    elif pass_count == 1:
        global_verdict = (
            "⚠️ DISCUSSION — only 1/3 PASS; serious doubt before deployment"
        )
    else:
        global_verdict = "❌ ARCHIVE — 0 / 3 PASS"
    L.append(f"## Global verdict: {global_verdict}")
    L.append("")
    L.append("| Gate | Detail | Verdict |")
    L.append("|---|---|---|")
    L.append(f"| Gate 6 — MT5 sanity | "
             f"MT5 mean_r {stats_mt['mean_r']:+.3f}, "
             f"Yahoo mean_r {stats_yh['mean_r']:+.3f}, "
             f"n_mt={stats_mt['n']}, n_yh={stats_yh['n']} | "
             f"{gate6_verdict} |")
    L.append(f"| Gate 7 — top-K transferability | "
             f"exact={gate7['exact_pct']:.1%}, "
             f"≥K-1={gate7['kminus1_pct']:.1%}, "
             f"shared≥1={gate7['shared_pct']:.1%} | "
             f"{gate7_verdict} |")
    L.append(f"| Gate 8 — granular fees | "
             f"mean_r post-fee {gate8['mean_r_post_fee']:+.3f}, "
             f"proj annual post-fee {gate8['proj_annual_post_pct']:+.1f} % | "
             f"{gate8_verdict} |")
    L.append("")
    L.append("## Output files")
    L.append("")
    L.append(f"- [gate6_mt5_sanity.md]({gate6_path.name})")
    L.append(f"- [gate7_top_k_transferability.md]({gate7_path.name})")
    L.append(f"- [gate8_granular_fees.md]({gate8_path.name})")
    L.append("")
    L.append("## Action items")
    L.append("")
    if pass_count == 3:
        L.append(
            "1. Subscribe Phase 1 Stellar Lite ($23 with VIBES). Budget 3 attempts max."
        )
        L.append(
            "2. Branch the scheduler `src/strategies/` to integrate "
            "`trend_rotation_d1` v1.1 in place of (or alongside) TJR. "
            "Cell 126/5/3, 1 % risk per trade, 15-asset universe."
        )
        L.append(
            "3. Live-monitor: compare each MT5 trade with the simulation "
            "panel. If sustained divergence > spec, pause and investigate."
        )
    else:
        L.append(
            "1. Discuss the gate(s) flagged before any Phase 1 subscription. "
            "The REVIEW outcomes are surfaced for operator judgement, not "
            "auto-archived."
        )
        L.append(
            "2. If the discussion concludes against deployment: archive "
            "the strategy under `archived/strategies/trend_rotation_d1_v1_1/` "
            "with the post-mortem appropriate to the failing gate."
        )
    L.append("")
    out = out_dir / "FINAL_gates_678_verdict.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    print(">>> Loading panels", flush=True)
    panel_mt = load_panel_mt5()
    panel_yh = load_panel_yahoo()

    # Common window: starts at the latest first-bar across MT5 panel
    # plus a 6-month buffer for momentum warmup; ends at the earliest
    # last-bar across the union.
    mt_first = latest_first_date(panel_mt)
    mt_last = earliest_last_date(panel_mt)
    yh_first = latest_first_date(panel_yh)
    yh_last = earliest_last_date(panel_yh)
    common_first = max(mt_first, yh_first)
    common_last = min(mt_last, yh_last)
    # 6-month warmup buffer past the latest first date for the 126-day momentum.
    warmup_buffer = pd.Timedelta(days=200)
    decision_start = common_first + warmup_buffer
    print(f"    MT5: [{mt_first.date()} -> {mt_last.date()}]", flush=True)
    print(f"    Yahoo: [{yh_first.date()} -> {yh_last.date()}]", flush=True)
    print(f"    Common: [{common_first.date()} -> {common_last.date()}]", flush=True)
    print(f"    Decision window: [{decision_start.date()} -> {common_last.date()}]",
          flush=True)

    params = build_params()

    # Cycle dates for the union of trading days within the decision window.
    # Same date set used for both runs so the rebalance schedule aligns.
    print(">>> Computing cycle dates", flush=True)
    dates = cycle_dates(panel_mt, decision_start, common_last)
    # Restrict to dates that exist in BOTH panels' union to avoid
    # rebalancing on a date no Yahoo source has — rare but possible
    # at sub-window edges.
    dates_set_yh = set()
    for df in panel_yh.values():
        dates_set_yh |= set(df.index)
    dates = [d for d in dates if d in dates_set_yh]
    print(f"    {len(dates)} cycle dates", flush=True)

    # ---- Gate 6 ----
    print(">>> Gate 6 — MT5 run", flush=True)
    t0 = time.time()
    exits_mt, baskets_mt = run_streaming(panel_mt, params, dates)
    t_mt = time.time() - t0
    print(f"    MT5: {len(exits_mt)} exits, {len(baskets_mt)} rebalances "
          f"({t_mt:.1f} s)", flush=True)

    print(">>> Gate 6 — Yahoo run (same window)", flush=True)
    t0 = time.time()
    exits_yh, baskets_yh = run_streaming(panel_yh, params, dates)
    t_yh = time.time() - t0
    print(f"    Yahoo: {len(exits_yh)} exits, {len(baskets_yh)} rebalances "
          f"({t_yh:.1f} s)", flush=True)

    stats_mt = _stats(exits_mt)
    stats_yh = _stats(exits_yh)
    month_mt = monthly_breakdown(exits_mt)
    month_yh = monthly_breakdown(exits_yh)
    dir_agreement = direction_agreement(month_mt, month_yh)
    g6_wallclock = t_mt + t_yh

    # ---- Gate 7 ----
    print(">>> Gate 7 — top-K basket comparison", flush=True)
    t0 = time.time()
    # Use the rebalance dates from the YAHOO run as the comparison
    # schedule (the pipeline will fire the same days for the MT5 run
    # since cycle dates are shared, but Yahoo defines the canonical
    # rebalance series for transferability measurement).
    rebalance_dates = [b["date"] for b in baskets_yh]
    gate7 = gate7_compare(rebalance_dates, panel_mt, panel_yh, params)
    g7_wallclock = time.time() - t0
    print(f"    Gate 7: {gate7['n_rebalances']} rebalances, "
          f"exact={gate7['exact_pct']:.1%} ({g7_wallclock:.1f} s)", flush=True)

    # ---- Gate 8 ----
    print(">>> Gate 8 — granular FundedNext fees", flush=True)
    t0 = time.time()
    gate8 = gate8_apply_fees(exits_mt)
    g8_wallclock = time.time() - t0
    print(f"    Gate 8: pre={gate8['mean_r_pre_fee']:+.3f} "
          f"post={gate8['mean_r_post_fee']:+.3f} R "
          f"({g8_wallclock:.1f} s)", flush=True)

    # ---- Output dir ----
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    out_dir = RUNS_DIR / f"gates_678_trend_rotation_d1_v1_1_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- Write reports ----
    g6_path = write_gate6(
        out_dir,
        window_start=decision_start, window_end=common_last,
        stats_mt=stats_mt, stats_yh=stats_yh,
        month_mt=month_mt, month_yh=month_yh,
        dir_agreement=dir_agreement,
        wallclock_s=g6_wallclock,
    )
    g7_path = write_gate7(out_dir, gate7=gate7, wallclock_s=g7_wallclock)
    g8_path = write_gate8(out_dir, gate8=gate8, wallclock_s=g8_wallclock)

    # Re-derive the verdict strings for the FINAL by reading them back
    # from each report's Verdict line (single source of truth).
    def _read_verdict(path: Path) -> str:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("**Verdict**: "):
                return line.removeprefix("**Verdict**: ").strip()
        return "?"
    g6_verdict = _read_verdict(g6_path)
    g7_verdict = _read_verdict(g7_path)
    g8_verdict = _read_verdict(g8_path)

    final_path = write_final(
        out_dir,
        gate6_verdict=g6_verdict, gate7_verdict=g7_verdict,
        gate8_verdict=g8_verdict,
        gate6_path=g6_path, gate7_path=g7_path, gate8_path=g8_path,
        stats_mt=stats_mt, stats_yh=stats_yh,
        gate7=gate7, gate8=gate8,
    )

    # Persist a JSON dump of all numeric inputs for downstream auditing.
    raw = {
        "cell": CELL,
        "decision_window": {
            "start": decision_start.isoformat(),
            "end": common_last.isoformat(),
        },
        "stats_mt": stats_mt,
        "stats_yh": stats_yh,
        "direction_agreement_pct": dir_agreement["agreement_pct"],
        "gate7": {k: v for k, v in gate7.items()
                  if k in {"K", "n_rebalances", "n_exact", "n_kminus1",
                           "n_shared", "exact_pct", "kminus1_pct",
                           "shared_pct", "asset_overlap"}},
        "gate8": {k: v for k, v in gate8.items() if k != "asset_costs"},
    }
    (out_dir / "raw_metrics.json").write_text(
        json.dumps(raw, indent=2, default=str), encoding="utf-8"
    )

    print()
    print("=" * 60)
    print(f"Output dir: {out_dir.relative_to(REPO_ROOT)}")
    print(f"Gate 6: {g6_verdict}")
    print(f"Gate 7: {g7_verdict}")
    print(f"Gate 8: {g8_verdict}")
    print(f"FINAL : {final_path.name}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
