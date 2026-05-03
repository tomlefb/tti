"""Gate 4 of ``docs/STRATEGY_RESEARCH_PROTOCOL.md`` for the
breakout-retest H4 strategy: train grid → param selection → holdout
evaluation → 10-hypothesis verdict.

Anti-data-dredging contract
---------------------------
The 10 hypotheses (spec §4) are frozen at commit ``b14e054`` and
**not** modified post-hoc, even if the grid surfaces a "more
defensible" set. The selection criteria for the train grid are
likewise pre-specified:

1. ``mean_r_ci_95.lower >= 0`` (no measurable edge otherwise)
2. ``temporal_concentration < 0.4`` (regime-fitting flag)
3. ``n_closed >= 50`` (protocol §5 admission gate)

Among the cells that pass those three, the highest ``mean_r`` is
selected; tie-break = highest ``setups_per_month``.

Cost model (FundedNext, gate-4 approximation)
---------------------------------------------
Per the user brief: flat round-trip cost as a fraction of R:

- XAUUSD: 0.05 R / trade (≈ $32 commission + spread on 1 % risk)
- NDX100: 0.03 R / trade (≈ $30 spread)
- SPX500: 0.01 R / trade (≈ $10 spread)

These are first-order approximations sufficient for H4
(post-cost mean R). A precise per-trade computation in $ from
entry price and lot size lives in gate 8 (Phase C).

Outputs
-------
- ``calibration/runs/gate4_breakout_retest_h4_<TS>/report.md``
- ``train_grid_<instrument>.json`` per instrument (one BacktestResult
  per cell)
- ``holdout_<instrument>.json`` per instrument (one BacktestResult
  with the selected params)
- ``h10_transferability.json`` if MT5 fixtures cover the window.

Run
---
    python -m calibration.run_breakout_retest_h4_grid
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

from calibration.audit_breakout_retest_h4 import (  # noqa: E402
    HOLDOUT_END,
    HOLDOUT_START,
    TRAIN_END,
    TRAIN_START,
    load_duk_m5,
    resample_m5_to_d1_close,
    resample_m5_to_h4,
    run_streaming,
    to_pipeline_h4,
)
from src.backtest.result import BacktestResult, SetupRecord  # noqa: E402
from src.strategies.breakout_retest_h4 import (  # noqa: E402
    Setup,
    StrategyParams,
)

DUK_ROOT = REPO_ROOT / "tests" / "fixtures" / "dukascopy"
MT5_ROOT = REPO_ROOT / "tests" / "fixtures" / "historical"
RUNS_DIR = REPO_ROOT / "calibration" / "runs"

INSTRUMENTS: list[str] = ["XAUUSD", "NDX100", "SPX500"]

# Spec §3.2 grid (Step B). Three values per axis, 3×3 = 9 cells per
# instrument. Anchored values, NOT post-hoc.
GRID_SPEC: dict[str, dict[str, list[float]]] = {
    "XAUUSD": {
        "retest_tolerance": [0.5, 1.0, 2.0],
        "sl_buffer": [0.3, 0.5, 1.0],
    },
    "NDX100": {
        "retest_tolerance": [3.0, 5.0, 8.0],
        "sl_buffer": [2.0, 3.0, 5.0],
    },
    "SPX500": {
        "retest_tolerance": [1.0, 2.0, 3.0],
        "sl_buffer": [0.5, 1.0, 2.0],
    },
}

# MAX_RISK_DISTANCE per instrument (spec §3.2: 3× 30-day median
# range). Picked permissive enough to NOT filter setups in normal
# market regimes but tight enough that a degenerate retest with a
# multi-day-range wick is rejected. These values are the gate-4
# approximation; gate 8 / Phase C may refine them based on the
# actual realised distribution.
MAX_RISK_DISTANCE: dict[str, float] = {
    "XAUUSD": 50.0,  # USD on spot price ~2000
    "NDX100": 1500.0,  # points
    "SPX500": 200.0,  # points
}

# Cost model in R units (round-trip). See module docstring.
COST_R_PER_TRADE: dict[str, float] = {
    "XAUUSD": 0.05,
    "NDX100": 0.03,
    "SPX500": 0.01,
}

# Selection criteria for the best train cell (spec §3.2). Frozen.
MIN_N_CLOSED = 50  # protocol §5 admission gate
MAX_TEMPORAL_CONCENTRATION = 0.4  # spec H8

# Hypothesis bands — spec §4. Frozen. Do not edit without a versioned
# spec change.
HYPOTHESES: dict[str, dict] = {
    "H1": {
        "name": "Setups / month / instrument in [1, 3]",
        "low": 1.0,
        "high": 3.0,
    },
    "H2": {
        "name": "Win rate (closed) in [40 %, 55 %]",
        "low": 0.40,
        "high": 0.55,
    },
    "H3": {
        "name": "Mean R (pre-cost) in [+0.4, +1.2]",
        "low": 0.4,
        "high": 1.2,
    },
    "H4": {
        "name": "Mean R (post-cost) in [+0.3, +1.0]",
        "low": 0.3,
        "high": 1.0,
    },
    "H5": {
        "name": "Projected annual return % in [15, 40]",
        "low": 15.0,
        "high": 40.0,
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

# Gate 4 verdict cut-offs (spec §4 verdict rule on the holdout).
PROMOTE_THRESHOLD = 6
ARCHIVE_THRESHOLD = 3


# ---------------------------------------------------------------------------
# Outcome simulator (H4-bar conservative)
# ---------------------------------------------------------------------------


def simulate_outcomes(
    setups: list[Setup],
    df_h4: pd.DataFrame,
) -> list[SetupRecord]:
    """Walk forward H4 bars to determine each setup's realised R.

    Convention (gate 4 H4-resolution):

    - Entry is filled at the retest bar's close (the same bar that
      produced the setup). Monitoring starts on the *next* H4 bar.
    - On each subsequent bar, check (in this order):
        - SL hit if ``low <= stop_loss`` (long) or ``high >= stop_loss`` (short).
        - TP hit if ``high >= take_profit`` (long) or ``low <= take_profit`` (short).
      Within a bar, **SL is checked before TP** — conservative
      assumption when both levels are inside the bar (we don't have
      tick-level resolution to disambiguate).
    - If neither hit before end of frame: ``open_at_horizon``, R=0.

    Returns:
        ``SetupRecord`` list. R is **pre-cost** (the cost model is
        applied separately in ``apply_cost``).
    """
    records: list[SetupRecord] = []
    times = pd.to_datetime(df_h4["time"], utc=True)
    highs = df_h4["high"].to_numpy(dtype="float64")
    lows = df_h4["low"].to_numpy(dtype="float64")

    # Map retest timestamp → bar index. The retest bar IS in df_h4 by
    # construction (the pipeline produced the setup from it).
    ts_to_idx: dict[pd.Timestamp, int] = {}
    for i, t in enumerate(times):
        ts_to_idx[pd.Timestamp(t)] = i

    for s in setups:
        retest_ts = pd.Timestamp(s.timestamp_utc)
        if retest_ts not in ts_to_idx:
            # Should not happen — the pipeline emitted this setup
            # from a bar in df_h4. Skip if it does, with a marker
            # outcome so the BacktestResult still reflects the count.
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
        retest_idx = ts_to_idx[retest_ts]
        outcome, r = _simulate_one(s, retest_idx, highs, lows)
        records.append(
            SetupRecord(
                timestamp_utc=s.timestamp_utc.isoformat(),
                instrument=s.instrument,
                direction=s.direction,
                # The breakout-retest v1 has no quality grading layer
                # — every emitted setup is treated as "A". The grade
                # field is preserved here for BacktestResult schema
                # compatibility only.
                quality="A",
                realized_r=r,
                outcome=outcome,
            )
        )
    return records


def _simulate_one(
    setup: Setup,
    retest_idx: int,
    highs,
    lows,
) -> tuple[str, float]:
    """Walk H4 bars after ``retest_idx`` until SL or TP hits.

    Returns ``(outcome_string, realized_R)``. R is pre-cost, in
    multiples of the trade's risk (so TP at 2 R returns 2.0 / SL
    returns -1.0).
    """
    n = len(highs)
    sl = setup.stop_loss
    tp = setup.take_profit
    direction = setup.direction
    rr = setup.risk_reward

    for j in range(retest_idx + 1, n):
        bar_high = float(highs[j])
        bar_low = float(lows[j])
        if direction == "long":
            sl_hit = bar_low <= sl
            tp_hit = bar_high >= tp
        else:
            sl_hit = bar_high >= sl
            tp_hit = bar_low <= tp
        if sl_hit and tp_hit:
            # Conservative: SL first.
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
    close_d1: pd.Series,
    retest_tolerance: float,
    sl_buffer: float,
    max_risk_distance: float,
    period_start: pd.Timestamp,
    period_end: pd.Timestamp,
    bh_close_start: float,
    bh_close_end: float,
) -> tuple[BacktestResult, list[Setup]]:
    """Run streaming pipeline + outcome simulator + BacktestResult.

    Returns the BacktestResult plus the raw Setup list (used by H10
    transferability without re-running the pipeline).
    """
    params = StrategyParams(
        retest_tolerance=retest_tolerance,
        sl_buffer=sl_buffer,
        max_risk_distance=max_risk_distance,
    )
    setups = run_streaming(df_h4, close_d1, instrument, params)
    records = simulate_outcomes(setups, df_h4)
    result = BacktestResult.from_setups(
        strategy_name="breakout_retest_h4",
        instrument=instrument,
        period_start=period_start.date(),
        period_end=period_end.date(),
        setups=records,
        params_used={
            "retest_tolerance": retest_tolerance,
            "sl_buffer": sl_buffer,
            "max_risk_distance": max_risk_distance,
            "n_swing": params.n_swing,
            "n_retest": params.n_retest,
            "rr_target": params.rr_target,
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
    close_d1: pd.Series,
    period_start: pd.Timestamp,
    period_end: pd.Timestamp,
    bh_close_start: float,
    bh_close_end: float,
    log: bool = True,
) -> dict[tuple[float, float], BacktestResult]:
    """Run the 3×3 grid for ``instrument`` on the train window."""
    grid = GRID_SPEC[instrument]
    out: dict[tuple[float, float], BacktestResult] = {}
    if log:
        print(f"\n=== Train grid for {instrument} ===", flush=True)
    for tol in grid["retest_tolerance"]:
        for slb in grid["sl_buffer"]:
            t0 = time.perf_counter()
            result, _ = run_grid_cell(
                instrument=instrument,
                df_h4=df_h4,
                close_d1=close_d1,
                retest_tolerance=tol,
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
                print(
                    f"  tol={tol} sl={slb}: n={result.n_setups} "
                    f"mean_r={result.mean_r:+.3f} CI=[{ci[0]:+.3f}, {ci[1]:+.3f}] "
                    f"win={result.win_rate:.1%} setups/mo={result.setups_per_month:.2f} "
                    f"tc={tc if tc is not None else 'na'} ({dt:.1f}s)",
                    flush=True,
                )
            out[(tol, slb)] = result
    return out


def select_best_cell(
    grid: dict[tuple[float, float], BacktestResult],
) -> tuple[tuple[float, float] | None, str]:
    """Apply pre-specified selection criteria.

    Returns ``((retest_tolerance, sl_buffer), reason)`` or
    ``(None, reason)``. ``reason`` is a short string explaining the
    selection (or the lack of one) — surfaced in the report.
    """
    candidates: list[tuple[tuple[float, float], BacktestResult]] = []
    for params, result in grid.items():
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
    # Highest mean_r, tie-break highest setups_per_month.
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
# H10 transferability
# ---------------------------------------------------------------------------


def load_mt5_m5(instrument: str) -> pd.DataFrame:
    """Load MT5 M5 fixture indexed by tz-aware ``time``."""
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

    Mismatch = ``1 - |intersection| / |union|`` per spec §6 gate 7
    convention. Same-bar match (no ±1 tolerance) — H10's threshold
    of 30 % already absorbs minor cross-source noise.
    """
    try:
        mt5_m5 = load_mt5_m5(instrument)
    except FileNotFoundError as e:
        return {"error": str(e)}

    # Common sub-window driven by MT5 coverage.
    actual_start = max(start, pd.Timestamp(mt5_m5.index.min()))
    actual_end = min(end, pd.Timestamp(mt5_m5.index.max()))
    if actual_end <= actual_start:
        return {"error": "MT5 window does not overlap holdout"}

    duk_m5 = load_duk_m5(instrument, actual_start, actual_end)
    duk_h4 = resample_m5_to_h4(duk_m5)
    duk_d1 = resample_m5_to_d1_close(duk_m5)
    duk_df_h4 = to_pipeline_h4(duk_h4)
    duk_setups = run_streaming(duk_df_h4, duk_d1, instrument, params)

    mt5_m5_window = mt5_m5.loc[(mt5_m5.index >= actual_start) & (mt5_m5.index <= actual_end)]
    mt5_h4 = resample_m5_to_h4(mt5_m5_window)
    mt5_d1 = resample_m5_to_d1_close(mt5_m5_window)
    mt5_df_h4 = to_pipeline_h4(mt5_h4)
    mt5_setups = run_streaming(mt5_df_h4, mt5_d1, instrument, params)

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


