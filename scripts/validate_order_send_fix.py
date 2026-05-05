"""Live shadow validation — confirm the fixed comment shape passes
``mt5.order_check`` for every asset in the projected basket.

Read-only. Calls ``order_check`` (server-side validation, no
execution) for each leg the next rebalance would open. NEVER calls
``order_send``.

Run on the Windows host with the live MT5 terminal connected:

    python scripts/validate_order_send_fix.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from datetime import UTC, datetime  # noqa: E402

import MetaTrader5 as mt5  # type: ignore[import-not-found]  # noqa: E402
import pandas as pd  # noqa: E402

from config import settings  # noqa: E402
from src.execution.order_manager_rotation import compute_rotation_volume  # noqa: E402
from src.execution.safe_guards import adaptive_risk_per_trade_pct  # noqa: E402
from src.mt5_client.client import MT5Client, _sanitize_order_comment  # noqa: E402
from src.strategies.trend_rotation_d1 import StrategyParams  # noqa: E402
from src.strategies.trend_rotation_d1.pipeline import _score_one_asset  # noqa: E402
from src.strategies.trend_rotation_d1.ranking import select_top_k  # noqa: E402


def main() -> int:
    client = MT5Client(
        login=int(settings.MT5_LOGIN),
        password=str(settings.MT5_PASSWORD),
        server=str(settings.MT5_SERVER),
    )
    client.connect()
    try:
        account = client.get_account_info()
        balance = float(account.balance)

        # Compute basket via pipeline helpers (same code path the cycle uses).
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

        panel = {}
        for asset in universe:
            df = client.fetch_ohlc(asset, "D1", n_bars)
            panel[asset] = df.set_index("time").sort_index()

        scores: dict[str, float | None] = {}
        atrs: dict[str, float] = {}
        for asset in universe:
            score, atr = _score_one_asset(panel[asset], now_utc, params)
            scores[asset] = score
            atrs[asset] = atr
        basket = sorted(set(select_top_k(scores, K)))

        # Sizing at the deployed risk schedule.
        risk_pct = adaptive_risk_per_trade_pct(
            current_capital_usd=balance,
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
        risk_usd = balance * risk_pct
        magic = int(getattr(settings, "ROTATION_MAGIC_NUMBER", 7799))

        print("=" * 72)
        print("ORDER_CHECK SHADOW VALIDATION — fixed comment shape")
        print("=" * 72)
        print(f"Balance: ${balance:,.2f}  risk: {risk_pct:.2%} -> ${risk_usd:.2f}/leg")
        print(f"Basket:  {basket}")
        print(f"Magic:   {magic}")
        print()
        print(f"{'Symbol':<8} {'Lot':>10} {'Comment':<22} {'len':>4} {'retcode':>8}")
        print("-" * 72)

        all_ok = True
        for sym in basket:
            sym_info = client.get_symbol_info(sym)
            try:
                lot = compute_rotation_volume(
                    risk_usd=risk_usd,
                    atr_at_entry=atrs[sym],
                    symbol_info=sym_info,
                )
            except ValueError as exc:
                print(f"{sym:<8} sizing failed: {exc!r}")
                all_ok = False
                continue
            comment_raw = f"r{magic}_{sym}"
            comment = _sanitize_order_comment(comment_raw)
            ask = float(sym_info.ask)
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": sym,
                "volume": float(lot),
                "type": mt5.ORDER_TYPE_BUY,
                "price": ask,
                "sl": 0.0,
                "tp": 0.0,
                "magic": magic,
                "comment": comment,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            result = mt5.order_check(request)
            if result is None:
                err = mt5.last_error()
                print(f"{sym:<8} {lot:>10.4f} {comment:<22} "
                      f"{len(comment):>4} {'NONE':>8}  err={err!r}")
                all_ok = False
                continue
            retcode = int(result.retcode)
            print(f"{sym:<8} {lot:>10.4f} {comment:<22} "
                  f"{len(comment):>4} {retcode:>8}")
            if retcode != mt5.TRADE_RETCODE_DONE:
                # 0 (REQUOTE) at order_check is also acceptable; only flag
                # explicit invalid-request retcodes as failures.
                if retcode in (10013, 10014, 10015):
                    all_ok = False

        print("-" * 72)
        verdict = "PASS" if all_ok else "FAIL"
        print(f"Overall verdict: {verdict}")
        print("=" * 72)
    finally:
        client.shutdown()
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
