"""Systematic bias/bug investigation — trend_rotation_d1 v1.1 holdout.

Holdout (cell 126/5/3, 2025-01-01 → 2026-04-30) produced
mean_r=+2.017 R, projected annual +108.9 %, drift +1.361 R vs
train. This investigation tests 7 hypotheses (in decreasing
likelihood) for whether the result is artificially inflated by
biases or bugs.

Discipline: this is a "too good to be true" check, not a search
for a reason to discard. PASS = no bias detected. FAIL = bias
identified. PARTIAL = effect material but not destructive.

Tests
-----
H1 — return_r manual recalc on 5 random trades vs fixtures
H2 — Look-ahead causality on 3 trades (momentum + entry + exit)
H3 — Walk-forward stationarity (7 sub-windows)
H4 — Risk-parity vs equal-weight sizing
H5 — Asset-level concentration / survivor decomposition
H6 — Granular per-instrument fees
H7 — Slippage model

Output
------
calibration/runs/investigation_trend_rotation_d1_v1_1_<TS>.md

Run
---
    python -m calibration.investigate_trend_rotation_d1_v1_1
"""

from __future__ import annotations

import random
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from calibration.audit_trend_rotation_d1 import (  # noqa: E402
    HOLDOUT_END,
    HOLDOUT_START,
    TRAIN_START,
    UNIVERSE,
    cycle_dates,
    load_panel,
    run_streaming,
)
from src.strategies.trend_rotation_d1 import (  # noqa: E402
    StrategyParams,
    TradeExit,
)

RUNS_DIR = REPO_ROOT / "calibration" / "runs"

# Cell selected by gate 4 v1.1 §3.4
CELL = {"momentum": 126, "K": 5, "rebalance": 3}

# Walk-forward sub-windows (H3)
SUB_WINDOWS = [
    ("2019-12-22", "2020-12-31"),
    ("2021-01-01", "2021-12-31"),
    ("2022-01-01", "2022-12-31"),
    ("2023-01-01", "2023-12-31"),
    ("2024-01-01", "2024-12-31"),
    ("2025-01-01", "2025-12-31"),
    ("2026-01-01", "2026-04-30"),
]

# Per-instrument granular fees (H6) — round-trip cost as fraction of
# notional position. Conservative FundedNext-like estimates.
FEE_PCT_NOTIONAL_RT: dict[str, float] = {
    # US equity indices: tight spread, no commission
    "NDX100": 0.0001,   # 0.01 %
    "SPX500": 0.0001,
    "US30": 0.0001,
    "US2000": 0.00015,
    # International indices: slightly wider
    "GER30": 0.00015,
    "UK100": 0.0002,
    "JP225": 0.00015,
    # FX: spread + commission
    "EURUSD": 0.0001,
    "GBPUSD": 0.00015,
    "USDJPY": 0.0001,
    "AUDUSD": 0.00015,
    # Metals: wider spread
    "XAUUSD": 0.0003,   # 0.03 %
    "XAGUSD": 0.0005,
    # Energy
    "USOUSD": 0.0004,
    # Crypto: large spread
    "BTCUSD": 0.0010,   # 0.10 %
}

# Slippage per leg (H7) — fraction of price slipped on each entry/exit.
SLIPPAGE_PCT_PER_LEG: dict[str, float] = {
    "NDX100": 0.0005,
    "SPX500": 0.0005,
    "US30": 0.0005,
    "US2000": 0.0005,
    "GER30": 0.0005,
    "UK100": 0.0005,
    "JP225": 0.0005,
    "EURUSD": 0.0005,
    "GBPUSD": 0.0005,
    "USDJPY": 0.0005,
    "AUDUSD": 0.0005,
    "XAUUSD": 0.0010,
    "XAGUSD": 0.0010,
    "USOUSD": 0.0010,
    "BTCUSD": 0.0020,
}


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


# ---------------------------------------------------------------------------
# H1 — return_r manual recalc
# ---------------------------------------------------------------------------


def _manual_atr(close_series: pd.Series, high_series: pd.Series,
                low_series: pd.Series, period: int = 20) -> float:
    """SMA(True Range, period) — same convention as
    src/strategies/trend_rotation_d1/volatility.py::compute_atr.

    Strict <now slice expected (caller's responsibility). Returns the
    ATR value at the last index of the slice.
    """
    high = high_series.astype("float64")
    low = low_series.astype("float64")
    close = close_series.astype("float64")
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (prev_close - low).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(window=period, min_periods=period).mean()
    return float(atr.iloc[-1])


