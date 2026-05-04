"""Operational risk simulation — trend_rotation_d1 v1.1, cell 126/5/3.

Simulates a $5K Phase-1 FundedNext account compounded over the
20-year walk-forward (2006-01 → 2026-04). Detects bust events
(drawdown from running peak < -8 % of initial capital, FundedNext
Phase-1 total-DD limit) and daily-violation events (single-day
loss > 4 % of initial = -$200).

Outputs:
- ``calibration/runs/operational_risk_trend_rotation_d1_v1_1_<TS>.md``

Methodological note
-------------------
The simulation uses **realised P&L only** (each trade's R hits
equity at exit_timestamp). Mark-to-market intra-trade drawdown
is NOT modelled — open positions can have unrealised drawdown
that would also count against Phase-1 limits in a real account.
The realised-only DD is therefore a LOWER BOUND on real Phase-1
DD risk. Caveat documented in the report.

Run
---
    python -m calibration.operational_risk_trend_rotation_d1_v1_1
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from calibration.walkforward_extended_trend_rotation_d1_v1_1 import (  # noqa: E402
    CELL,
    END,
    START,
    UNIVERSE,
    cycle_dates,
    load_panel_yahoo,
    run_streaming,
)
from src.strategies.trend_rotation_d1 import StrategyParams, TradeExit  # noqa: E402

RUNS_DIR = REPO_ROOT / "calibration" / "runs"

INITIAL_CAPITAL = 5_000.0
DAILY_LIMIT_PCT = -0.04   # -4 % of initial = -$200 max single-day loss
TOTAL_LIMIT_PCT = -0.08   # -8 % of initial = -$400 floor ($4,600)
PROFIT_TARGET_PCT = 0.08  # FundedNext Stellar Lite Phase 1 = +8 %


def simulate_account(
    exits: list[TradeExit],
    *,
    risk_pct: float,
    initial_capital: float = INITIAL_CAPITAL,
) -> dict:
    """Simulate Phase-1 sequential-attempt compatibility.

    Each "attempt" starts at ``initial_capital`` (fresh $5K) and ends
    on the first of:
    - **PASS**: capital ≥ initial × (1 + PROFIT_TARGET_PCT) = $5,400
      (FundedNext Stellar Lite Phase 1 +8 % target → graduate to
      Phase 2; reset and start new attempt).
    - **FAIL total**: capital ≤ initial × (1 + TOTAL_LIMIT_PCT) =
      $4,600 = -8 % static floor.
    - **FAIL daily**: single calendar-day net P&L ≤ -4 % of initial
      = -$200.

    Compounding applies WITHIN an attempt (each trade $ risk =
    risk_pct × current_attempt_capital). Reset to initial after
    every termination.

    The 20-y span thus contains some number of sequential attempts,
    each independent. PASS rate measures Phase-1 compatibility.
    """
    by_day: dict[pd.Timestamp, list[TradeExit]] = defaultdict(list)
    for e in exits:
        d = pd.Timestamp(e.exit_timestamp_utc).normalize()
        if d.tzinfo is None:
            d = d.tz_localize("UTC")
        by_day[d].append(e)

    capital = initial_capital
    attempts: list[dict] = []      # closed attempts (PASS or FAIL)
    daily_equity: list[tuple[pd.Timestamp, float]] = []
    month_data: dict[str, dict] = defaultdict(lambda: {
        "n": 0, "sum_r": 0.0, "net_pnl": 0.0,
        "intra_month_capital_min": float("inf"),
        "intra_month_capital_start": None,
    })
    last_capital_eod = initial_capital

    bust_floor = initial_capital * (1.0 + TOTAL_LIMIT_PCT)
    daily_loss_limit = initial_capital * abs(DAILY_LIMIT_PCT)
    profit_target = initial_capital * (1.0 + PROFIT_TARGET_PCT)

    attempt_start_date = START
    attempt_n_trades = 0
    attempt_sum_r = 0.0
    attempt_id = 1

    def _close_attempt(outcome: str, day, *, asset=None, trade_r=None,
                       trade_pnl=None, day_pnl=None):
        nonlocal attempt_id, attempt_start_date, attempt_n_trades, \
            attempt_sum_r, capital
        attempts.append({
            "id": attempt_id,
            "outcome": outcome,
            "start": attempt_start_date.date().isoformat(),
            "end": day.date().isoformat(),
            "duration_days": (day - attempt_start_date).days,
            "n_trades": attempt_n_trades,
            "sum_r": attempt_sum_r,
            "final_capital": capital,
            "asset": asset,
            "trigger_trade_r": trade_r,
            "trigger_trade_pnl": trade_pnl,
            "trigger_day_pnl": day_pnl,
        })
        attempt_id += 1
        capital = initial_capital
        attempt_start_date = day + pd.Timedelta(days=1)
        attempt_n_trades = 0
        attempt_sum_r = 0.0

    for day in pd.date_range(START, END, freq="D", tz="UTC"):
        day_pnl = 0.0
        day_n = 0
        day_sum_r = 0.0
        capital_at_day_start = capital
        attempt_terminated = False
        terminating_trade = None
        for e in by_day.get(day, []):
            if attempt_terminated:
                # No further trades for this attempt today (closed)
                break
            risk_dollars = capital * risk_pct
            pnl = e.return_r * risk_dollars
            day_pnl += pnl
            day_n += 1
            day_sum_r += e.return_r
            attempt_n_trades += 1
            attempt_sum_r += e.return_r
            capital += pnl

            if capital <= bust_floor:
                _close_attempt(
                    "FAIL_TOTAL", day,
                    asset=e.asset, trade_r=e.return_r, trade_pnl=pnl,
                    day_pnl=day_pnl,
                )
                attempt_terminated = True
                terminating_trade = e
                break
            if capital >= profit_target:
                _close_attempt(
                    "PASS", day,
                    asset=e.asset, trade_r=e.return_r, trade_pnl=pnl,
                    day_pnl=day_pnl,
                )
                attempt_terminated = True
                terminating_trade = e
                break

        # Daily-loss check (only if attempt still open)
        if not attempt_terminated and -day_pnl >= daily_loss_limit:
            _close_attempt(
                "FAIL_DAILY", day,
                day_pnl=day_pnl,
            )
            attempt_terminated = True

        ymonth = f"{day.year:04d}-{day.month:02d}"
        m = month_data[ymonth]
        if m["intra_month_capital_start"] is None:
            m["intra_month_capital_start"] = last_capital_eod
            m["intra_month_capital_min"] = capital
        m["n"] += day_n
        m["sum_r"] += day_sum_r
        m["net_pnl"] += day_pnl
        if capital < m["intra_month_capital_min"]:
            m["intra_month_capital_min"] = capital

        last_capital_eod = capital
        daily_equity.append((day, capital))

    for ymonth, m in month_data.items():
        start_cap = m["intra_month_capital_start"] or initial_capital
        m["intra_month_dd_pct"] = (
            (m["intra_month_capital_min"] - start_cap) / initial_capital
            if start_cap else 0.0
        )

    monthly_keys = sorted(month_data.keys())
    max_streak = 0
    cur_streak = 0
    streak_start = None
    streak_max_start = None
    streak_max_end = None
    for k in monthly_keys:
        if month_data[k]["net_pnl"] < 0:
            cur_streak += 1
            if cur_streak == 1:
                streak_start = k
            if cur_streak > max_streak:
                max_streak = cur_streak
                streak_max_start = streak_start
                streak_max_end = k
        else:
            cur_streak = 0
            streak_start = None

    # Max attempt DD: deepest equity drop within any single attempt
    # (relative to that attempt's $5K starting capital).
    max_dd_pct = 0.0
    attempt_low = initial_capital
    for _, cap in daily_equity:
        if cap == initial_capital:
            attempt_low = initial_capital
        if cap < attempt_low:
            attempt_low = cap
            dd = (cap - initial_capital) / initial_capital
            if dd < max_dd_pct:
                max_dd_pct = dd

    n_pass = sum(1 for a in attempts if a["outcome"] == "PASS")
    n_fail_total = sum(1 for a in attempts if a["outcome"] == "FAIL_TOTAL")
    n_fail_daily = sum(1 for a in attempts if a["outcome"] == "FAIL_DAILY")
    n_attempts = len(attempts)
    pass_rate = n_pass / n_attempts if n_attempts else 0.0

    return {
        "daily_equity": daily_equity,
        "attempts": attempts,
        "n_attempts": n_attempts,
        "n_pass": n_pass,
        "n_fail_total": n_fail_total,
        "n_fail_daily": n_fail_daily,
        "pass_rate": pass_rate,
        # Legacy aliases for the report (n_busts ≡ FAIL_TOTAL,
        # n_daily_violations ≡ FAIL_DAILY)
        "n_busts": n_fail_total,
        "n_daily_violations": n_fail_daily,
        "bust_events": [a for a in attempts if a["outcome"] == "FAIL_TOTAL"],
        "daily_violations": [a for a in attempts if a["outcome"] == "FAIL_DAILY"],
        "max_dd": max_dd_pct,
        "final_capital": capital,
        "month_table": dict(month_data),
        "max_streak_neg_months": max_streak,
        "max_streak_window": (streak_max_start, streak_max_end),
    }


def write_report(out_path: Path, *, exits: list[TradeExit],
                 sim_1pct: dict, sim_05pct: dict,
                 wallclock_s: float) -> Path:
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    L: list[str] = []
    L.append(f"# Operational risk simulation 20y — trend_rotation_d1 v1.1 cell 126/5/3 ({ts})")
    L.append("")
    L.append(
        f"Capital: ${INITIAL_CAPITAL:.0f} (FundedNext Stellar Lite Phase 1). "
        f"Daily-loss limit: {DAILY_LIMIT_PCT:.0%} of initial = "
        f"${INITIAL_CAPITAL*abs(DAILY_LIMIT_PCT):.0f} max single-day loss. "
        f"Total-DD limit: equity must stay ≥ initial − 8 % = "
        f"${INITIAL_CAPITAL*(1+TOTAL_LIMIT_PCT):.0f} (static threshold). "
        f"On bust: account closed and capital reset to ${INITIAL_CAPITAL:.0f} "
        f"for next Phase-1 attempt. Window 2006-01 → 2026-04, 20.3 y. "
        f"Pipeline cell 126/5/3 on the 15-asset Yahoo panel produced "
        f"{len(exits)} closed trades."
    )
    L.append("")
    L.append(f"Wallclock: {wallclock_s:.1f} s.")
    L.append("")

    L.append("**Methodological caveat**: realised-P&L-only simulation. "
             "Mark-to-market intra-trade drawdown is not modelled. The "
             "DD numbers below are LOWER bounds on real Phase-1 risk.")
    L.append("")

    # Synthèse
    L.append("## Synthèse — sequential Phase-1 attempts")
    L.append("")
    L.append(
        "Each attempt starts at $5K. End on first of: PASS (+8 % = "
        "$5,400), FAIL_TOTAL (≤ -8 % = $4,600), FAIL_DAILY (single-"
        "day -$200). Reset on close, continue with next attempt."
    )
    L.append("")
    L.append("| Risk | n attempts | PASS | FAIL_TOTAL | FAIL_DAILY | PASS rate | Worst attempt DD |")
    L.append("|---|---:|---:|---:|---:|---:|---:|")
    L.append(
        f"| **1.0 %** | {sim_1pct['n_attempts']} | {sim_1pct['n_pass']} | "
        f"{sim_1pct['n_fail_total']} | {sim_1pct['n_fail_daily']} | "
        f"{sim_1pct['pass_rate']:.1%} | {sim_1pct['max_dd']:+.1%} |"
    )
    L.append(
        f"| **0.5 %** | {sim_05pct['n_attempts']} | {sim_05pct['n_pass']} | "
        f"{sim_05pct['n_fail_total']} | {sim_05pct['n_fail_daily']} | "
        f"{sim_05pct['pass_rate']:.1%} | {sim_05pct['max_dd']:+.1%} |"
    )
    L.append("")

    # Verdict — pass-rate based (Phase-1 compatibility ≈ how many
    # attempts would have succeeded). Pre-spec verdict thresholds
    # adapted from "n busts" form (which only counted failures) to
    # the more informative pass-rate.
    def _v(pass_rate: float, n_fail_total: int, n_fail_daily: int) -> str:
        n_fail = n_fail_total + n_fail_daily
        if pass_rate >= 0.80 and n_fail <= 2:
            return f"✅ PHASE-1-COMPATIBLE (pass rate {pass_rate:.0%}, {n_fail} fails)"
        if pass_rate >= 0.50:
            return f"⚠️ RISQUÉ MAIS ACCEPTABLE (pass rate {pass_rate:.0%}, {n_fail} fails)"
        if pass_rate >= 0.20:
            return f"❌ NON-DÉPLOYABLE TEL QUEL (pass rate {pass_rate:.0%}, {n_fail} fails)"
        return f"❌ STRUCTURELLEMENT INCOMPATIBLE (pass rate {pass_rate:.0%})"

    L.append(f"- **Verdict 1 % risk**: {_v(sim_1pct['pass_rate'], sim_1pct['n_fail_total'], sim_1pct['n_fail_daily'])}")
    L.append(f"- **Verdict 0.5 % risk**: {_v(sim_05pct['pass_rate'], sim_05pct['n_fail_total'], sim_05pct['n_fail_daily'])}")
    L.append("")

    # Per-attempt outcomes (1% risk)
    L.append("## 1. Per-attempt outcomes @ 1 % risk")
    L.append("")
    L.append(
        "| # | start → end | days | n_trades | sum_r | outcome | trigger | final $ |"
    )
    L.append("|---:|---|---:|---:|---:|:---:|---|---:|")
    for a in sim_1pct["attempts"][:40]:
        emoji = {"PASS": "✅", "FAIL_TOTAL": "❌", "FAIL_DAILY": "🔻"}[a["outcome"]]
        trigger = ""
        if a["asset"]:
            trigger = f"{a['asset']} R={a['trigger_trade_r']:+.2f}"
            if a["outcome"] == "FAIL_DAILY":
                trigger = f"day P&L ${a['trigger_day_pnl']:+,.0f}"
        L.append(
            f"| {a['id']} | {a['start']} → {a['end']} | {a['duration_days']} | "
            f"{a['n_trades']} | {a['sum_r']:+.1f} | {emoji} {a['outcome']} | "
            f"{trigger} | ${a['final_capital']:,.0f} |"
        )
    if len(sim_1pct["attempts"]) > 40:
        L.append(f"\n…(+ {len(sim_1pct['attempts']) - 40} more attempts)")
    L.append("")

    # Per-attempt outcomes (0.5% risk)
    L.append("## 2. Per-attempt outcomes @ 0.5 % risk")
    L.append("")
    L.append(
        "| # | start → end | days | n_trades | sum_r | outcome | trigger | final $ |"
    )
    L.append("|---:|---|---:|---:|---:|:---:|---|---:|")
    for a in sim_05pct["attempts"][:40]:
        emoji = {"PASS": "✅", "FAIL_TOTAL": "❌", "FAIL_DAILY": "🔻"}[a["outcome"]]
        trigger = ""
        if a["asset"]:
            trigger = f"{a['asset']} R={a['trigger_trade_r']:+.2f}"
            if a["outcome"] == "FAIL_DAILY":
                trigger = f"day P&L ${a['trigger_day_pnl']:+,.0f}"
        L.append(
            f"| {a['id']} | {a['start']} → {a['end']} | {a['duration_days']} | "
            f"{a['n_trades']} | {a['sum_r']:+.1f} | {emoji} {a['outcome']} | "
            f"{trigger} | ${a['final_capital']:,.0f} |"
        )
    if len(sim_05pct["attempts"]) > 40:
        L.append(f"\n…(+ {len(sim_05pct['attempts']) - 40} more attempts)")
    L.append("")

    # Monthly granularity (1% risk)
    L.append("## 3. Drawdown granularité mensuelle @ 1 % risk")
    L.append("")
    months = sim_1pct["month_table"]
    months_sorted = sorted(months.items())
    n_months = len(months_sorted)
    n_neg = sum(1 for _, m in months_sorted if m["net_pnl"] < 0)
    n_dd_warn = sum(
        1 for _, m in months_sorted
        if m["intra_month_dd_pct"] < -0.04
    )
    n_dd_severe = sum(
        1 for _, m in months_sorted
        if m["intra_month_dd_pct"] < -0.08
    )
    L.append(f"- Total months: {n_months}")
    L.append(f"- Months with negative net P&L: **{n_neg} / {n_months}** "
             f"({n_neg / n_months * 100:.1f} %)")
    L.append(f"- Months with intra-month DD < -4 % of init: **{n_dd_warn} / {n_months}** "
             f"(warning zone)")
    L.append(f"- Months with intra-month DD < -8 % of init: **{n_dd_severe} / {n_months}** "
             f"(would have busted)")
    L.append("")

    # Worst negative streak
    streak = sim_1pct["max_streak_neg_months"]
    s_start, s_end = sim_1pct["max_streak_window"]
    L.append(
        f"- **Worst consecutive-negative-months streak**: {streak} months "
        f"({s_start} → {s_end})"
    )
    L.append("")

    # Worst 12 months by intra-month DD
    L.append("### Worst 12 months by intra-month DD (1 % risk)")
    L.append("")
    by_dd = sorted(months_sorted, key=lambda x: x[1]["intra_month_dd_pct"])
    L.append("| Month | n_trades | sum_r | net P&L $ | intra-month DD % | violation |")
    L.append("|---|---:|---:|---:|---:|:---:|")
    for ym, m in by_dd[:12]:
        viol = "❌" if m["intra_month_dd_pct"] < -0.08 else (
            "⚠️" if m["intra_month_dd_pct"] < -0.04 else ""
        )
        L.append(
            f"| {ym} | {m['n']} | {m['sum_r']:+.2f} | "
            f"${m['net_pnl']:+,.0f} | "
            f"{m['intra_month_dd_pct']:+.1%} | {viol} |"
        )
    L.append("")

    # FAIL pattern analysis
    fails_1 = [a for a in sim_1pct["attempts"]
               if a["outcome"] in ("FAIL_TOTAL", "FAIL_DAILY")]
    if fails_1:
        L.append("## 4. FAIL pattern analysis @ 1 % risk")
        L.append("")
        by_year: dict[str, int] = defaultdict(int)
        by_asset: dict[str, int] = defaultdict(int)
        for a in fails_1:
            by_year[a["end"][:4]] += 1
            if a.get("asset"):
                by_asset[a["asset"]] += 1
        L.append("Fails by year:")
        L.append("")
        L.append("| Year | n fails |")
        L.append("|---|---:|")
        for y in sorted(by_year.keys()):
            L.append(f"| {y} | {by_year[y]} |")
        L.append("")
        if by_asset:
            L.append("Fails by triggering asset:")
            L.append("")
            L.append("| Asset | n fails |")
            L.append("|---|---:|")
            for a in sorted(by_asset.keys(), key=lambda x: -by_asset[x]):
                L.append(f"| {a} | {by_asset[a]} |")
            L.append("")

    # Path forward
    L.append("## 5. Path forward decision")
    L.append("")
    pr_1 = sim_1pct["pass_rate"]
    pr_05 = sim_05pct["pass_rate"]
    if pr_1 >= 0.80:
        L.append(
            f"Strategy is Phase-1 compatible at 1 % risk: pass rate "
            f"{pr_1:.0%} over {sim_1pct['n_attempts']} sequential attempts "
            f"on 20 y. **Suggested next**: gate 6 MT5 sanity check on "
            f"FundedNext Stellar Lite Phase 1 demo, with explicit MTM "
            f"risk monitoring during the first month live."
        )
    elif pr_05 >= 0.80:
        L.append(
            f"Pass rate at 1 % risk = {pr_1:.0%} (insuffisant); à 0.5 % "
            f"risk = {pr_05:.0%} (compatible). **Suggested next**: "
            f"discussion opérateur sur trade-off magnitude (proj annual "
            f"réduit ÷ 2 à 0.5 % risk) vs sécurité Phase 1. Si OK avec "
            f"magnitude réduite, gate 6 MT5 à 0.5 % risk per trade."
        )
    elif pr_1 >= 0.50 or pr_05 >= 0.50:
        L.append(
            f"Pass rate borderline: 1 % = {pr_1:.0%}, 0.5 % = {pr_05:.0%}. "
            f"Strategy survives Phase 1 ~ half the time. Réalistement, "
            f"l'opérateur paierait $50-100 par tentative pour ~50 % de "
            f"taux de réussite. **REVIEW** — discussion opérateur sur "
            f"acceptabilité ou re-spec avec exclusion crypto."
        )
    else:
        L.append(
            f"Pass rate < 50 % à 1 % et 0.5 % risk → **non-déployable "
            f"Phase 1**. Per spec v1.1 footer, classe non-viable pour "
            f"le contexte opérateur. **Suggested next**: ARCHIVE final, "
            f"5e archive de la phase strategy-research."
        )
    L.append("")

    # Caveats
    L.append("## 6. Caveats")
    L.append("")
    L.append(
        "- **Realised-P&L only**: open positions can have unrealised "
        "drawdown not captured here. Real Phase-1 mark-to-market DD is "
        "≥ realised DD; busts could fire during open trades that this "
        "simulation misses. **The bust counts above are LOWER BOUNDS.**"
    )
    L.append(
        "- **No spread / commission / slippage** in the simulation: "
        "real broker fills add 0.1-0.3 R/trade cost, which would push "
        "DD deeper. Combined with the H6 + H7 investigation finding, "
        "expect ~5-10 % additional DD vs realised-only."
    )
    L.append(
        "- **Compounding sized at exit_time of prior trade**, not at "
        "entry_time of current trade. Mismatch is small (overlapping "
        "trades within the K=5 basket) but means actual position "
        "sizing can diverge by 1-2 % from the simulated."
    )
    L.append(
        "- **Yahoo data quality**: futures continuous contracts (GC=F, "
        "SI=F, CL=F) have level offsets vs FundedNext spot/CFD. The "
        "ratio-based momentum signal is preserved but the realised R "
        "magnitude on those instruments may differ slightly on real "
        "FundedNext fills."
    )
    L.append("")

    out_path.write_text("\n".join(L) + "\n")
    return out_path


def main() -> int:
    t0 = time.perf_counter()
    print("Loading 15-asset Yahoo panel...", flush=True)
    panel = load_panel_yahoo()

    params = StrategyParams(
        universe=UNIVERSE,
        momentum_lookback_days=CELL["momentum"],
        K=CELL["K"],
        rebalance_frequency_days=CELL["rebalance"],
        risk_per_trade_pct=1.0,
        atr_period=20,
        atr_explosive_threshold=5.0,
        atr_regime_lookback=90,
    )
    print(f"Running cell 126/5/3 on {START.date()} → {END.date()}...", flush=True)
    dates = cycle_dates(panel, START, END)
    exits, _ = run_streaming(panel, params, dates)
    print(f"  {len(exits)} closed trades", flush=True)

    print("\nSimulating sequential Phase-1 attempts @ 1.0 % risk...", flush=True)
    sim_1 = simulate_account(exits, risk_pct=0.01)
    print(f"  attempts={sim_1['n_attempts']} PASS={sim_1['n_pass']} "
          f"FAIL_T={sim_1['n_fail_total']} FAIL_D={sim_1['n_fail_daily']} "
          f"pass_rate={sim_1['pass_rate']:.1%} maxDD={sim_1['max_dd']:+.1%}",
          flush=True)

    print("\nSimulating sequential Phase-1 attempts @ 0.5 % risk...", flush=True)
    sim_05 = simulate_account(exits, risk_pct=0.005)
    print(f"  attempts={sim_05['n_attempts']} PASS={sim_05['n_pass']} "
          f"FAIL_T={sim_05['n_fail_total']} FAIL_D={sim_05['n_fail_daily']} "
          f"pass_rate={sim_05['pass_rate']:.1%} maxDD={sim_05['max_dd']:+.1%}",
          flush=True)

    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    out_path = RUNS_DIR / f"operational_risk_trend_rotation_d1_v1_1_{ts}.md"
    wallclock = time.perf_counter() - t0
    write_report(
        out_path, exits=exits,
        sim_1pct=sim_1, sim_05pct=sim_05,
        wallclock_s=wallclock,
    )

    # Compact JSON dump (no daily equity to save space)
    json_path = RUNS_DIR / f"operational_risk_trend_rotation_d1_v1_1_{ts}.json"
    def _slim(s: dict) -> dict:
        return {
            "n_attempts": s["n_attempts"],
            "n_pass": s["n_pass"],
            "n_fail_total": s["n_fail_total"],
            "n_fail_daily": s["n_fail_daily"],
            "pass_rate": s["pass_rate"],
            "max_dd": s["max_dd"],
            "max_streak_neg_months": s["max_streak_neg_months"],
            "attempts": s["attempts"],
        }
    json_dump = {
        "n_trades": len(exits),
        "sim_1pct": _slim(sim_1),
        "sim_05pct": _slim(sim_05),
    }
    json_path.write_text(json.dumps(json_dump, indent=2, default=str))

    print(f"\nReport: {out_path}")
    print(f"Total wallclock: {wallclock:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
