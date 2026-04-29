"""Unit tests for src.mt5_client.client.

Mock the ``MetaTrader5`` package via dependency injection (``mt5_module``
constructor kwarg) so the tests exercise pure logic without touching
the real MT5 terminal.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from src.mt5_client.client import MT5Client
from src.mt5_client.exceptions import (
    MT5AccountError,
    MT5ConnectionError,
    MT5DataError,
)


def _make_mt5_mock(
    *,
    initialize_returns: bool = True,
    rates: np.ndarray | None = None,
    account: object | None = None,
    deals: list | None = None,
    tick_time: float | None = None,
):
    """Build a SimpleNamespace mimicking the ``MetaTrader5`` module."""
    m = MagicMock()
    m.initialize.return_value = initialize_returns
    m.shutdown.return_value = None
    m.last_error.return_value = (1, "Success")
    m.copy_rates_from_pos.return_value = rates
    m.account_info.return_value = account
    m.history_deals_get.return_value = deals
    m.symbol_info_tick.return_value = (
        SimpleNamespace(time=tick_time) if tick_time is not None else None
    )

    # Timeframe constants — real MT5 values.
    m.TIMEFRAME_M1 = 1
    m.TIMEFRAME_M5 = 5
    m.TIMEFRAME_M15 = 15
    m.TIMEFRAME_H1 = 16385
    m.TIMEFRAME_H4 = 16388
    m.TIMEFRAME_D1 = 16408
    return m


def _make_rates(base_seconds: float, count: int, *, step_seconds: int = 300) -> np.ndarray:
    """Build an MT5-shaped recarray with monotonic timestamps."""
    times = np.array([base_seconds + i * step_seconds for i in range(count)], dtype=np.int64)
    return np.array(
        list(
            zip(
                times,
                [1.0 + i for i in range(count)],
                [1.5 + i for i in range(count)],
                [0.5 + i for i in range(count)],
                [1.2 + i for i in range(count)],
                [100 + i for i in range(count)],
                [0] * count,
                [10 + i for i in range(count)],
            )
        ),
        dtype=[
            ("time", "i8"),
            ("open", "f8"),
            ("high", "f8"),
            ("low", "f8"),
            ("close", "f8"),
            ("tick_volume", "i8"),
            ("spread", "i4"),
            ("real_volume", "i8"),
        ],
    )


# -----------------------------------------------------------------------------
# connect / shutdown
# -----------------------------------------------------------------------------


def test_connect_raises_when_initialize_returns_false():
    mt5 = _make_mt5_mock(initialize_returns=False)
    client = MT5Client(login=1, password="x", server="srv", mt5_module=mt5)
    with pytest.raises(MT5ConnectionError, match="initialize"):
        client.connect()


def test_connect_succeeds_and_caches_offset():
    # Broker tick is +3h ahead of UTC.
    now_utc = datetime.now(tz=UTC).replace(microsecond=0)
    tick_time = now_utc.timestamp() + 3 * 3600
    mt5 = _make_mt5_mock(tick_time=tick_time)
    client = MT5Client(login=12345, password="x", server="srv", mt5_module=mt5)
    client.connect()
    assert client.is_connected()
    assert client._broker_offset_hours == 3


def test_shutdown_is_idempotent():
    mt5 = _make_mt5_mock(tick_time=datetime.now(tz=UTC).timestamp())
    client = MT5Client(login=1, password="x", server="srv", mt5_module=mt5)
    client.connect()
    client.shutdown()
    client.shutdown()  # second call must not raise
    assert not client.is_connected()
    # mt5.shutdown is invoked exactly once (second call short-circuits).
    assert mt5.shutdown.call_count == 1


# -----------------------------------------------------------------------------
# fetch_ohlc
# -----------------------------------------------------------------------------


def test_fetch_ohlc_raises_when_not_connected():
    mt5 = _make_mt5_mock()
    client = MT5Client(login=1, password="x", server="srv", mt5_module=mt5)
    with pytest.raises(MT5ConnectionError, match="not connected"):
        client.fetch_ohlc("XAUUSD", "M5", 10)


def test_fetch_ohlc_raises_on_unknown_timeframe():
    now_utc = datetime.now(tz=UTC)
    mt5 = _make_mt5_mock(tick_time=now_utc.timestamp())
    client = MT5Client(login=1, password="x", server="srv", mt5_module=mt5)
    client.connect()
    with pytest.raises(MT5DataError, match="unknown timeframe"):
        client.fetch_ohlc("XAUUSD", "Z9", 10)


def test_fetch_ohlc_raises_on_empty_result():
    now_utc = datetime.now(tz=UTC)
    mt5 = _make_mt5_mock(rates=None, tick_time=now_utc.timestamp())
    client = MT5Client(login=1, password="x", server="srv", mt5_module=mt5)
    client.connect()
    with pytest.raises(MT5DataError, match="returned no data"):
        client.fetch_ohlc("XAUUSD", "M5", 10)


def test_fetch_ohlc_converts_timestamps_to_utc_using_broker_offset():
    """Broker returns POSIX seconds for 13:00 broker time at +3 → 10:00 UTC."""
    # Broker offset detection: tick_time at +3h.
    now_utc = datetime(2026, 7, 15, 10, 0, 0, tzinfo=UTC)
    tick_seconds = now_utc.timestamp() + 3 * 3600

    # Rates: candle wallclock 13:00 broker-naive. Encoded as POSIX seconds
    # whose decode-as-UTC reads 13:00 (the MT5 convention).
    candle_wallclock_broker = datetime(2026, 7, 15, 13, 0, 0, tzinfo=UTC).timestamp()
    rates = _make_rates(candle_wallclock_broker, count=3, step_seconds=300)

    mt5 = _make_mt5_mock(rates=rates, tick_time=tick_seconds)
    client = MT5Client(login=1, password="x", server="srv", mt5_module=mt5)
    client.connect()

    df = client.fetch_ohlc("XAUUSD", "M5", 3)
    assert len(df) == 3
    # First candle wallclock 13:00 broker → 10:00 UTC.
    assert df["time"].iloc[0].to_pydatetime().astimezone(UTC) == datetime(
        2026, 7, 15, 10, 0, 0, tzinfo=UTC
    )
    # Volume normalized from tick_volume.
    assert "volume" in df.columns
    assert int(df["volume"].iloc[0]) == 100


def test_fetch_ohlc_handles_volume_fallback_chain():
    """When 'tick_volume' is absent but 'real_volume' is present, use it."""
    now_utc = datetime(2026, 7, 15, 10, 0, 0, tzinfo=UTC)
    tick_seconds = now_utc.timestamp() + 2 * 3600  # +2h winter convention

    base = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC).timestamp()
    rates = np.array(
        [
            (int(base), 1.0, 1.5, 0.5, 1.2, 0, 999),
            (int(base) + 300, 1.1, 1.6, 0.6, 1.3, 0, 1001),
        ],
        dtype=[
            ("time", "i8"),
            ("open", "f8"),
            ("high", "f8"),
            ("low", "f8"),
            ("close", "f8"),
            ("spread", "i4"),
            ("real_volume", "i8"),
        ],
    )

    mt5 = _make_mt5_mock(rates=rates, tick_time=tick_seconds)
    client = MT5Client(login=1, password="x", server="srv", mt5_module=mt5)
    client.connect()

    df = client.fetch_ohlc("XAUUSD", "M5", 2)
    assert int(df["volume"].iloc[0]) == 999


# -----------------------------------------------------------------------------
# account_info
# -----------------------------------------------------------------------------


def test_get_account_info_raises_when_mt5_returns_none():
    now_utc = datetime.now(tz=UTC)
    mt5 = _make_mt5_mock(account=None, tick_time=now_utc.timestamp())
    client = MT5Client(login=1, password="x", server="srv", mt5_module=mt5)
    client.connect()
    with pytest.raises(MT5AccountError, match="account_info"):
        client.get_account_info()


def test_get_account_info_returns_typed_snapshot():
    now_utc = datetime.now(tz=UTC)
    account = SimpleNamespace(
        login=1234567,
        currency="USD",
        balance=5000.0,
        equity=4950.0,
        profit=-25.0,
        margin_level=0.0,
        leverage=100,
    )
    mt5 = _make_mt5_mock(account=account, tick_time=now_utc.timestamp())
    client = MT5Client(login=1234567, password="x", server="srv", mt5_module=mt5)
    client.connect()

    info = client.get_account_info()
    assert info.login_masked == "***4567"
    assert info.currency == "USD"
    assert info.balance == 5000.0
    assert info.equity == 4950.0
    assert info.profit == -25.0
    assert info.leverage == 100


# -----------------------------------------------------------------------------
# get_recent_trades
# -----------------------------------------------------------------------------


def test_get_recent_trades_rejects_naive_since():
    now_utc = datetime.now(tz=UTC)
    mt5 = _make_mt5_mock(tick_time=now_utc.timestamp())
    client = MT5Client(login=1, password="x", server="srv", mt5_module=mt5)
    client.connect()
    with pytest.raises(ValueError, match="UTC-aware"):
        client.get_recent_trades(datetime(2026, 1, 1, 0, 0, 0))


def test_get_recent_trades_returns_empty_on_none_deals():
    now_utc = datetime.now(tz=UTC)
    mt5 = _make_mt5_mock(deals=None, tick_time=now_utc.timestamp())
    client = MT5Client(login=1, password="x", server="srv", mt5_module=mt5)
    client.connect()
    out = client.get_recent_trades(datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC))
    assert out == []


def test_get_recent_trades_pairs_entry_and_exit_deals():
    """Entry+exit deals on the same position_id collapse into one Mt5Trade."""
    now_utc = datetime.now(tz=UTC)
    tick_seconds = now_utc.timestamp() + 3 * 3600

    entry_seconds = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC).timestamp()
    exit_seconds = datetime(2026, 4, 1, 12, 30, 0, tzinfo=UTC).timestamp()

    deals = [
        SimpleNamespace(
            position_id=42,
            symbol="XAUUSD",
            type=1,  # SELL → short
            entry=0,  # IN
            time=entry_seconds,
            price=2400.0,
            profit=0.0,
        ),
        SimpleNamespace(
            position_id=42,
            symbol="XAUUSD",
            type=0,  # opposite side at close
            entry=1,  # OUT
            time=exit_seconds,
            price=2390.0,
            profit=15.0,
        ),
    ]
    mt5 = _make_mt5_mock(deals=deals, tick_time=tick_seconds)
    client = MT5Client(login=1, password="x", server="srv", mt5_module=mt5)
    client.connect()

    trades = client.get_recent_trades(datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC))
    assert len(trades) == 1
    t = trades[0]
    assert t.ticket == 42
    assert t.symbol == "XAUUSD"
    assert t.direction == "short"
    # Entry wallclock 12:00 broker (+3) → 09:00 UTC.
    assert t.entry_time_utc == datetime(2026, 4, 1, 9, 0, 0, tzinfo=UTC)
    assert t.exit_time_utc == datetime(2026, 4, 1, 9, 30, 0, tzinfo=UTC)
    assert t.entry_price == 2400.0
    assert t.exit_price == 2390.0
    assert t.profit_usd == 15.0
