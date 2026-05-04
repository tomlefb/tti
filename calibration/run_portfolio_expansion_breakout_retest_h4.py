"""Portfolio expansion test for the (archived) breakout_retest_h4 strategy.

Re-runs the gate-4 grid + holdout + 10-hypothesis evaluation against
the three out-of-portfolio instruments — EURUSD, GBPUSD, BTCUSD —
using the same selection criteria, hypothesis bands, and verdict
rule as the original gate-4 run on XAU/NDX/SPX.

Per-instrument grid + cost overrides are injected at module load
time (the original ``INSTRUMENTS`` / ``GRID_SPEC`` /
``MAX_RISK_DISTANCE`` / ``COST_R_PER_TRADE`` / ``HYPOTHESES`` are
overridden in place rather than forking the gate-4 script). Cost
values are placeholders adequate for the verdict — admission is
gated on PRE-cost criteria (n_closed, ci_low, temporal_concentration).

Run
---
    python -m calibration.run_portfolio_expansion_breakout_retest_h4
"""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from calibration import run_breakout_retest_h4_grid as gate4  # noqa: E402

# --- Inject portfolio-expansion config -------------------------------------

NEW_INSTRUMENTS = ["EURUSD", "GBPUSD", "BTCUSD"]

# Grids per the operator brief.
NEW_GRID_SPEC: dict = {
    "EURUSD": {
        "retest_tolerance": [0.0003, 0.0005, 0.0008],   # 3 / 5 / 8 pips
        "sl_buffer": [0.0003, 0.0005, 0.0008],
    },
    "GBPUSD": {
        "retest_tolerance": [0.0003, 0.0005, 0.0008],
        "sl_buffer": [0.0003, 0.0005, 0.0008],
    },
    "BTCUSD": {
        "retest_tolerance": [30.0, 60.0, 100.0],
        "sl_buffer": [50.0, 100.0, 200.0],
    },
}

# 3× 30-day median range, conservative.
NEW_MAX_RISK_DISTANCE: dict = {
    "EURUSD": 0.05,    # 500 pips — far above any realistic retest wick
    "GBPUSD": 0.05,
    "BTCUSD": 5000.0,  # USD
}

# Pre-cost admission (n_closed / ci_low / tc) is what determines the
# verdict; cost values are placeholders sized to the FundedNext-style
# 1 % risk per trade convention.
NEW_COST_R_PER_TRADE: dict = {
    "EURUSD": 0.02,   # ~1 pip spread on a 50-pip SL
    "GBPUSD": 0.02,
    "BTCUSD": 0.05,   # crypto spread is wider
}

gate4.INSTRUMENTS = NEW_INSTRUMENTS
gate4.GRID_SPEC = NEW_GRID_SPEC
gate4.MAX_RISK_DISTANCE = NEW_MAX_RISK_DISTANCE
gate4.COST_R_PER_TRADE = NEW_COST_R_PER_TRADE


