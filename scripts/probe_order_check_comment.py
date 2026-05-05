"""Read-only probe — find which mt5.order_send `comment` shape FundedNext
accepts. Uses ``mt5.order_check`` (validates the request server-side
without executing) so this script NEVER places an order.

Run:
    python scripts/probe_order_check_comment.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import MetaTrader5 as mt5  # type: ignore[import-not-found]  # noqa: E402

from config import settings  # noqa: E402
from src.mt5_client.client import MT5Client  # noqa: E402


VARIANTS: list[tuple[str, object]] = [
    ("original_failing",      "rotation:trend_rotation_d1:open"),  # 31 chars, : delim
    ("with_slash",            "rotation/trend_rotation_d1/open"),
    ("dot_v_dash",             "rotation-trend_rotation_d1-open"),
    ("underscore_only",       "rotation_trend_rotation_d1_open"),  # 31 chars, no special
    ("truncated_31_chars_n",  "rotation_trend_rotation_d1_open"),
    ("truncated_20_chars",    "rotation_trend_d1_o"),
    ("truncated_16_chars",    "r7799_trend_open"),
    ("simple_lower",          "rotation"),
    ("conventional_short",    "r7799_JP225"),
    ("empty_string",          ""),
    ("none_omitted",          None),  # don't include the key
]


def _request(symbol: str, ask: float) -> dict:
    return {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": 0.01,
        "type": mt5.ORDER_TYPE_BUY,
        "price": ask,
        "sl": 0.0,
        "tp": 0.0,
        "magic": 7799,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }


def main() -> int:
    client = MT5Client(
        login=int(settings.MT5_LOGIN),
        password=str(settings.MT5_PASSWORD),
        server=str(settings.MT5_SERVER),
    )
    client.connect()
    try:
        symbol = "JP225"
        info = mt5.symbol_info(symbol)
        if info is None or not info.visible:
            print(f"[skip] {symbol} not visible — try another universe member")
            return 1
        tick = mt5.symbol_info_tick(symbol)
        ask = float(tick.ask)
        print(f"Symbol: {symbol}  ask={ask}")
        print()
        print(f"{'name':<24} {'len':>4} {'retcode':>8}  comment_repr")
        print("-" * 80)

        for name, comment in VARIANTS:
            req = _request(symbol, ask)
            if comment is not None:
                req["comment"] = comment
            result = mt5.order_check(req)
            if result is None:
                err = mt5.last_error()
                print(f"{name:<24} {'?':>4} {'NONE':>8}  err={err!r}")
                continue
            retcode = int(result.retcode)
            length = len(comment) if comment is not None else 0
            comment_disp = "<omitted>" if comment is None else repr(comment)
            print(f"{name:<24} {length:>4} {retcode:>8}  {comment_disp}")

        print()
        print("Retcode reference:")
        print("  0          = TRADE_RETCODE_REQUOTE / generic OK at order_check")
        print(
            f"  {mt5.TRADE_RETCODE_DONE} = TRADE_RETCODE_DONE — request would succeed"
        )
        print("  10013      = TRADE_RETCODE_INVALID_REQUEST — comment is rejected")
        print("  10027      = TRADE_RETCODE_LIMIT_VOLUME / MARKET_CLOSED")
    finally:
        client.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
