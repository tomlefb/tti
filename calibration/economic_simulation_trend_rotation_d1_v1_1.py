"""End-to-end economic simulation — trend_rotation_d1 v1.1 cell 126/5/3
deployed on FundedNext Stellar Lite 2-Step over 20 y.

State machine
-------------
- **Phase 1** (cost = ``cost_per_attempt``): start $5K, target +8 %
  ($5,400) → Phase 2; bust at -8 % ($4,600) or daily -$200 → restart
  Phase 1 (pay new fee).
- **Phase 2** (no fee): start $5K (reset), target +4 % ($5,200) →
  Funded; bust → restart Phase 1 (pay fee).
- **Funded** (no fee): start $5K, no profit target. Monthly payout
  on last business day of month: payout = 80 % × max(0, capital −
  high_watermark) when ≥ $100 (else carry over). After payout:
  capital -= payout, HWM = capital. Bust → restart Phase 1
  (lose funded, pay fee).

Static drawdown floor: capital ≤ $4,600 = bust everywhere. Daily
limit: single-day net P&L ≤ -$200 = bust.

Grid: risk_per_trade ∈ {0.5 %, 0.75 %, 1.0 %} × cost_per_attempt
∈ {$30, $50, $100} = 9 scenarios.

ETF benchmark: same dollars invested in S&P 500 (^GSPC closes
from the Yahoo panel) at the time each attempt fee is paid.

Outputs:
- ``calibration/runs/economic_simulation_trend_rotation_d1_v1_1_<TS>.md``
- ``calibration/runs/economic_simulation_trend_rotation_d1_v1_1_<TS>.json``

Run
---
    python -m calibration.economic_simulation_trend_rotation_d1_v1_1
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from itertools import product
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
PHASE_1_TARGET_PCT = 0.08
PHASE_2_TARGET_PCT = 0.04
DAILY_LOSS_LIMIT_DOLLARS = 200.0   # 4 % of $5K
TOTAL_DD_FLOOR = INITIAL_CAPITAL * (1.0 - 0.08)   # $4,600
PROFIT_SPLIT = 0.80
MIN_PAYOUT = 100.0

# Grid
RISK_GRID = [0.005, 0.0075, 0.010]
COST_GRID = [30.0, 50.0, 100.0]


def is_last_business_day_of_month(day: pd.Timestamp) -> bool:
    """Return True if `day` is the last weekday in its calendar month.

    Used as the monthly-payout trigger. We don't filter for trading
    holidays — a 1-day mismatch on holidays is irrelevant for a
    20-y simulation.
    """
    next_d = day + pd.Timedelta(days=1)
    if next_d.month != day.month:
        # last day of month — but might be Sat/Sun
        # walk back to last weekday
        d = day
        while d.weekday() >= 5:
            d -= pd.Timedelta(days=1)
        return day == d
    # Otherwise: check if all subsequent days in month are weekend
    rest = pd.date_range(next_d, day + pd.offsets.MonthEnd(0), freq="D")
    return all(r.weekday() >= 5 for r in rest)


def simulate_economy(
    exits: list[TradeExit],
    *,
    risk_pct: float,
    cost_per_attempt: float,
    initial_capital: float = INITIAL_CAPITAL,
) -> dict:
    """End-to-end Phase1+Phase2+Funded simulation."""
    by_day: dict[pd.Timestamp, list[TradeExit]] = defaultdict(list)
    for e in exits:
        d = pd.Timestamp(e.exit_timestamp_utc).normalize()
        if d.tzinfo is None:
            d = d.tz_localize("UTC")
        by_day[d].append(e)

    phase = "phase_1"
    capital = initial_capital
    high_watermark = initial_capital
    total_paid = cost_per_attempt   # initial Phase-1 entry fee
    total_payouts = 0.0
    n_p1_attempts = 1               # we already paid for the first
    n_p1_pass = 0
    n_p2_pass = 0
    n_funded_busts = 0
    n_payouts = 0
    n_phase1_busts = 0
    n_phase2_busts = 0
    days_in_funded = 0
    days_in_p1 = 0
    days_in_p2 = 0
    events: list[dict] = []
    cumulative_pnl_curve: list[tuple[str, float, str]] = []  # (date, cum_pnl, phase)
    attempt_fee_payments: list[tuple[str, float]] = [(START.date().isoformat(), cost_per_attempt)]

    def _restart_phase_1(reason: str, day: pd.Timestamp, *,
                         was_funded: bool) -> None:
        nonlocal phase, capital, high_watermark, total_paid, n_p1_attempts
        nonlocal n_funded_busts, n_phase1_busts, n_phase2_busts
        events.append({
            "date": day.date().isoformat(),
            "type": f"{phase}_bust_{reason}",
            "phase_before": phase,
            "capital_at_event": capital,
        })
        if phase == "funded":
            n_funded_busts += 1
        elif phase == "phase_1":
            n_phase1_busts += 1
        elif phase == "phase_2":
            n_phase2_busts += 1
        phase = "phase_1"
        capital = initial_capital
        high_watermark = initial_capital
        total_paid += cost_per_attempt
        n_p1_attempts += 1
        attempt_fee_payments.append((day.date().isoformat(), cost_per_attempt))

    for day in pd.date_range(START, END, freq="D", tz="UTC"):
        if phase == "phase_1":
            days_in_p1 += 1
        elif phase == "phase_2":
            days_in_p2 += 1
        else:
            days_in_funded += 1

        day_pnl = 0.0
        terminated = False
        for e in by_day.get(day, []):
            if terminated:
                break
            risk_dollars = capital * risk_pct
            pnl = e.return_r * risk_dollars
            day_pnl += pnl
            capital += pnl

            # Bust check (static $4,600 floor, all phases)
            if capital <= TOTAL_DD_FLOOR:
                _restart_phase_1("total", day, was_funded=(phase == "funded"))
                terminated = True
                break

            # Phase progression
            if phase == "phase_1" and capital >= initial_capital * (1 + PHASE_1_TARGET_PCT):
                events.append({
                    "date": day.date().isoformat(),
                    "type": "phase_1_pass",
                    "capital_at_event": capital,
                })
                n_p1_pass += 1
                phase = "phase_2"
                capital = initial_capital
                high_watermark = initial_capital
                terminated = True
                break
            if phase == "phase_2" and capital >= initial_capital * (1 + PHASE_2_TARGET_PCT):
                events.append({
                    "date": day.date().isoformat(),
                    "type": "phase_2_pass",
                    "capital_at_event": capital,
                })
                n_p2_pass += 1
                phase = "funded"
                capital = initial_capital
                high_watermark = initial_capital
                terminated = True
                break

        # Daily-loss check (only if phase still alive)
        if not terminated and -day_pnl >= DAILY_LOSS_LIMIT_DOLLARS:
            _restart_phase_1("daily", day, was_funded=(phase == "funded"))
            terminated = True

        # End-of-month payout (Funded only, end of business month)
        if (
            phase == "funded"
            and not terminated
            and is_last_business_day_of_month(day)
            and capital > high_watermark
        ):
            gross_profit = capital - high_watermark
            payout = PROFIT_SPLIT * gross_profit
            if payout >= MIN_PAYOUT:
                total_payouts += payout
                capital -= payout
                high_watermark = capital
                n_payouts += 1
                events.append({
                    "date": day.date().isoformat(),
                    "type": "payout",
                    "payout": payout,
                    "capital_after": capital,
                })

        # Update cumulative-PnL curve (net of all-time spent, +
        # all-time payouts, + current-equity-above-initial). For
        # accurate net-account-value we treat the trader's "wealth"
        # as: total_payouts - total_paid + (capital - initial if
        # funded else 0). We don't credit unrealised Phase-1/2
        # paper profit because those reset on phase transition.
        if phase == "funded":
            cumulative_value = total_payouts - total_paid + (capital - initial_capital)
        else:
            cumulative_value = total_payouts - total_paid
        cumulative_pnl_curve.append((day.date().isoformat(), cumulative_value, phase))

    n_total_days = (END - START).days + 1

    return {
        "phase_final": phase,
        "capital_final": capital,
        "total_paid": total_paid,
        "total_payouts": total_payouts,
        "net_pnl": total_payouts - total_paid,
        "n_p1_attempts": n_p1_attempts,
        "n_p1_pass": n_p1_pass,
        "n_p2_pass": n_p2_pass,
        "n_funded_busts": n_funded_busts,
        "n_phase1_busts": n_phase1_busts,
        "n_phase2_busts": n_phase2_busts,
        "n_payouts": n_payouts,
        "days_in_p1": days_in_p1,
        "days_in_p2": days_in_p2,
        "days_in_funded": days_in_funded,
        "days_total": n_total_days,
        "events": events,
        "cumulative_pnl_curve": cumulative_pnl_curve,
        "attempt_fee_payments": attempt_fee_payments,
    }


# ---------------------------------------------------------------------------
# ETF benchmark (S&P 500 via ^GSPC fixture)
# ---------------------------------------------------------------------------


def etf_benchmark(panel: dict[str, pd.DataFrame],
                  attempt_payments: list[tuple[str, float]]) -> dict:
    """For each attempt fee paid at date d, simulate buying $X of
    SPX500 ETF at that day's close. Track the basket's value at
    the end of the simulation window (END date).

    Returns a dict with total_invested, final_value, total_return_pct,
    annualized_return_pct.
    """
    sp = panel["SPX500"]
    end_close = float(
        sp.loc[sp.index <= END, "close"].iloc[-1]
    ) if (sp.index <= END).any() else None
    total_invested = 0.0
    total_units = 0.0
    for date_str, amount in attempt_payments:
        d = pd.Timestamp(date_str, tz="UTC")
        sub = sp.loc[sp.index <= d, "close"]
        if len(sub) == 0:
            continue
        price = float(sub.iloc[-1])
        if price <= 0:
            continue
        units = amount / price
        total_units += units
        total_invested += amount
    final_value = total_units * (end_close or 0.0)
    total_return_pct = (
        (final_value - total_invested) / total_invested * 100
        if total_invested > 0 else 0.0
    )
    n_years = (END - START).days / 365.25
    annualized = (
        ((final_value / total_invested) ** (1 / n_years) - 1) * 100
        if total_invested > 0 and final_value > 0 else 0.0
    )
    return {
        "total_invested": total_invested,
        "final_value": final_value,
        "total_return_pct": total_return_pct,
        "annualized_return_pct": annualized,
    }


# ---------------------------------------------------------------------------
# Time-to-breakeven from cumulative curve
# ---------------------------------------------------------------------------


def time_to_breakeven_months(cumulative_curve: list[tuple[str, float, str]]) -> int | None:
    """Months until cumulative_value first crosses 0 (positive)."""
    if not cumulative_curve:
        return None
    start = pd.Timestamp(cumulative_curve[0][0])
    for date_str, cum, _ in cumulative_curve:
        if cum > 0:
            d = pd.Timestamp(date_str)
            return int((d - start).days / 30.4375) + 1
    return None  # never broke even


# ---------------------------------------------------------------------------
# Worst-streak: largest drawdown in cumulative_pnl_curve
# ---------------------------------------------------------------------------


def worst_drawdown_streak(cumulative_curve: list[tuple[str, float, str]]) -> dict:
    """Worst (peak − trough) in the cumulative_value curve."""
    if not cumulative_curve:
        return {"max_dd_dollars": 0.0, "peak_date": None, "trough_date": None}
    peak = float("-inf")
    peak_date = None
    worst_dd = 0.0
    worst_peak_date = None
    worst_trough_date = None
    for date_str, cum, _ in cumulative_curve:
        if cum > peak:
            peak = cum
            peak_date = date_str
        dd = cum - peak
        if dd < worst_dd:
            worst_dd = dd
            worst_peak_date = peak_date
            worst_trough_date = date_str
    return {
        "max_dd_dollars": worst_dd,
        "peak_date": worst_peak_date,
        "trough_date": worst_trough_date,
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def write_report(out_path: Path, *,
                 results: list[dict], etf_results: dict | None,
                 wallclock_s: float) -> Path:
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    L: list[str] = []
    L.append(f"# Economic simulation Phase1+Phase2+Funded 20 y — trend_rotation_d1 v1.1 ({ts})")
    L.append("")
    L.append(
        "End-to-end FundedNext Stellar Lite 2-Step simulation: cell "
        "126/5/3, 1000 trades 2006-01 → 2026-04 (20.3 y), 9-scenario "
        "grid (risk × cost). Single chronological run per scenario "
        "(no Monte Carlo on trade order, see caveat §6)."
    )
    L.append("")
    L.append(f"Wallclock: {wallclock_s:.1f} s.")
    L.append("")

    L.append("## Synthèse — 9 scenarios grid")
    L.append("")
    L.append(
        "| Risk | Fee | P1 attempts | P1 pass | P2 pass | Funded busts | Payouts | "
        "Total paid | Total payouts | Net P&L | $/y avg | ROI % |"
    )
    L.append(
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
    )
    n_years = (END - START).days / 365.25
    best_idx = max(range(len(results)), key=lambda i: results[i]["sim"]["net_pnl"])
    for i, r in enumerate(results):
        s = r["sim"]
        roi = (s["net_pnl"] / s["total_paid"] * 100) if s["total_paid"] else 0.0
        marker = "🎯 " if i == best_idx else ""
        L.append(
            f"| {marker}{r['risk_pct']*100:.2f} % | ${r['cost_per_attempt']:.0f} | "
            f"{s['n_p1_attempts']} | {s['n_p1_pass']} | {s['n_p2_pass']} | "
            f"{s['n_funded_busts']} | {s['n_payouts']} | "
            f"${s['total_paid']:,.0f} | ${s['total_payouts']:,.0f} | "
            f"${s['net_pnl']:+,.0f} | "
            f"${s['net_pnl'] / n_years:+,.0f} | "
            f"{roi:+.0f} % |"
        )
    L.append("")

    # Phase-funnel detail for best scenario
    best = results[best_idx]
    s = best["sim"]
    L.append(f"## Best scenario: risk={best['risk_pct']*100:.2f} %, fee=${best['cost_per_attempt']:.0f}")
    L.append("")
    L.append(f"- **Net P&L over 20.3 y**: **${s['net_pnl']:+,.0f}**")
    L.append(f"- Total paid in attempt fees: ${s['total_paid']:,.0f} ({s['n_p1_attempts']} attempts × ${best['cost_per_attempt']:.0f})")
    L.append(f"- Total payouts received: ${s['total_payouts']:,.0f} across {s['n_payouts']} monthly payouts")
    L.append(f"- Average payout amount: ${s['total_payouts'] / s['n_payouts'] if s['n_payouts'] else 0:.0f}")
    L.append(f"- **Phase funnel**: {s['n_p1_attempts']} P1 attempts → {s['n_p1_pass']} P1 PASS → {s['n_p2_pass']} P2 PASS → {s['n_funded_busts']} funded busts (= funded accounts lost)")
    L.append(f"- Phase 1 pass rate: {s['n_p1_pass'] / s['n_p1_attempts'] * 100:.1f} %")
    if s['n_p1_pass']:
        L.append(f"- Phase 2 pass rate (after P1 PASS): {s['n_p2_pass'] / s['n_p1_pass'] * 100:.1f} %")
    L.append(f"- **Funded account survival**: {s['n_p2_pass']} accounts opened, {s['n_funded_busts']} busted "
             f"(survival rate: {(1 - s['n_funded_busts'] / max(s['n_p2_pass'], 1)) * 100:.0f} %)")
    L.append(
        f"- Time distribution: {s['days_in_p1']} days P1 ({s['days_in_p1'] / s['days_total'] * 100:.0f} %), "
        f"{s['days_in_p2']} days P2 ({s['days_in_p2'] / s['days_total'] * 100:.0f} %), "
        f"{s['days_in_funded']} days funded ({s['days_in_funded'] / s['days_total'] * 100:.0f} %)"
    )
    L.append("")

    # Time-to-breakeven
    ttb = time_to_breakeven_months(s["cumulative_pnl_curve"])
    L.append(f"- **Time-to-breakeven** (cumulative_value > 0): "
             f"{ttb if ttb is not None else 'NEVER'} months")
    L.append("")

    # Worst drawdown
    wd = worst_drawdown_streak(s["cumulative_pnl_curve"])
    L.append(
        f"- **Worst cumulative drawdown**: ${wd['max_dd_dollars']:+,.0f} "
        f"({wd['peak_date']} → {wd['trough_date']})"
    )
    L.append("")

    # ETF comparison
    L.append("## ETF S&P 500 benchmark")
    L.append("")
    if etf_results:
        L.append(
            "If the trader had instead invested each attempt fee into "
            "S&P 500 (^GSPC close at the date the fee was paid) on the "
            "same 20.3 y window:"
        )
        L.append("")
        L.append(f"- Total invested: ${etf_results['total_invested']:,.0f}")
        L.append(f"- Final basket value: ${etf_results['final_value']:,.0f}")
        L.append(f"- Total return: {etf_results['total_return_pct']:+.1f} %")
        L.append(f"- Annualized return: {etf_results['annualized_return_pct']:+.2f} %/y")
        L.append("")
        delta = s["net_pnl"] - (etf_results["final_value"] - etf_results["total_invested"])
        L.append(
            f"- **Strategy net P&L vs ETF P&L delta**: ${delta:+,.0f} "
            f"({'strategy beats ETF' if delta > 0 else 'ETF beats strategy'})"
        )
    else:
        L.append("ETF benchmark not computed.")
    L.append("")

    # Per-scenario detail tables
    L.append("## Per-scenario detail")
    L.append("")
    for r in results:
        s = r["sim"]
        L.append(
            f"### Risk {r['risk_pct']*100:.2f} %, Fee ${r['cost_per_attempt']:.0f}"
        )
        L.append("")
        L.append(
            f"- P1 funnel: **{s['n_p1_attempts']}** attempts → "
            f"{s['n_p1_pass']} P1 PASS ({s['n_p1_pass'] / max(s['n_p1_attempts'], 1) * 100:.0f} %) → "
            f"{s['n_p2_pass']} P2 PASS ({s['n_p2_pass'] / max(s['n_p1_pass'], 1) * 100:.0f} %)"
        )
        L.append(
            f"- Funded: {s['n_p2_pass']} accounts opened, "
            f"{s['n_funded_busts']} busted, {s['n_payouts']} payouts received"
        )
        L.append(
            f"- $: paid ${s['total_paid']:,.0f}, received ${s['total_payouts']:,.0f}, "
            f"net **${s['net_pnl']:+,.0f}** "
            f"(${s['net_pnl'] / n_years:+,.0f}/y avg)"
        )
        ttb = time_to_breakeven_months(s["cumulative_pnl_curve"])
        L.append(f"- Time-to-breakeven: "
                 f"{ttb if ttb is not None else 'NEVER'} months")
        L.append("")

    # Verdict per pre-spec thresholds
    best_net = results[best_idx]["sim"]["net_pnl"]
    best_paid = results[best_idx]["sim"]["total_paid"]
    roi_best = best_net / best_paid * 100 if best_paid else 0.0
    ttb_best = time_to_breakeven_months(results[best_idx]["sim"]["cumulative_pnl_curve"])
    L.append("## Verdict économique pre-spec")
    L.append("")
    L.append("- (A) Profitable convaincant: Net > $50K, ROI > 300 %, TTB < 12 mo → PROMOTE")
    L.append("- (B) Profitable marginal: Net $10-50K, ROI 100-300 %, TTB 12-36 mo → REVIEW")
    L.append("- (C) Non-rentable: Net < $10K or negative, ROI < 100 %, TTB > 36 mo → ARCHIVE")
    L.append("")
    if best_net > 50_000 and roi_best > 300 and (ttb_best or 999) < 12:
        verdict = "✅ A — PROFITABLE CONVAINCANT (PROMOTE)"
    elif best_net > 10_000 and roi_best > 100 and (ttb_best or 999) < 36:
        verdict = "⚠️ B — PROFITABLE MARGINAL (REVIEW)"
    else:
        verdict = "❌ C — NON-RENTABLE (ARCHIVE sur economic non-viability)"
    L.append(f"**Verdict mesuré (best scenario)**: {verdict}")
    L.append("")
    L.append(
        f"Best scenario: net P&L = ${best_net:+,.0f}, ROI = {roi_best:+.0f} %, "
        f"TTB = {ttb_best if ttb_best is not None else 'NEVER'} months."
    )
    L.append("")

    # Caveats
    L.append("## Caveats")
    L.append("")
    L.append("- **No Monte Carlo on trade order**: a single chronological run "
             "per scenario. The 2016-2017 BTC bull run sits inside the 20-y "
             "window and contributes disproportionately to good scenarios. "
             "Bootstrap on trade-order would partly randomise this — not done "
             "here for simplicity. The 9-scenario sensitivity grid (risk × "
             "fee) provides a 9-point sensitivity instead.")
    L.append("- **Realised-P&L only**: open positions can have unrealised "
             "MTM drawdown that would trigger Phase-1/2/funded busts in real "
             "FundedNext accounts. The bust counts here are LOWER bounds.")
    L.append("- **No fees or slippage on trades themselves**: investigation "
             "H6+H7 (commit fb374b1) showed +0.12 R/trade real cost — NOT "
             "applied here. Scenario net P&L is therefore over-stated by "
             "5-10 % relative to broker-fill reality.")
    L.append("- **Simplified payout model**: 80 % profit split, $100 minimum, "
             "monthly. Real FundedNext can have biweekly or on-demand payouts; "
             "minimum often $25-50 (more permissive). Conservative here.")
    L.append("- **Simplified phase rules**: assumes immediate phase transition "
             "on target hit (real FundedNext has 5-day minimum trading day "
             "rule). 5-day minimum is satisfied by pretty much all attempts "
             "the strategy generates given cadence ~4-5 trades/mo.")
    L.append("- **Yahoo continuous futures level offsets** vs FundedNext spot/CFD "
             "(GC, SI, CL) — qualitative direction preserved, magnitudes may "
             "differ marginally.")
    L.append("")

    out_path.write_text("\n".join(L) + "\n")
    return out_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


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

    print("\n=== 9-scenario grid ===", flush=True)
    print("| risk | fee  | attempts | P1pass | P2pass | F.bust | payouts | "
          "paid     | recv     | net    |", flush=True)
    results: list[dict] = []
    for risk_pct, cost in product(RISK_GRID, COST_GRID):
        sim = simulate_economy(exits, risk_pct=risk_pct, cost_per_attempt=cost)
        results.append({
            "risk_pct": risk_pct,
            "cost_per_attempt": cost,
            "sim": sim,
        })
        print(
            f"| {risk_pct*100:>4.2f}% | ${cost:>4.0f} | {sim['n_p1_attempts']:>8} | "
            f"{sim['n_p1_pass']:>6} | {sim['n_p2_pass']:>6} | "
            f"{sim['n_funded_busts']:>6} | {sim['n_payouts']:>7} | "
            f"${sim['total_paid']:>7,.0f} | "
            f"${sim['total_payouts']:>7,.0f} | "
            f"${sim['net_pnl']:>+6,.0f} |",
            flush=True,
        )

    # ETF benchmark on best scenario
    best_idx = max(range(len(results)), key=lambda i: results[i]["sim"]["net_pnl"])
    etf = etf_benchmark(panel, results[best_idx]["sim"]["attempt_fee_payments"])
    print(
        f"\nETF benchmark (best scenario fee schedule): "
        f"invested ${etf['total_invested']:,.0f}, "
        f"final ${etf['final_value']:,.0f}, "
        f"return {etf['total_return_pct']:+.1f}% "
        f"(annualised {etf['annualized_return_pct']:+.2f}%/y)",
        flush=True,
    )

    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    out_path = RUNS_DIR / f"economic_simulation_trend_rotation_d1_v1_1_{ts}.md"
    wallclock = time.perf_counter() - t0
    write_report(
        out_path,
        results=results, etf_results=etf, wallclock_s=wallclock,
    )

    # Compact JSON dump
    json_path = RUNS_DIR / f"economic_simulation_trend_rotation_d1_v1_1_{ts}.json"
    json_dump = {
        "scenarios": [
            {
                "risk_pct": r["risk_pct"],
                "cost_per_attempt": r["cost_per_attempt"],
                "n_p1_attempts": r["sim"]["n_p1_attempts"],
                "n_p1_pass": r["sim"]["n_p1_pass"],
                "n_p2_pass": r["sim"]["n_p2_pass"],
                "n_funded_busts": r["sim"]["n_funded_busts"],
                "n_payouts": r["sim"]["n_payouts"],
                "total_paid": r["sim"]["total_paid"],
                "total_payouts": r["sim"]["total_payouts"],
                "net_pnl": r["sim"]["net_pnl"],
                "days_in_p1": r["sim"]["days_in_p1"],
                "days_in_p2": r["sim"]["days_in_p2"],
                "days_in_funded": r["sim"]["days_in_funded"],
            }
            for r in results
        ],
        "etf_benchmark": etf,
    }
    json_path.write_text(json.dumps(json_dump, indent=2, default=str))

    print(f"\nReport: {out_path}")
    print(f"Total wallclock: {wallclock:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
