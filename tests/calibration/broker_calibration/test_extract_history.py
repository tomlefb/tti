"""Unit tests for the broker_calibration history-parsing helpers.

These cover the pure functions that turn MT5 deal/order namedtuples
into reconstituted ``Trade`` records and per-symbol summaries. The
live MT5 connection (``main()``) is exercised manually on the Windows
host — these tests do not require a terminal.
"""

from __future__ import annotations

import pytest

from calibration.broker_calibration.extract_history import (
    DEAL_ENTRY_IN,
    DEAL_ENTRY_OUT,
    DEAL_TYPE_BUY,
    DEAL_TYPE_SELL,
    aggregate_by_symbol,
    deals_to_dicts,
    orders_to_dicts,
    reconstitute_trades,
    symbol_info_to_spec,
)


# ----------------------------------------------------------------------
# Fixtures: namedtuple-style fakes (MT5 Python returns these)
# ----------------------------------------------------------------------


class _Obj:
    """Lightweight stand-in for an MT5 namedtuple."""

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def _deal(
    *,
    ticket: int,
    position_id: int,
    time: int,
    symbol: str,
    type: int,
    entry: int,
    volume: float,
    price: float,
    commission: float = 0.0,
    swap: float = 0.0,
    profit: float = 0.0,
    order: int = 0,
):
    return _Obj(
        ticket=ticket,
        position_id=position_id,
        order=order,
        time=time,
        time_msc=time * 1000,
        symbol=symbol,
        type=type,
        entry=entry,
        volume=volume,
        price=price,
        commission=commission,
        swap=swap,
        profit=profit,
        magic=0,
        comment="",
        reason=0,
    )


def _order(
    *,
    ticket: int,
    position_id: int,
    symbol: str,
    type: int,
    price_open: float,
    sl: float,
    tp: float,
    time_setup: int,
    time_done: int,
    volume_initial: float = 1.0,
):
    return _Obj(
        ticket=ticket,
        position_id=position_id,
        time_setup=time_setup,
        time_done=time_done,
        symbol=symbol,
        type=type,
        state=4,  # ORDER_STATE_FILLED
        volume_initial=volume_initial,
        volume_current=0.0,
        price_open=price_open,
        price_current=price_open,
        sl=sl,
        tp=tp,
        magic=0,
        comment="",
        reason=0,
    )


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


class TestDealsToDicts:
    def test_strips_balance_operations(self):
        balance_op = _Obj(
            ticket=1, position_id=0, order=0, time=0, time_msc=0,
            symbol="", type=2, entry=0, volume=0.0, price=0.0,
            commission=0.0, swap=0.0, profit=10000.0, magic=0,
            comment="initial deposit", reason=0,
        )
        deals = deals_to_dicts([balance_op])
        assert deals == []

    def test_keeps_trading_deals(self):
        d = _deal(
            ticket=10, position_id=100, time=1700000000,
            symbol="XAUUSD", type=DEAL_TYPE_BUY, entry=DEAL_ENTRY_IN,
            volume=0.1, price=2050.50, commission=-0.7,
        )
        deals = deals_to_dicts([d])
        assert len(deals) == 1
        assert deals[0]["symbol"] == "XAUUSD"
        assert deals[0]["price"] == pytest.approx(2050.50)
        assert deals[0]["commission"] == pytest.approx(-0.7)