def _post_cost_mean_r(holdout: BacktestResult, instrument: str) -> float:
    """Subtract the per-trade cost from realised R, then take the mean
    over closed trades."""
    cost = COST_R_PER_TRADE.get(instrument, 0.0)
    closed = [s for s in holdout.setups if s.outcome not in ("entry_not_hit", "open_at_horizon")]
    if not closed:
        return 0.0
    rs = [s.realized_r - cost for s in closed]
    return sum(rs) / len(rs)


def _post_cost_projected_annual(holdout: BacktestResult, instrument: str) -> float:
    return (
        _post_cost_mean_r(holdout, instrument)
        * holdout.setups_per_month
        * 12.0
        * holdout.risk_per_trade_pct
    )


def evaluate_hypotheses(
    *,
    instrument: str,
    holdout: BacktestResult,
    h10: dict | None,
) -> dict[str, dict]:
    """Return a per-hypothesis dict ``{hk: {pass, value, target}}``.

    H4 / H5 use the post-cost recomputation (the BacktestResult's
    own ``mean_r`` is pre-cost by construction — see module
    docstring). H10 returns ``{pass: None, ...}`` when MT5 data
    are missing for the window.
    """
    out: dict[str, dict] = {}

    # H1
    spm = holdout.setups_per_month
    out["H1"] = {
        "name": HYPOTHESES["H1"]["name"],
        "value": spm,
        "pass": HYPOTHESES["H1"]["low"] <= spm <= HYPOTHESES["H1"]["high"],
    }

    # H2
    wr = holdout.win_rate
    out["H2"] = {
        "name": HYPOTHESES["H2"]["name"],
        "value": wr,
        "pass": HYPOTHESES["H2"]["low"] <= wr <= HYPOTHESES["H2"]["high"],
    }

    # H3 — pre-cost mean R
    out["H3"] = {
        "name": HYPOTHESES["H3"]["name"],
        "value": holdout.mean_r,
        "pass": (HYPOTHESES["H3"]["low"] <= holdout.mean_r <= HYPOTHESES["H3"]["high"]),
    }

    # H4 — post-cost mean R
    pc_mean_r = _post_cost_mean_r(holdout, instrument)
    out["H4"] = {
        "name": HYPOTHESES["H4"]["name"],
        "value": pc_mean_r,
        "pass": HYPOTHESES["H4"]["low"] <= pc_mean_r <= HYPOTHESES["H4"]["high"],
        "cost_r_per_trade": COST_R_PER_TRADE[instrument],
    }

    # H5 — projected annual % (use post-cost to be defensible)
    pc_proj = _post_cost_projected_annual(holdout, instrument)
    out["H5"] = {
        "name": HYPOTHESES["H5"]["name"],
        "value": pc_proj,
        "pass": HYPOTHESES["H5"]["low"] <= pc_proj <= HYPOTHESES["H5"]["high"],
        "pre_cost_value": holdout.projected_annual_return_pct,
    }

    # H6 — CI lower > 0
    ci_low = holdout.mean_r_ci_95[0]
    out["H6"] = {
        "name": HYPOTHESES["H6"]["name"],
        "value": ci_low,
        "pass": ci_low > 0,
    }

    # H7 — outlier-robustness trim_5_5
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

    # H8 — temporal_concentration
    tc = holdout.temporal_concentration
    out["H8"] = {
        "name": HYPOTHESES["H8"]["name"],
        "value": tc,
        "pass": tc is not None and tc < HYPOTHESES["H8"]["max"],
    }

    # H9 — vs buy-and-hold
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

    # H10 — transferability
    if h10 is None or "error" in h10:
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
    """Apply the verdict rule on the per-hypothesis pass/fail map.

    PASS counts the boolean ``True`` results only — entries with
    ``pass=None`` (e.g. H10 unavailable) are excluded from the
    numerator AND the denominator. The §4 rule reads off the
    holdout PASS count alone, so absence of evidence (None) is
    treated as "not counted in either direction".

    Returns ``(verdict, n_pass_out_of_evaluated)``.
    """
    n_pass = sum(1 for h in eval_result.values() if h["pass"] is True)
    if n_pass >= PROMOTE_THRESHOLD:
        verdict = "PROMOTE"
    elif n_pass >= ARCHIVE_THRESHOLD:
        verdict = "REVIEW"
    else:
        verdict = "ARCHIVE"
    # Surface the denominator so a 5/9 (one H10-unavailable) reads
    # differently from 5/10.
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


