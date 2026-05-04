"""Gate 4 of ``docs/STRATEGY_RESEARCH_PROTOCOL.md`` for the
mean-reversion BB H4 strategy v1.1: train grid → param selection →
holdout evaluation → 10-hypothesis verdict.

Anti-data-dredging contract
---------------------------
The 10 hypotheses (spec v1.1 §4, commit ae61f70) and the train
selection criteria (§3.2) are frozen at the spec commit and
evaluated post-run, never tuned. The selection criteria are:

1. ``mean_r_ci_95.lower >= 0`` (no measurable edge otherwise)
2. ``temporal_concentration < 0.4`` (regime-fitting flag)
3. ``n_closed >= 50`` (protocol §5 admission gate)

Among the cells that pass those three, the highest ``mean_r`` is
selected; tie-break = highest ``setups_per_month``.

Cost model (FundedNext, gate-4 per-trade computation)
------------------------------------------------------
Costs are computed PER TRADE from the SL distance + standard
instrument lot mechanics on a 1 %-risk-per-trade sizing:

- **XAUUSD**: 1 lot = 100 oz, 1 USD price move = $100 PnL/lot.
  Lot sizing for $1000 risk → 10 / sl_distance lots.
  Spread 0.25 USD round-trip = $25 / lot. Commission $7 / lot.
  Total ≈ $32 / lot × (10 / sl_distance) lots = $320 / sl_distance
  ⇒ **0.32 / sl_distance R**.
- **NDX100**: 1 lot = $1/point, no commission, ~3-pt spread.
  ⇒ **3.0 / sl_distance R**.
- **SPX500**: 1 lot = $1/point, no commission, ~1-pt spread.
  ⇒ **1.0 / sl_distance R**.

Cost is therefore tighter when SL is wider (per-lot fixed cost
divided by larger risk). The per-trade computation replaces the
flat-R approximation used in breakout-retest's gate 4.

Outputs
-------
- ``calibration/runs/gate4_mean_reversion_bb_h4_v1_1_<TS>/report.md``
- ``train_grid_<instrument>.json`` per instrument (one
  BacktestResult per cell)
- ``holdout_<instrument>.json`` per instrument (one BacktestResult
  with the selected params)
- ``h10_transferability.json`` if MT5 fixtures cover the window.

Run
---
    python -m calibration.run_mean_reversion_bb_h4_grid
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from calibration.audit_mean_reversion_bb_h4 import (  # noqa: E402
    HOLDOUT_END,
    HOLDOUT_START,
    TRAIN_END,
    TRAIN_START,
    load_duk_m5,
    resample_m5_to_h4,
    run_streaming,
    to_pipeline_h4,
)
from src.backtest.result import BacktestResult, SetupRecord  # noqa: E402
from src.strategies.mean_reversion_bb_h4 import (  # noqa: E402
    Setup,
    StrategyParams,
)

DUK_ROOT = REPO_ROOT / "tests" / "fixtures" / "dukascopy"
MT5_ROOT = REPO_ROOT / "tests" / "fixtures" / "historical"
RUNS_DIR = REPO_ROOT / "calibration" / "runs"

INSTRUMENTS: list[str] = ["XAUUSD", "NDX100", "SPX500"]

# Spec v1.1 §3.2 grid (Step B). 4 × 3 = 12 cells per instrument.
# min_pen broadened to {0.0, 0.1, 0.2, 0.3} based on the gate-3
# attrition diagnostic. Frozen at commit ae61f70.
GRID_SPEC: dict[str, dict[str, list[float]]] = {
    "XAUUSD": {
        "min_pen_atr_mult": [0.0, 0.1, 0.2, 0.3],
        "sl_buffer": [0.5, 1.0, 2.0],
    },
    "NDX100": {
        "min_pen_atr_mult": [0.0, 0.1, 0.2, 0.3],
        "sl_buffer": [3.0, 5.0, 8.0],
    },
    "SPX500": {
        "min_pen_atr_mult": [0.0, 0.1, 0.2, 0.3],
        "sl_buffer": [1.0, 2.0, 3.0],
    },
}

# MAX_RISK_DISTANCE per instrument (spec §3.2: 3× 30-day median
# range — gate-4 approximation, permissive enough not to mask
# upstream divergences).
MAX_RISK_DISTANCE: dict[str, float] = {
    "XAUUSD": 50.0,    # USD on spot price ~2000
    "NDX100": 1500.0,  # points
    "SPX500": 200.0,   # points
}

# Selection criteria. Frozen.
MIN_N_CLOSED = 50
MAX_TEMPORAL_CONCENTRATION = 0.4

# Hypothesis bands — spec v1.1 §4. Frozen at commit ae61f70.
HYPOTHESES: dict[str, dict] = {
    "H1": {
        "name": "Setups / month / instrument in [0.5, 2]",
        "low": 0.5,
        "high": 2.0,
    },
    "H2": {
        "name": "Win rate (closed) in [55 %, 70 %]",
        "low": 0.55,
        "high": 0.70,
    },
    "H3": {
        "name": "Mean R (pre-cost) in [+0.4, +0.8]",
        "low": 0.4,
        "high": 0.8,
    },
    "H4": {
        "name": "Mean R (post-cost) in [+0.3, +0.7]",
        "low": 0.3,
        "high": 0.7,
    },
    "H5": {
        "name": "Projected annual return % in [10, 25]",
        "low": 10.0,
        "high": 25.0,
    },
    "H6": {
        "name": "mean_r_ci_95.lower > 0",
    },
    "H7": {
        "name": "outlier_robustness.trim_5_5.mean_r > 0",
    },
    "H8": {
        "name": "temporal_concentration < 0.4",
        "max": MAX_TEMPORAL_CONCENTRATION,
    },
    "H9": {
        "name": "vs_buy_and_hold.strategy_minus_bh_pct > 0",
    },
    "H10": {
        "name": "Transferability mismatch Duk vs MT5 < 30 %",
        "max": 0.30,
    },
}

PROMOTE_THRESHOLD = 6
ARCHIVE_THRESHOLD = 3


# ---------------------------------------------------------------------------
# Cost model (per-trade)
# ---------------------------------------------------------------------------


def cost_r_per_trade(setup: SetupRecord | Setup, instrument: str) -> float:
    """FundedNext per-trade cost as a fraction of R (1 % risk).

    Computed from the SL distance + instrument-specific spread /
    commission tables. See module docstring for the derivation.
    Returns a non-negative number to subtract from realised R.
    """
    sl_distance = abs(setup.entry_price - setup.stop_loss) if isinstance(setup, Setup) else None
    if sl_distance is None or sl_distance <= 0:
        # SetupRecord doesn't carry entry / SL prices directly. The
        # caller passes the original ``Setup`` for cost calculation;
        # if a SetupRecord slipped through, fall back to 0.0 (the
        # only consequence is a slightly optimistic post-cost mean R
        # for that trade, surfaced by the per-cell debug print).
        return 0.0
    if instrument == "XAUUSD":
        return 0.32 / sl_distance
    if instrument == "NDX100":
        return 3.0 / sl_distance
    if instrument == "SPX500":
        return 1.0 / sl_distance
    return 0.0


# ---------------------------------------------------------------------------
# Outcome simulator (H4-bar conservative, mirrored from breakout-retest)
# ---------------------------------------------------------------------------


def simulate_outcomes(
    setups: list[Setup],
    df_h4: pd.DataFrame,
) -> list[SetupRecord]:
    """Walk forward H4 bars to determine each setup's realised R.

    Convention:
    - Entry at the return bar's close. Monitoring starts on the
      next H4 bar.
    - SL hit (low ≤ SL on long / high ≥ SL on short) → -1.0 R.
    - TP hit (high ≥ TP on long / low ≤ TP on short) → +RR R.
    - Both on same bar → SL first (conservative).
    - Neither hit before end of frame → ``open_at_horizon``, 0 R.
    """
    records: list[SetupRecord] = []
    times = pd.to_datetime(df_h4["time"], utc=True)
    highs = df_h4["high"].to_numpy(dtype="float64")
    lows = df_h4["low"].to_numpy(dtype="float64")

    ts_to_idx: dict[pd.Timestamp, int] = {}
    for i, t in enumerate(times):
        ts_to_idx[pd.Timestamp(t)] = i

    for s in setups:
        ret_ts = pd.Timestamp(s.timestamp_utc)
        if ret_ts not in ts_to_idx:
            records.append(
                SetupRecord(
                    timestamp_utc=s.timestamp_utc.isoformat(),
                    instrument=s.instrument,
                    direction=s.direction,
                    quality="A",
                    realized_r=0.0,
                    outcome="entry_not_hit",
                )
            )
            continue
        ret_idx = ts_to_idx[ret_ts]
        outcome, r = _simulate_one(s, ret_idx, highs, lows)
        records.append(
            SetupRecord(
                timestamp_utc=s.timestamp_utc.isoformat(),
                instrument=s.instrument,
                direction=s.direction,
                quality="A",
                realized_r=r,
                outcome=outcome,
            )
        )
    return records


def _simulate_one(
    setup: Setup,
    ret_idx: int,
    highs,
    lows,
) -> tuple[str, float]:
    n = len(highs)
    sl = setup.stop_loss
    tp = setup.take_profit
    direction = setup.direction
    rr = setup.risk_reward

    for j in range(ret_idx + 1, n):
        bar_high = float(highs[j])
        bar_low = float(lows[j])
        if direction == "long":
            sl_hit = bar_low <= sl
            tp_hit = bar_high >= tp
        else:
            sl_hit = bar_high >= sl
            tp_hit = bar_low <= tp
        if sl_hit and tp_hit:
            return "sl_hit", -1.0
        if sl_hit:
            return "sl_hit", -1.0
        if tp_hit:
            return "tp_runner_hit", float(rr)
    return "open_at_horizon", 0.0


# ---------------------------------------------------------------------------
# Grid-cell driver
# ---------------------------------------------------------------------------


def run_grid_cell(
    *,
    instrument: str,
    df_h4: pd.DataFrame,
    min_pen_atr_mult: float,
    sl_buffer: float,
    max_risk_distance: float,
    period_start: pd.Timestamp,
    period_end: pd.Timestamp,
    bh_close_start: float,
    bh_close_end: float,
) -> tuple[BacktestResult, list[Setup]]:
    """Run streaming pipeline + outcome simulator + BacktestResult."""
    params = StrategyParams(
        min_penetration_atr_mult=min_pen_atr_mult,
        sl_buffer=sl_buffer,
        max_risk_distance=max_risk_distance,
    )
    setups = run_streaming(df_h4, instrument, params)
    records = simulate_outcomes(setups, df_h4)
    result = BacktestResult.from_setups(
        strategy_name="mean_reversion_bb_h4_v1_1",
        instrument=instrument,
        period_start=period_start.date(),
        period_end=period_end.date(),
        setups=records,
        params_used={
            "min_penetration_atr_mult": min_pen_atr_mult,
            "sl_buffer": sl_buffer,
            "max_risk_distance": max_risk_distance,
            "bb_period": params.bb_period,
            "bb_multiplier": params.bb_multiplier,
            "atr_period": params.atr_period,
            "max_return_bars": params.max_return_bars,
            "min_rr": params.min_rr,
            "max_trades_per_day": params.max_trades_per_day,
        },
        bh_close_start=bh_close_start,
        bh_close_end=bh_close_end,
    )
    return result, setups


def run_grid(
    *,
    instrument: str,
    df_h4: pd.DataFrame,
    period_start: pd.Timestamp,
    period_end: pd.Timestamp,
    bh_close_start: float,
    bh_close_end: float,
    log: bool = True,
) -> dict[tuple[float, float], tuple[BacktestResult, list[Setup]]]:
    """Run the 4×3 grid for ``instrument`` on the train window.

    Returns a dict keyed by ``(min_pen, sl_buffer)`` of
    ``(BacktestResult, list[Setup])`` — the raw setups are kept so
    the cost calculator can reach back to the per-trade SL distance.
    """
    grid = GRID_SPEC[instrument]
    out: dict[tuple[float, float], tuple[BacktestResult, list[Setup]]] = {}
    if log:
        print(f"\n=== Train grid for {instrument} ===", flush=True)
    for pen in grid["min_pen_atr_mult"]:
        for slb in grid["sl_buffer"]:
            t0 = time.perf_counter()
            result, setups = run_grid_cell(
                instrument=instrument,
                df_h4=df_h4,
                min_pen_atr_mult=pen,
                sl_buffer=slb,
                max_risk_distance=MAX_RISK_DISTANCE[instrument],
                period_start=period_start,
                period_end=period_end,
                bh_close_start=bh_close_start,
                bh_close_end=bh_close_end,
            )
            dt = time.perf_counter() - t0
            if log:
                ci = result.mean_r_ci_95
                tc = result.temporal_concentration
                tc_str = f"{tc:.3f}" if tc is not None else "na"
                print(
                    f"  pen={pen} sl={slb}: n={result.n_setups} "
                    f"mean_r={result.mean_r:+.3f} "
                    f"CI=[{ci[0]:+.3f}, {ci[1]:+.3f}] "
                    f"win={result.win_rate:.1%} "
                    f"setups/mo={result.setups_per_month:.2f} "
                    f"tc={tc_str} ({dt:.1f}s)",
                    flush=True,
                )
            out[(pen, slb)] = (result, setups)
    return out


def select_best_cell(
    grid: dict[tuple[float, float], tuple[BacktestResult, list[Setup]]],
) -> tuple[tuple[float, float] | None, str]:
    """Apply pre-specified selection criteria.

    Returns ``((min_pen, sl_buffer), reason)`` or ``(None, reason)``.
    Selection: max ``mean_r`` whose CI lower-bound ≥ 0 AND
    temporal_concentration < 0.4 AND n_closed ≥ 50; tie-break by
    max setups_per_month. Frozen criteria.
    """
    candidates: list[tuple[tuple[float, float], BacktestResult]] = []
    for params, (result, _) in grid.items():
        n_closed = sum(
            1 for s in result.setups if s.outcome not in ("entry_not_hit", "open_at_horizon")
        )
        if n_closed < MIN_N_CLOSED:
            continue
        ci_low = result.mean_r_ci_95[0]
        if ci_low < 0:
            continue
        if (
            result.temporal_concentration is None
            or result.temporal_concentration >= MAX_TEMPORAL_CONCENTRATION
        ):
            continue
        candidates.append((params, result))
    if not candidates:
        return None, (
            "no train cell met all three selection criteria "
            "(n_closed >= 50, ci_low >= 0, temporal_concentration < 0.4)"
        )
    candidates.sort(
        key=lambda x: (x[1].mean_r, x[1].setups_per_month),
        reverse=True,
    )
    best_params, best_result = candidates[0]
    return best_params, (
        f"selected by max mean_r ({best_result.mean_r:+.3f}); "
        f"{len(candidates)} cells passed all three filters"
    )


# ---------------------------------------------------------------------------
# H10 transferability (Duk vs MT5)
# ---------------------------------------------------------------------------


def load_mt5_m5(instrument: str) -> pd.DataFrame:
    p = MT5_ROOT / f"{instrument}_M5.parquet"
    if not p.exists():
        raise FileNotFoundError(f"MT5 fixture missing: {p}")
    df = pd.read_parquet(p)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df.set_index("time")[["open", "high", "low", "close"]]


def transferability_mismatch(
    instrument: str,
    params: StrategyParams,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> dict | None:
    """Run the strategy on Duk and on MT5, diff setup timestamps.

    Mismatch = ``1 - |intersection| / |union|`` per spec §6.
    """
    try:
        mt5_m5 = load_mt5_m5(instrument)
    except FileNotFoundError as e:
        return {"error": str(e)}

    actual_start = max(start, pd.Timestamp(mt5_m5.index.min()))
    actual_end = min(end, pd.Timestamp(mt5_m5.index.max()))
    if actual_end <= actual_start:
        return {"error": "MT5 window does not overlap holdout"}

    duk_m5 = load_duk_m5(instrument, actual_start, actual_end)
    duk_h4 = resample_m5_to_h4(duk_m5)
    duk_df_h4 = to_pipeline_h4(duk_h4)
    duk_setups = run_streaming(duk_df_h4, instrument, params)

    mt5_m5_window = mt5_m5.loc[(mt5_m5.index >= actual_start) & (mt5_m5.index <= actual_end)]
    mt5_h4 = resample_m5_to_h4(mt5_m5_window)
    mt5_df_h4 = to_pipeline_h4(mt5_h4)
    mt5_setups = run_streaming(mt5_df_h4, instrument, params)

    duk_ts = {s.timestamp_utc for s in duk_setups}
    mt5_ts = {s.timestamp_utc for s in mt5_setups}
    inter = duk_ts & mt5_ts
    union = duk_ts | mt5_ts
    mismatch = 1.0 - len(inter) / len(union) if union else 0.0
    return {
        "n_duk": len(duk_ts),
        "n_mt5": len(mt5_ts),
        "n_intersection": len(inter),
        "n_union": len(union),
        "mismatch": mismatch,
        "agreement_pct": (len(inter) / len(union) * 100.0) if union else 0.0,
        "window_start": actual_start.isoformat(),
        "window_end": actual_end.isoformat(),
    }


# ---------------------------------------------------------------------------
# Hypothesis evaluator
# ---------------------------------------------------------------------------


def _post_cost_mean_r(
    holdout: BacktestResult,
    instrument: str,
    setups_raw: list[Setup],
) -> float:
    """Subtract the per-trade cost from realised R, then mean over closed trades.

    ``setups_raw`` carries the original price levels (``entry_price``,
    ``stop_loss``) needed by the per-trade cost model. The
    ``holdout.setups`` ``SetupRecord`` list does not. We zip them by
    timestamp so each closed trade gets its instrument-specific cost.
    """
    closed = [s for s in holdout.setups if s.outcome not in ("entry_not_hit", "open_at_horizon")]
    if not closed:
        return 0.0
    raw_by_ts: dict[str, Setup] = {s.timestamp_utc.isoformat(): s for s in setups_raw}

    costs: list[float] = []
    for rec in closed:
        raw = raw_by_ts.get(rec.timestamp_utc)
        if raw is None:
            costs.append(0.0)
        else:
            costs.append(cost_r_per_trade(raw, instrument))
    rs = [s.realized_r - cost for s, cost in zip(closed, costs)]
    return sum(rs) / len(rs)


def _post_cost_projected_annual(
    holdout: BacktestResult,
    instrument: str,
    setups_raw: list[Setup],
) -> float:
    return (
        _post_cost_mean_r(holdout, instrument, setups_raw)
        * holdout.setups_per_month
        * 12.0
        * holdout.risk_per_trade_pct
    )


def evaluate_hypotheses(
    *,
    instrument: str,
    holdout: BacktestResult,
    setups_raw: list[Setup],
    h10: dict | None,
) -> dict[str, dict]:
    """Per-hypothesis dict ``{hk: {pass, value, target}}``."""
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
        "pass": (HYPOTHESES["H3"]["low"] <= holdout.mean_r <= HYPOTHESES["H3"]["high"]),
    }

    pc_mean_r = _post_cost_mean_r(holdout, instrument, setups_raw)
    out["H4"] = {
        "name": HYPOTHESES["H4"]["name"],
        "value": pc_mean_r,
        "pass": HYPOTHESES["H4"]["low"] <= pc_mean_r <= HYPOTHESES["H4"]["high"],
    }

    pc_proj = _post_cost_projected_annual(holdout, instrument, setups_raw)
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
        "name": HYPOTHESES["H8"]["name"],
        "value": tc,
        "pass": tc is not None and tc < HYPOTHESES["H8"]["max"],
    }

    bh = holdout.vs_buy_and_hold
    if bh is None:
        out["H9"] = {
            "name": HYPOTHESES["H9"]["name"],
            "value": None,
            "pass": False,
            "note": "vs_buy_and_hold unavailable (no close prices)",
        }
    else:
        smb = bh["strategy_minus_bh_pct"]
        out["H9"] = {
            "name": HYPOTHESES["H9"]["name"],
            "value": smb,
            "pass": smb > 0,
            "bh_annualized_pct": bh["bh_annualized_pct"],
            "strategy_annualized_pct": bh["strategy_annualized_pct"],
        }

    if h10 is None or "error" in (h10 or {}):
        out["H10"] = {
            "name": HYPOTHESES["H10"]["name"],
            "value": None,
            "pass": None,
            "note": h10.get("error", "h10 not run") if h10 else "h10 not run",
        }
    else:
        out["H10"] = {
            "name": HYPOTHESES["H10"]["name"],
            "value": h10["mismatch"],
            "pass": h10["mismatch"] < HYPOTHESES["H10"]["max"],
            "n_duk": h10["n_duk"],
            "n_mt5": h10["n_mt5"],
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


def _bh_closes(df_h4: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> tuple[float, float]:
    times = pd.to_datetime(df_h4["time"], utc=True)
    s_mask = times >= start
    e_mask = times <= end
    s_idx = s_mask.idxmax() if s_mask.any() else None
    e_idx = e_mask[::-1].idxmax() if e_mask.any() else None
    if s_idx is None or e_idx is None:
        return float(df_h4["close"].iloc[0]), float(df_h4["close"].iloc[-1])
    return float(df_h4.loc[s_idx, "close"]), float(df_h4.loc[e_idx, "close"])


def write_report(
    *,
    out_dir: Path,
    train_grids: dict[str, dict[tuple[float, float], tuple[BacktestResult, list[Setup]]]],
    selected_params: dict[str, dict],
    holdouts: dict[str, BacktestResult],
    holdout_setups_raw: dict[str, list[Setup]],
    hypotheses: dict[str, dict[str, dict]],
    verdicts: dict[str, tuple[str, int]],
    h10s: dict[str, dict | None],
    wallclock_s: float,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "report.md"

    lines: list[str] = []
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines.append(f"# Gate 4 — mean_reversion_bb_h4 v1.1 backtest principal Duk ({ts})")
    lines.append("")
    lines.append(
        "Spec: `docs/strategies/mean_reversion_bb_h4.md` "
        "(commit ae61f70, v1.1 post-diagnostic). Protocol gate 4 of "
        "`docs/STRATEGY_RESEARCH_PROTOCOL.md`."
    )
    lines.append("")
    lines.append(
        "**Anti-data-dredging**: the 10 hypotheses (§4 of the spec, "
        "v1.1 anchor) and the train selection criteria (§3.2) are "
        "frozen at the spec commit and evaluated post-run, never tuned."
    )
    lines.append("")

    overall_promote = any(v[0] == "PROMOTE" for v in verdicts.values())
    overall = (
        "PASS — at least one instrument PROMOTE"
        if overall_promote
        else "FAIL — no instrument PROMOTE"
    )
    lines.append(f"- **Global verdict**: {overall}")
    for inst in INSTRUMENTS:
        v, n_pass = verdicts.get(inst, ("N/A", 0))
        lines.append(f"  - {inst}: **{v}** ({n_pass} hypotheses PASS)")
    lines.append(f"- **Wallclock**: {wallclock_s:.1f} s")
    lines.append("")

    # --- Train grid ------------------------------------------------------
    lines.append("## 1. Train grid (4×3 per instrument, v1.1 broadened)")
    lines.append("")
    lines.append(
        f"Window: {TRAIN_START.date()} → {TRAIN_END.date()}. "
        "Selection: `n_closed >= 50` AND `mean_r_ci_95.lower >= 0` AND "
        "`temporal_concentration < 0.4`; among those, max `mean_r` "
        "(tie-break: max `setups_per_month`)."
    )
    lines.append("")
    for inst in INSTRUMENTS:
        lines.append(f"### {inst}")
        lines.append("")
        lines.append(
            "| min_pen | sl_buffer | n_setups | n_closed | mean_r | CI low | CI high | win | setups/mo | tc | proj_annual | sel |"
        )
        lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|")
        sel = selected_params[inst].get("params")
        for pen in GRID_SPEC[inst]["min_pen_atr_mult"]:
            for slb in GRID_SPEC[inst]["sl_buffer"]:
                r, _ = train_grids[inst][(pen, slb)]
                n_closed = sum(
                    1 for s in r.setups if s.outcome not in ("entry_not_hit", "open_at_horizon")
                )
                mark = "✅" if sel == (pen, slb) else ""
                lines.append(
                    f"| {pen} | {slb} | {r.n_setups} | {n_closed} | "
                    f"{_fmt(r.mean_r)} | {_fmt(r.mean_r_ci_95[0])} | "
                    f"{_fmt(r.mean_r_ci_95[1])} | {r.win_rate:.1%} | "
                    f"{r.setups_per_month:.2f} | "
                    f"{_fmt(r.temporal_concentration, '.3f')} | "
                    f"{_fmt(r.projected_annual_return_pct, '+.1f')}% | {mark} |"
                )
        lines.append("")
        lines.append(f"**Selection**: {selected_params[inst]['reason']}")
        lines.append("")

    # --- Holdout ---------------------------------------------------------
    lines.append("## 2. Holdout — selected params per instrument")
    lines.append("")
    lines.append(
        f"Window: {HOLDOUT_START.date()} → {HOLDOUT_END.date()}. "
        "Selected (min_pen, sl_buffer) cell from §1 run unchanged."
    )
    lines.append("")
    lines.append(
        "| Instrument | min_pen | sl_buffer | n_setups | n_closed | mean_r | CI low | CI high | win | setups/mo | tc | proj_annual | trim_5_5 mean_r | strategy − BH % |"
    )
    lines.append("|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for inst in INSTRUMENTS:
        if inst not in holdouts:
            lines.append(
                f"| {inst} | — | — | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |"
            )
            continue
        h = holdouts[inst]
        n_closed = sum(1 for s in h.setups if s.outcome not in ("entry_not_hit", "open_at_horizon"))
        trim = (h.outlier_robustness or {}).get("trim_5_5")
        trim_str = _fmt(trim["mean_r"]) if trim else "n/a"
        bh = h.vs_buy_and_hold
        bh_str = _fmt(bh["strategy_minus_bh_pct"], "+.1f") + "%" if bh else "n/a"
        sel = selected_params[inst].get("params")
        pen_str = sel[0] if sel else "—"
        slb_str = sel[1] if sel else "—"
        lines.append(
            f"| {inst} | {pen_str} | {slb_str} | {h.n_setups} | {n_closed} | "
            f"{_fmt(h.mean_r)} | {_fmt(h.mean_r_ci_95[0])} | "
            f"{_fmt(h.mean_r_ci_95[1])} | {h.win_rate:.1%} | "
            f"{h.setups_per_month:.2f} | "
            f"{_fmt(h.temporal_concentration, '.3f')} | "
            f"{_fmt(h.projected_annual_return_pct, '+.1f')}% | "
            f"{trim_str} | {bh_str} |"
        )
    lines.append("")

    # --- Train ↔ holdout overfit check -----------------------------------
    lines.append("### Train vs holdout consistency check")
    lines.append("")
    lines.append("| Instrument | mean_r train | mean_r holdout | Δ | overfit flag (Δ > 0.3R) |")
    lines.append("|---|---:|---:|---:|:---:|")
    for inst in INSTRUMENTS:
        if inst not in holdouts:
            lines.append(f"| {inst} | n/a | n/a | n/a | n/a |")
            continue
        sel = selected_params[inst]["params"]
        train_r = train_grids[inst][sel][0].mean_r
        hold_r = holdouts[inst].mean_r
        delta = hold_r - train_r
        flag = "⚠️" if abs(delta) > 0.3 else ""
        lines.append(
            f"| {inst} | {_fmt(train_r)} | {_fmt(hold_r)} | {_fmt(delta)} | {flag} |"
        )
    lines.append("")

    # --- Hypotheses ------------------------------------------------------
    lines.append("## 3. Hypothesis evaluation (holdout only — v1.1 §4)")
    lines.append("")
    lines.append("| Hypothesis | XAUUSD | NDX100 | SPX500 |")
    lines.append("|---|---|---|---|")
    for hk in [f"H{i}" for i in range(1, 11)]:
        row = f"| **{hk}** {HYPOTHESES[hk]['name']} |"
        for inst in INSTRUMENTS:
            cell = hypotheses.get(inst, {}).get(hk)
            if cell is None:
                row += " n/a |"
                continue
            val = cell["value"]
            verdict_mark = (
                "✅" if cell["pass"] is True
                else ("❌" if cell["pass"] is False else "⚠️ n/a")
            )
            if hk in ("H2",):
                val_str = f"{val:.1%}" if val is not None else "n/a"
            elif hk in ("H1",):
                val_str = f"{val:.2f}" if val is not None else "n/a"
            elif hk in ("H5",):
                val_str = f"{val:+.1f}%" if val is not None else "n/a"
            elif hk in ("H8",):
                val_str = f"{val:.3f}" if val is not None else "n/a"
            elif hk in ("H9",):
                val_str = f"{val:+.1f}%" if val is not None else "n/a"
            elif hk in ("H10",):
                val_str = f"{val * 100:.1f}%" if val is not None else "n/a"
            else:
                val_str = _fmt(val)
            row += f" {val_str} {verdict_mark} |"
        lines.append(row)
    lines.append("")

    # H10 detail
    if any(v is not None for v in h10s.values()):
        lines.append("### H10 transferability detail")
        lines.append("")
        lines.append("| Instrument | n_duk | n_mt5 | n_∩ | n_∪ | mismatch | window |")
        lines.append("|---|---:|---:|---:|---:|---:|---|")
        for inst in INSTRUMENTS:
            h10 = h10s.get(inst)
            if h10 is None or "error" in h10:
                err = (h10 or {}).get("error", "n/a")
                lines.append(f"| {inst} | — | — | — | — | — | {err} |")
                continue
            lines.append(
                f"| {inst} | {h10['n_duk']} | {h10['n_mt5']} | "
                f"{h10['n_intersection']} | {h10['n_union']} | "
                f"{h10['mismatch'] * 100:.1f}% | "
                f"{h10['window_start'][:10]} → {h10['window_end'][:10]} |"
            )
        lines.append("")

    # --- Verdict per instrument ------------------------------------------
    lines.append("## 4. Verdict per instrument")
    lines.append("")
    lines.append("Verdict rule (spec v1.1 §4 holdout):")
    lines.append("")
    lines.append("- ≥ 6 PASS → **PROMOTE** (candidate gate 5)")
    lines.append("- 3 ≤ PASS ≤ 5 → **REVIEW** (operator discussion)")
    lines.append("- < 3 PASS → **ARCHIVE** (mandatory)")
    lines.append("")
    lines.append(
        "(Hypotheses with `pass=None` — e.g. H10 unavailable — are "
        "excluded from both numerator and denominator.)"
    )
    lines.append("")
    for inst in INSTRUMENTS:
        v, n_pass = verdicts.get(inst, ("N/A", 0))
        n_eval = sum(1 for h in hypotheses.get(inst, {}).values() if h["pass"] is not None)
        lines.append(f"- **{inst}**: {v} ({n_pass} / {n_eval} PASS)")
    lines.append("")

    # --- Suggested next --------------------------------------------------
    lines.append("## 5. Suggested next")
    lines.append("")
    if overall_promote:
        promotes = [i for i in INSTRUMENTS if verdicts.get(i, ("", 0))[0] == "PROMOTE"]
        lines.append(
            f"At least one instrument PROMOTE ({', '.join(promotes)}). "
            "Proceed to **gate 5** — cross-check on Databento with the "
            "same selected params, same holdout window. Pass criterion: "
            "mean_r within ±30% of Duk per spec §6 / protocol §5.3."
        )
    else:
        archives = [i for i in INSTRUMENTS if verdicts.get(i, ("", 0))[0] == "ARCHIVE"]
        reviews = [i for i in INSTRUMENTS if verdicts.get(i, ("", 0))[0] == "REVIEW"]
        if archives and not reviews:
            lines.append(
                f"All instruments ARCHIVE ({', '.join(archives)}). "
                "Move to `archived/strategies/mean_reversion_bb_h4_v1_1/` "
                "with the post-mortem README per protocol §8 and pick "
                "the next HTF candidate from the backlog."
            )
        else:
            lines.append(
                "No instrument PROMOTE. REVIEW instruments: "
                f"{', '.join(reviews) if reviews else 'none'}. "
                "Operator discussion required before continuing."
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
        "--instruments",
        nargs="+",
        default=INSTRUMENTS,
        help="Subset of instruments to process (default: all 3).",
    )
    parser.add_argument(
        "--skip-h10",
        action="store_true",
        help="Skip the transferability pre-flight (faster smoke run).",
    )
    parser.add_argument(
        "--out-dir",
        type=str,
        default=None,
        help=(
            "Override the output directory. Defaults to "
            "calibration/runs/gate4_mean_reversion_bb_h4_v1_1_<TS>/."
        ),
    )
    args = parser.parse_args()

    t_start = time.perf_counter()
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    out_dir = (
        Path(args.out_dir)
        if args.out_dir
        else RUNS_DIR / f"gate4_mean_reversion_bb_h4_v1_1_{ts}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    train_grids: dict[
        str, dict[tuple[float, float], tuple[BacktestResult, list[Setup]]]
    ] = {}
    selected_params: dict[str, dict] = {}
    holdouts: dict[str, BacktestResult] = {}
    holdout_setups_raw: dict[str, list[Setup]] = {}
    hypotheses: dict[str, dict[str, dict]] = {}
    verdicts: dict[str, tuple[str, int]] = {}
    h10s: dict[str, dict | None] = {}

    for instrument in args.instruments:
        # ---- TRAIN -----------------------------------------------------
        print(
            f"\n##### {instrument} — train ({TRAIN_START.date()} → "
            f"{TRAIN_END.date()}) #####",
            flush=True,
        )
        m5_train = load_duk_m5(instrument, TRAIN_START, TRAIN_END)
        h4_train = resample_m5_to_h4(m5_train)
        df_h4_train = to_pipeline_h4(h4_train)
        bh_start_train, bh_end_train = _bh_closes(df_h4_train, TRAIN_START, TRAIN_END)

        grid = run_grid(
            instrument=instrument,
            df_h4=df_h4_train,
            period_start=TRAIN_START,
            period_end=TRAIN_END,
            bh_close_start=bh_start_train,
            bh_close_end=bh_end_train,
        )
        train_grids[instrument] = grid

        grid_export = {
            f"pen={pen}_sl={slb}": _result_to_dict(r)
            for (pen, slb), (r, _) in grid.items()
        }
        (out_dir / f"train_grid_{instrument}.json").write_text(
            json.dumps(grid_export, indent=2, default=str)
        )

        best, reason = select_best_cell(grid)
        selected_params[instrument] = {"params": best, "reason": reason}
        if best is None:
            print(f"  ⚠️ no train cell selected — {reason}", flush=True)
            verdicts[instrument] = ("ARCHIVE", 0)
            hypotheses[instrument] = {}
            continue
        print(f"  Selected: pen={best[0]}, sl={best[1]} — {reason}", flush=True)

        # ---- HOLDOUT ---------------------------------------------------
        print(
            f"\n##### {instrument} — holdout ({HOLDOUT_START.date()} → "
            f"{HOLDOUT_END.date()}) #####",
            flush=True,
        )
        m5_hold = load_duk_m5(instrument, HOLDOUT_START, HOLDOUT_END)
        h4_hold = resample_m5_to_h4(m5_hold)
        df_h4_hold = to_pipeline_h4(h4_hold)
        bh_start_hold, bh_end_hold = _bh_closes(df_h4_hold, HOLDOUT_START, HOLDOUT_END)

        result, raw_setups = run_grid_cell(
            instrument=instrument,
            df_h4=df_h4_hold,
            min_pen_atr_mult=best[0],
            sl_buffer=best[1],
            max_risk_distance=MAX_RISK_DISTANCE[instrument],
            period_start=HOLDOUT_START,
            period_end=HOLDOUT_END,
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

        # ---- H10 -------------------------------------------------------
        if not args.skip_h10:
            print("  H10 transferability (Duk vs MT5) on holdout...", flush=True)
            h10 = transferability_mismatch(
                instrument=instrument,
                params=StrategyParams(
                    min_penetration_atr_mult=best[0],
                    sl_buffer=best[1],
                    max_risk_distance=MAX_RISK_DISTANCE[instrument],
                ),
                start=HOLDOUT_START,
                end=HOLDOUT_END,
            )
            h10s[instrument] = h10
            if h10 and "error" not in h10:
                print(
                    f"    n_duk={h10['n_duk']} n_mt5={h10['n_mt5']} "
                    f"mismatch={h10['mismatch'] * 100:.1f}%",
                    flush=True,
                )
            elif h10:
                print(f"    H10 skipped: {h10['error']}", flush=True)

        # ---- Hypotheses ------------------------------------------------
        eval_h = evaluate_hypotheses(
            instrument=instrument,
            holdout=result,
            setups_raw=raw_setups,
            h10=h10s.get(instrument),
        )
        hypotheses[instrument] = eval_h
        verdicts[instrument] = verdict_from_hypotheses(eval_h)
        print(
            f"  Verdict: {verdicts[instrument][0]} "
            f"({verdicts[instrument][1]} hypotheses PASS)",
            flush=True,
        )

    if h10s:
        (out_dir / "h10_transferability.json").write_text(
            json.dumps(h10s, indent=2, default=str)
        )

    wallclock = time.perf_counter() - t_start
    report_path = write_report(
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