class TestReconstituteTrades:
    def test_simple_open_close(self):
        deals = deals_to_dicts(
            [
                _deal(
                    ticket=1, position_id=10, time=1700000000,
                    symbol="XAUUSD", type=DEAL_TYPE_BUY, entry=DEAL_ENTRY_IN,
                    volume=0.10, price=2050.0, commission=-0.7,
                ),
                _deal(
                    ticket=2, position_id=10, time=1700003600,
                    symbol="XAUUSD", type=DEAL_TYPE_SELL, entry=DEAL_ENTRY_OUT,
                    volume=0.10, price=2055.0, commission=-0.7, profit=50.0,
                ),
            ]
        )
        orders = orders_to_dicts(
            [
                _order(
                    ticket=1, position_id=10, symbol="XAUUSD", type=2,
                    price_open=2049.5, sl=2045.0, tp=2060.0,
                    time_setup=1699999000, time_done=1700000000,
                ),
            ]
        )

        trades = reconstitute_trades(deals, orders)
        assert len(trades) == 1
        t = trades[0]
        assert t.symbol == "XAUUSD"
        assert t.direction == "long"
        assert t.entry_price == pytest.approx(2050.0)
        assert t.exit_price_avg == pytest.approx(2055.0)
        assert t.is_closed is True
        assert t.duration_seconds == 3600
        assert t.sl_requested == pytest.approx(2045.0)
        assert t.tp_requested == pytest.approx(2060.0)
        assert t.requested_entry_price == pytest.approx(2049.5)
        # commission is summed across deals; profit is profit + commission + swap
        assert t.commission_total == pytest.approx(-1.4)
        assert t.profit_net == pytest.approx(50.0 - 1.4)
        assert t.n_exit_deals == 1

    def test_partial_close_volume_weighted_exit(self):
        """Exit price across two partial closes is volume-weighted."""
        deals = deals_to_dicts(
            [
                _deal(
                    ticket=1, position_id=20, time=1700000000,
                    symbol="NDX100", type=DEAL_TYPE_SELL, entry=DEAL_ENTRY_IN,
                    volume=1.0, price=18000.0, commission=-3.5,
                ),
                _deal(
                    ticket=2, position_id=20, time=1700001000,
                    symbol="NDX100", type=DEAL_TYPE_BUY, entry=DEAL_ENTRY_OUT,
                    volume=0.5, price=17990.0, commission=-1.75, profit=5.0,
                ),
                _deal(
                    ticket=3, position_id=20, time=1700002000,
                    symbol="NDX100", type=DEAL_TYPE_BUY, entry=DEAL_ENTRY_OUT,
                    volume=0.5, price=17980.0, commission=-1.75, profit=10.0,
                ),
            ]
        )
        trades = reconstitute_trades(deals, [])
        assert len(trades) == 1
        t = trades[0]
        assert t.direction == "short"
        # volume-weighted: (0.5*17990 + 0.5*17980) / 1.0 = 17985
        assert t.exit_price_avg == pytest.approx(17985.0)
        assert t.n_exit_deals == 2
        assert t.is_closed is True
        # close time = max of exit times
        assert t.close_time_unix == 1700002000

    def test_sl_hit_vs_tp_hit_recorded_via_exit_price(self):
        """Trade's exit price reflects whichever of SL/TP was hit."""
        # Long, SL @ 2045, TP @ 2060. Exit at 2045 → SL hit.
        deals = deals_to_dicts(
            [
                _deal(
                    ticket=1, position_id=30, time=1700000000,
                    symbol="XAUUSD", type=DEAL_TYPE_BUY, entry=DEAL_ENTRY_IN,
                    volume=0.1, price=2050.0,
                ),
                _deal(
                    ticket=2, position_id=30, time=1700003600,
                    symbol="XAUUSD", type=DEAL_TYPE_SELL, entry=DEAL_ENTRY_OUT,
                    volume=0.1, price=2045.0, profit=-50.0,
                ),
            ]
        )
        orders = orders_to_dicts(
            [
                _order(
                    ticket=1, position_id=30, symbol="XAUUSD", type=2,
                    price_open=2050.0, sl=2045.0, tp=2060.0,
                    time_setup=1699999000, time_done=1700000000,
                ),
            ]
        )
        trades = reconstitute_trades(deals, orders)
        t = trades[0]
        assert t.exit_price_avg == pytest.approx(2045.0)
        assert t.profit_net < 0
        # The exit price matches the requested SL → operator can classify
        # this as SL-hit downstream by comparing.
        assert abs(t.exit_price_avg - t.sl_requested) < 0.01

    def test_open_position_no_exit(self):
        deals = deals_to_dicts(
            [
                _deal(
                    ticket=1, position_id=40, time=1700000000,
                    symbol="XAUUSD", type=DEAL_TYPE_BUY, entry=DEAL_ENTRY_IN,
                    volume=0.1, price=2050.0,
                ),
            ]
        )
        trades = reconstitute_trades(deals, [])
        assert len(trades) == 1
        assert trades[0].is_closed is False
        assert trades[0].exit_price_avg is None
        assert trades[0].duration_seconds is None
        assert trades[0].n_exit_deals == 0

    def test_commission_only_no_swap(self):
        """A short-duration trade has commission but no swap."""
        deals = deals_to_dicts(
            [
                _deal(
                    ticket=1, position_id=50, time=1700000000,
                    symbol="XAUUSD", type=DEAL_TYPE_BUY, entry=DEAL_ENTRY_IN,
                    volume=0.1, price=2050.0, commission=-0.7,
                ),
                _deal(
                    ticket=2, position_id=50, time=1700001000,
                    symbol="XAUUSD", type=DEAL_TYPE_SELL, entry=DEAL_ENTRY_OUT,
                    volume=0.1, price=2052.0, commission=-0.7, swap=0.0,
                    profit=20.0,
                ),
            ]
        )
        trades = reconstitute_trades(deals, [])
        t = trades[0]
        assert t.commission_total == pytest.approx(-1.4)
        assert t.swap_total == 0.0
        assert t.profit_net == pytest.approx(20.0 - 1.4)

    def test_commission_plus_swap(self):
        """Multi-day trade accrues swap on top of commission."""
        deals = deals_to_dicts(
            [
                _deal(
                    ticket=1, position_id=60, time=1700000000,
                    symbol="XAUUSD", type=DEAL_TYPE_BUY, entry=DEAL_ENTRY_IN,
                    volume=0.5, price=2050.0, commission=-3.5,
                ),
                _deal(
                    ticket=2, position_id=60, time=1700200000,
                    symbol="XAUUSD", type=DEAL_TYPE_SELL, entry=DEAL_ENTRY_OUT,
                    volume=0.5, price=2055.0, commission=-3.5, swap=-12.0,
                    profit=250.0,
                ),
            ]
        )
        trades = reconstitute_trades(deals, [])
        t = trades[0]
        assert t.commission_total == pytest.approx(-7.0)
        assert t.swap_total == pytest.approx(-12.0)
        assert t.profit_net == pytest.approx(250.0 - 7.0 - 12.0)