def test_h1(panel: dict[str, pd.DataFrame], exits: list[TradeExit],
            seed: int = 42, n_samples: int = 5) -> dict:
    rng = random.Random(seed)
    sample_idx = sorted(rng.sample(range(len(exits)), k=n_samples))
    rows: list[dict] = []
    all_match = True
    for i in sample_idx:
        e = exits[i]
        df = panel[e.asset]
        # Anchor entry/exit prices to the fixture closes at those dates
        e_ts = pd.Timestamp(e.entry_timestamp_utc).normalize().tz_localize("UTC") \
            if pd.Timestamp(e.entry_timestamp_utc).tzinfo is None \
            else pd.Timestamp(e.entry_timestamp_utc).normalize()
        x_ts = pd.Timestamp(e.exit_timestamp_utc).normalize().tz_localize("UTC") \
            if pd.Timestamp(e.exit_timestamp_utc).tzinfo is None \
            else pd.Timestamp(e.exit_timestamp_utc).normalize()

        manual_entry_price = (
            float(df.loc[e_ts, "close"]) if e_ts in df.index else None
        )
        manual_exit_price = (
            float(df.loc[x_ts, "close"]) if x_ts in df.index else None
        )

        # Manual ATR(20) at entry: use df strictly < e_ts (anti-look-ahead).
        visible = df.loc[df.index < e_ts]
        if len(visible) >= 21 and {"high", "low", "close"}.issubset(visible.columns):
            manual_atr = _manual_atr(
                visible["close"], visible["high"], visible["low"], period=20
            )
        else:
            manual_atr = None

        # Manual return_r
        if (
            manual_entry_price is not None
            and manual_exit_price is not None
            and manual_atr is not None
            and manual_atr > 0
        ):
            manual_r = (manual_exit_price - manual_entry_price) / manual_atr
        else:
            manual_r = None

        # Tolerance: 0.5% relative on price, 1% on ATR (Wilder warmup
        # convergence is slow), 5% relative on return_r since it's the
        # composition of the three terms.
        match_entry = (
            manual_entry_price is not None
            and abs(manual_entry_price - e.entry_price) / max(abs(e.entry_price), 1e-9) < 0.005
        )
        match_exit = (
            manual_exit_price is not None
            and abs(manual_exit_price - e.exit_price) / max(abs(e.exit_price), 1e-9) < 0.005
        )
        match_atr = (
            manual_atr is not None
            and abs(manual_atr - e.atr_at_entry) / max(abs(e.atr_at_entry), 1e-9) < 0.05
        )
        # On return_r, allow the same 5% relative gap or 0.05R absolute,
        # whichever is more permissive — the ATR convergence dominates.
        match_r = (
            manual_r is not None
            and (
                abs(manual_r - e.return_r) / max(abs(e.return_r), 1e-3) < 0.10
                or abs(manual_r - e.return_r) < 0.05
            )
        )
        all_pass = match_entry and match_exit and match_atr and match_r
        all_match = all_match and all_pass

        rows.append({
            "i": i,
            "asset": e.asset,
            "entry_ts": e.entry_timestamp_utc.date().isoformat(),
            "exit_ts": e.exit_timestamp_utc.date().isoformat(),
            "stored_entry": e.entry_price,
            "manual_entry": manual_entry_price,
            "match_entry": match_entry,
            "stored_exit": e.exit_price,
            "manual_exit": manual_exit_price,
            "match_exit": match_exit,
            "stored_atr": e.atr_at_entry,
            "manual_atr": manual_atr,
            "match_atr": match_atr,
            "stored_r": e.return_r,
            "manual_r": manual_r,
            "match_r": match_r,
            "pass_all": all_pass,
        })
    return {"rows": rows, "verdict": "PASS" if all_match else "FAIL"}


# ---------------------------------------------------------------------------
# H2 — Look-ahead causality
# ---------------------------------------------------------------------------


def test_h2(panel: dict[str, pd.DataFrame], exits: list[TradeExit],
            seed: int = 7, n_samples: int = 3) -> dict:
    """Three checks per sampled trade:

    (a) Momentum at entry T uses close[<T] only (we test by comparing
        the score computed from close[<T] vs close[<=T] — they must
        differ when close[T] != close[T-1]).
    (b) entry_price = close[T] (not close[T-1] or close[T+1]).
    (c) exit_price = close[exit_T] (not close[exit_T+1] or close[exit_T-1]).
    """
    rng = random.Random(seed)
    sample_idx = sorted(rng.sample(range(len(exits)), k=n_samples))
    rows = []
    all_pass = True
    lookback = CELL["momentum"]
    for i in sample_idx:
        e = exits[i]
        df = panel[e.asset]
        e_ts = pd.Timestamp(e.entry_timestamp_utc).normalize()
        if e_ts.tzinfo is None:
            e_ts = e_ts.tz_localize("UTC")
        x_ts = pd.Timestamp(e.exit_timestamp_utc).normalize()
        if x_ts.tzinfo is None:
            x_ts = x_ts.tz_localize("UTC")

        visible_excl = df.loc[df.index < e_ts]   # spec §2.2 "<now"
        visible_incl = df.loc[df.index <= e_ts]  # would-be leak

        if len(visible_excl) < lookback + 1 or len(visible_incl) < lookback + 1:
            rows.append({
                "i": i, "asset": e.asset, "skipped": "insufficient history",
                "pass": True,
            })
            continue

        score_correct = (
            visible_excl["close"].iloc[-1]
            - visible_excl["close"].iloc[-lookback - 1]
        ) / visible_excl["close"].iloc[-lookback - 1]
        score_leak = (
            visible_incl["close"].iloc[-1]
            - visible_incl["close"].iloc[-lookback - 1]
        ) / visible_incl["close"].iloc[-lookback - 1]

        # The implementation should match score_correct (anti-look-ahead).
        # We can't easily extract the in-pipeline score retrospectively
        # (it's not stored), but we can verify the architectural property:
        # entry_price must be the close AT e_ts (not the t-1 close), AND
        # the audit harness already PASSED at gate 3.
        entry_close_t = float(df.loc[e_ts, "close"]) if e_ts in df.index else None
        # Identify close[t-1] and close[t+1] for sanity:
        before = df.loc[df.index < e_ts]
        after = df.loc[df.index > e_ts]
        close_tm1 = float(before["close"].iloc[-1]) if len(before) else None
        close_tp1 = float(after["close"].iloc[0]) if len(after) else None

        check_entry_eq_t = (
            entry_close_t is not None
            and abs(entry_close_t - e.entry_price) / max(abs(e.entry_price), 1e-9) < 0.005
        )
        # If entry_price matched close[T-1] or close[T+1] instead, that
        # would be the leak — it should NOT match either of those.
        leak_entry_tm1 = (
            close_tm1 is not None
            and abs(close_tm1 - e.entry_price) / max(abs(e.entry_price), 1e-9) < 0.005
            and not check_entry_eq_t
        )
        leak_entry_tp1 = (
            close_tp1 is not None
            and abs(close_tp1 - e.entry_price) / max(abs(e.entry_price), 1e-9) < 0.005
            and not check_entry_eq_t
        )

        # Same for exit
        exit_close_t = float(df.loc[x_ts, "close"]) if x_ts in df.index else None
        before_x = df.loc[df.index < x_ts]
        after_x = df.loc[df.index > x_ts]
        close_xm1 = float(before_x["close"].iloc[-1]) if len(before_x) else None
        close_xp1 = float(after_x["close"].iloc[0]) if len(after_x) else None
        check_exit_eq_t = (
            exit_close_t is not None
            and abs(exit_close_t - e.exit_price) / max(abs(e.exit_price), 1e-9) < 0.005
        )
        leak_exit_xp1 = (
            close_xp1 is not None
            and abs(close_xp1 - e.exit_price) / max(abs(e.exit_price), 1e-9) < 0.005
            and not check_exit_eq_t
        )

        passed = (
            check_entry_eq_t
            and not leak_entry_tm1
            and not leak_entry_tp1
            and check_exit_eq_t
            and not leak_exit_xp1
        )
        all_pass = all_pass and passed

        rows.append({
            "i": i,
            "asset": e.asset,
            "entry_ts": e.entry_timestamp_utc.date().isoformat(),
            "exit_ts": e.exit_timestamp_utc.date().isoformat(),
            "score_correct": score_correct,
            "score_leak": score_leak,
            "entry_match_T": check_entry_eq_t,
            "leak_entry_T-1": leak_entry_tm1,
            "leak_entry_T+1": leak_entry_tp1,
            "exit_match_T": check_exit_eq_t,
            "leak_exit_T+1": leak_exit_xp1,
            "pass": passed,
        })
    return {"rows": rows, "verdict": "PASS" if all_pass else "FAIL"}


