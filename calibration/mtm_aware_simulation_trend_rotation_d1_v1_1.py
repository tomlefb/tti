"""MTM-aware Phase-1/Phase-2 simulation — trend_rotation_d1 v1.1
cell 126/5/3.

Closed-only sims (commits 1644e55, 9dac82c, f282332) only checked
phase rules at trade exit_timestamps. Real FundedNext checks
equity continuously; intra-trade MTM drawdown can trigger busts
before exit.

This simulation walks every calendar day 2006-01 → 2026-04 with
multi-position MTM aggregation:

1. Each day: add positions that enter today, realize positions
   that exit today (full return_r), compute MTM equity for the
   rest at close[day].
2. equity_eod = capital_realized + sum(MTM open at close[day])
3. Daily P&L = equity_eod - equity_sod (start-of-day before
   today's events)
4. Bust checks (FundedNext static thresholds):
   - equity_eod ≤ $4,600 → FAIL_TOTAL
   - daily P&L ≤ -$200 → FAIL_DAILY
5. Phase progression checks (target hit at end-of-day):
   - phase 1 + equity ≥ $5,400 → PASS_P1 (move to phase 2 on
     fresh $5K)
   - phase 2 + equity ≥ $5,200 → PASS_P2 (open Funded)
6. On any event: realize ALL open positions at close[day]; reset
   state for next attempt.

Compares pass rate / net P&L against the closed-only baseline.

Run
---
    python -m calibration.mtm_aware_simulation_trend_rotation_d1_v1_1
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
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
P1_TARGET = 0.08    # +8 % → $5,400
P2_TARGET = 0.04    # +4 % → $5,200
TOTAL_DD_PCT = 0.08 # static -8 % from initial → $4,600 floor
DAILY_LOSS_LIMIT = INITIAL_CAPITAL * 0.04  # $200
PROFIT_TARGET_P1 = INITIAL_CAPITAL * (1 + P1_TARGET)
PROFIT_TARGET_P2 = INITIAL_CAPITAL * (1 + P2_TARGET)
BUST_FLOOR = INITIAL_CAPITAL * (1 - TOTAL_DD_PCT)
RISK_PCT = 0.01


def build_close_lookup(panel: dict[str, pd.DataFrame]) -> dict[str, pd.Series]:
    """Forward-fill close prices for fast daily lookup."""
    daily_idx = pd.date_range(START, END, freq="D", tz="UTC")
    out: dict[str, pd.Series] = {}
    for asset, df in panel.items():
        s = df["close"].copy()
        s = s[~s.index.duplicated(keep="first")].sort_index()
        out[asset] = s.reindex(daily_idx).ffill()
    return out


def simulate_mtm(
    exits: list[TradeExit],
    closes: dict[str, pd.Series],
    *,
    risk_pct: float = RISK_PCT,
) -> dict:
    """Walk-the-calendar MTM-aware simulation of sequential Phase-1/2 attempts."""
    # Index trades by entry/exit dates
    trades_by_entry: dict[pd.Timestamp, list[TradeExit]] = defaultdict(list)
    trades_by_exit: dict[pd.Timestamp, list[TradeExit]] = defaultdict(list)
    for e in exits:
        ed = pd.Timestamp(e.entry_timestamp_utc).normalize()
        if ed.tzinfo is None:
            ed = ed.tz_localize("UTC")
        xd = pd.Timestamp(e.exit_timestamp_utc).normalize()
        if xd.tzinfo is None:
            xd = xd.tz_localize("UTC")
        trades_by_entry[ed].append(e)
        trades_by_exit[xd].append(e)

    # State
    capital_realized = INITIAL_CAPITAL
    open_positions: list[tuple[TradeExit, float]] = []  # (trade, risk_dollars)
    phase = "phase_1"
    attempt_id = 1
    attempt_start_date = START
    attempt_n_trades = 0
    attempt_max_intra_dd_dollars = 0.0   # most negative MTM equity vs initial within attempt

    attempts: list[dict] = []
    daily_curve: list[tuple[str, float, str]] = []  # (date, equity_eod, phase)
    bust_by_asset: dict[str, int] = defaultdict(int)
    bust_by_year: dict[str, int] = defaultdict(int)
    bust_by_cause: dict[str, int] = defaultdict(int)

    prev_equity_eod = INITIAL_CAPITAL

    def _equity_at(day: pd.Timestamp) -> float:
        eq = capital_realized
        for t, rd in open_positions:
            asset_close = closes[t.asset].loc[day]
            if pd.isna(asset_close):
                continue
            mtm_r = (float(asset_close) - t.entry_price) / t.atr_at_entry
            eq += mtm_r * rd
        return eq

    def _close_attempt(outcome: str, day: pd.Timestamp,
                       equity_at_event: float, *, cause_asset: str | None = None,
                       cause: str = "unknown") -> None:
        nonlocal capital_realized, open_positions, phase, attempt_id
        nonlocal attempt_start_date, attempt_n_trades, attempt_max_intra_dd_dollars
        nonlocal prev_equity_eod
        attempts.append({
            "id": attempt_id,
            "outcome": outcome,
            "phase_at_event": phase,
            "start": attempt_start_date.date().isoformat(),
            "end": day.date().isoformat(),
            "duration_days": (day - attempt_start_date).days,
            "n_trades": attempt_n_trades,
            "n_open_at_event": len(open_positions),
            "equity_at_event": equity_at_event,
            "max_intra_dd_dollars": attempt_max_intra_dd_dollars,
            "cause": cause,
            "cause_asset": cause_asset,
        })
        # Track bust cause stats
        if outcome.startswith("FAIL"):
            bust_by_year[day.strftime("%Y")] += 1
            if cause_asset:
                bust_by_asset[cause_asset] += 1
            bust_by_cause[outcome] += 1
        # Reset state
        capital_realized = INITIAL_CAPITAL
        open_positions = []
        if outcome == "PASS_P1":
            phase = "phase_2"
        elif outcome == "PASS_P2":
            phase = "phase_2_done"  # would-be funded; we stop simulation logic at PASS P2
        else:
            phase = "phase_1"
        attempt_id += 1
        attempt_start_date = day + pd.Timedelta(days=1)
        attempt_n_trades = 0
        attempt_max_intra_dd_dollars = 0.0
        prev_equity_eod = INITIAL_CAPITAL

    # Walk calendar days
    for day in pd.date_range(START, END, freq="D", tz="UTC"):
        # If we just passed P2, we don't re-open a phase-1 attempt in
        # this simulation (the operator becomes funded — out of scope
        # for this MTM Phase-1/2 measurement). For the bust-rate
        # measurement we restart Phase 1 to get more attempts.
        if phase == "phase_2_done":
            phase = "phase_1"

        # Add trades opening today
        for e in trades_by_entry.get(day, []):
            risk_dollars = capital_realized * risk_pct
            open_positions.append((e, risk_dollars))
            attempt_n_trades += 1

        # Realize trades exiting today (full return_r)
        for e in trades_by_exit.get(day, []):
            for i, (t, rd) in enumerate(list(open_positions)):
                if t is e:
                    realized_pnl = e.return_r * rd
                    capital_realized += realized_pnl
                    open_positions.pop(i)
                    break

        # Equity at end-of-day
        equity_eod = _equity_at(day)

        # Track intra-attempt max DD
        intra_dd = equity_eod - INITIAL_CAPITAL
        if intra_dd < attempt_max_intra_dd_dollars:
            attempt_max_intra_dd_dollars = intra_dd

        # Daily P&L
        daily_pnl = equity_eod - prev_equity_eod

        # Find the position contributing the most negative MTM today
        # (used to attribute bust cause)
        worst_asset = None
        if open_positions:
            worst_mtm = 0.0
            for t, rd in open_positions:
                cls = closes[t.asset].loc[day]
                if pd.isna(cls):
                    continue
                m = (float(cls) - t.entry_price) / t.atr_at_entry * rd
                if m < worst_mtm:
                    worst_mtm = m
                    worst_asset = t.asset

        # Bust checks (priority: total over daily, since both can fire)
        if equity_eod <= BUST_FLOOR:
            _close_attempt("FAIL_TOTAL", day, equity_eod,
                           cause_asset=worst_asset, cause="total_dd_floor")
            daily_curve.append((day.date().isoformat(), equity_eod, "BUST_TOTAL"))
            continue
        if daily_pnl <= -DAILY_LOSS_LIMIT:
            _close_attempt("FAIL_DAILY", day, equity_eod,
                           cause_asset=worst_asset, cause="daily_loss_limit")
            daily_curve.append((day.date().isoformat(), equity_eod, "BUST_DAILY"))
            continue

        # Phase progression
        if phase == "phase_1" and equity_eod >= PROFIT_TARGET_P1:
            _close_attempt("PASS_P1", day, equity_eod, cause="phase_1_target")
            daily_curve.append((day.date().isoformat(), equity_eod, "PASS_P1"))
            continue
        if phase == "phase_2" and equity_eod >= PROFIT_TARGET_P2:
            _close_attempt("PASS_P2", day, equity_eod, cause="phase_2_target")
            daily_curve.append((day.date().isoformat(), equity_eod, "PASS_P2"))
            continue

        prev_equity_eod = equity_eod
        daily_curve.append((day.date().isoformat(), equity_eod, phase))

    # Counts
    n_p1_total = sum(1 for a in attempts if a["phase_at_event"] == "phase_1"
                     or (a["phase_at_event"] == "phase_2" and a["outcome"] == "PASS_P1"))
    n_p1_pass = sum(1 for a in attempts if a["outcome"] == "PASS_P1")
    n_p1_fail_total = sum(1 for a in attempts
                          if a["outcome"] == "FAIL_TOTAL" and a["phase_at_event"] == "phase_1")
    n_p1_fail_daily = sum(1 for a in attempts
                          if a["outcome"] == "FAIL_DAILY" and a["phase_at_event"] == "phase_1")
    n_p2_attempts = sum(1 for a in attempts if a["phase_at_event"] == "phase_2")
    n_p2_pass = sum(1 for a in attempts if a["outcome"] == "PASS_P2")
    n_p2_fail_total = sum(1 for a in attempts
                          if a["outcome"] == "FAIL_TOTAL" and a["phase_at_event"] == "phase_2")
    n_p2_fail_daily = sum(1 for a in attempts
                          if a["outcome"] == "FAIL_DAILY" and a["phase_at_event"] == "phase_2")

    p1_attempts_total = n_p1_pass + n_p1_fail_total + n_p1_fail_daily
    p1_pass_rate = n_p1_pass / p1_attempts_total if p1_attempts_total else 0.0
    p2_pass_rate = n_p2_pass / n_p2_attempts if n_p2_attempts else 0.0

    return {
        "attempts": attempts,
        "daily_curve": daily_curve,
        "n_p1_attempts": p1_attempts_total,
        "n_p1_pass": n_p1_pass,
        "n_p1_fail_total": n_p1_fail_total,
        "n_p1_fail_daily": n_p1_fail_daily,
        "p1_pass_rate": p1_pass_rate,
        "n_p2_attempts": n_p2_attempts,
        "n_p2_pass": n_p2_pass,
        "n_p2_fail_total": n_p2_fail_total,
        "n_p2_fail_daily": n_p2_fail_daily,
        "p2_pass_rate": p2_pass_rate,
        "n_funded_opened": n_p2_pass,
        "bust_by_asset": dict(bust_by_asset),
        "bust_by_year": dict(bust_by_year),
        "bust_by_cause": dict(bust_by_cause),
    }


# -- Economic projection ---------------------------------------------------


def project_economic(p1_rate: float, p2_rate: float,
                     n_p1_attempts_actual: int,
                     n_funded_actual: int,
                     fee_per_attempt: float = 30.0,
                     payout_per_funded_avg: float = 2755.0) -> dict:
    """Project net P&L from MTM pass rates assuming same attempt
    cadence as closed-only operational simulation.

    Closed-only baseline (commits 1644e55, 9dac82c):
    - 140 P1 attempts at 1% risk
    - 76 P1 PASS (54.3%)
    - 21 funded opened (P2 PASS)
    - 37 payouts × $1,564 avg = $57,851 received
    - $30 × 71 attempts paid = $2,130 (best scenario)

    For MTM projection: scale n_funded by p1_rate × p2_rate ratio.
    """
    closed_p1_attempts = 140
    closed_p1_pass = 76          # 54.3%
    closed_p2_pass = 21          # = funded opened
    closed_payouts_count = 37
    closed_payout_avg = 1564.0
    closed_total_paid = 2130.0
    closed_net = 55721.0

    # Naive scaling: assume same cadence (140 attempts), MTM rates apply
    n_p1_pass_proj = int(closed_p1_attempts * p1_rate)
    n_funded_proj = int(n_p1_pass_proj * p2_rate)
    # Payouts: assume same payout-per-funded ratio (37/21 ≈ 1.76)
    payouts_per_funded = closed_payouts_count / closed_p2_pass if closed_p2_pass else 0
    n_payouts_proj = int(n_funded_proj * payouts_per_funded)
    total_paid_proj = closed_total_paid * (closed_p1_attempts / closed_p1_attempts)  # same fees
    # Total received scales with n_payouts
    total_received_proj = n_payouts_proj * closed_payout_avg
    net_proj = total_received_proj - total_paid_proj

    return {
        "n_p1_attempts_proj": closed_p1_attempts,
        "n_p1_pass_proj": n_p1_pass_proj,
        "n_funded_proj": n_funded_proj,
        "n_payouts_proj": n_payouts_proj,
        "total_paid_proj": total_paid_proj,
        "total_received_proj": total_received_proj,
        "net_pnl_proj": net_proj,
    }


# -- Reporting --------------------------------------------------------------


def write_report(out_path: Path, *, sim: dict, closes: dict[str, pd.Series],
                 wallclock_s: float) -> Path:
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    L: list[str] = []
    L.append(f"# MTM-aware bust/pass simulation 20y — trend_rotation_d1 v1.1 cell 126/5/3 ({ts})")
    L.append("")
    L.append(
        "Cellule 126/5/3, fenêtre 2006-01-01 → 2026-04-30 (20.3 y, "
        "Yahoo D1, 1000 trades). Simulation Phase-1/2 séquentielle "
        f"avec MTM intra-position aggregé. Risque {RISK_PCT*100:.1f} %, "
        "FundedNext static thresholds ($4,600 floor, $200 daily limit, "
        "$5,400 P1 target, $5,200 P2 target)."
    )
    L.append("")
    L.append(f"Wallclock: {wallclock_s:.1f} s.")
    L.append("")

    # Closed-only baseline reference (from commit 1644e55)
    closed_p1 = 0.543
    closed_p2 = 0.636
    closed_combined = 0.543 * 0.636
    closed_funded = 21
    closed_attempts = 140

    L.append("## Comparaison closed-only vs MTM-aware")
    L.append("")
    L.append("| Métrique | Closed-only (1644e55) | MTM-aware | Δ |")
    L.append("|---|---:|---:|---:|")
    L.append(f"| Phase 1 pass rate | 54.3 % | **{sim['p1_pass_rate']*100:.1f} %** | {(sim['p1_pass_rate']-closed_p1)*100:+.1f} pp |")
    L.append(f"| Phase 2 pass rate | 63.6 % | **{sim['p2_pass_rate']*100:.1f} %** | {(sim['p2_pass_rate']-closed_p2)*100:+.1f} pp |")
    L.append(f"| Phase 1 + 2 combined | 34.5 % | **{sim['p1_pass_rate']*sim['p2_pass_rate']*100:.1f} %** | "
             f"{(sim['p1_pass_rate']*sim['p2_pass_rate']-closed_combined)*100:+.1f} pp |")
    L.append(f"| Phase 1 attempts | 140 | {sim['n_p1_attempts']} | {sim['n_p1_attempts']-closed_attempts:+d} |")
    L.append(f"| Funded opened | 21 | **{sim['n_funded_opened']}** | {sim['n_funded_opened']-closed_funded:+d} |")
    L.append("")

    # Distribution des bust causes
    L.append("## Distribution des causes de bust (MTM)")
    L.append("")
    by_cause = sim["bust_by_cause"]
    total_busts = sum(by_cause.values()) or 1
    for cause, n in sorted(by_cause.items(), key=lambda x: -x[1]):
        L.append(f"- {cause}: {n} ({n/total_busts*100:.1f} %)")
    L.append("")

    # By asset
    L.append("### Bust triggers by asset (Phase 1/2)")
    L.append("")
    L.append("| Asset | n busts (cause) |")
    L.append("|---|---:|")
    for asset, n in sorted(sim["bust_by_asset"].items(), key=lambda x: -x[1]):
        L.append(f"| {asset} | {n} |")
    L.append("")

    # By year
    L.append("### Bust by year")
    L.append("")
    L.append("| Year | n busts |")
    L.append("|---|---:|")
    for year in sorted(sim["bust_by_year"].keys()):
        L.append(f"| {year} | {sim['bust_by_year'][year]} |")
    L.append("")

    # Phase-1 attempt detail (first 50)
    L.append("## Phase-1 attempt detail (first 50)")
    L.append("")
    L.append(
        "| # | start → end | days | n trades | outcome | phase | "
        "equity@event | max intra-DD | cause | cause asset |"
    )
    L.append("|---:|---|---:|---:|:---:|:---:|---:|---:|---|---|")
    p1_attempts_only = [a for a in sim["attempts"] if a["phase_at_event"] == "phase_1"]
    for a in p1_attempts_only[:50]:
        emoji = {
            "PASS_P1": "✅", "FAIL_TOTAL": "❌", "FAIL_DAILY": "🔻",
            "PASS_P2": "✅", "PASS_FUNDED": "💰",
        }.get(a["outcome"], "?")
        L.append(
            f"| {a['id']} | {a['start']} → {a['end']} | {a['duration_days']} | "
            f"{a['n_trades']} | {emoji} {a['outcome']} | {a['phase_at_event']} | "
            f"${a['equity_at_event']:,.0f} | "
            f"${a['max_intra_dd_dollars']:+,.0f} | "
            f"{a['cause']} | {a['cause_asset'] or '—'} |"
        )
    if len(p1_attempts_only) > 50:
        L.append(f"\n…(+ {len(p1_attempts_only)-50} more P1 attempts)")
    L.append("")

    # Average max intra-DD on graduating P1 attempts
    grad_p1 = [a for a in p1_attempts_only if a["outcome"] == "PASS_P1"]
    if grad_p1:
        avg_dd = np.mean([a["max_intra_dd_dollars"] for a in grad_p1])
        median_dd = float(np.median([a["max_intra_dd_dollars"] for a in grad_p1]))
        worst_dd = float(min(a["max_intra_dd_dollars"] for a in grad_p1))
        L.append("## Phase-1 graduating attempts — intra-DD stats")
        L.append("")
        L.append(f"- n = {len(grad_p1)}")
        L.append(f"- Avg max intra-DD: ${avg_dd:+.0f}")
        L.append(f"- Median max intra-DD: ${median_dd:+.0f}")
        L.append(f"- Worst max intra-DD on a graduate: ${worst_dd:+.0f}")
        L.append("")

    # Economic projection
    proj = project_economic(
        sim["p1_pass_rate"], sim["p2_pass_rate"],
        n_p1_attempts_actual=sim["n_p1_attempts"],
        n_funded_actual=sim["n_funded_opened"],
    )
    L.append("## Projection économique 20 ans (MTM rates × closed-only cadence)")
    L.append("")
    L.append(
        "Recalcule le Net P&L 20 y de l'economic baseline (commit "
        "9dac82c) en remplaçant les pass rates closed-only par les "
        "MTM rates mesurés. Cadence d'attempts conservée (140 P1 sur 20y)."
    )
    L.append("")
    L.append(f"- Funded accounts projeté MTM: {proj['n_funded_proj']} (vs 21 closed-only)")
    L.append(f"- Payouts projeté MTM: {proj['n_payouts_proj']} (vs 37 closed-only)")
    L.append(f"- Total reçu projeté: ${proj['total_received_proj']:,.0f} (vs $57,851 closed-only)")
    L.append(f"- Total payé: ${proj['total_paid_proj']:,.0f}")
    L.append(f"- **Net P&L 20 y projeté MTM: ${proj['net_pnl_proj']:+,.0f}**")
    L.append(f"  (closed-only baseline: +$55,721)")
    L.append("")

    # Verdict
    net_proj = proj["net_pnl_proj"]
    if net_proj > 40_000:
        verdict = "✅ A — PROFITABLE CONVAINCANT (déploiement OK)"
    elif net_proj > 20_000:
        verdict = "⚠️ B — PROFITABLE MARGINAL (discussion opérateur)"
    else:
        verdict = "❌ C — NON-RENTABLE en pratique (ARCHIVE)"
    L.append("## Verdict MTM")
    L.append("")
    L.append("Bandes pré-spec:")
    L.append("- (A) Net > $40K → PROFITABLE CONVAINCANT — déploiement OK")
    L.append("- (B) Net $20-40K → PROFITABLE MARGINAL — discussion")
    L.append("- (C) Net < $20K → NON-RENTABLE — ARCHIVE")
    L.append("")
    L.append(f"**Verdict mesuré**: {verdict}")
    L.append("")

    # Action recommandée
    L.append("## Action recommandée")
    L.append("")
    if net_proj > 40_000:
        L.append(
            "Le MTM-aware confirme la profitabilité du baseline closed-only. "
            "Déploiement gate 6 MT5 sanity peut procéder. KILL_SWITCH peut "
            "être retiré (sous réserve des autres validations: gate 6 MT5 "
            "direction agreement, gate 7 transferability, gate 8 Phase C "
            "frais granulaires)."
        )
    elif net_proj > 20_000:
        L.append(
            "Le MTM-aware réduit la profitabilité mais reste rentable. "
            "Discussion opérateur requise: l'ordre de grandeur (×0.5 vs "
            "closed-only) est-il acceptable? Possibilité de tester K=3 au "
            "lieu de K=5 pour réduire le MTM stress."
        )
    else:
        L.append(
            "Le MTM-aware révèle que la stratégie est non-rentable en "
            "pratique sous contrainte FundedNext. ARCHIVE recommandé. "
            "KILL_SWITCH reste armé. Pivot vers autre classe de stratégie "
            "(HTF single-asset wick-sensitive, LTF M5/M15)."
        )
    L.append("")

    # Caveats
    L.append("## Caveats")
    L.append("")
    L.append("- **Yahoo continuous futures** (GC, SI, CL, BTC) ont level offset vs FundedNext "
             "spot/CFD. Pour les trades sur ces actifs, le MTM intra-position peut différer "
             "marginalement de la réalité broker.")
    L.append("- **No fees / slippage on trades**: investigation H6+H7 = +0.12 R/trade. "
             "Réduit légèrement la net P&L MTM (~5-10 %).")
    L.append("- **Forced close on event**: à BUST/PASS, toutes les positions ouvertes sont "
             "réalisées au close[day_of_event]. Peut créer une déconnection avec le pipeline "
             "trade list (les trades restants sont 'consumés' par l'attempt qui s'est clôturé). "
             "Effet: certaines positions ouvertes prématurément clôturées peuvent avoir un "
             "return_r réalisé différent de leur exit_timestamp original.")
    L.append("- **Naive economic projection**: assume same payout cadence et fee schedule "
             "que closed-only. La vraie MTM-economic simulation devrait re-exécuter le pipeline "
             "Phase1+Phase2+Funded avec MTM checks à chaque jour, ce qui est plus complexe.")
    L.append("- **Position sizing simplifiée**: trade i sized sur capital_realized au moment "
             "de son entry. Capital_realized n'inclut pas la MTM des autres positions ouvertes "
             "(simplification).")
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

    print("Building close-price lookup (forward-filled)...", flush=True)
    closes = build_close_lookup(panel)

    print("Simulating MTM-aware Phase-1/2 attempts...", flush=True)
    sim = simulate_mtm(exits, closes)
    print(
        f"  P1: {sim['n_p1_pass']}/{sim['n_p1_attempts']} = "
        f"{sim['p1_pass_rate']*100:.1f}% (closed-only: 54.3%)",
        flush=True,
    )
    print(
        f"  P2: {sim['n_p2_pass']}/{sim['n_p2_attempts']} = "
        f"{sim['p2_pass_rate']*100:.1f}% (closed-only: 63.6%)",
        flush=True,
    )
    print(f"  Funded opened: {sim['n_funded_opened']} (closed-only: 21)", flush=True)

    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    out_md = RUNS_DIR / f"mtm_aware_simulation_trend_rotation_d1_v1_1_{ts}.md"
    wallclock = time.perf_counter() - t0
    write_report(out_md, sim=sim, closes=closes, wallclock_s=wallclock)

    out_json = RUNS_DIR / f"mtm_aware_simulation_trend_rotation_d1_v1_1_{ts}.json"
    json_dump = {
        "p1_pass_rate": sim["p1_pass_rate"],
        "p2_pass_rate": sim["p2_pass_rate"],
        "n_p1_attempts": sim["n_p1_attempts"],
        "n_p1_pass": sim["n_p1_pass"],
        "n_p1_fail_total": sim["n_p1_fail_total"],
        "n_p1_fail_daily": sim["n_p1_fail_daily"],
        "n_p2_attempts": sim["n_p2_attempts"],
        "n_p2_pass": sim["n_p2_pass"],
        "n_funded_opened": sim["n_funded_opened"],
        "bust_by_asset": sim["bust_by_asset"],
        "bust_by_year": sim["bust_by_year"],
        "bust_by_cause": sim["bust_by_cause"],
        "first_50_attempts": sim["attempts"][:50],
    }
    out_json.write_text(json.dumps(json_dump, indent=2, default=str))

    print(f"\nReport: {out_md}")
    print(f"Total wallclock: {wallclock:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
