"""MT5 connectivity smoke test (Sprint 0).

Run this on the Windows host with the MT5 terminal open and logged in.

What it does:
    1. Initializes the MetaTrader5 Python API with credentials from
       ``config.secrets`` (via ``config.settings``).
    2. Fetches the last 10 M5 candles for XAUUSD via
       ``mt5.copy_rates_from_pos`` and prints them as a pandas DataFrame.
    3. Prints account info (balance, equity, currency, leverage) with the
       account login MASKED (only last 4 digits visible).
    4. Cleanly shuts the MT5 connection down in a try/finally.
    5. On failure, prints ``mt5.last_error()`` with a clear message.

HARD CONSTRAINT — DO NOT REMOVE:
    This script must contain ZERO calls to any order placement /
    modification / closure function:
        mt5.order_send, mt5.order_modify, mt5.order_close, mt5.order_check,
        mt5.order_calc_margin, or any other ``mt5.order_*`` function.
    The TJR system is detection + notification only. The human places
    every trade manually. See CLAUDE.md rule #1 and
    docs/04_PROJECT_RULES.md "no auto-trading code".
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

from _bootstrap import load_settings


def _mask_login(login: int) -> str:
    """Return ``****1234`` style masked representation of an MT5 login."""
    s = str(login)
    if len(s) <= 4:
        return "*" * len(s)
    return "*" * (len(s) - 4) + s[-4:]


def main() -> int:
    settings = load_settings()

    try:
        import MetaTrader5 as mt5  # type: ignore[import-not-found]
    except ImportError:
        print(
            "ERROR: MetaTrader5 package not installed. This script must run "
            "on the Windows host. Install with `pip install -r requirements.txt`.",
            file=sys.stderr,
        )
        return 2

    try:
        import pandas as pd
    except ImportError:
        print("ERROR: pandas not installed — see requirements.txt.", file=sys.stderr)
        return 2

    print(f"[{datetime.now(UTC).isoformat()}] MT5 smoke test starting")

    initialized = False
    try:
        initialized = mt5.initialize(
            login=int(settings.MT5_LOGIN),
            password=str(settings.MT5_PASSWORD),
            server=str(settings.MT5_SERVER),
        )
        if not initialized:
            err = mt5.last_error()
            print(
                "ERROR: mt5.initialize() failed. Check that the MT5 "
                "terminal is open and logged in, and that MT5_LOGIN / "
                "MT5_PASSWORD / MT5_SERVER in config/secrets.py are correct.",
                file=sys.stderr,
            )
            print(f"  mt5.last_error() = {err!r}", file=sys.stderr)
            return 1

        account = mt5.account_info()
        if account is None:
            print(
                f"ERROR: mt5.account_info() returned None — last_error=" f"{mt5.last_error()!r}",
                file=sys.stderr,
            )
            return 1

        print("Account:")
        print(f"  login    : {_mask_login(account.login)}")
        print(f"  currency : {account.currency}")
        print(f"  leverage : 1:{account.leverage}")
        print(f"  balance  : {account.balance:.2f}")
        print(f"  equity   : {account.equity:.2f}")

        symbol = "XAUUSD"
        timeframe = mt5.TIMEFRAME_M5
        n_candles = 10

        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, n_candles)
        if rates is None or len(rates) == 0:
            print(
                f"ERROR: copy_rates_from_pos returned no data for {symbol}. "
                "Check the symbol name in your terminal — some brokers use "
                "suffixes like XAUUSD.r or XAUUSD.m.",
                file=sys.stderr,
            )
            print(f"  mt5.last_error() = {mt5.last_error()!r}", file=sys.stderr)
            return 1

        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        print(f"\nLast {n_candles} M5 candles for {symbol}:")
        print(df.to_string(index=False))

        print("\nMT5 smoke test OK.")
        return 0

    finally:
        if initialized:
            mt5.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