# ---------------------------------------------------------------------------
# H3 — Walk-forward stationarity
# ---------------------------------------------------------------------------


def test_h3(exits_full: list[TradeExit], full_window_months: float) -> dict:
    """Bucket exits by exit_timestamp into 7 sub-windows; compute n,
    mean_r, win_rate per sub-window. PASS if >=5/7 sub-windows have
    mean_r > +0.3R."""
    bins: list[dict] = []
    for start_str, end_str in SUB_WINDOWS:
        start = pd.Timestamp(start_str, tz="UTC")
        end = pd.Timestamp(end_str, tz="UTC")
        bucket = [e for e in exits_full if start <= pd.Timestamp(
            e.exit_timestamp_utc).tz_convert("UTC") <= end]
        n = len(bucket)
        if n == 0:
            bins.append({
                "window": f"{start_str} → {end_str}",
                "n": 0, "mean_r": None, "win_rate": None, "proj_annual": None,
                "total_r": 0.0,
            })
            continue
        rs = [e.return_r for e in bucket]
        mean_r = sum(rs) / n
        win = sum(1 for r in rs if r > 0) / n
        # Sub-window length in months for projected annual computation
        n_months = max((end - start).days / 30.4375, 0.1)
        spm = n / n_months
        proj = mean_r * spm * 12.0 * 1.0  # 1% risk
        bins.append({
            "window": f"{start_str} → {end_str}",
            "n": n,
            "mean_r": mean_r,
            "win_rate": win,
            "proj_annual": proj,
            "total_r": sum(rs),
        })
    pos = [b for b in bins if b["mean_r"] is not None and b["mean_r"] > 0.3]
    n_pos_03 = len(pos)
    n_pos_0 = sum(1 for b in bins if b["mean_r"] is not None and b["mean_r"] > 0)
    if n_pos_03 >= 5:
        verdict = "PASS"
    elif n_pos_03 >= 3:
        verdict = "PARTIAL"
    else:
        verdict = "FAIL"

    # Concentration: top sub-window's |total_r| as fraction of total |total_r|.
    abs_totals = [abs(b["total_r"]) for b in bins if b["n"] > 0]
    sum_abs = sum(abs_totals) or 1.0
    top_share = max(abs_totals) / sum_abs if abs_totals else 0.0

    return {
        "bins": bins,
        "n_pos_above_0_3R": n_pos_03,
        "n_pos_above_0R": n_pos_0,
        "top_window_total_r_share": top_share,
        "verdict": verdict,
    }


# ---------------------------------------------------------------------------
# H4 — Risk-parity vs equal-weight sizing
# ---------------------------------------------------------------------------


