"""MT5 connectivity wrapper — connect, fetch OHLC, time conversions, account info."""

from src.mt5_client.client import AccountInfo, MT5Client
from src.mt5_client.exceptions import (
    MT5AccountError,
    MT5ConnectionError,
    MT5DataError,
    MT5Error,
)
from src.mt5_client.retry import with_retry
from src.mt5_client.time_conversion import (
    broker_naive_seconds_to_utc,
    broker_naive_to_utc,
    detect_broker_offset_hours,
)

__all__ = [
    "AccountInfo",
    "MT5AccountError",
    "MT5Client",
    "MT5ConnectionError",
    "MT5DataError",
    "MT5Error",
    "broker_naive_seconds_to_utc",
    "broker_naive_to_utc",
    "detect_broker_offset_hours",
    "with_retry",
]
