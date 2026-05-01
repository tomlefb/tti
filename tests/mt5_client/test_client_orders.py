"""Sprint 7 — order operations on ``MT5Client``.

These methods are the thin layer that the order_manager / lifecycle /
recovery modules call. They wrap ``mt5.order_send`` / ``mt5.symbol_info`` /
``mt5.positions_get`` / ``mt5.orders_get`` with typed return values and
raise ``MT5Error`` on unexpected None / malformed responses.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from src.mt5_client.client import (
    MT5Client,
    PendingOrderSnapshot,
    PositionSnapshot,
    SymbolInfoSnapshot,
)
from src.mt5_client.exceptions import MT5Error


def _connected_client(mt5: MagicMock) -> MT5Client:
    """Build a connected MT5Client with the given mt5 mock."""
    mt5.initialize.return_value = True
    mt5.last_error.return_value = (1, "Success")
    real_now_utc = datetime.now(tz=UTC).replace(microsecond=0)
    mt5.symbol_info_tick.return_value = SimpleNamespace(
        time=real_now_utc.timestamp() + 3 * 3600
    )
    # Required MT5 constants for trade requests.
    mt5.TRADE_ACTION_PENDING = 5
    mt5.TRADE_ACTION_REMOVE = 8
    mt5.TRADE_ACTION_SLTP = 7
    mt5.ORDER_TYPE_BUY_LIMIT = 2
    mt5.ORDER_TYPE_SELL_LIMIT = 3
    mt5.ORDER_TIME_GTC = 0
    mt5.ORDER_FILLING_IOC = 1
    client = MT5Client(login=1, password="x", server="srv", mt5_module=mt5)
    client.connect()
    return client


# -----------------------------------------------------------------------------
# get_symbol_info
# -----------------------------------------------------------------------------


def test_get_symbol_info_returns_typed_snapshot():
    mt5 = MagicMock()
    mt5.symbol_info.return_value = SimpleNamespace(
        name="XAUUSD",
        trade_contract_size=100.0,
        point=0.01,
        volume_min=0.01,
        volume_step=0.01,
        volume_max=100.0,
        ask=4360.5,
        bid=4360.0,
    )
    client = _connected_client(mt5)

    info = client.get_symbol_info("XAUUSD")
    assert isinstance(info, SymbolInfoSnapshot)
    assert info.symbol == "XAUUSD"
    assert info.trade_contract_size == 100.0
    assert info.volume_min == 0.01
    assert info.volume_step == 0.01
    assert info.ask == 4360.5
    assert info.bid == 4360.0


def test_get_symbol_info_raises_when_mt5_returns_none():
    mt5 = MagicMock()
    mt5.symbol_info.return_value = None
    client = _connected_client(mt5)

    with pytest.raises(MT5Error, match="symbol_info"):
        client.get_symbol_info("XAUUSD")


# -----------------------------------------------------------------------------
# place_limit_order
# -----------------------------------------------------------------------------


def test_place_limit_order_short_sends_sell_limit_request():
    mt5 = MagicMock()
    mt5.order_send.return_value = SimpleNamespace(
        retcode=10009, order=999, deal=0, comment="Done", request_id=1
    )
    client = _connected_client(mt5)

    result = client.place_limit_order(
        symbol="XAUUSD",
        direction="short",
        volume=0.05,
        price=4360.0,
        sl=4375.0,
        tp=4080.5,
        magic=7766,
        comment="sprint7:A",
    )

    assert result.retcode == 10009
    assert result.order == 999
    # Verify request payload.
    args, _ = mt5.order_send.call_args
    request = args[0]
    assert request["action"] == mt5.TRADE_ACTION_PENDING
    assert request["type"] == mt5.ORDER_TYPE_SELL_LIMIT
    assert request["symbol"] == "XAUUSD"
    assert request["volume"] == 0.05
    assert request["price"] == 4360.0
    assert request["sl"] == 4375.0
    assert request["tp"] == 4080.5
    assert request["magic"] == 7766


def test_place_limit_order_long_sends_buy_limit_request():
    mt5 = MagicMock()
    mt5.order_send.return_value = SimpleNamespace(
        retcode=10009, order=1000, deal=0, comment="Done", request_id=1
    )
    client = _connected_client(mt5)

    client.place_limit_order(
        symbol="NDX100",
        direction="long",
        volume=1.0,
        price=20000.0,
        sl=19990.0,
        tp=20100.0,
        magic=7766,
    )

    args, _ = mt5.order_send.call_args
    request = args[0]
    assert request["type"] == mt5.ORDER_TYPE_BUY_LIMIT


def test_place_limit_order_unknown_direction_raises():
    mt5 = MagicMock()
    client = _connected_client(mt5)
    with pytest.raises(ValueError, match="direction"):
        client.place_limit_order(
            symbol="XAUUSD",
            direction="sideways",  # invalid
            volume=0.05,
            price=4360.0,
            sl=4375.0,
            tp=4300.0,
            magic=7766,
        )


def test_place_limit_order_propagates_mt5_none_as_error():
    """When mt5.order_send returns None (terminal disconnect), wrap in MT5Error."""
    mt5 = MagicMock()
    mt5.order_send.return_value = None
    client = _connected_client(mt5)

    with pytest.raises(MT5Error, match="order_send"):
        client.place_limit_order(
            symbol="XAUUSD",
            direction="short",
            volume=0.05,
            price=4360.0,
            sl=4375.0,
            tp=4080.5,
            magic=7766,
        )


# -----------------------------------------------------------------------------
# cancel_pending_order
# -----------------------------------------------------------------------------


def test_cancel_pending_order_sends_remove_request():
    mt5 = MagicMock()
    mt5.order_send.return_value = SimpleNamespace(retcode=10009, comment="Done")
    client = _connected_client(mt5)

    ok = client.cancel_pending_order(12345)
    assert ok is True

    args, _ = mt5.order_send.call_args
    request = args[0]
    assert request["action"] == mt5.TRADE_ACTION_REMOVE
    assert request["order"] == 12345


def test_cancel_pending_order_returns_false_on_non_done_retcode():
    mt5 = MagicMock()
    mt5.order_send.return_value = SimpleNamespace(retcode=10004, comment="Requote")
    client = _connected_client(mt5)
    assert client.cancel_pending_order(12345) is False


# -----------------------------------------------------------------------------
# modify_position_sl
# -----------------------------------------------------------------------------


def test_modify_position_sl_sends_sltp_request():
    mt5 = MagicMock()
    # The position must exist so the modify call can read its current TP.
    mt5.positions_get.return_value = [
        SimpleNamespace(
            ticket=42,
            symbol="XAUUSD",
            volume=0.05,
            type=1,  # SELL
            price_open=4360.0,
            sl=4375.0,
            tp=4080.5,
            magic=7766,
            time=int(datetime.now(tz=UTC).timestamp()),
            profit=0.0,
        )
    ]
    mt5.order_send.return_value = SimpleNamespace(retcode=10009, comment="Done")
    client = _connected_client(mt5)

    ok = client.modify_position_sl(ticket=42, new_sl=4360.0)
    assert ok is True

    args, _ = mt5.order_send.call_args
    request = args[0]
    assert request["action"] == mt5.TRADE_ACTION_SLTP
    assert request["position"] == 42
    assert request["sl"] == 4360.0
    # TP preserved from the existing position.
    assert request["tp"] == 4080.5


def test_modify_position_sl_unknown_ticket_returns_false():
    mt5 = MagicMock()
    mt5.positions_get.return_value = []
    client = _connected_client(mt5)
    assert client.modify_position_sl(ticket=999, new_sl=1.0) is False


# -----------------------------------------------------------------------------
# get_open_positions / get_pending_orders
# -----------------------------------------------------------------------------


def test_get_open_positions_filters_by_magic():
    mt5 = MagicMock()
    real_now_utc = datetime.now(tz=UTC).replace(microsecond=0)
    # Two positions, one ours (magic 7766), one not.
    mt5.positions_get.return_value = [
        SimpleNamespace(
            ticket=42,
            symbol="XAUUSD",
            type=1,
            volume=0.05,
            price_open=4360.0,
            sl=4375.0,
            tp=4080.5,
            magic=7766,
            time=int(real_now_utc.timestamp()),
            profit=15.5,
        ),
        SimpleNamespace(
            ticket=43,
            symbol="EURUSD",
            type=0,
            volume=0.1,
            price_open=1.07,
            sl=1.06,
            tp=1.08,
            magic=9999,
            time=int(real_now_utc.timestamp()),
            profit=-5.0,
        ),
    ]
    client = _connected_client(mt5)

    out = client.get_open_positions(magic=7766)
    assert len(out) == 1
    assert out[0].ticket == 42
    assert out[0].direction == "short"
    assert out[0].magic == 7766


def test_get_open_positions_no_magic_returns_all():
    mt5 = MagicMock()
    real_now_utc = datetime.now(tz=UTC).replace(microsecond=0)
    mt5.positions_get.return_value = [
        SimpleNamespace(
            ticket=42,
            symbol="XAUUSD",
            type=0,
            volume=0.05,
            price_open=4360.0,
            sl=4350.0,
            tp=4400.0,
            magic=7766,
            time=int(real_now_utc.timestamp()),
            profit=0.0,
        ),
        SimpleNamespace(
            ticket=43,
            symbol="EURUSD",
            type=1,
            volume=0.1,
            price_open=1.07,
            sl=1.08,
            tp=1.06,
            magic=9999,
            time=int(real_now_utc.timestamp()),
            profit=0.0,
        ),
    ]
    client = _connected_client(mt5)

    out = client.get_open_positions()
    assert len(out) == 2


def test_get_pending_orders_filters_by_magic():
    mt5 = MagicMock()
    real_now_utc = datetime.now(tz=UTC).replace(microsecond=0)
    mt5.orders_get.return_value = [
        SimpleNamespace(
            ticket=100,
            symbol="XAUUSD",
            type=3,  # SELL_LIMIT
            volume_initial=0.05,
            price_open=4360.0,
            sl=4375.0,
            tp=4080.5,
            magic=7766,
            time_setup=int(real_now_utc.timestamp()),
        ),
        SimpleNamespace(
            ticket=101,
            symbol="EURUSD",
            type=2,
            volume_initial=0.1,
            price_open=1.07,
            sl=1.06,
            tp=1.08,
            magic=9999,
            time_setup=int(real_now_utc.timestamp()),
        ),
    ]
    # The client maps SELL_LIMIT (3) → "short", BUY_LIMIT (2) → "long".
    mt5.ORDER_TYPE_BUY_LIMIT = 2
    mt5.ORDER_TYPE_SELL_LIMIT = 3
    client = _connected_client(mt5)

    out = client.get_pending_orders(magic=7766)
    assert len(out) == 1
    assert out[0].ticket == 100
    assert out[0].direction == "short"


# -----------------------------------------------------------------------------
# close_partial_position
# -----------------------------------------------------------------------------


def test_close_partial_position_sends_market_close_for_short():
    """Closing half of a short position = BUY market order with `position`
    field pointing to our ticket."""
    mt5 = MagicMock()
    real_now_utc = datetime.now(tz=UTC).replace(microsecond=0)
    mt5.positions_get.return_value = [
        SimpleNamespace(
            ticket=42,
            symbol="XAUUSD",
            type=1,  # SELL — we're short
            volume=0.05,
            price_open=4360.0,
            sl=4375.0,
            tp=4080.5,
            magic=7766,
            time=int(real_now_utc.timestamp()),
            profit=0.0,
        )
    ]
    mt5.order_send.return_value = SimpleNamespace(retcode=10009, comment="Done")
    mt5.symbol_info_tick.return_value = SimpleNamespace(
        bid=4280.0,
        ask=4280.5,
        time=real_now_utc.timestamp() + 3 * 3600,
    )
    mt5.TRADE_ACTION_DEAL = 1
    mt5.ORDER_TYPE_BUY = 0
    mt5.ORDER_TYPE_SELL = 1
    client = _connected_client(mt5)

    ok = client.close_partial_position(ticket=42, volume=0.025)
    assert ok is True

    args, _ = mt5.order_send.call_args
    request = args[0]
    assert request["action"] == mt5.TRADE_ACTION_DEAL
    assert request["position"] == 42
    assert request["volume"] == 0.025
    # BUY to close a SHORT position.
    assert request["type"] == mt5.ORDER_TYPE_BUY


def test_close_partial_position_unknown_ticket_returns_false():
    mt5 = MagicMock()
    mt5.positions_get.return_value = []
    client = _connected_client(mt5)
    assert client.close_partial_position(ticket=999, volume=0.025) is False


# -----------------------------------------------------------------------------
# get_position_close_info
# -----------------------------------------------------------------------------


def test_get_position_close_info_extracts_exit_price_and_profit():
    """When a position has been closed, history_deals_get returns the
    exit deal(s); the helper returns exit_price + profit_usd."""
    mt5 = MagicMock()
    real_now_utc = datetime.now(tz=UTC).replace(microsecond=0)
    entry_seconds = real_now_utc.timestamp() + 3 * 3600 - 600  # 10 min ago broker-naive
    exit_seconds = real_now_utc.timestamp() + 3 * 3600
    mt5.history_deals_get.return_value = [
        SimpleNamespace(
            position_id=42,
            symbol="XAUUSD",
            type=1,
            entry=0,  # IN
            time=entry_seconds,
            price=4360.0,
            profit=0.0,
        ),
        SimpleNamespace(
            position_id=42,
            symbol="XAUUSD",
            type=0,
            entry=1,  # OUT
            time=exit_seconds,
            price=4080.0,
            profit=125.0,
        ),
    ]
    client = _connected_client(mt5)

    info = client.get_position_close_info(42)
    assert info is not None
    assert info["exit_price"] == 4080.0
    assert info["profit_usd"] == 125.0
    assert info["exit_time_utc"] is not None


def test_get_position_close_info_returns_none_for_unknown_ticket():
    mt5 = MagicMock()
    mt5.history_deals_get.return_value = []
    client = _connected_client(mt5)
    assert client.get_position_close_info(999) is None