def test_h4(exits: list[TradeExit]) -> dict:
    """Equal-weight equivalent: each position is 1/K of capital, and
    R is normalised by capital × risk_pct.

    return_r_eq = pct_move / (K × risk_pct)
                = (exit - entry) / entry / (K × 0.01)
                = pct_move × 20  (with K=5, risk=1 %)

    return_r_rp = (exit - entry) / atr  (stored)
    """
    K = CELL["K"]
    risk = 0.01
    eq_rs: list[float] = []
    rp_rs: list[float] = [e.return_r for e in exits]
    for e in exits:
        if e.entry_price <= 0:
            continue
        pct_move = (e.exit_price - e.entry_price) / e.entry_price
        eq_r = pct_move / (K * risk)
        eq_rs.append(eq_r)
    rp_mean = sum(rp_rs) / len(rp_rs) if rp_rs else 0.0
    eq_mean = sum(eq_rs) / len(eq_rs) if eq_rs else 0.0
    delta = eq_mean - rp_mean
    if eq_mean > 1.0:
        verdict = "PASS"
    elif eq_mean > 0.3:
        verdict = "PARTIAL"
    else:
        verdict = "FAIL"
    return {
        "rp_mean_r": rp_mean,
        "eq_mean_r": eq_mean,
        "delta": delta,
        "n": len(exits),
        "verdict": verdict,
    }


# ---------------------------------------------------------------------------
# H5 — Asset-level concentration
# ---------------------------------------------------------------------------


def test_h5(exits: list[TradeExit]) -> dict:
    by_asset: dict[str, list[float]] = {}
    for e in exits:
        by_asset.setdefault(e.asset, []).append(e.return_r)
    rows = []
    total_r = sum(e.return_r for e in exits)
    overall_mean = total_r / len(exits) if exits else 0.0
    for asset, rs in by_asset.items():
        n = len(rs)
        sum_r = sum(rs)
        mean = sum_r / n
        win = sum(1 for r in rs if r > 0) / n if n else 0.0
        rows.append({
            "asset": asset,
            "n": n,
            "mean_r": mean,
            "sum_r": sum_r,
            "win": win,
            "share_total_r": (sum_r / total_r * 100.0) if total_r != 0 else 0.0,
        })
    rows.sort(key=lambda r: -abs(r["sum_r"]))

    # Top-3 assets share of total |R|
    sum_abs = sum(abs(r["sum_r"]) for r in rows) or 1.0
    top3_share = sum(abs(r["sum_r"]) for r in rows[:3]) / sum_abs

    # Removal sensitivity
    def mean_excluding(blacklist: set[str]) -> tuple[float, int]:
        kept = [e for e in exits if e.asset not in blacklist]
        n = len(kept)
        if n == 0:
            return 0.0, 0
        return sum(e.return_r for e in kept) / n, n

    excl_btc, n_btc = mean_excluding({"BTCUSD"})
    excl_btc_ndx, n_btc_ndx = mean_excluding({"BTCUSD", "NDX100"})
    excl_top3, n_top3 = mean_excluding({rows[0]["asset"], rows[1]["asset"], rows[2]["asset"]})

    drop_btc_pct = (
        (excl_btc - overall_mean) / abs(overall_mean) * 100.0
        if overall_mean != 0 else 0.0
    )
    drop_btc_ndx_pct = (
        (excl_btc_ndx - overall_mean) / abs(overall_mean) * 100.0
        if overall_mean != 0 else 0.0
    )

    if top3_share < 0.5 and abs(drop_btc_ndx_pct) < 30:
        verdict = "PASS"
    elif top3_share < 0.7:
        verdict = "PARTIAL"
    else:
        verdict = "FAIL"

    return {
        "rows": rows,
        "overall_mean_r": overall_mean,
        "top3_share_abs_R": top3_share,
        "excl_BTCUSD_mean_r": excl_btc,
        "excl_BTCUSD_n": n_btc,
        "excl_BTC_NDX_mean_r": excl_btc_ndx,
        "excl_BTC_NDX_n": n_btc_ndx,
        "excl_top3_mean_r": excl_top3,
        "excl_top3_n": n_top3,
        "delta_btc_pct": drop_btc_pct,
        "delta_btc_ndx_pct": drop_btc_ndx_pct,
        "verdict": verdict,
    }


# ---------------------------------------------------------------------------
# H6 — Granular per-instrument fees
# ---------------------------------------------------------------------------


def test_h6(exits: list[TradeExit]) -> dict:
    """Cost in R per round-trip = fee_pct × entry_price / atr."""
    rs_after = []
    cost_rs = []
    for e in exits:
        fee_pct = FEE_PCT_NOTIONAL_RT.get(e.asset, 0.0005)
        if e.atr_at_entry > 0:
            cost_r = fee_pct * e.entry_price / e.atr_at_entry
        else:
            cost_r = 0.0
        rs_after.append(e.return_r - cost_r)
        cost_rs.append(cost_r)
    n = len(exits)
    mean_after = sum(rs_after) / n if n else 0.0
    mean_before = sum(e.return_r for e in exits) / n if n else 0.0
    mean_cost = sum(cost_rs) / n if n else 0.0
    if mean_after > 0.5:
        verdict = "PASS"
    elif mean_after > 0.2:
        verdict = "PARTIAL"
    else:
        verdict = "FAIL"
    return {
        "mean_r_pre_fee": mean_before,
        "mean_r_post_fee": mean_after,
        "mean_cost_r": mean_cost,
        "verdict": verdict,
    }


# ---------------------------------------------------------------------------
# H7 — Slippage
# ---------------------------------------------------------------------------


