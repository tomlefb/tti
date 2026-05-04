"""Gates 6 / 7 / 8 — corrected re-run with H1 + H2 alignment.

Follow-up to ``run_gates_678_trend_rotation_d1_v1_1.py`` (which
ran the gates on raw panels and produced a 22.7 % gate-7
exact-match) and to
``investigate_top_k_divergence.py`` (which proved the 22.7 %
was a measurement artefact: MT5 broker-timezone bar labels +
calendar-day asymmetry on weekend-trading assets).

This driver applies the diagnosed-and-fixed alignment protocol
(soon-to-be-codified in `docs/STRATEGY_RESEARCH_PROTOCOL.md`
§6.5) and re-records all three gate verdicts:

    H1 — normalise every bar to UTC calendar-date index (drops
         the broker-tz hour label so the two sources reference
         the same calendar day).
    H2 — restrict each per-asset frame to dates present in BOTH
         sources (drops Yahoo BTC weekends, MT5 broker-extra
         Sunday-evening index opens, etc.).

Outputs:

    calibration/runs/gates_678_corrected_trend_rotation_d1_v1_1_<TS>/
        gate6_corrected.md
        gate7_corrected.md
        gate8_corrected.md
        FINAL_corrected_verdict.md
        raw_metrics.json

The gate-8 fee model is unchanged — fees are per-trade, not per-
calendar-alignment. Gate-8 is re-recorded only for completeness
(the trade list comes from the corrected MT5 run).
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
from src.strategies.trend_rotation_d1.pipeline import _score_one_asset  # noqa: E402
from src.strategies.trend_rotation_d1.ranking import select_top_k  # noqa: E402

from calibration.investigate_top_k_divergence import (  # noqa: E402
    intersect_panels,
    load_raw,
    normalise_to_calendar_date,
)
from calibration.investigate_trend_rotation_d1_v1_1 import (  # noqa: E402
    FEE_PCT_NOTIONAL_RT,
)
from calibration.run_gates_678_trend_rotation_d1_v1_1 import (  # noqa: E402
    UNIVERSE,
    CELL,
    MT5_DIR,
    YAHOO_DIR,
    RUNS_DIR,
    bootstrap_ci,
    cycle_dates,
    direction_agreement,
    gate8_apply_fees,
    monthly_breakdown,
    _stats,
)


# ---------------------------------------------------------------------------
# Pipeline runner — produces both exits AND per-rebalance baskets
# ---------------------------------------------------------------------------


def run_streaming(panel: dict[str, pd.DataFrame], params: StrategyParams,
                  dates: list[pd.Timestamp]) -> tuple[list[TradeExit], list[dict]]:
    state = StrategyState()
    exits: list[TradeExit] = []
    baskets: list[dict] = []
    prev_rebal = state.last_rebalance_date
    for d in dates:
        new_exits = build_rebalance_candidates(
            panel, params, state, now_utc=d.to_pydatetime()
        )
        exits.extend(new_exits)
        if state.last_rebalance_date != prev_rebal:
            baskets.append({"date": d, "basket": set(state.current_basket)})
            prev_rebal = state.last_rebalance_date
    return exits, baskets


def basket_at_date(panel: dict[str, pd.DataFrame], params: StrategyParams,
                   now: pd.Timestamp) -> set[str]:
    scores: dict[str, float | None] = {}
    for asset in params.universe:
        df = panel.get(asset)
        if df is None:
            scores[asset] = None
            continue
        s, _ = _score_one_asset(df, now.to_pydatetime(), params)
        scores[asset] = s
    return set(select_top_k(scores, params.K))


# ---------------------------------------------------------------------------
# Per-asset alignment diagnostic (per protocol §6.5 (c))
# ---------------------------------------------------------------------------


def alignment_diagnostic(panels_norm: dict[str, dict[str, pd.DataFrame]],
                         start: pd.Timestamp, end: pd.Timestamp) -> list[dict]:
    rows = []
    for asset in UNIVERSE:
        mt = panels_norm["mt5"][asset]
        yh = panels_norm["yh"][asset]
        mt_w = mt.loc[(mt.index >= start) & (mt.index <= end)]
        yh_w = yh.loc[(yh.index >= start) & (yh.index <= end)]
        common = mt_w.index.intersection(yh_w.index)
        only_mt = mt_w.index.difference(yh_w.index)
        only_yh = yh_w.index.difference(mt_w.index)
        loss_mt = len(only_mt) / len(mt_w) if len(mt_w) else 0.0
        loss_yh = len(only_yh) / len(yh_w) if len(yh_w) else 0.0
        rows.append({
            "asset": asset,
            "mt5_n": len(mt_w),
            "yh_n": len(yh_w),
            "common_n": len(common),
            "dropped_mt5": len(only_mt),
            "dropped_yh": len(only_yh),
            "loss_pct_mt5": loss_mt,
            "loss_pct_yh": loss_yh,
            "at_risk": max(loss_mt, loss_yh) > 0.30,
        })
    return rows


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def write_gate6(out_dir: Path, *, window_start: pd.Timestamp,
                window_end: pd.Timestamp,
                stats_mt: dict, stats_yh: dict, dir_agreement: dict,
                month_mt: dict, month_yh: dict,
                stats_mt_orig: dict, stats_yh_orig: dict,
                dir_agreement_orig_pct: float,
                wallclock_s: float) -> tuple[Path, str]:
    abs_diff = (
        abs(stats_mt["mean_r"] - stats_yh["mean_r"]) / abs(stats_yh["mean_r"])
        if stats_yh["mean_r"] != 0 else float("inf")
    )
    sign_match = (stats_mt["mean_r"] > 0) == (stats_yh["mean_r"] > 0)
    if not sign_match:
        verdict = "❌ ARCHIVE — opposite-sign mean R"
    elif abs_diff > 0.5 or dir_agreement["agreement_pct"] < 0.7:
        if abs_diff < 0.5 and dir_agreement["agreement_pct"] >= 0.6:
            verdict = (
                f"⚠️ REVIEW — direction agreement "
                f"{dir_agreement['agreement_pct']:.1%} below 70 % threshold "
                f"despite alignment"
            )
        else:
            verdict = (
                f"⚠️ REVIEW — mismatch {abs_diff:.1%} or direction "
                f"agreement {dir_agreement['agreement_pct']:.1%} miss"
            )
    elif abs_diff < 0.3:
        verdict = "✅ PASS (excellent: < 30 % mismatch)"
    else:
        verdict = "✅ PASS (acceptable: 30-50 % mismatch, agreement > 70 %)"

    L: list[str] = []
    L.append("# Gate 6 corrected — MT5 sanity (H1+H2 alignment applied)")
    L.append("")
    L.append(f"**Date**: {datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    L.append(f"**Cell**: 126/5/3 (gate-4-v1.1 selected, commit `efe599e`)")
    L.append(
        f"**Window**: `{window_start.date()} -> {window_end.date()}` "
        f"({(window_end - window_start).days / 365.25:.2f} y)"
    )
    L.append(f"**Wallclock**: {wallclock_s:.1f} s")
    L.append("")
    L.append(f"**Verdict**: {verdict}")
    L.append("")
    L.append(
        "**Alignment**: per protocol §6.5 (a)+(b) — both panels "
        "normalised to UTC calendar-date index; per-asset frames "
        "intersected to dates present in BOTH sources."
    )
    L.append("")
    L.append("## Original vs corrected — pooled metrics")
    L.append("")
    L.append("| Source | Run | n | mean_r | win | CI low | CI high |")
    L.append("|---|---|---:|---:|---:|---:|---:|")
    for src, orig, corr in (
        ("MT5", stats_mt_orig, stats_mt),
        ("Yahoo", stats_yh_orig, stats_yh),
    ):
        for label, s in (("orig", orig), ("**corrected**", corr)):
            ci_lo = f"{s['ci_low']:+.3f}" if s.get("ci_low") is not None else "—"
            ci_hi = f"{s['ci_high']:+.3f}" if s.get("ci_high") is not None else "—"
            L.append(
                f"| {src} | {label} | {s['n']} | {s['mean_r']:+.3f} "
                f"| {s['win_rate']:.1%} | {ci_lo} | {ci_hi} |"
            )
    L.append("")
    L.append(
        f"Mean R absolute mismatch (corrected): **{abs_diff:.1%}** "
        f"(original 44.4 %, threshold < 30 % great / 30-50 % acceptable / > 50 % review)."
    )
    L.append("")
    L.append("## Direction agreement — month by month")
    L.append("")
    L.append(
        f"Original: {dir_agreement_orig_pct:.1%} (n=65 common months). "
        f"Corrected: **{dir_agreement['agreement_pct']:.1%}** "
        f"({dir_agreement['agree']} / {dir_agreement['common_months']} "
        f"common months). Threshold: > 70 %."
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
    out = out_dir / "gate6_corrected.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    return out, verdict


def write_gate7(out_dir: Path, *, gate7: dict, gate7_orig: dict,
                wallclock_s: float) -> tuple[Path, str]:
    if gate7["exact_pct"] > 0.70:
        verdict = "✅ PASS — exact match > 70 % (corrected)"
    elif gate7["exact_pct"] > 0.50:
        verdict = "⚠️ REVIEW — exact match between 50 % and 70 % (corrected)"
    else:
        verdict = "❌ ARCHIVE — exact match still < 50 % (corrected)"

    L: list[str] = []
    L.append("# Gate 7 corrected — top-K transferability (H1+H2 alignment applied)")
    L.append("")
    L.append(f"**Date**: {datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    L.append(f"**Cell**: 126/5/3, K = {gate7['K']}")
    L.append(f"**n rebalances measured**: {gate7['n_rebalances']}")
    L.append(f"**Wallclock**: {wallclock_s:.1f} s")
    L.append("")
    L.append(f"**Verdict**: {verdict}")
    L.append("")
    L.append("## Original vs corrected")
    L.append("")
    L.append("| Metric | Original | Corrected | Δ |")
    L.append("|---|---:|---:|---:|")
    L.append(
        f"| n rebalances | {gate7_orig['n_rebalances']} "
        f"| {gate7['n_rebalances']} "
        f"| {gate7['n_rebalances'] - gate7_orig['n_rebalances']:+d} |"
    )
    L.append(
        f"| Exact match (top-K identical) | {gate7_orig['exact_pct']:.1%} "
        f"({gate7_orig['n_exact']}) | **{gate7['exact_pct']:.1%}** "
        f"({gate7['n_exact']}) | "
        f"{gate7['exact_pct'] - gate7_orig['exact_pct']:+.1%} |"
    )
    L.append(
        f"| ≥ K-1 overlap | {gate7_orig['kminus1_pct']:.1%} "
        f"| {gate7['kminus1_pct']:.1%} | "
        f"{gate7['kminus1_pct'] - gate7_orig['kminus1_pct']:+.1%} |"
    )
    L.append(
        f"| ≥ 1 shared | {gate7_orig['shared_pct']:.1%} "
        f"| {gate7['shared_pct']:.1%} | "
        f"{gate7['shared_pct'] - gate7_orig['shared_pct']:+.1%} |"
    )
    L.append("")
    L.append(
        "Pass criterion (spec §6 H10): exact match > 70 %. The corrected "
        "run uses the protocol §6.5 (a)+(b) alignment so the two sources "
        "score the same calendar dates and label them with the same hour, "
        "removing the H1+H2 measurement artefacts diagnosed in "
        "`investigation_top_k_divergence_2026-05-04T22-27-08Z.md`."
    )
    L.append("")
    L.append("## Per-asset overlap (corrected)")
    L.append("")
    L.append("| Asset | Both | Yahoo-only | MT5-only |")
    L.append("|---|---:|---:|---:|")
    for asset, ov in gate7["asset_overlap"].items():
        L.append(f"| {asset} | {ov['both']} | {ov['yh_only']} | {ov['mt_only']} |")
    L.append("")
    L.append("## Sample — first 10 rebalances")
    L.append("")
    L.append("| Date | Yahoo basket | MT5 basket | shared | exact |")
    L.append("|---|---|---|---:|:---:|")
    for r in gate7["rows"][:10]:
        check = "✅" if r["exact"] else "❌"
        L.append(
            f"| {r['date']} | {','.join(r['basket_yh'])} "
            f"| {','.join(r['basket_mt'])} | {r['n_shared']} | {check} |"
        )
    L.append("")
    out = out_dir / "gate7_corrected.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    return out, verdict


def write_gate8(out_dir: Path, *, gate8: dict, gate8_orig: dict,
                wallclock_s: float) -> tuple[Path, str]:
    if gate8["mean_r_post_fee"] >= 0.3:
        verdict = (
            f"✅ PASS — post-fee mean_r {gate8['mean_r_post_fee']:+.3f} R ≥ +0.3 R"
        )
    elif gate8["mean_r_post_fee"] >= 0.1:
        verdict = "⚠️ REVIEW — post-fee mean_r in [+0.1, +0.3)"
    else:
        verdict = "❌ ARCHIVE — post-fee mean_r < +0.1 R"

    L: list[str] = []
    L.append("# Gate 8 corrected — granular FundedNext fees (H1+H2 alignment applied)")
    L.append("")
    L.append(f"**Date**: {datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    L.append(f"**Source for trades**: MT5 panel after H1+H2 alignment")
    L.append(f"**n trades**: {gate8['n']}")
    L.append(f"**Wallclock**: {wallclock_s:.1f} s")
    L.append("")
    L.append(f"**Verdict**: {verdict}")
    L.append("")
    L.append("## Aggregate")
    L.append("")
    L.append("| Metric | Original (raw panel) | Corrected (aligned panel) |")
    L.append("|---|---:|---:|")
    L.append(
        f"| n trades | {gate8_orig['n']} | {gate8['n']} |"
    )
    L.append(
        f"| mean_r pre-fee | {gate8_orig['mean_r_pre_fee']:+.4f} "
        f"| {gate8['mean_r_pre_fee']:+.4f} |"
    )
    L.append(
        f"| mean_r post-fee | {gate8_orig['mean_r_post_fee']:+.4f} "
        f"| {gate8['mean_r_post_fee']:+.4f} |"
    )
    L.append(
        f"| mean cost / trade | {gate8_orig['mean_cost_r']:.4f} R "
        f"| {gate8['mean_cost_r']:.4f} R |"
    )
    L.append(
        f"| proj annual @ 1 % risk | "
        f"{gate8_orig['proj_annual_post_pct']:+.1f} % "
        f"| {gate8['proj_annual_post_pct']:+.1f} % |"
    )
    L.append("")
    L.append("## Per-instrument cost (aligned run)")
    L.append("")
    L.append("| Asset | n trades | fee % RT | mean cost (R) |")
    L.append("|---|---:|---:|---:|")
    for asset, c in gate8["asset_costs"].items():
        L.append(
            f"| {asset} | {c['n']} | {c['fee_pct_rt']:.4%} "
            f"| {c['mean_cost_r']:+.4f} |"
        )
    L.append("")
    out = out_dir / "gate8_corrected.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    return out, verdict


def write_final(out_dir: Path, *,
                v6: str, v7: str, v8: str,
                stats_mt: dict, stats_yh: dict,
                gate7: dict, gate8: dict,
                dir_agreement_pct: float,
                alignment_rows: list[dict]) -> Path:
    pass_count = sum(1 for v in (v6, v7, v8) if v.startswith("✅"))
    review_count = sum(1 for v in (v6, v7, v8) if v.startswith("⚠️"))
    archive_count = sum(1 for v in (v6, v7, v8) if v.startswith("❌"))
    if archive_count > 0:
        global_verdict = (
            f"❌ ARCHIVE — {archive_count} gate(s) ARCHIVE; do NOT subscribe"
        )
    elif pass_count == 3:
        global_verdict = "✅ ALL PASS — Phase 1 deployment recommended"
    elif pass_count == 2:
        global_verdict = "⚠️ DISCUSSION — 2/3 PASS; review the REVIEW gate"
    else:
        global_verdict = (
            f"⚠️ DISCUSSION — only {pass_count}/3 PASS; serious doubt"
        )

    L: list[str] = []
    L.append("# Gates 6 / 7 / 8 corrected — FINAL verdict "
             "(trend_rotation_d1 v1.1, cell 126/5/3)")
    L.append("")
    L.append(f"**Date**: {datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    L.append(f"**Cell**: 126/5/3 (gate-4-v1.1 selected, commit `efe599e`)")
    L.append(
        f"**Alignment**: protocol §6.5 (a)+(b) — UTC calendar-date "
        f"normalisation + per-asset intersection across MT5 + Yahoo. "
        f"Diagnosed in `investigation_top_k_divergence_2026-05-04T22-27-08Z.md` "
        f"(commit pending), root cause of the original gate-7 22.7 % artefact."
    )
    L.append("")
    L.append(f"## Global verdict: {global_verdict}")
    L.append("")
    L.append("| Gate | Detail | Verdict |")
    L.append("|---|---|---|")
    L.append(
        f"| Gate 6 — MT5 sanity | "
        f"MT5 mean_r {stats_mt['mean_r']:+.3f}, "
        f"Yahoo mean_r {stats_yh['mean_r']:+.3f}, "
        f"direction agreement {dir_agreement_pct:.1%} | {v6} |"
    )
    L.append(
        f"| Gate 7 — top-K transferability | "
        f"exact={gate7['exact_pct']:.1%}, "
        f"≥K-1={gate7['kminus1_pct']:.1%} | {v7} |"
    )
    L.append(
        f"| Gate 8 — granular fees | "
        f"mean_r post-fee {gate8['mean_r_post_fee']:+.3f}, "
        f"proj annual {gate8['proj_annual_post_pct']:+.1f} % | {v8} |"
    )
    L.append("")
    L.append("## Output files")
    L.append("")
    L.append("- [gate6_corrected.md](gate6_corrected.md)")
    L.append("- [gate7_corrected.md](gate7_corrected.md)")
    L.append("- [gate8_corrected.md](gate8_corrected.md)")
    L.append("")
    L.append("## Alignment loss diagnostic (protocol §6.5 (c))")
    L.append("")
    L.append("Per-asset bar count before / after the H1+H2 alignment, on "
             "the gate window. Assets losing > 30 % of their bars are "
             "flagged — they remain available for trading but their "
             "aligned signal is computed on a meaningfully reduced sample.")
    L.append("")
    L.append("| Asset | MT5 raw | Yahoo raw | common | dropped MT5 | dropped Yahoo | at risk |")
    L.append("|---|---:|---:|---:|---:|---:|:---:|")
    for r in alignment_rows:
        flag = "⚠️" if r["at_risk"] else "✅"
        L.append(
            f"| {r['asset']} | {r['mt5_n']} | {r['yh_n']} | {r['common_n']} "
            f"| {r['dropped_mt5']} ({r['loss_pct_mt5']:.1%}) "
            f"| {r['dropped_yh']} ({r['loss_pct_yh']:.1%}) | {flag} |"
        )
    L.append("")
    L.append("## Action items")
    L.append("")
    if pass_count == 3:
        L.append(
            "1. Subscribe Phase 1 Stellar Lite ($23 with VIBES). Budget 3 attempts max."
        )
        L.append(
            "2. Branch the scheduler `src/strategies/` to integrate "
            "`trend_rotation_d1` v1.1 (cell 126/5/3, 1 % risk per trade, "
            "15-asset universe)."
        )
        L.append(
            "3. Live-monitor: compare each MT5 trade with the simulation. "
            "If sustained divergence emerges (mean_r drift > 0.3 R over 30 trades), "
            "pause and investigate."
        )
    elif pass_count == 2:
        L.append(
            "1. Discuss the REVIEW gate before any Phase 1 subscription. "
            "Particular focus on whether the residual is metric noise or a "
            "real edge limitation."
        )
        L.append(
            "2. If discussion concludes for deployment: proceed as the "
            "3/3-PASS branch but with reduced position sizing (0.5 %) on the "
            "first attempt to absorb the wider expected dispersion."
        )
        L.append(
            "3. If discussion concludes against: archive under "
            "`archived/strategies/trend_rotation_d1_v1_1/` with the "
            "post-mortem covering the failing gate."
        )
    else:
        L.append(
            "1. Operator review of gate-by-gate failures. Likely outcome: "
            "archive."
        )
    L.append("")
    out = out_dir / "FINAL_corrected_verdict.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    print(">>> Loading raw panels", flush=True)
    panels_raw = {
        "mt5": {a: load_raw(a, MT5_DIR) for a in UNIVERSE},
        "yh": {a: load_raw(a, YAHOO_DIR) for a in UNIVERSE},
    }

    print(">>> Normalising to calendar-date (H1)", flush=True)
    panels_norm = {
        "mt5": {a: normalise_to_calendar_date(panels_raw["mt5"][a]) for a in UNIVERSE},
        "yh": {a: normalise_to_calendar_date(panels_raw["yh"][a]) for a in UNIVERSE},
    }

    print(">>> Intersecting on common calendar dates (H2)", flush=True)
    panel_mt, panel_yh = intersect_panels(panels_norm["mt5"], panels_norm["yh"])

    # Common decision window — 200 d momentum warmup past the latest first
    # date across the aligned panels.
    mt_first = max(df.index.min() for df in panel_mt.values())
    yh_first = max(df.index.min() for df in panel_yh.values())
    common_first = max(mt_first, yh_first)
    common_last = min(
        min(df.index.max() for df in panel_mt.values()),
        min(df.index.max() for df in panel_yh.values()),
    )
    decision_start = common_first + pd.Timedelta(days=200)
    print(f"    aligned window: [{decision_start.date()} -> {common_last.date()}]",
          flush=True)

    # Cycle dates: union of trading days within the decision window AFTER
    # alignment. Both panels share the same date set (intersection), so
    # using either yields the same.
    dates = cycle_dates(panel_mt, decision_start, common_last)
    print(f"    {len(dates)} cycle dates", flush=True)

    params = StrategyParams(
        universe=UNIVERSE,
        momentum_lookback_days=CELL["momentum"],
        K=CELL["K"],
        rebalance_frequency_days=CELL["rebalance"],
    )

    # ---- Gate 6 ----
    print(">>> Gate 6 corrected — MT5 run", flush=True)
    t0 = time.time()
    exits_mt, baskets_mt = run_streaming(panel_mt, params, dates)
    t_mt = time.time() - t0
    print(f"    MT5: {len(exits_mt)} exits, {len(baskets_mt)} rebalances "
          f"({t_mt:.1f} s)", flush=True)

    print(">>> Gate 6 corrected — Yahoo run", flush=True)
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

    # ---- Gate 7 ----
    print(">>> Gate 7 corrected — top-K basket compare", flush=True)
    t0 = time.time()
    rebalance_dates = [b["date"] for b in baskets_yh]
    n_total = n_exact = n_kminus1 = n_shared = 0
    rows7 = []
    asset_overlap = {a: {"both": 0, "yh_only": 0, "mt_only": 0} for a in UNIVERSE}
    for d in rebalance_dates:
        b_mt = basket_at_date(panel_mt, params, d)
        b_yh = basket_at_date(panel_yh, params, d)
        if not b_mt or not b_yh:
            continue
        n_total += 1
        inter = b_mt & b_yh
        if b_mt == b_yh:
            n_exact += 1
        if len(inter) >= params.K - 1:
            n_kminus1 += 1
        if inter:
            n_shared += 1
        for a in UNIVERSE:
            in_yh = a in b_yh
            in_mt = a in b_mt
            if in_yh and in_mt:
                asset_overlap[a]["both"] += 1
            elif in_yh:
                asset_overlap[a]["yh_only"] += 1
            elif in_mt:
                asset_overlap[a]["mt_only"] += 1
        rows7.append({
            "date": d.strftime("%Y-%m-%d"),
            "basket_yh": sorted(b_yh),
            "basket_mt": sorted(b_mt),
            "n_shared": len(inter),
            "exact": b_mt == b_yh,
        })
    gate7 = {
        "K": params.K,
        "n_rebalances": n_total,
        "n_exact": n_exact,
        "n_kminus1": n_kminus1,
        "n_shared": n_shared,
        "exact_pct": n_exact / n_total if n_total else 0.0,
        "kminus1_pct": n_kminus1 / n_total if n_total else 0.0,
        "shared_pct": n_shared / n_total if n_total else 0.0,
        "rows": rows7,
        "asset_overlap": asset_overlap,
    }
    t_g7 = time.time() - t0
    print(f"    Gate 7 corrected: {gate7['n_rebalances']} rebalances, "
          f"exact={gate7['exact_pct']:.1%} ({t_g7:.1f} s)", flush=True)

    # ---- Gate 8 ----
    print(">>> Gate 8 corrected — granular fees", flush=True)
    t0 = time.time()
    gate8 = gate8_apply_fees(exits_mt)
    t_g8 = time.time() - t0
    print(f"    Gate 8 corrected: pre={gate8['mean_r_pre_fee']:+.3f} "
          f"post={gate8['mean_r_post_fee']:+.3f} R ({t_g8:.1f} s)", flush=True)

    # ---- Per-asset alignment diagnostic (§6.5 (c)) ----
    align_rows = alignment_diagnostic(panels_norm, decision_start, common_last)

    # ---- Output dir ----
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    out_dir = (
        RUNS_DIR
        / f"gates_678_corrected_trend_rotation_d1_v1_1_{ts}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    # Hard-code original numbers for the comparison columns. Sourced from
    # the raw-panel run committed to
    # `gates_678_trend_rotation_d1_v1_1_2026-05-04T22-19-21Z`.
    stats_mt_orig = {
        "n": 337, "mean_r": 1.166, "win_rate": 0.501,
        "ci_low": 0.529, "ci_high": 1.886, "total_r": 393.09,
    }
    stats_yh_orig = {
        "n": 231, "mean_r": 2.098, "win_rate": 0.524,
        "ci_low": 0.743, "ci_high": 3.845, "total_r": 484.65,
    }
    gate7_orig = {
        "n_rebalances": 607, "n_exact": 138, "n_kminus1": 484,
        "n_shared": 607, "exact_pct": 0.227, "kminus1_pct": 0.797,
        "shared_pct": 1.000,
    }
    gate8_orig = {
        "n": 337, "mean_r_pre_fee": 1.1664, "mean_r_post_fee": 1.1519,
        "mean_cost_r": 0.0145, "proj_annual_post_pct": 67.1,
    }

    g6_path, v6 = write_gate6(
        out_dir,
        window_start=decision_start, window_end=common_last,
        stats_mt=stats_mt, stats_yh=stats_yh, dir_agreement=dir_agreement,
        month_mt=month_mt, month_yh=month_yh,
        stats_mt_orig=stats_mt_orig, stats_yh_orig=stats_yh_orig,
        dir_agreement_orig_pct=0.631,
        wallclock_s=t_mt + t_yh,
    )
    g7_path, v7 = write_gate7(
        out_dir, gate7=gate7, gate7_orig=gate7_orig, wallclock_s=t_g7
    )
    g8_path, v8 = write_gate8(
        out_dir, gate8=gate8, gate8_orig=gate8_orig, wallclock_s=t_g8
    )
    final_path = write_final(
        out_dir,
        v6=v6, v7=v7, v8=v8,
        stats_mt=stats_mt, stats_yh=stats_yh,
        gate7=gate7, gate8=gate8,
        dir_agreement_pct=dir_agreement["agreement_pct"],
        alignment_rows=align_rows,
    )

    # ---- Raw metrics ----
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
        "alignment_rows": align_rows,
    }
    (out_dir / "raw_metrics.json").write_text(
        json.dumps(raw, indent=2, default=str), encoding="utf-8"
    )

    print()
    print("=" * 60)
    print(f"Output dir: {out_dir.relative_to(REPO_ROOT)}")
    print(f"Gate 6 corrected: {v6}".encode("ascii", errors="replace").decode())
    print(f"Gate 7 corrected: {v7}".encode("ascii", errors="replace").decode())
    print(f"Gate 8 corrected: {v8}".encode("ascii", errors="replace").decode())
    print(f"FINAL : {final_path.name}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