def _bh_close_for(df_h4: pd.DataFrame, when: pd.Timestamp) -> float:
    """First H4 close ≥ ``when`` (or the last available if past end)."""
    times = pd.to_datetime(df_h4["time"], utc=True)
    mask = times >= when
    if mask.any():
        idx = mask.idxmax()
        return float(df_h4.loc[idx, "close"])
    return float(df_h4["close"].iloc[-1])


def write_report(
    *,
    out_dir: Path,
    train_grids: dict[str, dict[tuple[float, float], BacktestResult]],
    selected_params: dict[str, dict],
    holdouts: dict[str, BacktestResult],
    hypotheses: dict[str, dict[str, dict]],
    verdicts: dict[str, tuple[str, int]],
    h10s: dict[str, dict | None],
    wallclock_s: float,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "report.md"

    lines: list[str] = []
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines.append(f"# Gate 4 — breakout_retest_h4 backtest principal Duk ({ts})")
    lines.append("")
    lines.append(
        "Spec: `docs/strategies/breakout_retest_h4.md` "
        "(commits b14e054 / 689287f). Protocol gate 4 of "
        "`docs/STRATEGY_RESEARCH_PROTOCOL.md`."
    )
    lines.append("")
    lines.append(
        "**Anti-data-dredging**: the 10 hypotheses (§4 of the spec) "
        "and the train selection criteria (§3.2) are frozen at the "
        "spec commit and evaluated post-run, never tuned."
    )
    lines.append("")

    # --- Global verdict --------------------------------------------------
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
    lines.append("## 1. Train grid (3×3 per instrument)")
    lines.append("")
    lines.append(
        "Window: "
        f"{TRAIN_START.date()} → {TRAIN_END.date()}. "
        "Selection: `n_closed >= 50` AND `mean_r_ci_95.lower >= 0` AND "
        "`temporal_concentration < 0.4`; among those, max `mean_r` "
        "(tie-break: max `setups_per_month`)."
    )
    lines.append("")
    for inst in INSTRUMENTS:
        lines.append(f"### {inst}")
        lines.append("")
        lines.append(
            "| retest_tol | sl_buffer | n_setups | n_closed | mean_r | CI low | CI high | win_rate | setups/mo | temp_conc | proj_annual | selected |"
        )
        lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|")
        sel = selected_params[inst].get("params")
        for tol in GRID_SPEC[inst]["retest_tolerance"]:
            for slb in GRID_SPEC[inst]["sl_buffer"]:
                r = train_grids[inst][(tol, slb)]
                n_closed = sum(
                    1 for s in r.setups if s.outcome not in ("entry_not_hit", "open_at_horizon")
                )
                mark = "✅" if sel == (tol, slb) else ""
                lines.append(
                    f"| {tol} | {slb} | {r.n_setups} | {n_closed} | "
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
        f"Window: {HOLDOUT_START.date()} → {HOLDOUT_END.date()}. The "
        "selected (retest_tolerance, sl_buffer) cell from §1 is run "
        "unchanged on the holdout."
    )
    lines.append("")
    lines.append(
        "| Instrument | retest_tol | sl_buffer | n_setups | n_closed | mean_r | CI low | CI high | win_rate | setups/mo | temp_conc | proj_annual | trim_5_5 mean_r | strategy − BH % |"
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
        tol_str = sel[0] if sel else "—"
        slb_str = sel[1] if sel else "—"
        lines.append(
            f"| {inst} | {tol_str} | {slb_str} | {h.n_setups} | {n_closed} | "
            f"{_fmt(h.mean_r)} | {_fmt(h.mean_r_ci_95[0])} | "
            f"{_fmt(h.mean_r_ci_95[1])} | {h.win_rate:.1%} | "
            f"{h.setups_per_month:.2f} | "
            f"{_fmt(h.temporal_concentration, '.3f')} | "
            f"{_fmt(h.projected_annual_return_pct, '+.1f')}% | "
            f"{trim_str} | {bh_str} |"
        )
    lines.append("")

    # --- Hypotheses ------------------------------------------------------
    lines.append("## 3. Hypothesis evaluation (holdout only — §4)")
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
                "✅" if cell["pass"] is True else ("❌" if cell["pass"] is False else "⚠️ n/a")
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

    # --- H10 detail ------------------------------------------------------
    if any(h10s.values()):
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
    lines.append("Verdict rule (spec §4 holdout):")
    lines.append("")
    lines.append("- ≥ 6 PASS → **PROMOTE** (candidate gate 5)")
    lines.append("- 3 ≤ PASS ≤ 5 → **REVIEW** (operator discussion)")
    lines.append("- < 3 PASS → **ARCHIVE** (mandatory)")
    lines.append("")
    lines.append(
        "(Hypotheses with `pass=None` — e.g. H10 unavailable — are excluded from both numerator and denominator.)"
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
                "Move to `archived/strategies/breakout_retest_h4/` with "
                "the post-mortem README per protocol §8 and pick the "
                "next HTF candidate from the backlog. The post-mortem "
                "should record: (a) what assumptions in §4 were "
                "violated by the data, (b) any structural finding "
                "that can inform v2 / next-strategy specs (e.g. the "
                "observed setups/month overshoot, the win-rate "
                "shortfall vs trend-following baseline)."
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


def _bh_closes(df_h4: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> tuple[float, float]:
    """First close at/after ``start`` and last close at/before ``end``."""
    times = pd.to_datetime(df_h4["time"], utc=True)
    s_mask = times >= start
    e_mask = times <= end
    s_idx = s_mask.idxmax() if s_mask.any() else None
    e_idx = e_mask[::-1].idxmax() if e_mask.any() else None
    if s_idx is None or e_idx is None:
        return float(df_h4["close"].iloc[0]), float(df_h4["close"].iloc[-1])
    return float(df_h4.loc[s_idx, "close"]), float(df_h4.loc[e_idx, "close"])


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
        help="Override the output directory. Defaults to "
        "calibration/runs/gate4_breakout_retest_h4_<TS>/.",
    )
    args = parser.parse_args()

    t_start = time.perf_counter()
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    out_dir = Path(args.out_dir) if args.out_dir else RUNS_DIR / f"gate4_breakout_retest_h4_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)

    train_grids: dict[str, dict[tuple[float, float], BacktestResult]] = {}
    selected_params: dict[str, dict] = {}
    holdouts: dict[str, BacktestResult] = {}
    hypotheses: dict[str, dict[str, dict]] = {}
    verdicts: dict[str, tuple[str, int]] = {}
    h10s: dict[str, dict | None] = {}

    for instrument in args.instruments:
        # ---- TRAIN -----------------------------------------------------
        print(
            f"\n##### {instrument} — train ({TRAIN_START.date()} → {TRAIN_END.date()}) #####",
            flush=True,
        )
        m5_train = load_duk_m5(instrument, TRAIN_START, TRAIN_END)
        h4_train = resample_m5_to_h4(m5_train)
        d1_train = resample_m5_to_d1_close(m5_train)
        df_h4_train = to_pipeline_h4(h4_train)
        bh_start_train, bh_end_train = _bh_closes(df_h4_train, TRAIN_START, TRAIN_END)

        grid = run_grid(
            instrument=instrument,
            df_h4=df_h4_train,
            close_d1=d1_train,
            period_start=TRAIN_START,
            period_end=TRAIN_END,
            bh_close_start=bh_start_train,
            bh_close_end=bh_end_train,
        )
        train_grids[instrument] = grid

        # Persist train grid as JSON (one file per instrument).
        grid_export = {f"tol={tol}_sl={slb}": _result_to_dict(r) for (tol, slb), r in grid.items()}
        (out_dir / f"train_grid_{instrument}.json").write_text(
            json.dumps(grid_export, indent=2, default=str)
        )

        best, reason = select_best_cell(grid)
        selected_params[instrument] = {"params": best, "reason": reason}
        if best is None:
            print(f"  ⚠️ no train cell selected — {reason}", flush=True)
            # No calibration possible → mandatory ARCHIVE per spec §4
            # (a strategy that can't even pass calibration on train
            # has no edge to validate on holdout). Skip downstream
            # phases but record an explicit ARCHIVE verdict so the
            # report doesn't say "N/A" — the no-cell-selected outcome
            # is itself a hard verdict.
            verdicts[instrument] = ("ARCHIVE", 0)
            hypotheses[instrument] = {}
            continue
        print(f"  Selected: tol={best[0]}, sl={best[1]} — {reason}", flush=True)

        # ---- HOLDOUT ---------------------------------------------------
        print(
            f"\n##### {instrument} — holdout ({HOLDOUT_START.date()} → {HOLDOUT_END.date()}) #####",
            flush=True,
        )
        m5_hold = load_duk_m5(instrument, HOLDOUT_START, HOLDOUT_END)
        h4_hold = resample_m5_to_h4(m5_hold)
        d1_hold = resample_m5_to_d1_close(m5_hold)
        df_h4_hold = to_pipeline_h4(h4_hold)
        bh_start_hold, bh_end_hold = _bh_closes(df_h4_hold, HOLDOUT_START, HOLDOUT_END)

        result, _ = run_grid_cell(
            instrument=instrument,
            df_h4=df_h4_hold,
            close_d1=d1_hold,
            retest_tolerance=best[0],
            sl_buffer=best[1],
            max_risk_distance=MAX_RISK_DISTANCE[instrument],
            period_start=HOLDOUT_START,
            period_end=HOLDOUT_END,
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

        # ---- H10 -------------------------------------------------------
        if not args.skip_h10:
            print("  H10 transferability (Duk vs MT5) on holdout...", flush=True)
            h10 = transferability_mismatch(
                instrument=instrument,
                params=StrategyParams(
                    retest_tolerance=best[0],
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
            h10=h10s.get(instrument),
        )
        hypotheses[instrument] = eval_h
        verdicts[instrument] = verdict_from_hypotheses(eval_h)
        print(
            f"  Verdict: {verdicts[instrument][0]} " f"({verdicts[instrument][1]} hypotheses PASS)",
            flush=True,
        )

    if h10s:
        (out_dir / "h10_transferability.json").write_text(json.dumps(h10s, indent=2, default=str))

    wallclock = time.perf_counter() - t_start
    report_path = write_report(
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


def _result_to_dict(r: BacktestResult) -> dict:
    """Compact JSON view of a BacktestResult (drop the full setup
    list to keep the train-grid JSON small — the per-setup detail
    lives in ``holdout_*.json`` for the selected cells)."""
    d = asdict(r)
    d.pop("setups", None)
    d["projected_annual_return_pct"] = r.projected_annual_return_pct
    return d


if __name__ == "__main__":
    sys.exit(main())
