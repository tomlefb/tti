"""Minimal sanity check for the FundedNext MT5 terminal connection.

Run this BEFORE any of the other broker_calibration scripts. If it
fails, no other script in this directory will succeed — fix the
terminal/credentials issue first.

Usage (Windows host, MT5 terminal already open and logged in):

    python -m calibration.broker_calibration.test_mt5_connection

The script never logs the password and only exposes the masked tail
of the account number (`1180****53` style).
"""

from __future__ import annotations

import sys

try:
    import MetaTrader5 as mt5  # type: ignore[import-not-found]
except ImportError:
    print(
        "[FAIL] MetaTrader5 package is not installed. "
        "Run on the Windows host with `pip install MetaTrader5`."
    )
    sys.exit(2)

from config.secrets import MT5_LOGIN, MT5_PASSWORD, MT5_SERVER


def _mask_login(login: int) -> str:
    s = str(login)
    if len(s) <= 4:
        return "*" * len(s)
    return s[:4] + "*" * (len(s) - 6) + s[-2:]


def main() -> int:
    print(f"[INFO] Connecting to MT5 (login={_mask_login(MT5_LOGIN)}, server={MT5_SERVER!r})")

    ok = mt5.initialize(
        login=int(MT5_LOGIN),
        password=str(MT5_PASSWORD),
        server=str(MT5_SERVER),
    )
    if not ok:
        print(f"[FAIL] mt5.initialize() failed. last_error={mt5.last_error()!r}")
        return 1

    try:
        info = mt5.account_info()
        if info is None:
            print(f"[FAIL] account_info() returned None. last_error={mt5.last_error()!r}")
            return 1

        print("[OK] Connected.")
        print(f"  login            : {_mask_login(info.login)}")
        print(f"  currency         : {info.currency}")
        print(f"  balance          : {info.balance:.2f}")
        print(f"  equity           : {info.equity:.2f}")
        print(f"  leverage         : 1:{info.leverage}")
        print(f"  trade_allowed    : {bool(getattr(info, 'trade_allowed', False))}")
        print(f"  margin_mode      : {getattr(info, 'margin_mode', 'n/a')}")

        symbols = mt5.symbols_get() or []
        print(f"[INFO] {len(symbols)} symbols visible from terminal.")
        # Show only the ones we actually care about (and a few others if missing).
        wanted = ("XAUUSD", "NDX100", "US100", "USTEC", "NAS100")
        present = [s.name for s in symbols if s.name in wanted]
        print(f"  watched/known    : {present}")

        return 0
    finally:
        mt5.shutdown()
        print("[INFO] Connection closed.")


if __name__ == "__main__":
    sys.exit(main())