class TestAggregateBySymbol:
    def test_separates_symbols_and_computes_commission_per_lot(self):
        deals = deals_to_dicts(
            [
                _deal(
                    ticket=1, position_id=1, time=100, symbol="XAUUSD",
                    type=DEAL_TYPE_BUY, entry=DEAL_ENTRY_IN,
                    volume=0.10, price=2050.0, commission=-0.35,
                ),
                _deal(
                    ticket=2, position_id=1, time=200, symbol="XAUUSD",
                    type=DEAL_TYPE_SELL, entry=DEAL_ENTRY_OUT,
                    volume=0.10, price=2055.0, commission=-0.35, profit=50.0,
                ),
                _deal(
                    ticket=3, position_id=2, time=300, symbol="NDX100",
                    type=DEAL_TYPE_SELL, entry=DEAL_ENTRY_IN,
                    volume=1.0, price=18000.0, commission=0.0,
                ),
                _deal(
                    ticket=4, position_id=2, time=400, symbol="NDX100",
                    type=DEAL_TYPE_BUY, entry=DEAL_ENTRY_OUT,
                    volume=1.0, price=17990.0, commission=0.0, profit=10.0,
                ),
            ]
        )
        trades = reconstitute_trades(deals, [])
        agg = aggregate_by_symbol(trades)

        assert set(agg.keys()) == {"XAUUSD", "NDX100"}
        # XAUUSD: 0.10-lot round-trip @ $0.35/side → $7/lot round-turn (FundedNext Stellar Lite spec)
        assert agg["XAUUSD"]["commission_per_lot_usd"] == pytest.approx(7.0)
        # NDX100: zero commission per FundedNext doc on Stellar Lite
        assert agg["NDX100"]["commission_per_lot_usd"] == pytest.approx(0.0)
        assert agg["XAUUSD"]["n_closed"] == 1
        assert agg["NDX100"]["directions"]["short"] == 1

    def test_entry_slippage_signed_by_direction(self):
        # Long limit @ 2050 filled at 2050.5 → adverse +0.5
        # Short limit @ 18000 filled at 17999.5 → adverse +0.5
        deals = deals_to_dicts(
            [
                _deal(
                    ticket=1, position_id=1, time=100, symbol="XAUUSD",
                    type=DEAL_TYPE_BUY, entry=DEAL_ENTRY_IN,
                    volume=0.1, price=2050.5,
                ),
                _deal(
                    ticket=2, position_id=2, time=200, symbol="NDX100",
                    type=DEAL_TYPE_SELL, entry=DEAL_ENTRY_IN,
                    volume=1.0, price=17999.5,
                ),
            ]
        )
        orders = orders_to_dicts(
            [
                _order(
                    ticket=1, position_id=1, symbol="XAUUSD", type=2,
                    price_open=2050.0, sl=0.0, tp=0.0,
                    time_setup=50, time_done=100,
                ),
                _order(
                    ticket=2, position_id=2, symbol="NDX100", type=3,
                    price_open=18000.0, sl=0.0, tp=0.0,
                    time_setup=150, time_done=200,
                ),
            ]
        )
        trades = reconstitute_trades(deals, orders)
        agg = aggregate_by_symbol(trades)
        # Each symbol has one observation, mean = the single value
        assert agg["XAUUSD"]["entry_slippage_mean"] == pytest.approx(0.5)
        assert agg["NDX100"]["entry_slippage_mean"] == pytest.approx(0.5)


class TestSymbolInfoToSpec:
    def test_extracts_critical_fields(self):
        info = _Obj(
            name="XAUUSD",
            trade_contract_size=100.0,
            point=0.01,
            digits=2,
            trade_tick_size=0.01,
            trade_tick_value=1.0,
            volume_min=0.01,
            volume_step=0.01,
            volume_max=200.0,
            trade_mode=4,
            swap_long=-3.5,
            swap_short=1.0,
            swap_mode=1,
            currency_base="XAU",
            currency_profit="USD",
            currency_margin="USD",
        )
        spec = symbol_info_to_spec(info, current_spread_points=15)
        assert spec["name"] == "XAUUSD"
        assert spec["trade_contract_size"] == 100.0
        assert spec["volume_min"] == 0.01
        assert spec["spread_current_points"] == 15
        assert spec["spread_current_native"] == pytest.approx(0.15)
        assert spec["currency_profit"] == "USD"
