"""Read-only margin diagnostic — what would the next 1 % rebalance cost?

Connects to MT5, computes the current rotation top-K basket using
the same pipeline helpers the production cycle uses, sizes each
opening position at 1 % of live balance, and reports the broker's
margin requirement per leg via ``mt5.order_calc_margin``. **Never
places an order.**

The script's process is independent of the running scheduler — it
opens its own MT5 IPC channel and shuts it down at exit, so the
scheduler's existing connection (in another PID) is unaffected.

Run on the Windows host with the MT5 terminal connected:

    python scripts/check_margin_at_1pct.py

Outputs a per-asset margin table, totals, and a SAFE / TIGHT /
INSUFFICIENT verdict mirroring the operator's pre-spec thresholds.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import MetaTrader5 as mt5  # type: ignore[import-not-found]  # noqa: E402
import pandas as pd  # noqa: E402

from config import settings  # noqa: E402
from src.execution.order_manager_rotation import compute_rotation_volume  # noqa: E402
from src.execution.safe_guards import adaptive_risk_per_trade_pct  # noqa: E402
from src.mt5_client.client import MT5Client  # noqa: E402
from src.strategies.trend_rotation_d1 import StrategyParams  # noqa: E402
from src.strategies.trend_rotation_d1.pipeline import _score_one_asset  # noqa: E402
from src.strategies.trend_rotation_d1.ranking import select_top_k  # noqa: E402

logger = logging.getLogger(__name__)
_TZ_PARIS = ZoneInfo("Europe/Paris")


def _next_2300_paris() -> datetime:
    now_paris = datetime.now(_TZ_PARIS)
    target = now_paris.replace(hour=23, minute=0, second=0, microsecond=0)
    if now_paris >= target:
        from datetime import timedelta
        target = target + timedelta(days=1)
    while target.weekday() >= 5:
        from datetime import timedelta
        target = target + timedelta(days=1)
    return target


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    print("=" * 72)
    print("MARGIN CHECK — projected basket at 1 % constant per trade")
    print("=" * 72)

    # ---- Connect MT5 (own IPC channel; doesn't touch running scheduler) ----
    client = MT5Client(
        login=int(settings.MT5_LOGIN),
        password=str(settings.MT5_PASSWORD),
        server=str(settings.MT5_SERVER),
    )
    client.connect()
    try:
        account = client.get_account_info()
        # mt5.account_info() also exposes margin_free / margin_level which
        # the typed AccountInfo doesn't carry — read raw for those.
        raw = mt5.account_info()
        margin_free = float(getattr(raw, "margin_free", 0.0))
        margin_used = float(getattr(raw, "margin", 0.0))
        margin_level = float(getattr(raw, "margin_level", 0.0))

        print()
        print("Account state:")
        print(f"  Balance:        ${account.balance:>12,.2f}")
        print(f"  Equity:         ${account.equity:>12,.2f}")
        print(f"  Margin used:    ${margin_used:>12,.2f}")
        print(f"  Margin free:    ${margin_free:>12,.2f}")
        print(f"  Margin level:   {margin_level:>12,.2f} %"
              if margin_level else "  Margin level:   n/a (no open positions)")

        # ---- Compute risk at the deployed schedule ----
        risk_pct = adaptive_risk_per_trade_pct(
            current_capital_usd=account.balance,
            capital_floor_for_full_risk_usd=float(
                getattr(settings, "ROTATION_CAPITAL_FLOOR_FOR_FULL_RISK_USD", 4950.0)
            ),
            risk_full_pct=float(
                getattr(settings, "ROTATION_RISK_PER_TRADE_FULL_PCT", 0.01)
            ),
            risk_reduced_pct=float(
                getattr(settings, "ROTATION_RISK_PER_TRADE_REDUCED_PCT", 0.005)
            ),
        )
        risk_usd = account.balance * risk_pct
        print(f"\nRisk schedule: {risk_pct:.2%} per trade -> "
              f"${risk_usd:.2f} risk per leg")

        # ---- Build panel + score universe ----
        universe = tuple(getattr(settings, "ROTATION_UNIVERSE", ()))
        K = int(getattr(settings, "ROTATION_K", 5))
        momentum = int(getattr(settings, "ROTATION_MOMENTUM_LOOKBACK_DAYS", 126))
        atr_period = int(getattr(settings, "ROTATION_ATR_PERIOD", 20))
        n_bars = momentum + atr_period + 30

        params = StrategyParams(
            universe=universe, momentum_lookback_days=momentum, K=K,
            rebalance_frequency_days=int(
                getattr(settings, "ROTATION_REBALANCE_DAYS", 5)
            ),
            atr_period=atr_period,
        )
        now_utc = datetime.now(UTC)

        panel: dict[str, pd.DataFrame] = {}
        for asset in universe:
            try:
                df = client.fetch_ohlc(asset, "D1", n_bars)
            except Exception as exc:  # noqa: BLE001
                print(f"  [WARN] fetch_ohlc({asset}) failed: {exc!r}")
                continue
            panel[asset] = df.set_index("time").sort_index()

        scores: dict[str, float | None] = {}
        atrs: dict[str, float] = {}
        for asset in universe:
            df = panel.get(asset)
            if df is None:
                scores[asset] = None
                continue
            score, atr = _score_one_asset(df, now_utc, params)
            scores[asset] = score
            atrs[asset] = atr
        basket = sorted(set(select_top_k(scores, K)))
        print(f"\nProjected top-K basket ({K}): {basket}")

        # ---- Per-leg sizing + margin lookup ----
        print()
        print(f"{'Symbol':<8} {'Lot':>10} {'ATR':>10} {'Notional':>14} "
              f"{'Margin req':>14} {'Cumul':>14}")
        print("-" * 72)
        cumul_margin = 0.0
        cumul_notional = 0.0
        rows: list[tuple] = []
        for sym in basket:
            sym_info = client.get_symbol_info(sym)
            atr = atrs.get(sym, float("nan"))
            try:
                lot = compute_rotation_volume(
                    risk_usd=risk_usd, atr_at_entry=atr, symbol_info=sym_info,
                )
            except ValueError as exc:
                print(f"  [WARN] sizing {sym}: {exc!r}")
                continue
            ask = float(sym_info.ask)
            notional = lot * ask * float(sym_info.trade_contract_size)
            margin = mt5.order_calc_margin(
                mt5.ORDER_TYPE_BUY, sym, lot, ask
            )
            if margin is None:
                err = mt5.last_error()
                print(f"  [WARN] order_calc_margin({sym}) returned None: "
                      f"last_error={err!r}")
                continue
            margin = float(margin)
            cumul_margin += margin
            cumul_notional += notional
            rows.append((sym, lot, atr, notional, margin, cumul_margin))
            print(f"{sym:<8} {lot:>10.4f} {atr:>10.4f} "
                  f"${notional:>13,.2f} ${margin:>13,.2f} ${cumul_margin:>13,.2f}")

        print("-" * 72)
        print(f"{'TOTAL':<8} {'':>10} {'':>10} "
              f"${cumul_notional:>13,.2f} ${cumul_margin:>13,.2f}")

        # ---- Ratios + verdict ----
        ratio_used = (cumul_margin / margin_free) if margin_free > 0 else float("inf")
        post_margin_used = margin_used + cumul_margin
        post_equity = account.equity
        post_margin_level = (
            (post_equity / post_margin_used * 100.0)
            if post_margin_used > 0 else float("inf")
        )
        post_margin_free = margin_free - cumul_margin

        print()
        print("Projected after-rebalance state:")
        print(f"  Margin used would be:        ${post_margin_used:,.2f}")
        print(f"  Margin free would be:        ${post_margin_free:,.2f}")
        print(f"  Margin level would be:       {post_margin_level:,.2f} %")
        print(f"  Ratio (req / free now):      {ratio_used:.1%}")

        print()
        if ratio_used < 0.60 and post_margin_level > 200.0:
            verdict = "SAFE"
            print(f"VERDICT: [PASS] {verdict} -- first cycle 23:00 Paris OK to fire")
        elif ratio_used < 0.80 and post_margin_level > 100.0:
            verdict = "TIGHT"
            print(f"VERDICT: [WARN] {verdict}")
            print("  Drawdown intra-position could margin-call before next rebalance.")
            print("  Operator decision: tolerate, OR touch KILL_SWITCH and revert "
                  "to 0.5 %.")
        else:
            verdict = "INSUFFICIENT"
            print(f"VERDICT: [FAIL] {verdict}")
            print("  Action required before 23:00 Paris:")
            print("    1. touch KILL_SWITCH at the project root to block the cycle")
            print("    2. revert config to ROTATION_RISK_PER_TRADE_REDUCED_PCT = 0.005")
            print("    3. restart scheduler")

        # Time-to-cycle
        from datetime import timedelta
        next_cycle = _next_2300_paris()
        delta = next_cycle - datetime.now(_TZ_PARIS)
        hours = delta.total_seconds() / 3600.0
        print()
        print(f"Next cycle: {next_cycle.strftime('%Y-%m-%d %H:%M')} Paris "
              f"(in {hours:.1f}h)")
        print("=" * 72)

    finally:
        # IMPORTANT: this is OUR python process's IPC channel; calling
        # shutdown only closes our own connection. The running scheduler
        # in another PID retains its own MT5 link.
        client.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
