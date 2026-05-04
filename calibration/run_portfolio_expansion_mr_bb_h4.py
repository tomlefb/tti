"""Portfolio expansion test for the (archived) mean_reversion_bb_h4 v1.1 strategy.

Re-runs the gate-4 grid + holdout + 10-hypothesis evaluation against
the three out-of-portfolio instruments — EURUSD, GBPUSD, BTCUSD —
using the same selection criteria, hypothesis bands, and verdict
rule as the original gate-4 run on XAU/NDX/SPX.

Per-instrument grid + cost overrides are injected at module load
time. Cost values are placeholders; admission is gated on PRE-cost
criteria (n_closed, ci_low, temporal_concentration).

Run
---
    python -m calibration.run_portfolio_expansion_mr_bb_h4
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

from calibration import run_mean_reversion_bb_h4_grid as gate4  # noqa: E402

# --- Inject portfolio-expansion config -------------------------------------

NEW_INSTRUMENTS = ["EURUSD", "GBPUSD", "BTCUSD"]

NEW_GRID_SPEC: dict = {
    "EURUSD": {
        "min_pen_atr_mult": [0.0, 0.1, 0.2, 0.3],
        "sl_buffer": [0.0003, 0.0005, 0.0008],
    },
    "GBPUSD": {
        "min_pen_atr_mult": [0.0, 0.1, 0.2, 0.3],
        "sl_buffer": [0.0003, 0.0005, 0.0008],
    },
    "BTCUSD": {
        "min_pen_atr_mult": [0.0, 0.1, 0.2, 0.3],
        "sl_buffer": [50.0, 100.0, 200.0],
    },
}

NEW_MAX_RISK_DISTANCE: dict = {
    "EURUSD": 0.05,
    "GBPUSD": 0.05,
    "BTCUSD": 5000.0,
}

gate4.INSTRUMENTS = NEW_INSTRUMENTS
gate4.GRID_SPEC = NEW_GRID_SPEC
gate4.MAX_RISK_DISTANCE = NEW_MAX_RISK_DISTANCE


# Patch cost_r_per_trade to support the new instruments. Pre-cost
# admission criteria are unaffected; this only matters for H4/H5 if
# any cell promotes.
_orig_cost_r = gate4.cost_r_per_trade


def _expanded_cost_r(setup, instrument: str) -> float:
    sl_distance = (
        abs(setup.entry_price - setup.stop_loss)
        if hasattr(setup, "entry_price")
        else None
    )
    if sl_distance is None or sl_distance <= 0:
        return 0.0
    if instrument in ("EURUSD", "GBPUSD"):
        # 1 pip ≈ 0.0001 spread, scaled by lot size at 1 % risk.
        # cost = 0.0001 / sl_distance R (analogous to the XAU formula).
        return 0.0001 / sl_distance
    if instrument == "BTCUSD":
        # ~$5 spread per BTC at FundedNext, lot scaled to 1 % risk.
        return 5.0 / sl_distance
    return _orig_cost_r(setup, instrument)


gate4.cost_r_per_trade = _expanded_cost_r


def main() -> int:
    """Replicate gate4.main() but with portfolio-expansion config and out-dir."""
    t_start = time.perf_counter()
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    out_dir = (
        REPO_ROOT
        / "calibration"
        / "runs"
        / f"portfolio_expansion_mr_bb_h4_v1_1_{ts}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    train_grids: dict = {}
    selected_params: dict = {}
    holdouts: dict = {}
    holdout_setups_raw: dict = {}
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
        df_h4_train = gate4.to_pipeline_h4(h4_train)
        bh_start_train, bh_end_train = gate4._bh_closes(
            df_h4_train, gate4.TRAIN_START, gate4.TRAIN_END
        )

        grid = gate4.run_grid(
            instrument=instrument,
            df_h4=df_h4_train,
            period_start=gate4.TRAIN_START,
            period_end=gate4.TRAIN_END,
            bh_close_start=bh_start_train,
            bh_close_end=bh_end_train,
        )
        train_grids[instrument] = grid

        grid_export = {
            f"pen={pen}_sl={slb}": gate4._result_to_dict(r)
            for (pen, slb), (r, _) in grid.items()
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
        print(f"  Selected: pen={best[0]}, sl={best[1]} — {reason}", flush=True)

        # Holdout
        print(
            f"\n##### {instrument} — holdout "
            f"({gate4.HOLDOUT_START.date()} → {gate4.HOLDOUT_END.date()}) #####",
            flush=True,
        )
        m5_hold = gate4.load_duk_m5(instrument, gate4.HOLDOUT_START, gate4.HOLDOUT_END)
        h4_hold = gate4.resample_m5_to_h4(m5_hold)
        df_h4_hold = gate4.to_pipeline_h4(h4_hold)
        bh_start_hold, bh_end_hold = gate4._bh_closes(
            df_h4_hold, gate4.HOLDOUT_START, gate4.HOLDOUT_END
        )
        result, raw_setups = gate4.run_grid_cell(
            instrument=instrument,
            df_h4=df_h4_hold,
            min_pen_atr_mult=best[0],
            sl_buffer=best[1],
            max_risk_distance=NEW_MAX_RISK_DISTANCE[instrument],
            period_start=gate4.HOLDOUT_START,
            period_end=gate4.HOLDOUT_END,
            bh_close_start=bh_start_hold,
            bh_close_end=bh_end_hold,
        )
        holdouts[instrument] = result
        holdout_setups_raw[instrument] = raw_setups
        result.to_json(out_dir / f"holdout_{instrument}.json")
        print(
            f"  holdout: n={result.n_setups} mean_r={result.mean_r:+.3f} "
            f"CI=[{result.mean_r_ci_95[0]:+.3f}, {result.mean_r_ci_95[1]:+.3f}] "
            f"setups/mo={result.setups_per_month:.2f} "
            f"proj={result.projected_annual_return_pct:+.1f}%",
            flush=True,
        )

        h10s[instrument] = None
        eval_h = gate4.evaluate_hypotheses(
            instrument=instrument,
            holdout=result,
            setups_raw=raw_setups,
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
        holdout_setups_raw=holdout_setups_raw,
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
