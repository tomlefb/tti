"""Pyramidal + Scale-Up economic simulation — trend_rotation_d1 v1.1
cell 126/5/3, 4 scaling strategies compared over 20 y of history.

FundedNext Stellar Lite rules (verified April 2026):
- Tier sizes: $5K, $25K, $50K, $100K, $200K
- Fees with VIBES 30 % off promo: $23, $97, $160, $385, $770
- P1 target +8 %, P2 target +4 %, daily DD -4 %, max DD -8 %
- Profit split: 80 % initial → 90 % post first Scale-Up
- Stellar Lite specific: P1 fee refunded at 3rd payout received
- First payout 21 days post funded entry, then every 14 days
- Scale-Up: +40 % balance every 4 calendar months if profitable
  (≥ 2 payouts in the 4-mo period); cap at $300K (CFD practical)
- Stellar Lite tier cap: $200K (Scale-Up beyond breaks tier limit)

Strategies compared:
- A — Baseline: 1× $5K loop, no scaling.
- B — Pyramidal manual: on each "first payout received" of an
  account, upgrade to next tier (use accumulated wallet to pay fee).
- C — Scale-Up native: stay at $5K initial, trigger native Scale-Up
  every 4 months when eligible.
- D — Hybrid: Pyramid up to $25K, then Scale-Up on $25K account.

Output:
- ``calibration/runs/economic_pyramidal_trend_rotation_d1_v1_1_<TS>.md``
- ``..._<TS>.json``

Run
---
    python -m calibration.economic_pyramidal_trend_rotation_d1_v1_1
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

TIERS = [
    {"size": 5_000.0,   "fee": 23.0},
    {"size": 25_000.0,  "fee": 97.0},
    {"size": 50_000.0,  "fee": 160.0},
    {"size": 100_000.0, "fee": 385.0},
    {"size": 200_000.0, "fee": 770.0},
]

DAILY_LIMIT_PCT = 0.04
TOTAL_DD_PCT = 0.08
P1_TARGET = 0.08
P2_TARGET = 0.04
PROFIT_SPLIT_INITIAL = 0.80
PROFIT_SPLIT_POST_SU = 0.90

# Stellar Lite payout cadence
DAYS_TO_FIRST_PAYOUT = 21
DAYS_BETWEEN_PAYOUTS = 14
PAYOUT_FOR_FEE_REFUND = 3  # Stellar Lite refund at 3rd payout
MIN_PAYOUT_USD = 100.0

# Scale-Up rules
SCALEUP_PERIOD_DAYS = 120        # 4 calendar months
SCALEUP_MIN_PAYOUTS = 2          # required in the 4-month period
SCALEUP_FACTOR = 1.40
SCALEUP_CAP = 300_000.0          # CFD practical cap

RISK_PCT = 0.01                  # best from prior simulation


def initial_state(strategy: str) -> dict:
    """Common state machine for all 4 strategies."""
    t0 = TIERS[0]
    return {
        "strategy": strategy,
        "wallet": -t0["fee"],            # cumulative cash net (already paid initial fee)
        "tier_idx": 0,                   # current account tier (index in TIERS)
        "balance": t0["size"],           # current account equity
        "tier_initial_balance": t0["size"],   # account's starting balance (changes on Scale-Up)
        "phase": "phase_1",
        "hwm": t0["size"],               # high-watermark for funded payouts
        "funded_entry_date": None,       # when this account entered funded
        "next_payout_date": None,        # next eligible payout date (Stellar Lite)
        "payouts_this_account": 0,       # for fee refund + pyramidal trigger
        "payouts_in_scaleup_period": 0,  # for Scale-Up eligibility
        "scaleup_period_start": None,
        "profit_split": PROFIT_SPLIT_INITIAL,
        "fee_refunded": False,
        "scaleups_done": 0,
        "max_balance_reached": t0["size"],
        # Counters
        "n_attempts_paid": 1,            # initial entry counts
        "n_p1_pass": 0,
        "n_p2_pass": 0,
        "n_funded_busts": 0,
        "n_phase_busts": 0,              # P1 or P2 busts
        "n_payouts_total": 0,
        "n_tier_upgrades": 0,
        "n_scaleups": 0,
        "total_paid": t0["fee"],
        "total_payouts": 0.0,
        "events": [],
    }


def _restart_phase_1_same_tier(state: dict, day: pd.Timestamp,
                               *, was_funded: bool, reason: str) -> None:
    """On bust: rebuy the SAME tier if wallet allows; else step down to
    highest affordable tier (lowest = $5K which is always affordable
    for typical wallet trajectories)."""
    tier = TIERS[state["tier_idx"]]
    state["events"].append({
        "date": day.date().isoformat(),
        "type": f"bust_{reason}",
        "phase": state["phase"],
        "tier_idx": state["tier_idx"],
        "tier_size": tier["size"],
        "balance_at_bust": state["balance"],
        "was_funded": was_funded,
    })
    if was_funded:
        state["n_funded_busts"] += 1
    else:
        state["n_phase_busts"] += 1

    # Choose tier to rebuy: same if affordable, else step down
    target_idx = state["tier_idx"]
    while target_idx > 0:
        # Check if we can afford. We don't actually require wallet > 0;
        # we just record the cost. (Operator pays out of cumulative net.)
        # Pyramidal logic: prefer same tier. If we're going to be deeply
        # negative with same tier, drop. Threshold: don't rebuy if
        # wallet < -2 × tier fee (arbitrary heuristic).
        if state["wallet"] >= -2 * TIERS[target_idx]["fee"]:
            break
        target_idx -= 1
    new_tier = TIERS[target_idx]
    state["wallet"] -= new_tier["fee"]
    state["total_paid"] += new_tier["fee"]
    state["n_attempts_paid"] += 1
    state["tier_idx"] = target_idx
    state["balance"] = new_tier["size"]
    state["tier_initial_balance"] = new_tier["size"]
    state["phase"] = "phase_1"
    state["hwm"] = new_tier["size"]
    state["funded_entry_date"] = None
    state["next_payout_date"] = None
    state["payouts_this_account"] = 0
    state["payouts_in_scaleup_period"] = 0
    state["scaleup_period_start"] = None
    state["profit_split"] = PROFIT_SPLIT_INITIAL
    state["fee_refunded"] = False
    state["scaleups_done"] = 0


def _try_upgrade_tier(state: dict, day: pd.Timestamp) -> None:
    """Strategy B: on first payout, upgrade to next tier (discard current)."""
    if state["tier_idx"] >= len(TIERS) - 1:
        return
    next_tier = TIERS[state["tier_idx"] + 1]
    if state["wallet"] < next_tier["fee"]:
        return
    state["events"].append({
        "date": day.date().isoformat(),
        "type": "tier_upgrade",
        "from_tier": TIERS[state["tier_idx"]]["size"],
        "to_tier": next_tier["size"],
    })
    state["wallet"] -= next_tier["fee"]
    state["total_paid"] += next_tier["fee"]
    state["n_attempts_paid"] += 1
    state["n_tier_upgrades"] += 1
    state["tier_idx"] += 1
    state["balance"] = next_tier["size"]
    state["tier_initial_balance"] = next_tier["size"]
    state["phase"] = "phase_1"
    state["hwm"] = next_tier["size"]
    state["funded_entry_date"] = None
    state["next_payout_date"] = None
    state["payouts_this_account"] = 0
    state["payouts_in_scaleup_period"] = 0
    state["scaleup_period_start"] = None
    state["profit_split"] = PROFIT_SPLIT_INITIAL
    state["fee_refunded"] = False
    state["scaleups_done"] = 0


def _try_scale_up(state: dict, day: pd.Timestamp) -> None:
    """Strategy C / D: trigger native Scale-Up if eligible.

    Eligibility (per FundedNext): in funded, ≥ 4 calendar months since
    last check, ≥ 2 payouts in the period, account profitable (capital
    > tier_initial_balance).
    """
    if state["phase"] != "funded":
        return
    if state["scaleup_period_start"] is None:
        return
    days_since = (day - state["scaleup_period_start"]).days
    if days_since < SCALEUP_PERIOD_DAYS:
        return
    if state["payouts_in_scaleup_period"] < SCALEUP_MIN_PAYOUTS:
        # Reset period without scaling
        state["scaleup_period_start"] = day
        state["payouts_in_scaleup_period"] = 0
        return
    # Eligible: scale up
    new_balance = min(
        state["tier_initial_balance"] * SCALEUP_FACTOR, SCALEUP_CAP
    )
    if new_balance <= state["tier_initial_balance"]:
        return
    state["events"].append({
        "date": day.date().isoformat(),
        "type": "scale_up",
        "old_tier_initial": state["tier_initial_balance"],
        "new_tier_initial": new_balance,
        "balance_before": state["balance"],
    })
    diff = new_balance - state["tier_initial_balance"]
    state["balance"] += diff       # FundedNext credits the +40% to current equity
    state["tier_initial_balance"] = new_balance
    state["hwm"] = state["balance"]
    state["scaleups_done"] += 1
    state["scaleup_period_start"] = day
    state["payouts_in_scaleup_period"] = 0
    if state["profit_split"] < PROFIT_SPLIT_POST_SU:
        state["profit_split"] = PROFIT_SPLIT_POST_SU


def simulate(exits: list[TradeExit], strategy: str) -> dict:
    """Run simulation for one strategy."""
    by_day: dict[pd.Timestamp, list[TradeExit]] = defaultdict(list)
    for e in exits:
        d = pd.Timestamp(e.exit_timestamp_utc).normalize()
        if d.tzinfo is None:
            d = d.tz_localize("UTC")
        by_day[d].append(e)

    state = initial_state(strategy)
    cumulative_curve: list[tuple[str, float, str, int]] = []
    # (date, wallet+balance_above_initial, phase, tier_idx)

    for day in pd.date_range(START, END, freq="D", tz="UTC"):
        day_pnl = 0.0
        terminated = False
        for e in by_day.get(day, []):
            if terminated:
                break
            risk_dollars = state["balance"] * RISK_PCT
            pnl = e.return_r * risk_dollars
            day_pnl += pnl
            state["balance"] += pnl
            if state["balance"] > state["max_balance_reached"]:
                state["max_balance_reached"] = state["balance"]

            tier_init = state["tier_initial_balance"]
            # Bust check: capital ≤ initial × (1 − 0.08)
            if state["balance"] <= tier_init * (1 - TOTAL_DD_PCT):
                _restart_phase_1_same_tier(
                    state, day,
                    was_funded=(state["phase"] == "funded"),
                    reason="total",
                )
                terminated = True
                break

            # Phase progression
            if state["phase"] == "phase_1" and state["balance"] >= tier_init * (1 + P1_TARGET):
                state["events"].append({
                    "date": day.date().isoformat(),
                    "type": "phase_1_pass",
                    "tier": tier_init,
                })
                state["n_p1_pass"] += 1
                state["phase"] = "phase_2"
                state["balance"] = tier_init   # reset
                state["hwm"] = tier_init
                terminated = True
                break
            if state["phase"] == "phase_2" and state["balance"] >= tier_init * (1 + P2_TARGET):
                state["events"].append({
                    "date": day.date().isoformat(),
                    "type": "phase_2_pass",
                    "tier": tier_init,
                })
                state["n_p2_pass"] += 1
                state["phase"] = "funded"
                state["balance"] = tier_init
                state["hwm"] = tier_init
                state["funded_entry_date"] = day
                state["next_payout_date"] = day + pd.Timedelta(days=DAYS_TO_FIRST_PAYOUT)
                state["scaleup_period_start"] = day
                state["payouts_in_scaleup_period"] = 0
                terminated = True
                break

        # Daily-loss check (still alive)
        if not terminated and -day_pnl >= state["tier_initial_balance"] * DAILY_LIMIT_PCT:
            _restart_phase_1_same_tier(
                state, day,
                was_funded=(state["phase"] == "funded"),
                reason="daily",
            )
            terminated = True

        # Payout check (Funded + payout date reached)
        if (
            state["phase"] == "funded"
            and state["next_payout_date"] is not None
            and day >= state["next_payout_date"]
            and state["balance"] > state["hwm"]
        ):
            gross_profit = state["balance"] - state["hwm"]
            payout = state["profit_split"] * gross_profit
            if payout >= MIN_PAYOUT_USD:
                state["wallet"] += payout
                state["total_payouts"] += payout
                state["balance"] -= payout
                state["hwm"] = state["balance"]
                state["payouts_this_account"] += 1
                state["payouts_in_scaleup_period"] += 1
                state["n_payouts_total"] += 1
                state["events"].append({
                    "date": day.date().isoformat(),
                    "type": "payout",
                    "amount": payout,
                    "tier_initial": state["tier_initial_balance"],
                    "balance_after": state["balance"],
                })

                # Stellar Lite fee refund at 3rd payout
                if (
                    not state["fee_refunded"]
                    and state["payouts_this_account"] == PAYOUT_FOR_FEE_REFUND
                ):
                    refund = TIERS[state["tier_idx"]]["fee"]
                    state["wallet"] += refund
                    state["fee_refunded"] = True
                    state["events"].append({
                        "date": day.date().isoformat(),
                        "type": "fee_refund",
                        "amount": refund,
                    })

                # Strategy B / D: upgrade tier on first payout
                if strategy == "B" and state["payouts_this_account"] == 1:
                    _try_upgrade_tier(state, day)
                elif (
                    strategy == "D"
                    and state["payouts_this_account"] == 1
                    and state["tier_idx"] < 1   # only pyramid up to $25K
                ):
                    _try_upgrade_tier(state, day)

                # Schedule next payout
                state["next_payout_date"] = day + pd.Timedelta(days=DAYS_BETWEEN_PAYOUTS)

        # Scale-Up check (C / D), once per day max
        if strategy in ("C", "D"):
            _try_scale_up(state, day)

        # Cumulative-value tracking: wallet (cash net) +
        # current-balance-above-initial-when-funded (paper profit
        # vested when payout happens, but track for visibility)
        paper_above = (
            state["balance"] - state["tier_initial_balance"]
            if state["phase"] == "funded"
            else 0.0
        )
        cumulative_curve.append(
            (day.date().isoformat(), state["wallet"] + paper_above,
             state["phase"], state["tier_idx"])
        )

    state["net_pnl"] = state["total_payouts"] - state["total_paid"]
    state["cumulative_curve"] = cumulative_curve
    return state


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------


def time_to_breakeven_months(curve) -> int | None:
    if not curve:
        return None
    start = pd.Timestamp(curve[0][0])
    for date_str, val, _, _ in curve:
        if val > 0:
            return int((pd.Timestamp(date_str) - start).days / 30.4375) + 1
    return None


def worst_drawdown(curve) -> dict:
    if not curve:
        return {"max_dd": 0.0, "peak_date": None, "trough_date": None}
    peak = float("-inf")
    peak_date = None
    worst_dd = 0.0
    worst_peak = None
    worst_trough = None
    for date_str, val, _, _ in curve:
        if val > peak:
            peak = val
            peak_date = date_str
        dd = val - peak
        if dd < worst_dd:
            worst_dd = dd
            worst_peak = peak_date
            worst_trough = date_str
    return {"max_dd": worst_dd, "peak_date": worst_peak, "trough_date": worst_trough}


def time_to_value(curve, target: float) -> int | None:
    if not curve:
        return None
    start = pd.Timestamp(curve[0][0])
    for date_str, val, _, _ in curve:
        if val >= target:
            return int((pd.Timestamp(date_str) - start).days / 30.4375) + 1
    return None


def etf_benchmark(panel: dict, total_invested: float) -> dict:
    """Lump-sum ETF comparison: total_invested at START, evaluated at END."""
    sp = panel["SPX500"]
    start_price = float(sp.loc[sp.index <= START, "close"].iloc[-1])
    end_price = float(sp.loc[sp.index <= END, "close"].iloc[-1])
    units = total_invested / start_price if start_price > 0 else 0
    final_value = units * end_price
    n_years = (END - START).days / 365.25
    annualised = (
        ((final_value / total_invested) ** (1 / n_years) - 1) * 100
        if total_invested > 0 and final_value > 0 else 0.0
    )
    return {
        "total_invested": total_invested,
        "final_value": final_value,
        "total_return_pct": (final_value - total_invested) / total_invested * 100
        if total_invested > 0 else 0.0,
        "annualized_return_pct": annualised,
    }


def write_report(out_path: Path, *,
                 results: dict[str, dict],
                 etf: dict, wallclock_s: float) -> Path:
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    L: list[str] = []
    L.append(f"# Pyramidal + Scale-Up economic simulation 20y — trend_rotation_d1 v1.1 ({ts})")
    L.append("")
    L.append(
        "Compares 4 capital-scaling strategies on the same 1000 trades "
        "2006-01 → 2026-04 (cell 126/5/3, 15-asset Yahoo panel). Risk per "
        f"trade: {RISK_PCT*100:.1f} %. Fees with VIBES 30 % off promo. "
        "Stellar Lite rules (April 2026): 80 % → 90 % profit split post "
        "Scale-Up, P1 fee refunded at 3rd payout, payouts every 14 d "
        "(first 21 d), Scale-Up +40 % every 4 mo if ≥ 2 payouts in period."
    )
    L.append("")
    L.append(f"Wallclock: {wallclock_s:.1f} s.")
    L.append("")

    L.append("## Synthèse — 4 stratégies comparées")
    L.append("")
    L.append(
        "| Strategy | Net P&L 20 y | Total paid | Total received | Max balance | "
        "Time → $50K | Worst DD | ROI |"
    )
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for label in ["A", "B", "C", "D"]:
        s = results[label]
        roi = (s["net_pnl"] / s["total_paid"] * 100) if s["total_paid"] else 0.0
        ttv = time_to_value(s["cumulative_curve"], 50_000)
        wd = worst_drawdown(s["cumulative_curve"])
        L.append(
            f"| **{label}** | ${s['net_pnl']:+,.0f} | ${s['total_paid']:,.0f} | "
            f"${s['total_payouts']:,.0f} | ${s['max_balance_reached']:,.0f} | "
            f"{ttv if ttv else 'never'} mo | "
            f"${wd['max_dd']:+,.0f} | {roi:+.0f} % |"
        )
    L.append("")

    # Find best
    best_label = max(results, key=lambda k: results[k]["net_pnl"])
    L.append(f"**Best strategy by Net P&L**: **{best_label}** (${results[best_label]['net_pnl']:+,.0f})")
    L.append("")

    # Per-strategy detail
    for label in ["A", "B", "C", "D"]:
        s = results[label]
        L.append(f"## Strategy {label}")
        L.append("")
        if label == "A":
            L.append("**Baseline**: 1× $5K loop, no scaling. Reference scenario.")
        elif label == "B":
            L.append("**Pyramidal manual**: on first payout received, upgrade to next tier "
                     "($5K → $25K → $50K → $100K → $200K). Cap at $200K.")
        elif label == "C":
            L.append("**Scale-Up native**: stay at $5K initial, trigger native Scale-Up "
                     "every 4 months when eligible. Cap at $300K.")
        elif label == "D":
            L.append("**Hybrid**: pyramidal up to $25K, then Scale-Up native on $25K. "
                     "Cap at $300K.")
        L.append("")
        L.append(f"- **Net P&L**: ${s['net_pnl']:+,.0f}")
        L.append(f"- Total paid: ${s['total_paid']:,.0f} ({s['n_attempts_paid']} fees)")
        L.append(f"- Total received: ${s['total_payouts']:,.0f} ({s['n_payouts_total']} payouts)")
        L.append(f"- Max balance reached: ${s['max_balance_reached']:,.0f}")
        L.append(f"- Funnel: {s['n_p1_pass']} P1 PASS, {s['n_p2_pass']} P2 PASS, "
                 f"{s['n_funded_busts']} funded busts")
        if label in ("B", "D"):
            L.append(f"- Tier upgrades performed: {s['n_tier_upgrades']}")
        if label in ("C", "D"):
            L.append(f"- Scale-Ups performed: {s['n_scaleups']}")
        ttb = time_to_breakeven_months(s["cumulative_curve"])
        L.append(f"- Time-to-breakeven: {ttb if ttb else 'never'} months")
        wd = worst_drawdown(s["cumulative_curve"])
        L.append(f"- Worst cumulative drawdown: ${wd['max_dd']:+,.0f} "
                 f"({wd['peak_date']} → {wd['trough_date']})")
        # End state
        end_tier = TIERS[s["tier_idx"]]
        L.append(f"- End state: tier ${end_tier['size']:,.0f}, balance ${s['balance']:,.0f}, "
                 f"phase {s['phase']}, scaleups {s['scaleups_done']}, "
                 f"profit split {s['profit_split']*100:.0f} %")
        L.append("")

    # Phase milestones for the best strategy
    L.append("## Phase milestones (best strategy)")
    L.append("")
    s_best = results[best_label]
    for tgt in [1_000, 5_000, 10_000, 25_000, 50_000, 100_000]:
        ttv = time_to_value(s_best["cumulative_curve"], tgt)
        L.append(f"- Time to cumulative ≥ ${tgt:,.0f}: "
                 f"{ttv if ttv else 'never'} months")
    L.append("")

    # ETF benchmark
    L.append("## ETF S&P 500 benchmark (lump-sum at start)")
    L.append("")
    L.append("Compare with $1K invested in SPX500 at 2006-01-01:")
    L.append("")
    L.append(f"- Total invested: ${etf['total_invested']:,.0f}")
    L.append(f"- Final value: ${etf['final_value']:,.0f}")
    L.append(f"- Total return: {etf['total_return_pct']:+.1f} %")
    L.append(f"- Annualized: {etf['annualized_return_pct']:+.2f} %/y")
    L.append("")
    L.append(
        f"Best strategy ({best_label}) beats $1K-lump-sum-ETF by "
        f"${s_best['net_pnl'] - (etf['final_value'] - etf['total_invested']):+,.0f} "
        "over 20.3 y."
    )
    L.append("")

    # Caveats
    L.append("## Caveats")
    L.append("")
    L.append("- **Single chronological run** per strategy: no Monte Carlo on trade order. "
             "BTC bull 2016-2017 sits inside window; alternative orderings would shift outcomes.")
    L.append("- **One active account at a time** for Strategy B: after upgrade, the previous "
             "tier is discarded (not kept in parallel). Real operator could run multiple "
             "accounts in parallel for higher payout extraction — modeled here as serial.")
    L.append("- **Realised-P&L only**: open positions can have unrealised MTM DD that would "
             "trigger busts. Bust counts here are LOWER bounds.")
    L.append("- **No fees / slippage on trades themselves** beyond the FundedNext attempt fees. "
             "Investigation H6+H7 showed +0.12 R/trade real cost; would reduce net P&L by 5-10 %.")
    L.append("- **Stellar Lite specifics**: 5-day minimum trading days assumed satisfied "
             "(cadence ~4-5 trades/mo). 21-day first-payout / 14-day subsequent enforced.")
    L.append("- **Scale-Up cap**: $300K used (CFD practical limit per FundedNext rules article "
             "April 2026). Real cap is $4M cumulative across multiple accounts.")
    L.append("- **Yahoo continuous futures** (GC, SI, CL) have level offset vs FundedNext "
             "spot/CFD — direction preserved, magnitudes may differ marginally.")
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
    print(f"Running cell {CELL} on {START.date()} → {END.date()}...", flush=True)
    dates = cycle_dates(panel, START, END)
    exits, _ = run_streaming(panel, params, dates)
    print(f"  {len(exits)} closed trades", flush=True)

    results: dict[str, dict] = {}
    for label in ["A", "B", "C", "D"]:
        print(f"\n=== Strategy {label} ===", flush=True)
        s = simulate(exits, label)
        results[label] = s
        print(f"  Net P&L: ${s['net_pnl']:+,.0f}", flush=True)
        print(f"  Total paid: ${s['total_paid']:,.0f} ({s['n_attempts_paid']} fees)", flush=True)
        print(f"  Total payouts: ${s['total_payouts']:,.0f} ({s['n_payouts_total']})", flush=True)
        print(f"  Max balance: ${s['max_balance_reached']:,.0f}", flush=True)
        print(f"  Funnel: P1 PASS={s['n_p1_pass']}, P2 PASS={s['n_p2_pass']}, "
              f"funded busts={s['n_funded_busts']}", flush=True)
        if label in ("B", "D"):
            print(f"  Tier upgrades: {s['n_tier_upgrades']}", flush=True)
        if label in ("C", "D"):
            print(f"  Scale-Ups: {s['n_scaleups']}", flush=True)

    # ETF comparison: use $1K lump-sum at start as reference point
    etf = etf_benchmark(panel, total_invested=1000.0)
    print(
        f"\nETF baseline ($1K lump 2006-01): final ${etf['final_value']:,.0f}, "
        f"+{etf['total_return_pct']:.0f} % ({etf['annualized_return_pct']:+.2f}%/y)",
        flush=True,
    )

    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    out_md = RUNS_DIR / f"economic_pyramidal_trend_rotation_d1_v1_1_{ts}.md"
    wallclock = time.perf_counter() - t0
    write_report(out_md, results=results, etf=etf, wallclock_s=wallclock)

    out_json = RUNS_DIR / f"economic_pyramidal_trend_rotation_d1_v1_1_{ts}.json"
    json_dump = {
        "strategies": {
            label: {
                k: v for k, v in s.items()
                if k not in ("events", "cumulative_curve")
            } | {
                "n_events": len(s["events"]),
                "first_30_events": s["events"][:30],
            }
            for label, s in results.items()
        },
        "etf": etf,
    }
    out_json.write_text(json.dumps(json_dump, indent=2, default=str))

    print(f"\nReport: {out_md}")
    print(f"Total wallclock: {wallclock:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