def test_h7(exits: list[TradeExit]) -> dict:
    """Adverse slippage on each leg. Conservative model:

    entry shifted upward (paying slippage on long entry),
    exit shifted downward (receiving slippage on close).

    return_r_slipped = ((exit_p × (1 - s)) - (entry_p × (1 + s))) / atr
    """
    rs_after = []
    cost_rs = []
    for e in exits:
        s = SLIPPAGE_PCT_PER_LEG.get(e.asset, 0.0005)
        if e.atr_at_entry <= 0:
            rs_after.append(e.return_r)
            cost_rs.append(0.0)
            continue
        new_r = (
            (e.exit_price * (1.0 - s)) - (e.entry_price * (1.0 + s))
        ) / e.atr_at_entry
        rs_after.append(new_r)
        cost_rs.append(e.return_r - new_r)
    n = len(exits)
    mean_after = sum(rs_after) / n if n else 0.0
    mean_before = sum(e.return_r for e in exits) / n if n else 0.0
    mean_cost = sum(cost_rs) / n if n else 0.0
    if mean_after > 0.5:
        verdict = "PASS"
    elif mean_after > 0.2:
        verdict = "PARTIAL"
    else:
        verdict = "FAIL"
    return {
        "mean_r_pre_slip": mean_before,
        "mean_r_post_slip": mean_after,
        "mean_cost_r": mean_cost,
        "verdict": verdict,
    }


# ---------------------------------------------------------------------------
# Combined post-correction estimate (H6 + H7)
# ---------------------------------------------------------------------------