def main() -> int:
    """Replicate gate4.main() but with portfolio-expansion config and
    out-dir. Skips H10 (MT5 fixtures may not exist for these
    instruments — admission verdict is the priority here)."""
    t_start = time.perf_counter()
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    out_dir = (
        REPO_ROOT
        / "calibration"
        / "runs"
        / f"portfolio_expansion_breakout_retest_h4_{ts}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    train_grids: dict = {}
    selected_params: dict = {}
    holdouts: dict = {}
    hypotheses: dict = {}
    verdicts: dict = {}
    h10s: dict = {}

    for instrument in NEW_INSTRUMENTS:
        print(
            f"\n##### {instrument} — train "
            f"({gate4.TRAIN_START.date()} → {gate4.TRAIN_END.date()}) #####",
            flush=True,
        )
        m5_train = gate4.load_duk_m5(instrument, gate4.TRAIN_START, gate4.TRAIN_END)
        h4_train = gate4.resample_m5_to_h4(m5_train)
        d1_train = gate4.resample_m5_to_d1_close(m5_train)
        df_h4_train = gate4.to_pipeline_h4(h4_train)
        bh_start_train, bh_end_train = gate4._bh_closes(
            df_h4_train, gate4.TRAIN_START, gate4.TRAIN_END
        )

        grid = gate4.run_grid(
            instrument=instrument,
            df_h4=df_h4_train,
            close_d1=d1_train,
            period_start=gate4.TRAIN_START,
            period_end=gate4.TRAIN_END,
            bh_close_start=bh_start_train,
            bh_close_end=bh_end_train,
        )
        train_grids[instrument] = grid

        grid_export = {
            f"tol={tol}_sl={slb}": gate4._result_to_dict(r)
            for (tol, slb), r in grid.items()
        }
        (out_dir / f"train_grid_{instrument}.json").write_text(
            json.dumps(grid_export, indent=2, default=str)
        )

        best, reason = gate4.select_best_cell(grid)
        selected_params[instrument] = {"params": best, "reason": reason}
        if best is None:
            print(f"  ⚠️ no train cell selected — {reason}", flush=True)
            verdicts[instrument] = ("ARCHIVE", 0)
            hypotheses[instrument] = {}
            continue
        print(f"  Selected: tol={best[0]}, sl={best[1]} — {reason}", flush=True)

        # Holdout
        print(
            f"\n##### {instrument} — holdout "
            f"({gate4.HOLDOUT_START.date()} → {gate4.HOLDOUT_END.date()}) #####",
            flush=True,
        )
        m5_hold = gate4.load_duk_m5(instrument, gate4.HOLDOUT_START, gate4.HOLDOUT_END)
        h4_hold = gate4.resample_m5_to_h4(m5_hold)
        d1_hold = gate4.resample_m5_to_d1_close(m5_hold)
        df_h4_hold = gate4.to_pipeline_h4(h4_hold)
        bh_start_hold, bh_end_hold = gate4._bh_closes(
            df_h4_hold, gate4.HOLDOUT_START, gate4.HOLDOUT_END
        )
        result, _ = gate4.run_grid_cell(
            instrument=instrument,
            df_h4=df_h4_hold,
            close_d1=d1_hold,
            retest_tolerance=best[0],
            sl_buffer=best[1],
            max_risk_distance=NEW_MAX_RISK_DISTANCE[instrument],
            period_start=gate4.HOLDOUT_START,
            period_end=gate4.HOLDOUT_END,
            bh_close_start=bh_start_hold,
            bh_close_end=bh_end_hold,
        )
        holdouts[instrument] = result
        result.to_json(out_dir / f"holdout_{instrument}.json")
        print(
            f"  holdout: n={result.n_setups} mean_r={result.mean_r:+.3f} "
            f"CI=[{result.mean_r_ci_95[0]:+.3f}, {result.mean_r_ci_95[1]:+.3f}] "
            f"setups/mo={result.setups_per_month:.2f} "
            f"proj={result.projected_annual_return_pct:+.1f}%",
            flush=True,
        )

        h10s[instrument] = None  # H10 skipped for portfolio expansion
        eval_h = gate4.evaluate_hypotheses(
            instrument=instrument,
            holdout=result,
            h10=None,
        )
        hypotheses[instrument] = eval_h
        verdicts[instrument] = gate4.verdict_from_hypotheses(eval_h)
        print(
            f"  Verdict: {verdicts[instrument][0]} "
            f"({verdicts[instrument][1]} hypotheses PASS)",
            flush=True,
        )

    wallclock = time.perf_counter() - t_start
    report_path = gate4.write_report(
        out_dir=out_dir,
        train_grids=train_grids,
        selected_params=selected_params,
        holdouts=holdouts,
        hypotheses=hypotheses,
        verdicts=verdicts,
        h10s=h10s,
        wallclock_s=wallclock,
    )
    print(f"\nReport: {report_path}")
    print(f"Total wallclock: {wallclock:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
