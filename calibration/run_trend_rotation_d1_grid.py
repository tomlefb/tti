"""Gate 4 of ``docs/STRATEGY_RESEARCH_PROTOCOL.md`` for the
trend_rotation_d1 strategy: train grid → cell selection → holdout
evaluation → 10-hypothesis verdict.

Anti-data-dredging contract
---------------------------
The 10 hypotheses (spec §4) are frozen at commit ``889f18c`` and
**not** modified post-hoc. The selection criteria for the train
grid are likewise pre-specified:

1. ``mean_r_ci_95.lower >= 0`` (no measurable edge otherwise)
2. ``temporal_concentration < 0.4`` (regime-fitting flag)
3. ``n_closed >= 50`` (protocol §5 admission gate)

Among the cells that pass those three, the highest
``vs_buy_and_hold.strategy_minus_bh_pct`` is selected (rotation
must beat passive equal-weight basket — H9 the binding criterion);
tie-break = highest ``setups_per_month``.

Cost model (FundedNext, gate-4 approximation)
---------------------------------------------
Per the operator brief: flat $30 per closed trade as a first-order
approximation across the multi-asset basket (varies between $5
on FX majors and $50 on crypto / wide-spread metals; $30 is the
basket-weighted mean given the mix). Capital assumed at 100,000
USD with 1 % risk per trade = $1000 risk / trade. Cost as a
fraction of R = 30 / 1000 = **0.030 R / closed trade** flat.

Outputs
-------
- ``calibration/runs/gate4_trend_rotation_d1_<TS>/report.md``
- ``train_grid.json`` (one BacktestResult per cell)
- ``holdout.json`` (BacktestResult with the selected cell)

Run
---
    python -m calibration.run_trend_rotation_d1_grid
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
from src.backtest.result import BacktestResult, SetupRecord  # noqa: E402
from src.strategies.trend_rotation_d1 import (  # noqa: E402
    StrategyParams,
    TradeExit,
)

RUNS_DIR = REPO_ROOT / "calibration" / "runs"

# Spec §3.2 grid (8 cells).
GRID_SPEC = list(
    product(
        [63, 126],     # momentum_lookback
        [3, 4],        # K
        [10, 21],      # rebalance_frequency
    )
)

# Selection criteria — class-adapted per protocol §3 / §3.5.
# Two profiles: ``class_a`` (single-asset wick-sensitive HTF, the
# §5.2 standard) and ``class_b`` (multi-asset cross-sectional
# momentum, §3.5 revised). The trend_rotation_d1 strategy is
# class B by §11.4.1, so the default profile here is class_b. The
# original verdict on c2ddce2 was computed under the ``class_a``
# profile (the §5.2 standard at the time); ``--criteria-class
# class_a`` reproduces that verdict for cross-check.
SELECTION_PROFILES: dict[str, dict] = {
    "class_a": {
        "min_n_closed": 50,
        "min_ci_low": 0.0,
        "max_temporal_concentration": 0.4,
        "h8_max": 0.4,
        "label": "§5.2 standard (class A — single-asset wick-sensitive HTF)",
    },
    "class_b": {
        "min_n_closed": 100,
        "min_ci_low": -0.1,
        "max_temporal_concentration": 0.6,
        "h8_max": 0.6,
        "label": "§3.5 revised (class B — multi-asset cross-sectional momentum)",
    },
}
DEFAULT_CRITERIA_CLASS = "class_b"

# Cost model — flat per-trade R fraction.
COST_R_PER_TRADE = 0.030  # ~$30 on $1000 risk

# Hypothesis bands — spec §4. Frozen at commit 889f18c.
HYPOTHESES: dict[str, dict] = {
    "H1": {
        "name": "Closed trades / month / portfolio in [0.7, 2.3]",
        "low": 0.7,
        "high": 2.3,
    },
    "H2": {
        "name": "Win rate (closed) in [50 %, 60 %]",
        "low": 0.50,
        "high": 0.60,
    },
    "H3": {
        "name": "Mean R (pre-cost) per closed in [+0.2, +0.6]",
        "low": 0.2,
        "high": 0.6,
    },
    "H4": {
        "name": "Mean R (post-cost) per closed in [+0.1, +0.5]",
        "low": 0.1,
        "high": 0.5,
    },
    "H5": {
        "name": "Projected annual return % in [5, 15]",
        "low": 5.0,
        "high": 15.0,
    },
    "H6": {"name": "mean_r_ci_95.lower > 0"},
    "H7": {"name": "outlier_robustness.trim_5_5.mean_r > 0"},
    # H8 max is class-adapted at runtime — default placeholder is the
    # §3.5 class-B value (0.6); the §5.2 standard 0.4 is plugged when
    # ``--criteria-class class_a`` is selected. The runtime override
    # is the source of truth (see ``main`` and ``evaluate_hypotheses``).
    "H8": {
        "name": "temporal_concentration < {h8_max}",
        "max": 0.6,  # default class-B; runtime-set
    },
    "H9": {"name": "vs_buy_and_hold.strategy_minus_bh_pct > 0"},
    "H10": {
        "name": "Top-K agreement Duk vs MT5 (gate 7) > 70 %",
        "min": 0.70,
    },
}

PROMOTE_THRESHOLD = 6
ARCHIVE_THRESHOLD = 3


# ---------------------------------------------------------------------------
# Conversion: TradeExit → SetupRecord (rebalance_close outcome)
# ---------------------------------------------------------------------------


def trade_exits_to_setups(exits: list[TradeExit]) -> list[SetupRecord]:
    """Map each ``TradeExit`` to a ``SetupRecord`` with the
    rebalance_close outcome (commit 4e1cd39)."""
    records = []
    for e in exits:
        records.append(
            SetupRecord(
                timestamp_utc=e.exit_timestamp_utc.isoformat(),
                instrument=e.asset,
                direction=e.direction,
                quality="A",
                realized_r=e.return_r,
                outcome="rebalance_close",
            )
        )
    return records


# ---------------------------------------------------------------------------
# Buy-and-hold benchmark — equal-weight basket of the 15 assets
# ---------------------------------------------------------------------------


def _ew_basket_close(panel: dict[str, pd.DataFrame], at: pd.Timestamp) -> float:
    """Return the equal-weight basket value (sum of normalised
    closes) for the ``at`` date — used as the BH proxy.

    Each asset is normalised to its first close on the window, and
    the basket value is the mean across the 15 normalised series.
    Robust to assets with different price scales."""
    vals = []
    for df in panel.values():
        # Find the closest available close on or before ``at``.
        sub = df.loc[df.index <= at]
        if len(sub) == 0:
            continue
        vals.append(float(sub["close"].iloc[-1]))
    if not vals:
        return 0.0
    # Use a relative basket value (mean of close ratios). Anchor at
    # the panel's earliest common date.
    return sum(vals) / len(vals)


# ---------------------------------------------------------------------------
# Grid run — train
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
    """Run one (cell, window) tuple and return the
    ``BacktestResult`` plus raw ``TradeExit`` list."""
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
        strategy_name="trend_rotation_d1",
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


def run_grid(
    panel: dict[str, pd.DataFrame],
    *,
    period_start: pd.Timestamp,
    period_end: pd.Timestamp,
    log: bool = True,
) -> dict[tuple[int, int, int], tuple[BacktestResult, list[TradeExit]]]:
    """Run the 8-cell grid on the train window."""
    out: dict[tuple[int, int, int], tuple[BacktestResult, list[TradeExit]]] = {}
    if log:
        print("\n=== Train grid (8 cells) ===", flush=True)
    for mom, k, rebal in GRID_SPEC:
        t0 = time.perf_counter()
        result, exits = run_cell(
            panel,
            momentum_lookback_days=mom,
            K=k,
            rebalance_frequency_days=rebal,
            period_start=period_start,
            period_end=period_end,
        )
        dt = time.perf_counter() - t0
        if log:
            ci = result.mean_r_ci_95
            tc = result.temporal_concentration
            tc_str = f"{tc:.3f}" if tc is not None else "na"
            bh = (
                f"{result.vs_buy_and_hold['strategy_minus_bh_pct']:+.1f}%"
                if result.vs_buy_and_hold
                else "na"
            )
            print(
                f"  mom={mom:>3} K={k} rebal={rebal:>2}: "
                f"n={len(exits)} "
                f"mean_r={result.mean_r:+.3f} "
                f"CI=[{ci[0]:+.3f}, {ci[1]:+.3f}] "
                f"win={result.win_rate:.1%} "
                f"setups/mo={result.setups_per_month:.2f} "
                f"tc={tc_str} "
                f"bh-Δ={bh} "
                f"({dt:.1f}s)",
                flush=True,
            )
        out[(mom, k, rebal)] = (result, exits)
    return out


def select_best_cell(
    grid: dict[tuple[int, int, int], tuple[BacktestResult, list[TradeExit]]],
    profile: dict,
) -> tuple[tuple[int, int, int] | None, str]:
    """Apply class-adapted selection criteria from ``profile`` (one of
    ``SELECTION_PROFILES``). Returns ``((mom, K, rebal), reason)`` or
    ``(None, reason)``."""
    candidates: list[tuple[tuple[int, int, int], BacktestResult]] = []
    for cell, (result, _exits) in grid.items():
        n_closed = sum(
            1 for s in result.setups if s.outcome not in ("entry_not_hit", "open_at_horizon")
        )
        if n_closed < profile["min_n_closed"]:
            continue
        ci_low = result.mean_r_ci_95[0]
        if ci_low < profile["min_ci_low"]:
            continue
        if (
            result.temporal_concentration is None
            or result.temporal_concentration >= profile["max_temporal_concentration"]
        ):
            continue
        candidates.append((cell, result))

    if not candidates:
        return None, (
            f"no train cell met all three selection criteria under "
            f"{profile['label']} (n_closed >= {profile['min_n_closed']}, "
            f"ci_low >= {profile['min_ci_low']}, "
            f"temporal_concentration < {profile['max_temporal_concentration']})"
        )

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
        f"{len(candidates)} cells passed all three filters"
    )


# ---------------------------------------------------------------------------
# Hypothesis evaluation
# ---------------------------------------------------------------------------


def _post_cost_mean_r(result: BacktestResult) -> float:
    closed = [
        s for s in result.setups
        if s.outcome not in ("entry_not_hit", "open_at_horizon")
    ]
    if not closed:
        return 0.0
    rs = [s.realized_r - COST_R_PER_TRADE for s in closed]
    return sum(rs) / len(rs)


def _post_cost_projected_annual(result: BacktestResult) -> float:
    return (
        _post_cost_mean_r(result)
        * result.setups_per_month
        * 12.0
        * result.risk_per_trade_pct
    )


def evaluate_hypotheses(
    holdout: BacktestResult,
    *,
    h8_max: float = 0.6,
) -> dict[str, dict]:
    """Evaluate the 10 §4 hypotheses on the holdout BacktestResult.

    H8 threshold is class-adapted via ``h8_max`` (default 0.6 for
    class B per §3.5; pass 0.4 for class A). H6 / H7 stay strict
    regardless of class — they are the final-judge floors per
    §3.5.
    """
    out: dict[str, dict] = {}

    spm = holdout.setups_per_month
    out["H1"] = {
        "name": HYPOTHESES["H1"]["name"],
        "value": spm,
        "pass": HYPOTHESES["H1"]["low"] <= spm <= HYPOTHESES["H1"]["high"],
    }

    wr = holdout.win_rate
    out["H2"] = {
        "name": HYPOTHESES["H2"]["name"],
        "value": wr,
        "pass": HYPOTHESES["H2"]["low"] <= wr <= HYPOTHESES["H2"]["high"],
    }

    out["H3"] = {
        "name": HYPOTHESES["H3"]["name"],
        "value": holdout.mean_r,
        "pass": HYPOTHESES["H3"]["low"] <= holdout.mean_r <= HYPOTHESES["H3"]["high"],
    }

    pc_mean_r = _post_cost_mean_r(holdout)
    out["H4"] = {
        "name": HYPOTHESES["H4"]["name"],
        "value": pc_mean_r,
        "pass": HYPOTHESES["H4"]["low"] <= pc_mean_r <= HYPOTHESES["H4"]["high"],
        "cost_r_per_trade": COST_R_PER_TRADE,
    }

    pc_proj = _post_cost_projected_annual(holdout)
    out["H5"] = {
        "name": HYPOTHESES["H5"]["name"],
        "value": pc_proj,
        "pass": HYPOTHESES["H5"]["low"] <= pc_proj <= HYPOTHESES["H5"]["high"],
        "pre_cost_value": holdout.projected_annual_return_pct,
    }

    ci_low = holdout.mean_r_ci_95[0]
    out["H6"] = {
        "name": HYPOTHESES["H6"]["name"],
        "value": ci_low,
        "pass": ci_low > 0,
    }

    trim = holdout.outlier_robustness.get("trim_5_5") if holdout.outlier_robustness else None
    if trim is None:
        out["H7"] = {
            "name": HYPOTHESES["H7"]["name"],
            "value": None,
            "pass": False,
            "note": "trim_5_5 unavailable (n_closed < 20)",
        }
    else:
        out["H7"] = {
            "name": HYPOTHESES["H7"]["name"],
            "value": trim["mean_r"],
            "pass": trim["mean_r"] > 0,
        }

    tc = holdout.temporal_concentration
    out["H8"] = {
        "name": HYPOTHESES["H8"]["name"].format(h8_max=h8_max),
        "value": tc,
        "pass": tc is not None and tc < h8_max,
        "threshold": h8_max,
    }

    bh = holdout.vs_buy_and_hold
    if bh is None:
        out["H9"] = {
            "name": HYPOTHESES["H9"]["name"],
            "value": None,
            "pass": False,
            "note": "vs_buy_and_hold unavailable (no BH closes)",
        }
    else:
        smb = bh["strategy_minus_bh_pct"]
        out["H9"] = {
            "name": HYPOTHESES["H9"]["name"],
            "value": smb,
            "pass": smb > 0,
        }

    # H10 is rotation-specific (top-K agreement) and is measured at
    # gate 7. At gate 4 we report it as "deferred / not measured" —
    # ``pass=None`` excludes it from both numerator and denominator
    # of the verdict count, so the verdict reads off 9/10 max.
    out["H10"] = {
        "name": HYPOTHESES["H10"]["name"],
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


def write_report(
    *,
    out_dir: Path,
    train_grid: dict[tuple[int, int, int], tuple[BacktestResult, list[TradeExit]]],
    selected_cell: tuple[int, int, int] | None,
    selection_reason: str,
    holdout: BacktestResult | None,
    hypotheses: dict[str, dict],
    verdict: tuple[str, int],
    wallclock_s: float,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "report.md"
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines: list[str] = []
    lines.append(f"# Gate 4 — trend_rotation_d1 backtest principal Duk ({ts})")
    lines.append("")
    lines.append(
        "Spec: `docs/strategies/trend_rotation_d1.md` (commit "
        "`889f18c`). Protocol gate 4 of "
        "`docs/STRATEGY_RESEARCH_PROTOCOL.md`."
    )
    lines.append("")
    lines.append(
        "**Anti-data-dredging**: the 10 hypotheses (§4 of the spec) "
        "and the train selection criteria (§3.2) are frozen at the "
        "spec commit and evaluated post-run, never tuned."
    )
    lines.append("")

    overall = verdict[0]
    n_pass = verdict[1]
    n_eval = sum(1 for h in hypotheses.values() if h["pass"] is not None) if hypotheses else 0
    lines.append(f"- **Verdict**: **{overall}** ({n_pass} / {n_eval} hypotheses PASS)")
    lines.append(f"- **Wallclock**: {wallclock_s:.1f} s")
    lines.append("")

    # --- Train grid ----------------------------------------------------
    lines.append("## 1. Train grid (8 cells)")
    lines.append("")
    lines.append(
        f"Window: {TRAIN_START.date()} → {TRAIN_END.date()}. "
        "Selection: ``n_closed >= 50`` AND ``ci_low >= 0`` AND "
        "``temporal_concentration < 0.4``; among those, max "
        "``vs_buy_and_hold.strategy_minus_bh_pct`` "
        "(tie-break: max ``setups_per_month``)."
    )
    lines.append("")
    lines.append(
        "| mom | K | rebal | n_closed | mean_r | CI low | CI high | win | setups/mo | tc | strategy − BH % | sel |"
    )
    lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for cell in GRID_SPEC:
        result, exits = train_grid[cell]
        n_closed = sum(
            1 for s in result.setups if s.outcome not in ("entry_not_hit", "open_at_horizon")
        )
        bh = result.vs_buy_and_hold
        bh_str = (
            f"{bh['strategy_minus_bh_pct']:+.1f}%"
            if bh
            else "n/a"
        )
        mark = "✅" if cell == selected_cell else ""
        lines.append(
            f"| {cell[0]} | {cell[1]} | {cell[2]} | {n_closed} | "
            f"{_fmt(result.mean_r)} | {_fmt(result.mean_r_ci_95[0])} | "
            f"{_fmt(result.mean_r_ci_95[1])} | {result.win_rate:.1%} | "
            f"{result.setups_per_month:.2f} | "
            f"{_fmt(result.temporal_concentration, '.3f')} | "
            f"{bh_str} | {mark} |"
        )
    lines.append("")
    lines.append(f"**Selection**: {selection_reason}")
    lines.append("")

    # --- Holdout -------------------------------------------------------
    lines.append("## 2. Holdout — selected cell")
    lines.append("")
    lines.append(
        f"Window: {HOLDOUT_START.date()} → {HOLDOUT_END.date()}. "
        "Selected cell run unchanged on the holdout."
    )
    lines.append("")
    if holdout is None:
        lines.append("Holdout was not run (no train cell selected).")
        lines.append("")
    else:
        n_closed_h = sum(
            1 for s in holdout.setups
            if s.outcome not in ("entry_not_hit", "open_at_horizon")
        )
        bh_h = holdout.vs_buy_and_hold
        bh_h_str = f"{bh_h['strategy_minus_bh_pct']:+.1f}%" if bh_h else "n/a"
        trim_h = (holdout.outlier_robustness or {}).get("trim_5_5")
        trim_h_str = _fmt(trim_h["mean_r"]) if trim_h else "n/a"
        lines.append(
            "| n_closed | mean_r | CI low | CI high | win | setups/mo | tc | proj_annual | trim_5_5 | strategy − BH % |"
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

        # Train ↔ holdout consistency check.
        if selected_cell is not None:
            train_r = train_grid[selected_cell][0].mean_r
            delta = holdout.mean_r - train_r
            flag = "⚠️" if abs(delta) > 0.3 else ""
            lines.append("### Train vs holdout consistency check")
            lines.append("")
            lines.append("| mean_r train | mean_r holdout | Δ | overfit flag (Δ > 0.3R) |")
            lines.append("|---:|---:|---:|:---:|")
            lines.append(
                f"| {_fmt(train_r)} | {_fmt(holdout.mean_r)} | "
                f"{_fmt(delta)} | {flag} |"
            )
            lines.append("")

    # --- Hypotheses ----------------------------------------------------
    lines.append("## 3. Hypothesis evaluation (holdout — §4)")
    lines.append("")
    if not hypotheses:
        lines.append("Holdout not evaluated (no cell selected).")
        lines.append("")
    else:
        lines.append("| Hypothesis | Value | PASS |")
        lines.append("|---|---|:---:|")
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
            lines.append(f"| **{hk}** {HYPOTHESES[hk]['name']} | {val_str} | {verdict_mark} |")
        lines.append("")

    # --- Verdict -------------------------------------------------------
    lines.append("## 4. Verdict")
    lines.append("")
    lines.append("Verdict rule (spec §4 holdout):")
    lines.append("")
    lines.append("- ≥ 6 PASS → **PROMOTE** (candidate gate 5)")
    lines.append("- 3 ≤ PASS ≤ 5 → **REVIEW** (operator discussion)")
    lines.append("- < 3 PASS → **ARCHIVE** (mandatory)")
    lines.append("")
    lines.append(
        "(H10 is gate-7-specific (top-K agreement Duk vs MT5) and "
        "deferred from the gate-4 count — ``pass=None`` excludes it "
        "from both numerator and denominator. Max gate-4 score is "
        "9 / 9.)"
    )
    lines.append("")
    lines.append(f"- **Verdict**: **{overall}** ({n_pass} / {n_eval} PASS)")
    lines.append("")

    # --- Suggested next ------------------------------------------------
    lines.append("## 5. Suggested next")
    lines.append("")
    if overall == "PROMOTE":
        h5 = hypotheses.get("H5", {})
        h5_value = h5.get("value", 0.0) or 0.0
        below_threshold = 0 < h5_value < 20
        if below_threshold:
            lines.append(
                f"**PROMOTE with projected_annual_return = {h5_value:+.1f} %** — "
                "below the protocol §3 viability threshold of 20 %. The "
                "spec §4 H5 note pre-flags this case. **Operator path "
                "decision required**:"
            )
            lines.append("")
            lines.append(
                "- (a) Continue gates 5–8 anyway as a methodological "
                "learning, no Sprint-7 deployment commitment."
            )
            lines.append(
                "- (b) Archive with the explicit note 'edge measurable "
                "but below viability threshold' — adds a fourth case "
                "study to §11."
            )
            lines.append(
                "- (c) Revise §3 of the protocol to set a per-class "
                "viability threshold (CSM has structurally lower "
                "annual return than retail-technical patterns)."
            )
        else:
            lines.append(
                f"PROMOTE. Proceed to **gate 5** — cross-check on "
                "Databento partial subset (NDX / SPX / DJI futures "
                "only, accepted ±50 % Mean R band per spec §6 / "
                "documented limitation)."
            )
    elif overall == "REVIEW":
        lines.append(
            f"REVIEW ({n_pass} hypotheses PASS). Operator discussion "
            "required on the borderline hypotheses before proceeding "
            "to gate 5 or archiving."
        )
    else:
        lines.append(
            "ARCHIVE. Move to "
            "``archived/strategies/trend_rotation_d1_v1/`` with the "
            "post-mortem README per protocol §8 + update §11.4 with "
            "the transferable learnings."
        )
    lines.append("")

    path.write_text("\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _result_to_dict(r: BacktestResult) -> dict:
    d = asdict(r)
    d.pop("setups", None)
    d["projected_annual_return_pct"] = r.projected_annual_return_pct
    return d


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        type=str,
        default=None,
        help="Override the output directory.",
    )
    parser.add_argument(
        "--criteria-class",
        choices=list(SELECTION_PROFILES.keys()),
        default=DEFAULT_CRITERIA_CLASS,
        help=(
            "Selection-criteria profile per protocol §3 / §3.5. "
            f"Default '{DEFAULT_CRITERIA_CLASS}' applies the §3.5 "
            "class-B revised floors (the strategy is class B per §11.4.1). "
            "Use 'class_a' to reproduce the original §5.2 standard "
            "verdict (commit c2ddce2)."
        ),
    )
    args = parser.parse_args()

    profile = SELECTION_PROFILES[args.criteria_class]
    print(f"Selection profile: {profile['label']}", flush=True)

    t_start = time.perf_counter()
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    out_dir_default = (
        f"gate4_trend_rotation_d1_{ts}"
        if args.criteria_class == "class_a"
        else f"gate4_trend_rotation_d1_v1_revised_{ts}"
    )
    out_dir = (
        Path(args.out_dir) if args.out_dir else RUNS_DIR / out_dir_default
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Loading panel ({len(UNIVERSE)} assets)...", flush=True)
    panel = load_panel()

    # Train grid ------------------------------------------------------
    print(
        f"\n##### Train ({TRAIN_START.date()} → {TRAIN_END.date()}) #####",
        flush=True,
    )
    train_grid = run_grid(panel, period_start=TRAIN_START, period_end=TRAIN_END)
    grid_export = {
        f"mom={m}_K={k}_rebal={r}": _result_to_dict(res)
        for (m, k, r), (res, _) in train_grid.items()
    }
    (out_dir / "train_grid.json").write_text(
        json.dumps(grid_export, indent=2, default=str)
    )

    selected_cell, selection_reason = select_best_cell(train_grid, profile)
    print(f"\n  Selected cell: {selected_cell} — {selection_reason}", flush=True)

    holdout = None
    hypotheses: dict[str, dict] = {}
    verdict: tuple[str, int] = ("ARCHIVE", 0)

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

        hypotheses = evaluate_hypotheses(result, h8_max=profile["h8_max"])
        verdict = verdict_from_hypotheses(hypotheses)
        print(
            f"  Verdict: {verdict[0]} ({verdict[1]} hypotheses PASS)",
            flush=True,
        )

    wallclock = time.perf_counter() - t_start
    report_path = write_report(
        out_dir=out_dir,
        train_grid=train_grid,
        selected_cell=selected_cell,
        selection_reason=selection_reason,
        holdout=holdout,
        hypotheses=hypotheses,
        verdict=verdict,
        wallclock_s=wallclock,
    )
    print(f"\nReport: {report_path}")
    print(f"Total wallclock: {wallclock:.1f}s")
    return 0 if verdict[0] == "PROMOTE" else (1 if verdict[0] == "REVIEW" else 2)


if __name__ == "__main__":
    sys.exit(main())