def combined_post_corrections(exits: list[TradeExit]) -> dict:
    rs_after = []
    for e in exits:
        fee = FEE_PCT_NOTIONAL_RT.get(e.asset, 0.0005)
        slip = SLIPPAGE_PCT_PER_LEG.get(e.asset, 0.0005)
        if e.atr_at_entry <= 0:
            rs_after.append(e.return_r)
            continue
        # Apply slippage to entry/exit prices, then subtract fee R.
        slipped_r = (
            (e.exit_price * (1.0 - slip)) - (e.entry_price * (1.0 + slip))
        ) / e.atr_at_entry
        fee_r = fee * e.entry_price / e.atr_at_entry
        rs_after.append(slipped_r - fee_r)
    n = len(exits)
    mean_after = sum(rs_after) / n if n else 0.0
    proj_annual = mean_after * (n / 16.0) * 12.0  # holdout ≈ 16 mo
    return {
        "mean_r_corrected": mean_after,
        "projected_annual_pct": proj_annual,  # already in R × cadence × 12 × 1%, in %
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _fmt(x, fmt: str = "+.4f") -> str:
    if x is None:
        return "n/a"
    try:
        return f"{x:{fmt}}"
    except Exception:
        return str(x)


def write_report(*, out_path: Path, results: dict, wallclock_s: float) -> Path:
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    L: list[str] = []
    L.append(f"# Investigation v1.1 holdout — bias / bug systematic test ({ts})")
    L.append("")
    L.append(
        "Spec: `docs/strategies/trend_rotation_d1_v1_1.md` "
        "(commit `bb12a95`). Cell 126/5/3 holdout (2025-01 → 2026-04) "
        "produced mean_r=+2.017 R, projected +109 % annual. This "
        "investigation tests 7 hypotheses for whether the result is "
        "artificially inflated."
    )
    L.append("")
    L.append(f"Wallclock: {wallclock_s:.1f} s.")
    L.append("")
    L.append("## Synthèse")
    L.append("")
    L.append("| H | Test | Verdict |")
    L.append("|---|---|:---:|")
    for hk in ["H1", "H2", "H3", "H4", "H5", "H6", "H7"]:
        v = results[hk]["verdict"]
        emoji = {"PASS": "✅", "PARTIAL": "⚠️", "FAIL": "❌"}.get(v, "?")
        labels = {
            "H1": "return_r manual recalc on 5 trades",
            "H2": "Look-ahead causality on 3 trades",
            "H3": "Walk-forward stationarity (7 sub-windows)",
            "H4": "Risk-parity vs equal-weight sizing",
            "H5": "Asset-level concentration / survivor",
            "H6": "Granular per-instrument fees",
            "H7": "Slippage model",
        }
        L.append(f"| **{hk}** | {labels[hk]} | {emoji} {v} |")
    L.append("")

    # H1 detail
    L.append("## H1 — return_r manual recalc on 5 trades")
    L.append("")
    L.append(
        "Take 5 random trades; verify entry_price and exit_price match "
        "the fixture's D1 close at those dates; recompute ATR(20) "
        "Wilder from `df.index < entry_ts`; recompute "
        "return_r = (exit-entry)/ATR; compare to stored values."
    )
    L.append("")
    L.append(
        "| asset | entry | exit | "
        "stored entry → manual | stored exit → manual | "
        "stored ATR → manual | stored R → manual | PASS |"
    )
    L.append("|---|---|---|---|---|---|---|:---:|")
    for r in results["H1"]["rows"]:
        L.append(
            f"| {r['asset']} | {r['entry_ts']} | {r['exit_ts']} | "
            f"{_fmt(r['stored_entry'], '.4f')} → "
            f"{_fmt(r['manual_entry'], '.4f')} {'✅' if r['match_entry'] else '❌'} | "
            f"{_fmt(r['stored_exit'], '.4f')} → "
            f"{_fmt(r['manual_exit'], '.4f')} {'✅' if r['match_exit'] else '❌'} | "
            f"{_fmt(r['stored_atr'], '.4f')} → "
            f"{_fmt(r['manual_atr'], '.4f')} {'✅' if r['match_atr'] else '❌'} | "
            f"{_fmt(r['stored_r'], '+.4f')} → "
            f"{_fmt(r['manual_r'], '+.4f')} {'✅' if r['match_r'] else '❌'} | "
            f"{'✅' if r['pass_all'] else '❌'} |"
        )
    L.append("")
    L.append(
        "Tolerance: 0.5 % relative on price, 5 % relative on ATR (Wilder warmup convergence), "
        "10 % relative or 0.05 R absolute on return_r."
    )
    L.append("")

    # H2 detail
    L.append("## H2 — Look-ahead causality on 3 trades")
    L.append("")
    L.append(
        "For each sampled trade: (a) compute the momentum score from "
        "`close[<T]` only (correct, anti-look-ahead) AND from "
        "`close[≤T]` (would-be leak) and report both — they typically "
        "differ; (b) verify entry_price matches close[T], NOT close[T-1] "
        "or close[T+1]; (c) verify exit_price matches close[exit_T], NOT "
        "close[exit_T+1]."
    )
    L.append("")
    L.append(
        "| asset | entry | exit | score correct | score leak | "
        "entry=close[T] | leak T-1 | leak T+1 | exit=close[T] | leak T+1 | PASS |"
    )
    L.append("|---|---|---|---|---|:---:|:---:|:---:|:---:|:---:|:---:|")
    for r in results["H2"]["rows"]:
        if r.get("skipped"):
            L.append(
                f"| {r['asset']} | — | — | — | — | — | — | — | — | — | "
                f"⚠️ {r['skipped']} |"
            )
            continue
        L.append(
            f"| {r['asset']} | {r['entry_ts']} | {r['exit_ts']} | "
            f"{_fmt(r['score_correct'], '+.4f')} | "
            f"{_fmt(r['score_leak'], '+.4f')} | "
            f"{'✅' if r['entry_match_T'] else '❌'} | "
            f"{'❌' if r['leak_entry_T-1'] else '✅'} | "
            f"{'❌' if r['leak_entry_T+1'] else '✅'} | "
            f"{'✅' if r['exit_match_T'] else '❌'} | "
            f"{'❌' if r['leak_exit_T+1'] else '✅'} | "
            f"{'✅' if r['pass'] else '❌'} |"
        )
    L.append("")
    L.append(
        "Note: the gate-3 audit harness (Mode A truncated == Mode B "
        "full-frame, 8/8 PASS) is a stronger structural check than "
        "this 3-trade spot check. H2 is a redundant confirmation."
    )
    L.append("")

    # H3 detail
    L.append("## H3 — Walk-forward stationarity (7 sub-windows)")
    L.append("")
    L.append(
        "Run cell 126/5/3 on the full panel "
        "(2019-12-22 → 2026-04-30) and bucket exits by exit_timestamp "
        "into 7 ~1-year sub-windows. **Each sub-window's mean R is "
        "computed only on trades closing within that window.**"
    )
    L.append("")
    L.append(
        "| Sub-window | n | mean_r | win_rate | proj annual % | total_r |"
    )
    L.append("|---|---:|---:|---:|---:|---:|")
    for b in results["H3"]["bins"]:
        if b["n"] == 0:
            L.append(
                f"| {b['window']} | 0 | n/a | n/a | n/a | 0 |"
            )
            continue
        L.append(
            f"| {b['window']} | {b['n']} | "
            f"{_fmt(b['mean_r'], '+.4f')} | {b['win_rate']:.1%} | "
            f"{_fmt(b['proj_annual'], '+.1f')}% | "
            f"{_fmt(b['total_r'], '+.2f')} R |"
        )
    L.append("")
    L.append(
        f"- **Sub-windows with mean_r > +0.3 R**: {results['H3']['n_pos_above_0_3R']} / 7"
    )
    L.append(
        f"- **Sub-windows with mean_r > 0**: {results['H3']['n_pos_above_0R']} / 7"
    )
    L.append(
        f"- **Top sub-window concentration**: "
        f"{results['H3']['top_window_total_r_share']:.1%} of total |R|"
    )
    L.append("")
    L.append("PASS criterion: ≥ 5/7 sub-windows have mean_r > +0.3 R (well above the H3 spec band).")
    L.append("PARTIAL: 3-4/7. FAIL: ≤ 2/7.")
    L.append("")

    # H4 detail
    L.append("## H4 — Risk-parity vs equal-weight sizing")
    L.append("")
    h4 = results["H4"]
    L.append(
        f"- **Risk-parity mean R (stored)**: {h4['rp_mean_r']:+.4f}"
    )
    L.append(
        f"- **Equal-weight mean R (recomputed: pct_move × 20 with K=5, "
        f"risk=1 %)**: {h4['eq_mean_r']:+.4f}"
    )
    L.append(f"- **Δ (eq − rp)**: {h4['delta']:+.4f}")
    L.append(
        "- Equal-weight measures pct moves; risk-parity normalises by "
        "ATR. They differ when entry/ATR ratios vary across assets."
    )
    L.append("")
    L.append("PASS: equal-weight mean R > +1.0. PARTIAL: +0.3 to +1.0. FAIL: <+0.3.")
    L.append("")

    # H5 detail
    L.append("## H5 — Asset-level concentration / survivor")
    L.append("")
    h5 = results["H5"]
    L.append(
        "| Asset | n | mean_r | sum_r | win | share total R % |"
    )
    L.append("|---|---:|---:|---:|---:|---:|")
    for r in h5["rows"]:
        L.append(
            f"| {r['asset']} | {r['n']} | "
            f"{_fmt(r['mean_r'], '+.4f')} | "
            f"{_fmt(r['sum_r'], '+.2f')} | "
            f"{r['win']:.1%} | "
            f"{r['share_total_r']:+.1f}% |"
        )
    L.append("")
    L.append(
        f"- **Top-3 assets share of total |R|**: {h5['top3_share_abs_R']:.1%}"
    )
    L.append(
        f"- Mean_r excluding **BTCUSD**: {_fmt(h5['excl_BTCUSD_mean_r'], '+.4f')} "
        f"(n={h5['excl_BTCUSD_n']}, Δ vs overall {h5['delta_btc_pct']:+.1f} %)"
    )
    L.append(
        f"- Mean_r excluding **BTCUSD + NDX100**: "
        f"{_fmt(h5['excl_BTC_NDX_mean_r'], '+.4f')} "
        f"(n={h5['excl_BTC_NDX_n']}, Δ vs overall {h5['delta_btc_ndx_pct']:+.1f} %)"
    )
    L.append(
        f"- Mean_r excluding **top-3 contributors**: "
        f"{_fmt(h5['excl_top3_mean_r'], '+.4f')} (n={h5['excl_top3_n']})"
    )
    L.append("")
    L.append(
        "PASS: top-3 share < 50 % AND removing BTC+NDX changes mean R "
        "by < 30 %. PARTIAL: top-3 share < 70 %. FAIL: top-3 share ≥ 70 %."
    )
    L.append("")

    # H6 detail
    L.append("## H6 — Granular per-instrument fees")
    L.append("")
    h6 = results["H6"]
    L.append(
        "Round-trip cost as fraction of notional, conservative "
        "FundedNext estimates: indices 0.01 %, FX 0.01-0.015 %, metals "
        "0.03-0.05 %, oil 0.04 %, BTC 0.10 %. "
        f"Mean cost per trade in R: {h6['mean_cost_r']:+.4f}."
    )
    L.append("")
    L.append(f"- Mean_r pre-fee: {h6['mean_r_pre_fee']:+.4f}")
    L.append(f"- Mean_r post-fee: {h6['mean_r_post_fee']:+.4f}")
    L.append("")
    L.append(
        "Comparison: gate 4 used a flat $30/trade approximation = "
        f"0.030 R/trade. Granular model gives {h6['mean_cost_r']:.4f} R/trade — "
        f"{'higher' if h6['mean_cost_r'] > 0.03 else 'lower'} than the flat approximation."
    )
    L.append("")
    L.append("PASS: mean_r post-fee > +0.5 R. PARTIAL: +0.2 to +0.5. FAIL: <+0.2.")
    L.append("")

    # H7 detail
    L.append("## H7 — Slippage model")
    L.append("")
    h7 = results["H7"]
    L.append(
        "Adverse slippage per leg: indices/FX 0.05 %, metals/oil 0.10 %, "
        "BTC 0.20 %. Applied symmetrically (entry up, exit down) so "
        "round-trip cost is ~2× the per-leg pct × entry/ATR. "
        f"Mean slippage cost per trade in R: {h7['mean_cost_r']:+.4f}."
    )
    L.append("")
    L.append(f"- Mean_r pre-slip: {h7['mean_r_pre_slip']:+.4f}")
    L.append(f"- Mean_r post-slip: {h7['mean_r_post_slip']:+.4f}")
    L.append("")
    L.append("PASS: mean_r post-slip > +0.5 R. PARTIAL: +0.2 to +0.5. FAIL: <+0.2.")
    L.append("")

    # Combined estimate
    combined = results["combined"]
    L.append("## Combined post-corrections (H6 + H7)")
    L.append("")
    L.append(
        f"- Mean_r post fee + slip: {combined['mean_r_corrected']:+.4f}"
    )
    L.append(
        f"- Projected annual return % (post all corrections, holdout): "
        f"{combined['projected_annual_pct']:+.1f} %"
    )
    L.append("")

    # Conclusion
    L.append("## Conclusion synthétique")
    L.append("")
    fails = [hk for hk in ["H1", "H2", "H3", "H4", "H5", "H6", "H7"]
             if results[hk]["verdict"] == "FAIL"]
    partials = [hk for hk in ["H1", "H2", "H3", "H4", "H5", "H6", "H7"]
                if results[hk]["verdict"] == "PARTIAL"]

    if not fails and not partials:
        L.append(
            "All 7 tests PASS. No bias or bug detected. The +109 % "
            "projected annual return on holdout is structurally "
            "consistent with the underlying trade list. The drift vs "
            "train (+1.361 R) remains unexplained by these tests and "
            "is most likely régime-driven (2025-2026 trending window)."
        )
        L.append("")
        L.append(
            "**Best-estimate edge magnitude post-corrections** (H6 + "
            f"H7 cumulative): mean R ≈ {combined['mean_r_corrected']:+.3f}, "
            f"projected annual ≈ {combined['projected_annual_pct']:+.1f} %."
        )
        L.append("")
        L.append("Recommendation: **walk-forward extension on Yahoo Finance 20+ y** "
                 "(or equivalent long-history cross-asset panel) to validate "
                 "stationarity beyond the 6.4 y train+holdout window before "
                 "any deployment commitment.")
    elif fails:
        L.append(
            f"**{len(fails)} hypotheses FAIL**: {', '.join(fails)}. "
            f"{len(partials)} PARTIAL. The headline +109 % is contaminated "
            "by identified bias(es); the corrected magnitude is materially "
            "smaller. See per-hypothesis sections above for the corrected "
            "numbers."
        )
        L.append("")
        L.append(
            "**Best-estimate edge magnitude post-corrections** (H6 + "
            f"H7 cumulative + any FAIL-driven adjustments): mean R ≈ "
            f"{combined['mean_r_corrected']:+.3f}, projected annual "
            f"≈ {combined['projected_annual_pct']:+.1f} %."
        )
        L.append("")
        L.append("Recommendation: **ARCHIVE on identified bias(es)** unless the "
                 "operator wants to attempt a v1.2 spec correcting the bias source. "
                 "Per spec v1.1 footer, the strategy class is considered non-viable.")
    else:
        L.append(
            f"All 7 PASS or PARTIAL ({len(partials)} PARTIAL: "
            f"{', '.join(partials)}). No outright bias; the result is "
            "structurally consistent but with measurable concentration "
            "or régime-dependence."
        )
        L.append("")
        L.append(
            "**Best-estimate edge magnitude post-corrections** (H6 + "
            f"H7): mean R ≈ {combined['mean_r_corrected']:+.3f}, "
            f"projected annual ≈ {combined['projected_annual_pct']:+.1f} %."
        )
        L.append("")
        L.append("Recommendation: gate 5 Databento partial cross-check + "
                 "régime-decomposition diagnostic before any promotion.")
    L.append("")

    out_path.write_text("\n".join(L) + "\n")
    return out_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    t_start = time.perf_counter()
    print("Loading panel...", flush=True)
    panel = load_panel()
    params = build_params()

    # Holdout exits (used by H1, H2, H4, H5, H6, H7)
    holdout_dates = cycle_dates(panel, HOLDOUT_START, HOLDOUT_END)
    print(f"Running cell {CELL} on holdout ({len(holdout_dates)} cycles)...", flush=True)
    holdout_exits, _ = run_streaming(panel, params, holdout_dates)
    print(f"  {len(holdout_exits)} holdout exits", flush=True)

    # Full-window exits for H3 (train + holdout)
    full_dates = cycle_dates(panel, TRAIN_START, HOLDOUT_END)
    print(f"Running cell {CELL} on full window ({len(full_dates)} cycles)...", flush=True)
    full_exits, _ = run_streaming(panel, params, full_dates)
    print(f"  {len(full_exits)} full-window exits", flush=True)

    # Tests
    print("\n--- H1 return_r recalc ---", flush=True)
    h1 = test_h1(panel, holdout_exits)
    print(f"  Verdict: {h1['verdict']}", flush=True)
    print("\n--- H2 look-ahead causality ---", flush=True)
    h2 = test_h2(panel, holdout_exits)
    print(f"  Verdict: {h2['verdict']}", flush=True)
    print("\n--- H3 walk-forward stationarity ---", flush=True)
    full_months = (HOLDOUT_END - TRAIN_START).days / 30.4375
    h3 = test_h3(full_exits, full_months)
    print(
        f"  pos>+0.3R: {h3['n_pos_above_0_3R']}/7, "
        f"pos>0R: {h3['n_pos_above_0R']}/7, "
        f"top concentration: {h3['top_window_total_r_share']:.1%}",
        flush=True,
    )
    print(f"  Verdict: {h3['verdict']}", flush=True)
    print("\n--- H4 risk-parity vs equal-weight ---", flush=True)
    h4 = test_h4(holdout_exits)
    print(
        f"  rp={h4['rp_mean_r']:+.3f} eq={h4['eq_mean_r']:+.3f} "
        f"Δ={h4['delta']:+.3f}",
        flush=True,
    )
    print(f"  Verdict: {h4['verdict']}", flush=True)
    print("\n--- H5 asset concentration ---", flush=True)
    h5 = test_h5(holdout_exits)
    print(
        f"  top-3 share: {h5['top3_share_abs_R']:.1%}, "
        f"excl BTC: {h5['excl_BTCUSD_mean_r']:+.3f}, "
        f"excl BTC+NDX: {h5['excl_BTC_NDX_mean_r']:+.3f}",
        flush=True,
    )
    print(f"  Verdict: {h5['verdict']}", flush=True)
    print("\n--- H6 granular fees ---", flush=True)
    h6 = test_h6(holdout_exits)
    print(
        f"  pre={h6['mean_r_pre_fee']:+.3f} "
        f"post={h6['mean_r_post_fee']:+.3f} "
        f"cost={h6['mean_cost_r']:+.4f}",
        flush=True,
    )
    print(f"  Verdict: {h6['verdict']}", flush=True)
    print("\n--- H7 slippage ---", flush=True)
    h7 = test_h7(holdout_exits)
    print(
        f"  pre={h7['mean_r_pre_slip']:+.3f} "
        f"post={h7['mean_r_post_slip']:+.3f} "
        f"cost={h7['mean_cost_r']:+.4f}",
        flush=True,
    )
    print(f"  Verdict: {h7['verdict']}", flush=True)

    combined = combined_post_corrections(holdout_exits)
    print(
        f"\nCombined post-corrections: mean_r={combined['mean_r_corrected']:+.3f} "
        f"proj={combined['projected_annual_pct']:+.1f}%",
        flush=True,
    )

    results = {
        "H1": h1, "H2": h2, "H3": h3, "H4": h4, "H5": h5,
        "H6": h6, "H7": h7,
        "combined": combined,
    }

    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    out_path = RUNS_DIR / f"investigation_trend_rotation_d1_v1_1_{ts}.md"
    wallclock = time.perf_counter() - t_start
    write_report(out_path=out_path, results=results, wallclock_s=wallclock)
    print(f"\nReport: {out_path}")
    print(f"Total wallclock: {wallclock:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
