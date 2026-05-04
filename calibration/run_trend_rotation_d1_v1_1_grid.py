"""Gate 4 driver — trend_rotation_d1 v1.1.

Spec: ``docs/strategies/trend_rotation_d1_v1_1.md`` (commit
``bb12a95``). Implements Phase C of the v1.1 plan:

1. **§3.6 pre-measure** on the 18-cell grid §3.2 (train).
   Outputs ``premeasure_trend_rotation_d1_v1_1_<TS>.md`` with
   trades/mo portfolio per cell + viable yes/no.

2. **§3.4 selection** on §3.6-viable cells:
   - §3.5 class-B floors: ``n_closed >= 100``,
     ``mean_r_ci_95.lower >= -0.1``, ``temporal_concentration <
     0.6``.
   - §3.6 cadence floor: ``trades/mo train >= 4``.
   - Tie-break: max ``vs_buy_and_hold.strategy_minus_bh_pct``;
     secondary tie-break: max ``setups_per_month``.

3. **Holdout** evaluation on the selected cell.

4. **§4 v1.1 hypothesis evaluation** — 10 hypotheses with v1.1
   bands (frozen at spec commit, not adjustable post-hoc).

5. **§3.6 holdout double-check** — selected cell must also
   produce >= 4 trades/mo on holdout. Failure = ARCHIVE final
   regardless of §4 verdict.

6. **Verdict**: PROMOTE (>= 6/10 PASS) / REVIEW (3-5) / ARCHIVE
   (<3) per spec §4. §3.6 holdout failure overrides.

Outputs
-------
- ``calibration/runs/premeasure_trend_rotation_d1_v1_1_<TS>.md``
- ``calibration/runs/gate4_trend_rotation_d1_v1_1_<TS>/``
  - ``report.md`` (full gate 4 report with verdict)
  - ``train_grid.json`` (BacktestResult per cell, 18 cells)
  - ``holdout.json`` (BacktestResult selected cell, if any)
  - ``viability_check.json`` (§3.5 + §3.6 train + §3.6 holdout)

Run
---
    python -m calibration.run_trend_rotation_d1_v1_1_grid
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from itertools import product
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from calibration.audit_trend_rotation_d1 import (  # noqa: E402
    HOLDOUT_END,
    HOLDOUT_START,
    TRAIN_END,
    TRAIN_START,
    UNIVERSE,
    cycle_dates,
    load_panel,
    run_streaming,
)
from calibration.run_trend_rotation_d1_grid import (  # noqa: E402
    COST_R_PER_TRADE,
    _ew_basket_close,
    _post_cost_mean_r,
    _post_cost_projected_annual,
    _result_to_dict,
    trade_exits_to_setups,
)
from src.backtest.result import BacktestResult  # noqa: E402
from src.strategies.trend_rotation_d1 import (  # noqa: E402
    StrategyParams,
    TradeExit,
)

RUNS_DIR = REPO_ROOT / "calibration" / "runs"

# Spec §3.2 v1.1 grid (18 cells).
GRID_V1_1 = list(
    product(
        [63, 126],          # momentum_lookback
        [3, 4, 5],          # K
        [3, 5, 7],          # rebalance_frequency
    )
)

# Selection floors — §3.5 class-B + §3.6 (NEW v1.1)
FLOORS = {
    "min_n_closed": 100,                  # §3.5 class B
    "min_ci_low": -0.1,                   # §3.5 class B
    "max_temporal_concentration": 0.6,    # §3.5 class B
    "min_trades_per_mo": 4.0,             # §3.6 (NEW)
}
H8_MAX = 0.6  # class B per §3.5

# Hypothesis bands — spec v1.1 §4. Frozen at commit bb12a95.
HYPOTHESES_V1_1: dict[str, dict] = {
    "H1": {"name": "Closed trades / month / portfolio in [4, 8]",
           "low": 4.0, "high": 8.0},
    "H2": {"name": "Win rate (closed) in [48 %, 60 %]",
           "low": 0.48, "high": 0.60},
    "H3": {"name": "Mean R (pre-cost) per closed in [+0.1, +0.4]",
           "low": 0.1, "high": 0.4},
    "H4": {"name": "Mean R (post-cost) per closed in [+0.0, +0.3]",
           "low": 0.0, "high": 0.3},
    "H5": {"name": "Projected annual return % in [5, 25]",
           "low": 5.0, "high": 25.0},
    "H6": {"name": "mean_r_ci_95.lower > 0"},
    "H7": {"name": "outlier_robustness.trim_5_5.mean_r > 0"},
    "H8": {"name": f"temporal_concentration < {H8_MAX}", "max": H8_MAX},
    "H9": {"name": "vs_buy_and_hold.strategy_minus_bh_pct > 0"},
    "H10": {"name": "Top-K agreement Duk vs MT5 (gate 7) > 70 %",
            "min": 0.70},
}

PROMOTE_THRESHOLD = 6
ARCHIVE_THRESHOLD = 3


# ---------------------------------------------------------------------------
# Cell run
# ---------------------------------------------------------------------------


def run_cell(
    panel: dict[str, pd.DataFrame],
    *,
    momentum_lookback_days: int,
    K: int,
    rebalance_frequency_days: int,
    period_start: pd.Timestamp,
    period_end: pd.Timestamp,
) -> tuple[BacktestResult, list[TradeExit]]:
    params = StrategyParams(
        universe=UNIVERSE,
        momentum_lookback_days=momentum_lookback_days,
        K=K,
        rebalance_frequency_days=rebalance_frequency_days,
        risk_per_trade_pct=1.0,
        atr_period=20,
        atr_explosive_threshold=5.0,
        atr_regime_lookback=90,
    )
    dates = cycle_dates(panel, period_start, period_end)
    exits, _ = run_streaming(panel, params, dates)
    setups = trade_exits_to_setups(exits)
    bh_start = _ew_basket_close(panel, period_start)
    bh_end = _ew_basket_close(panel, period_end)
    result = BacktestResult.from_setups(
        strategy_name="trend_rotation_d1_v1_1",
        instrument="basket",
        period_start=period_start.date(),
        period_end=period_end.date(),
        setups=setups,
        params_used={
            "universe": list(UNIVERSE),
            "momentum_lookback_days": momentum_lookback_days,
            "K": K,
            "rebalance_frequency_days": rebalance_frequency_days,
            "risk_per_trade_pct": params.risk_per_trade_pct,
            "atr_period": params.atr_period,
            "atr_explosive_threshold": params.atr_explosive_threshold,
            "atr_regime_lookback": params.atr_regime_lookback,
        },
        bh_close_start=bh_start,
        bh_close_end=bh_end,
    )
    return result, exits


def n_closed_of(result: BacktestResult) -> int:
    return sum(
        1 for s in result.setups
        if s.outcome not in ("entry_not_hit", "open_at_horizon")
    )


# ---------------------------------------------------------------------------
# Pre-measure §3.6
# ---------------------------------------------------------------------------


def write_premeasure_report(
    out_path: Path,
    train_grid: dict[tuple[int, int, int], tuple[BacktestResult, list[TradeExit]]],
    *,
    wallclock_s: float,
) -> Path:
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: list[str] = []
    lines.append(f"# §3.6 pre-measure — trend_rotation_d1 v1.1 ({ts})")
    lines.append("")
    lines.append(
        "Spec: `docs/strategies/trend_rotation_d1_v1_1.md` "
        "(commit `bb12a95`). Pre-spec §1.0 cadence step adapted "
        "to §3.6 (operator viability floor 4 trades/mo)."
    )
    lines.append("")
    lines.append(
        f"Train window: {TRAIN_START.date()} → {TRAIN_END.date()} "
        f"(≈ 60 months). §3.6 floor: trades/mo portfolio ≥ 4 on train."
    )
    lines.append("")
    lines.append(f"Wallclock: {wallclock_s:.1f} s.")
    lines.append("")

    lines.append("## 18-cell grid §3.2 v1.1")
    lines.append("")
    lines.append(
        "| momentum | K | rebalance | n_closed train | trades/mo train | §3.6 viable |"
    )
    lines.append("|---:|---:|---:|---:|---:|:---:|")
    n_viable = 0
    for cell in GRID_V1_1:
        result, _ = train_grid[cell]
        n_closed = n_closed_of(result)
        spm = result.setups_per_month
        viable = spm >= FLOORS["min_trades_per_mo"]
        n_viable += int(viable)
        mom, k, rebal = cell
        mark = "✅" if viable else "❌"
        lines.append(
            f"| {mom} | {k} | {rebal} | {n_closed} | {spm:.2f} | {mark} |"
        )
    lines.append("")
    lines.append(f"**§3.6-viable cells**: **{n_viable} / 18**.")
    lines.append("")

    if n_viable == 0:
        lines.append("## Verdict pre-measure")
        lines.append("")
        lines.append(
            "**ARCHIVE by §3.6 construction**. No cell of the v1.1 "
            "expanded grid (rebalance ∈ {3, 5, 7} d × K ∈ {3, 4, 5}) "
            "produces ≥ 4 trades/mo portfolio on train. The strategy "
            "class **HTF cross-sectional momentum multi-asset** is "
            "structurally non-viable for the operator's deployment "
            "context regardless of edge magnitude. Gate 4 grid + "
            "holdout NOT executed — there is no §3.6-admissible cell "
            "to evaluate."
        )
    else:
        lines.append("## Sub-grid retained for gate 4 §3.4 selection")
        lines.append("")
        lines.append("| momentum | K | rebalance |")
        lines.append("|---:|---:|---:|")
        for cell in GRID_V1_1:
            result, _ = train_grid[cell]
            if result.setups_per_month >= FLOORS["min_trades_per_mo"]:
                mom, k, rebal = cell
                lines.append(f"| {mom} | {k} | {rebal} |")
        lines.append("")
        lines.append(
            f"Gate 4 §3.4 selection runs on the {n_viable}-cell §3.6-"
            "viable sub-grid only."
        )

    lines.append("")
    out_path.write_text("\n".join(lines) + "\n")
    return out_path


# ---------------------------------------------------------------------------
# Selection §3.4
# ---------------------------------------------------------------------------


def select_best_cell(
    grid: dict[tuple[int, int, int], tuple[BacktestResult, list[TradeExit]]],
) -> tuple[tuple[int, int, int] | None, str, list[dict]]:
    """Apply §3.5 class-B floors + §3.6 cadence floor on train.

    Returns:
        (cell, reason, candidate_table)
    """
    candidate_table: list[dict] = []
    candidates: list[tuple[tuple[int, int, int], BacktestResult]] = []
    for cell in GRID_V1_1:
        result, _exits = grid[cell]
        n_closed = n_closed_of(result)
        ci_low = result.mean_r_ci_95[0]
        tc = result.temporal_concentration
        spm = result.setups_per_month

        pass_n = n_closed >= FLOORS["min_n_closed"]
        pass_ci = ci_low >= FLOORS["min_ci_low"]
        pass_tc = tc is not None and tc < FLOORS["max_temporal_concentration"]
        pass_36 = spm >= FLOORS["min_trades_per_mo"]
        pass_all = pass_n and pass_ci and pass_tc and pass_36

        candidate_table.append(
            {
                "cell": cell,
                "n_closed": n_closed,
                "ci_low": ci_low,
                "tc": tc,
                "spm": spm,
                "pass_n_closed": pass_n,
                "pass_ci_low": pass_ci,
                "pass_tc": pass_tc,
                "pass_3_6": pass_36,
                "pass_all": pass_all,
                "mean_r": result.mean_r,
                "vs_bh_pct": (
                    result.vs_buy_and_hold.get("strategy_minus_bh_pct")
                    if result.vs_buy_and_hold else None
                ),
            }
        )
        if pass_all:
            candidates.append((cell, result))

    if not candidates:
        return None, (
            "no train cell met all four floors §3.5 class B + §3.6: "
            f"n_closed >= {FLOORS['min_n_closed']}, "
            f"ci_low >= {FLOORS['min_ci_low']}, "
            f"temporal_concentration < {FLOORS['max_temporal_concentration']}, "
            f"trades/mo >= {FLOORS['min_trades_per_mo']}"
        ), candidate_table

    def _bh_or_zero(r: BacktestResult) -> float:
        if r.vs_buy_and_hold is None:
            return 0.0
        return r.vs_buy_and_hold.get("strategy_minus_bh_pct", 0.0)

    candidates.sort(
        key=lambda x: (_bh_or_zero(x[1]), x[1].setups_per_month),
        reverse=True,
    )
    best_cell, best_result = candidates[0]
    return best_cell, (
        f"selected by max strategy_minus_bh_pct "
        f"({_bh_or_zero(best_result):+.1f} %); "
        f"{len(candidates)} cells passed all four floors"
    ), candidate_table


# ---------------------------------------------------------------------------
# Hypothesis evaluation
# ---------------------------------------------------------------------------


def evaluate_hypotheses(holdout: BacktestResult) -> dict[str, dict]:
    out: dict[str, dict] = {}
    spm = holdout.setups_per_month
    out["H1"] = {
        "name": HYPOTHESES_V1_1["H1"]["name"],
        "value": spm,
        "pass": HYPOTHESES_V1_1["H1"]["low"] <= spm <= HYPOTHESES_V1_1["H1"]["high"],
    }
    wr = holdout.win_rate
    out["H2"] = {
        "name": HYPOTHESES_V1_1["H2"]["name"],
        "value": wr,
        "pass": HYPOTHESES_V1_1["H2"]["low"] <= wr <= HYPOTHESES_V1_1["H2"]["high"],
    }
    out["H3"] = {
        "name": HYPOTHESES_V1_1["H3"]["name"],
        "value": holdout.mean_r,
        "pass": HYPOTHESES_V1_1["H3"]["low"] <= holdout.mean_r <= HYPOTHESES_V1_1["H3"]["high"],
    }
    pc_mean_r = _post_cost_mean_r(holdout)
    out["H4"] = {
        "name": HYPOTHESES_V1_1["H4"]["name"],
        "value": pc_mean_r,
        "pass": HYPOTHESES_V1_1["H4"]["low"] <= pc_mean_r <= HYPOTHESES_V1_1["H4"]["high"],
        "cost_r_per_trade": COST_R_PER_TRADE,
    }
    pc_proj = _post_cost_projected_annual(holdout)
    out["H5"] = {
        "name": HYPOTHESES_V1_1["H5"]["name"],
        "value": pc_proj,
        "pass": HYPOTHESES_V1_1["H5"]["low"] <= pc_proj <= HYPOTHESES_V1_1["H5"]["high"],
        "pre_cost_value": holdout.projected_annual_return_pct,
    }
    ci_low = holdout.mean_r_ci_95[0]
    out["H6"] = {
        "name": HYPOTHESES_V1_1["H6"]["name"],
        "value": ci_low,
        "pass": ci_low > 0,
    }
    trim = (
        holdout.outlier_robustness.get("trim_5_5")
        if holdout.outlier_robustness else None
    )
    if trim is None:
        out["H7"] = {
            "name": HYPOTHESES_V1_1["H7"]["name"],
            "value": None,
            "pass": False,
            "note": "trim_5_5 unavailable (n_closed < 20)",
        }
    else:
        out["H7"] = {
            "name": HYPOTHESES_V1_1["H7"]["name"],
            "value": trim["mean_r"],
            "pass": trim["mean_r"] > 0,
        }
    tc = holdout.temporal_concentration
    out["H8"] = {
        "name": HYPOTHESES_V1_1["H8"]["name"],
        "value": tc,
        "pass": tc is not None and tc < H8_MAX,
        "threshold": H8_MAX,
    }
    bh = holdout.vs_buy_and_hold
    if bh is None:
        out["H9"] = {
            "name": HYPOTHESES_V1_1["H9"]["name"],
            "value": None,
            "pass": False,
            "note": "vs_buy_and_hold unavailable (no BH closes)",
        }
    else:
        smb = bh["strategy_minus_bh_pct"]
        out["H9"] = {
            "name": HYPOTHESES_V1_1["H9"]["name"],
            "value": smb,
            "pass": smb > 0,
        }
    out["H10"] = {
        "name": HYPOTHESES_V1_1["H10"]["name"],
        "value": None,
        "pass": None,
        "note": "gate-7-specific; not measured at gate 4",
    }
    return out


def verdict_from_hypotheses(eval_result: dict[str, dict]) -> tuple[str, int]:
    n_pass = sum(1 for h in eval_result.values() if h["pass"] is True)
    if n_pass >= PROMOTE_THRESHOLD:
        verdict = "PROMOTE"
    elif n_pass >= ARCHIVE_THRESHOLD:
        verdict = "REVIEW"
    else:
        verdict = "ARCHIVE"
    return verdict, n_pass


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _fmt(x, fmt: str = "+.3f") -> str:
    if x is None:
        return "n/a"
    try:
        return f"{x:{fmt}}"
    except Exception:
        return str(x)


def write_gate4_report(
    *,
    out_dir: Path,
    train_grid: dict[tuple[int, int, int], tuple[BacktestResult, list[TradeExit]]],
    candidate_table: list[dict],
    selected_cell: tuple[int, int, int] | None,
    selection_reason: str,
    holdout: BacktestResult | None,
    hypotheses: dict[str, dict],
    verdict: tuple[str, int],
    holdout_36_pass: bool | None,
    drift_flag: str | None,
    drift_value: float | None,
    wallclock_s: float,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "report.md"
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    overall = verdict[0]
    n_pass = verdict[1]
    n_eval = sum(1 for h in hypotheses.values() if h["pass"] is not None) if hypotheses else 0

    lines: list[str] = []
    lines.append(f"# Gate 4 — trend_rotation_d1 v1.1 ({ts})")
    lines.append("")
    lines.append(
        "Spec: `docs/strategies/trend_rotation_d1_v1_1.md` "
        "(commit `bb12a95`). Protocol gate 4 of "
        "`docs/STRATEGY_RESEARCH_PROTOCOL.md`."
    )
    lines.append("")
    lines.append(
        "**Anti-data-dredging**: §4 v1.1 hypothesis bands and §3.4 "
        "selection floors frozen at the spec commit, evaluated post-"
        "run, never tuned."
    )
    lines.append("")
    lines.append(f"- **Verdict**: **{overall}** ({n_pass} / {n_eval} hypotheses PASS)")
    if holdout_36_pass is False:
        lines.append(
            "- **§3.6 holdout double-check**: ❌ FAIL — final verdict "
            "**ARCHIVE** by §3.6 regardless of §4 count."
        )
    elif holdout_36_pass is True:
        lines.append("- **§3.6 holdout double-check**: ✅ PASS")
    if drift_flag:
        lines.append(
            f"- **Train→holdout drift**: |Δ mean_r| = {drift_value:+.3f} R "
            f"{drift_flag} (advisory, not auto-overriding)"
        )
    lines.append(f"- **Wallclock**: {wallclock_s:.1f} s")
    lines.append("")

    # --- §3.6 pre-measure summary ---
    lines.append("## 1. §3.6 pre-measure (18 cells, train)")
    lines.append("")
    lines.append(
        f"Train window: {TRAIN_START.date()} → {TRAIN_END.date()} "
        f"(≈ 60 months). §3.6 floor: trades/mo portfolio ≥ "
        f"{FLOORS['min_trades_per_mo']}."
    )
    lines.append("")
    lines.append(
        "| momentum | K | rebalance | n_closed | trades/mo | §3.6 viable |"
    )
    lines.append("|---:|---:|---:|---:|---:|:---:|")
    n_viable = 0
    for cell in GRID_V1_1:
        result, _ = train_grid[cell]
        n_closed = n_closed_of(result)
        spm = result.setups_per_month
        viable = spm >= FLOORS["min_trades_per_mo"]
        n_viable += int(viable)
        mom, k, rebal = cell
        mark = "✅" if viable else "❌"
        lines.append(
            f"| {mom} | {k} | {rebal} | {n_closed} | {spm:.2f} | {mark} |"
        )
    lines.append("")
    lines.append(f"§3.6-viable cells: **{n_viable} / 18**.")
    lines.append("")

    # --- §3.4 selection ---
    lines.append("## 2. §3.4 selection (§3.5 class-B + §3.6 floors)")
    lines.append("")
    lines.append(
        f"Floors: n_closed ≥ {FLOORS['min_n_closed']}, "
        f"ci_low ≥ {FLOORS['min_ci_low']}, "
        f"tc < {FLOORS['max_temporal_concentration']}, "
        f"trades/mo ≥ {FLOORS['min_trades_per_mo']}. "
        "Tie-break: max strategy − BH %, then max trades/mo."
    )
    lines.append("")
    lines.append(
        "| mom | K | rebal | n_closed | mean_r | CI low | tc | trades/mo | "
        "BH-Δ % | floors | sel |"
    )
    lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|")
    for row in candidate_table:
        cell = row["cell"]
        floors_str = "".join(
            "✅" if row[k] else "❌"
            for k in ("pass_n_closed", "pass_ci_low", "pass_tc", "pass_3_6")
        )
        bh_str = (
            f"{row['vs_bh_pct']:+.1f}%" if row["vs_bh_pct"] is not None else "n/a"
        )
        mark = "🎯" if cell == selected_cell else ""
        lines.append(
            f"| {cell[0]} | {cell[1]} | {cell[2]} | {row['n_closed']} | "
            f"{_fmt(row['mean_r'])} | {_fmt(row['ci_low'])} | "
            f"{_fmt(row['tc'], '.3f')} | {row['spm']:.2f} | "
            f"{bh_str} | {floors_str} | {mark} |"
        )
    lines.append("")
    lines.append(f"**Selection**: {selection_reason}")
    lines.append("")

    # --- Holdout ---
    lines.append("## 3. Holdout — selected cell")
    lines.append("")
    if holdout is None or selected_cell is None:
        lines.append("Holdout was not run (no §3.4-passing cell selected).")
        lines.append("")
    else:
        lines.append(
            f"Window: {HOLDOUT_START.date()} → {HOLDOUT_END.date()} "
            f"(≈ 16 months). Selected cell {selected_cell} run unchanged."
        )
        lines.append("")
        n_closed_h = n_closed_of(holdout)
        bh_h = holdout.vs_buy_and_hold
        bh_h_str = f"{bh_h['strategy_minus_bh_pct']:+.1f}%" if bh_h else "n/a"
        trim_h = (holdout.outlier_robustness or {}).get("trim_5_5")
        trim_h_str = _fmt(trim_h["mean_r"]) if trim_h else "n/a"
        lines.append(
            "| n_closed | mean_r | CI low | CI high | win | trades/mo | "
            "tc | proj_annual | trim_5_5 | BH-Δ % |"
        )
        lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        lines.append(
            f"| {n_closed_h} | {_fmt(holdout.mean_r)} | "
            f"{_fmt(holdout.mean_r_ci_95[0])} | "
            f"{_fmt(holdout.mean_r_ci_95[1])} | {holdout.win_rate:.1%} | "
            f"{holdout.setups_per_month:.2f} | "
            f"{_fmt(holdout.temporal_concentration, '.3f')} | "
            f"{_fmt(holdout.projected_annual_return_pct, '+.1f')}% | "
            f"{trim_h_str} | {bh_h_str} |"
        )
        lines.append("")

        # §3.6 holdout double-check
        lines.append("### §3.6 holdout double-check")
        lines.append("")
        lines.append(
            f"Holdout trades/mo: {holdout.setups_per_month:.2f} — "
            f"floor §3.6 = {FLOORS['min_trades_per_mo']}. "
            f"{'✅ PASS' if holdout_36_pass else '❌ FAIL — verdict ARCHIVE by §3.6.'}"
        )
        lines.append("")

        # Drift
        if selected_cell is not None:
            train_r = train_grid[selected_cell][0].mean_r
            delta = holdout.mean_r - train_r
            flag = "⚠️ overfit-suspect" if abs(delta) > 0.3 else ""
            lines.append("### Train ↔ holdout drift (advisory)")
            lines.append("")
            lines.append("| mean_r train | mean_r holdout | Δ | flag |")
            lines.append("|---:|---:|---:|:---:|")
            lines.append(
                f"| {_fmt(train_r)} | {_fmt(holdout.mean_r)} | "
                f"{_fmt(delta)} | {flag} |"
            )
            lines.append("")

    # --- Hypotheses ---
    lines.append("## 4. Hypothesis evaluation §4 v1.1 (holdout)")
    lines.append("")
    if not hypotheses:
        lines.append("Holdout not evaluated (no cell selected).")
        lines.append("")
    else:
        lines.append("| Hypothesis | Bande | Value | PASS |")
        lines.append("|---|---|---|:---:|")
        for hk in [f"H{i}" for i in range(1, 11)]:
            cell = hypotheses[hk]
            val = cell["value"]
            verdict_mark = (
                "✅" if cell["pass"] is True
                else ("❌" if cell["pass"] is False else "⚠️ deferred")
            )
            if hk == "H2":
                val_str = f"{val:.1%}" if val is not None else "n/a"
            elif hk == "H1":
                val_str = f"{val:.2f}" if val is not None else "n/a"
            elif hk == "H5":
                val_str = f"{val:+.1f}%" if val is not None else "n/a"
            elif hk == "H8":
                val_str = f"{val:.3f}" if val is not None else "n/a"
            elif hk == "H9":
                val_str = f"{val:+.1f}%" if val is not None else "n/a"
            else:
                val_str = _fmt(val)
            lines.append(
                f"| **{hk}** {HYPOTHESES_V1_1[hk]['name']} | — | {val_str} | {verdict_mark} |"
            )
        lines.append("")

    # --- Verdict ---
    lines.append("## 5. Verdict")
    lines.append("")
    lines.append("Verdict rule (spec §4 v1.1):")
    lines.append("")
    lines.append("- ≥ 6 PASS → **PROMOTE** (gate 5+)")
    lines.append("- 3 ≤ PASS ≤ 5 → **REVIEW** (operator discussion)")
    lines.append("- < 3 PASS → **ARCHIVE**")
    lines.append("")
    lines.append(
        "(H10 = gate-7-specific top-K agreement; deferred at gate 4. "
        "Max gate-4 score = 9 / 9 effective hypotheses.)"
    )
    lines.append("")
    lines.append(
        "**§3.6 holdout floor overrides**: even with ≥ 6 hypotheses "
        "PASS, holdout cadence < 4 trades/mo → ARCHIVE final."
    )
    lines.append("")
    lines.append(f"- **Verdict**: **{overall}** ({n_pass} / {n_eval} PASS)")
    if holdout_36_pass is False:
        lines.append(
            "- §3.6 holdout floor FAILED → **final verdict locked to "
            "ARCHIVE** regardless of §4 count above."
        )
    lines.append("")

    # --- Suggested next ---
    lines.append("## 6. Suggested next")
    lines.append("")
    if overall == "PROMOTE" and holdout_36_pass is not False:
        lines.append(
            "PROMOTE. Operator discussion required before gate 5 on "
            "edge magnitude (vs v1 +84 %), régime stationarity, and "
            "H5 path-decision per spec §4 H5 note if proj annual "
            "outside [20, 25] %."
        )
    elif overall == "REVIEW":
        lines.append(
            f"REVIEW ({n_pass} hypotheses PASS). Operator discussion "
            "required on borderline hypotheses before continuing."
        )
    else:
        # ARCHIVE
        lines.append(
            "ARCHIVE. Per spec v1.1 footer: this is the final v1.x "
            "iteration. The strategy class **HTF cross-sectional "
            "momentum multi-asset** is considered structurally non-"
            "viable for the operator's deployment context."
        )
        lines.append("")
        lines.append("Action items:")
        lines.append(
            "- Move `src/strategies/trend_rotation_d1/` → "
            "`archived/strategies/trend_rotation_d1_v1_1/` per protocol "
            "§8 + post-mortem README."
        )
        lines.append(
            "- Update protocol §11.4 / §11.5 with the v1.1 archive "
            "case study (5th archive in the strategy-research phase)."
        )
        lines.append(
            "- Operator discussion on next strategy class (HTF "
            "single-asset wick-sensitive variants per §11.5 backlog)."
        )
    lines.append("")

    path.write_text("\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        type=str,
        default=None,
        help="Override the output directory.",
    )
    args = parser.parse_args()

    t_start = time.perf_counter()
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    out_dir = (
        Path(args.out_dir)
        if args.out_dir
        else RUNS_DIR / f"gate4_trend_rotation_d1_v1_1_{ts}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Loading panel ({len(UNIVERSE)} assets)...", flush=True)
    panel = load_panel()

    # ---- Train grid (18 cells) ----
    print(
        f"\n##### Train grid v1.1 — 18 cells "
        f"({TRAIN_START.date()} → {TRAIN_END.date()}) #####",
        flush=True,
    )
    train_grid: dict[tuple[int, int, int], tuple[BacktestResult, list[TradeExit]]] = {}
    t_train_start = time.perf_counter()
    for mom, k, rebal in GRID_V1_1:
        t0 = time.perf_counter()
        result, exits = run_cell(
            panel,
            momentum_lookback_days=mom,
            K=k,
            rebalance_frequency_days=rebal,
            period_start=TRAIN_START,
            period_end=TRAIN_END,
        )
        dt = time.perf_counter() - t0
        ci = result.mean_r_ci_95
        tc = result.temporal_concentration
        tc_str = f"{tc:.3f}" if tc is not None else "na"
        bh = (
            f"{result.vs_buy_and_hold['strategy_minus_bh_pct']:+.1f}%"
            if result.vs_buy_and_hold else "na"
        )
        print(
            f"  mom={mom:>3} K={k} rebal={rebal:>2}: "
            f"n={len(exits)} mean_r={result.mean_r:+.3f} "
            f"CI=[{ci[0]:+.3f}, {ci[1]:+.3f}] win={result.win_rate:.1%} "
            f"setups/mo={result.setups_per_month:.2f} "
            f"tc={tc_str} bh-Δ={bh} ({dt:.1f}s)",
            flush=True,
        )
        train_grid[(mom, k, rebal)] = (result, exits)

    t_train = time.perf_counter() - t_train_start

    grid_export = {
        f"mom={m}_K={k}_rebal={r}": _result_to_dict(res)
        for (m, k, r), (res, _) in train_grid.items()
    }
    (out_dir / "train_grid.json").write_text(
        json.dumps(grid_export, indent=2, default=str)
    )

    # ---- §3.6 pre-measure report ----
    pm_path = (
        RUNS_DIR / f"premeasure_trend_rotation_d1_v1_1_{ts}.md"
    )
    write_premeasure_report(pm_path, train_grid, wallclock_s=t_train)
    print(f"\nPre-measure §3.6 report: {pm_path}")

    n_viable = sum(
        1 for cell in GRID_V1_1
        if train_grid[cell][0].setups_per_month >= FLOORS["min_trades_per_mo"]
    )
    print(f"  §3.6-viable cells: {n_viable} / 18", flush=True)

    # ---- §3.4 selection ----
    selected_cell, selection_reason, candidate_table = select_best_cell(train_grid)
    print(f"\n  §3.4 selected cell: {selected_cell} — {selection_reason}", flush=True)

    holdout = None
    hypotheses: dict[str, dict] = {}
    verdict: tuple[str, int] = ("ARCHIVE", 0)
    holdout_36_pass: bool | None = None
    drift_flag: str | None = None
    drift_value: float | None = None

    if selected_cell is not None:
        print(
            f"\n##### Holdout ({HOLDOUT_START.date()} → "
            f"{HOLDOUT_END.date()}) — selected cell #####",
            flush=True,
        )
        mom, k, rebal = selected_cell
        result, _exits = run_cell(
            panel,
            momentum_lookback_days=mom,
            K=k,
            rebalance_frequency_days=rebal,
            period_start=HOLDOUT_START,
            period_end=HOLDOUT_END,
        )
        holdout = result
        result.to_json(out_dir / "holdout.json")
        ci = result.mean_r_ci_95
        bh = result.vs_buy_and_hold
        bh_str = f"{bh['strategy_minus_bh_pct']:+.1f}%" if bh else "na"
        print(
            f"  holdout: n={len(_exits)} mean_r={result.mean_r:+.3f} "
            f"CI=[{ci[0]:+.3f}, {ci[1]:+.3f}] "
            f"win={result.win_rate:.1%} "
            f"setups/mo={result.setups_per_month:.2f} "
            f"proj={result.projected_annual_return_pct:+.1f}% "
            f"bh-Δ={bh_str}",
            flush=True,
        )

        hypotheses = evaluate_hypotheses(result)
        verdict = verdict_from_hypotheses(hypotheses)

        # §3.6 holdout double-check
        holdout_36_pass = result.setups_per_month >= FLOORS["min_trades_per_mo"]
        # Drift advisory
        train_r = train_grid[selected_cell][0].mean_r
        drift_value = result.mean_r - train_r
        if abs(drift_value) > 0.3:
            drift_flag = "⚠️ overfit-suspect (|Δ| > 0.3R)"

        # §3.6 holdout fail overrides verdict
        if not holdout_36_pass:
            verdict = ("ARCHIVE", verdict[1])

        print(
            f"  Verdict: {verdict[0]} ({verdict[1]} hypotheses PASS); "
            f"§3.6 holdout: {'PASS' if holdout_36_pass else 'FAIL'}",
            flush=True,
        )

    # ---- viability_check.json ----
    viability = {
        "candidate_table": [
            {
                **{k: v for k, v in row.items() if k != "cell"},
                "cell": list(row["cell"]),
            }
            for row in candidate_table
        ],
        "selected_cell": (
            list(selected_cell) if selected_cell is not None else None
        ),
        "selection_reason": selection_reason,
        "floors": FLOORS,
        "holdout_setups_per_month": (
            holdout.setups_per_month if holdout is not None else None
        ),
        "holdout_3_6_pass": holdout_36_pass,
        "drift_train_to_holdout_mean_r": drift_value,
        "drift_flag": drift_flag,
    }
    (out_dir / "viability_check.json").write_text(
        json.dumps(viability, indent=2, default=str)
    )

    wallclock = time.perf_counter() - t_start
    report_path = write_gate4_report(
        out_dir=out_dir,
        train_grid=train_grid,
        candidate_table=candidate_table,
        selected_cell=selected_cell,
        selection_reason=selection_reason,
        holdout=holdout,
        hypotheses=hypotheses,
        verdict=verdict,
        holdout_36_pass=holdout_36_pass,
        drift_flag=drift_flag,
        drift_value=drift_value,
        wallclock_s=wallclock,
    )
    print(f"\nReport: {report_path}")
    print(f"Total wallclock: {wallclock:.1f}s")
    return 0 if verdict[0] == "PROMOTE" else (1 if verdict[0] == "REVIEW" else 2)


if __name__ == "__main__":
    sys.exit(main())
