"""Unit tests for ``src.execution.order_manager_rotation``.

Sister suite to ``test_order_manager.py``. Targets the rotation-strategy
order primitives: ATR-based sizing, single open / close, and the
``execute_rebalance_transitions`` orchestrator that drives a full
basket cycle.

The MT5 client is a small fake dataclass exposing exactly the methods
the rotation order manager calls — keeps the tests self-contained and
fast (no MT5 lib import needed). The journal runs against an in-memory
SQLite engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from src.execution.order_manager_rotation import (
    RebalanceClose,
    RebalanceOpen,
    RotationOrderResult,
    close_rotation_position,
    compute_rotation_volume,
    execute_rebalance_transitions,
    open_rotation_position,
)
from src.journal.db import get_engine, init_db, session_scope
from src.journal.models import RotationPositionRow
from src.journal.repository import (
    get_open_rotation_position,
    insert_rebalance_transition,
    insert_rotation_position,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    eng = get_engine(":memory:")
    init_db(eng)
    return eng


@pytest.fixture
def session_factory(engine):
    def factory():
        return session_scope(engine)

    return factory


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        ROTATION_MAGIC_NUMBER=7799,
        SPREAD_ANOMALY_MULTIPLIER=3.0,
        TYPICAL_SPREADS={
            "XAUUSD": 0.5,
            "NDX100": 2.0,
            "BTCUSD": 50.0,
            "EURUSD": 0.0001,
        },
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _symbol_info(
    *,
    contract_size: float = 100.0,
    point: float = 0.01,
    volume_min: float = 0.01,
    volume_step: float = 0.01,
    volume_max: float = 100.0,
    ask: float = 2400.5,
    bid: float = 2400.0,
):
    return SimpleNamespace(
        trade_contract_size=contract_size,
        point=point,
        volume_min=volume_min,
        volume_step=volume_step,
        volume_max=volume_max,
        ask=ask,
        bid=bid,
    )


@dataclass
class _FakeMT5Client:
    """Minimal MT5Client double — only the methods the rotation order
    manager calls, returning canned values. ``calls`` records every
    invocation in order so tests can assert sequencing.

    ``next_market_result`` is the template returned by
    ``place_market_order``; the deal ticket is auto-incremented on each
    call (starting from ``ticket_counter``) so multiple opens in a
    single rebalance don't collide on the journal's UNIQUE(mt5_ticket).
    """

    symbol_info: dict[str, Any] = field(default_factory=dict)
    next_market_result: Any = None
    next_close_ok: bool = True
    close_info: dict[int, dict[str, Any]] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    ticket_counter: int = 2001

    def get_symbol_info(self, symbol: str):
        self.calls.append(("get_symbol_info", {"symbol": symbol}))
        if symbol not in self.symbol_info:
            return _symbol_info()
        return self.symbol_info[symbol]

    def place_market_order(self, **kwargs):
        self.calls.append(("place_market_order", kwargs))
        if self.next_market_result is None:
            return None
        # Issue a fresh deal ticket per call to avoid UNIQUE collisions.
        deal = self.ticket_counter
        self.ticket_counter += 1
        base = self.next_market_result
        return SimpleNamespace(
            retcode=getattr(base, "retcode", 10009),
            order=getattr(base, "order", 0),
            deal=deal,
            comment=getattr(base, "comment", ""),
            request_id=getattr(base, "request_id", 1),
        )

    def close_position_at_market(self, ticket: int) -> bool:
        self.calls.append(("close_position_at_market", {"ticket": ticket}))
        return self.next_close_ok

    def get_position_close_info(self, ticket: int):
        self.calls.append(("get_position_close_info", {"ticket": ticket}))
        return self.close_info.get(int(ticket))


# ---------------------------------------------------------------------------
# compute_rotation_volume — risk-parity sizing
# ---------------------------------------------------------------------------


def test_compute_rotation_volume_basic_xauusd():
    """XAUUSD contract=100, ATR=12.5, risk=$24.25 -> 0.01 lots floor."""
    info = _symbol_info(contract_size=100.0, volume_min=0.01, volume_step=0.01)
    vol = compute_rotation_volume(
        risk_usd=24.25, atr_at_entry=12.5, symbol_info=info
    )
    # raw = 24.25 / (12.5 * 100) = 0.0194 -> floor to 0.01
    assert vol == pytest.approx(0.01)


def test_compute_rotation_volume_floors_to_step():
    """Snap-to-step: never overshoot the risk budget by rounding up."""
    info = _symbol_info(contract_size=100.0, volume_min=0.01, volume_step=0.01)
    vol = compute_rotation_volume(
        risk_usd=100.0, atr_at_entry=12.5, symbol_info=info
    )
    # raw = 100 / 1250 = 0.08 -> exactly 0.08
    assert vol == pytest.approx(0.08)


def test_compute_rotation_volume_clamps_to_max():
    info = _symbol_info(volume_max=10.0)
    vol = compute_rotation_volume(
        risk_usd=10_000_000.0, atr_at_entry=0.5, symbol_info=info
    )
    assert vol == pytest.approx(10.0)


def test_compute_rotation_volume_clamps_to_min():
    info = _symbol_info(volume_min=0.10, volume_step=0.10)
    vol = compute_rotation_volume(
        risk_usd=0.05, atr_at_entry=12.5, symbol_info=info
    )
    # raw -> 0.00004, floors to 0, but volume_min=0.10 takes over.
    assert vol == pytest.approx(0.10)


def test_compute_rotation_volume_rejects_zero_atr():
    with pytest.raises(ValueError, match="atr_at_entry"):
        compute_rotation_volume(
            risk_usd=24.25, atr_at_entry=0.0, symbol_info=_symbol_info(),
        )


def test_compute_rotation_volume_rejects_nan_atr():
    with pytest.raises(ValueError, match="atr_at_entry"):
        compute_rotation_volume(
            risk_usd=24.25, atr_at_entry=float("nan"),
            symbol_info=_symbol_info(),
        )


# ---------------------------------------------------------------------------
# open_rotation_position
# ---------------------------------------------------------------------------


def test_open_rotation_position_dry_run_does_not_touch_mt5_or_journal(
    session_factory, engine
):
    mt5 = _FakeMT5Client(
        symbol_info={"XAUUSD": _symbol_info(ask=2400.5, bid=2400.0)},
    )
    result = open_rotation_position(
        symbol="XAUUSD", direction="long", volume=0.05,
        atr_at_entry=12.5, risk_usd=24.25,
        mt5_client=mt5, journal_session_factory=session_factory,
        settings=_settings(), now_utc=datetime(2026, 5, 5, 21, 0, tzinfo=UTC),
        strategy="trend_rotation_d1", entry_rebalance_uid=None,
        dry_run=True,
    )
    assert result.success
    assert result.ticket is None
    assert result.price == pytest.approx(2400.5)
    # MT5 was queried for symbol_info but order_send NOT called.
    methods = [c[0] for c in mt5.calls]
    assert "get_symbol_info" in methods
    assert "place_market_order" not in methods
    # No journal row inserted.
    with session_scope(engine) as s:
        row = get_open_rotation_position(
            s, strategy="trend_rotation_d1", symbol="XAUUSD"
        )
        assert row is None


def test_open_rotation_position_success_persists_journal_row(
    session_factory, engine
):
    mt5 = _FakeMT5Client(
        symbol_info={"XAUUSD": _symbol_info(ask=2400.5, bid=2400.0)},
        next_market_result=SimpleNamespace(
            retcode=10009, order=0, deal=0, comment="Done", request_id=1
        ),
        ticket_counter=12345,
    )
    result = open_rotation_position(
        symbol="XAUUSD", direction="long", volume=0.05,
        atr_at_entry=12.5, risk_usd=24.25,
        mt5_client=mt5, journal_session_factory=session_factory,
        settings=_settings(), now_utc=datetime(2026, 5, 5, 21, 0, tzinfo=UTC),
        strategy="trend_rotation_d1", entry_rebalance_uid=None,
    )
    assert result.success
    assert result.ticket == 12345
    assert result.price == pytest.approx(2400.5)
    with session_scope(engine) as s:
        row = get_open_rotation_position(
            s, strategy="trend_rotation_d1", symbol="XAUUSD"
        )
        assert row is not None
        assert row.mt5_ticket == 12345
        assert row.entry_price == pytest.approx(2400.5)
        assert row.atr_at_entry == pytest.approx(12.5)
        assert row.risk_usd == pytest.approx(24.25)


def test_open_rotation_position_blocks_below_volume_min(session_factory):
    mt5 = _FakeMT5Client(
        symbol_info={"XAUUSD": _symbol_info(volume_min=0.01)},
    )
    result = open_rotation_position(
        symbol="XAUUSD", direction="long", volume=0.001,  # below min
        atr_at_entry=12.5, risk_usd=24.25,
        mt5_client=mt5, journal_session_factory=session_factory,
        settings=_settings(), now_utc=datetime(2026, 5, 5, 21, 0, tzinfo=UTC),
        strategy="trend_rotation_d1", entry_rebalance_uid=None,
    )
    assert not result.success
    assert "volume_below_minimum" in str(result.error_code)


def test_open_rotation_position_failed_retcode_does_not_persist(
    session_factory, engine
):
    mt5 = _FakeMT5Client(
        symbol_info={"XAUUSD": _symbol_info()},
        next_market_result=SimpleNamespace(
            retcode=10004, order=0, deal=0, comment="Trade timeout",
            request_id=1,
        ),
    )
    result = open_rotation_position(
        symbol="XAUUSD", direction="long", volume=0.05,
        atr_at_entry=12.5, risk_usd=24.25,
        mt5_client=mt5, journal_session_factory=session_factory,
        settings=_settings(), now_utc=datetime(2026, 5, 5, 21, 0, tzinfo=UTC),
        strategy="trend_rotation_d1", entry_rebalance_uid=None,
    )
    assert not result.success
    assert result.error_code == 10004
    with session_scope(engine) as s:
        row = get_open_rotation_position(
            s, strategy="trend_rotation_d1", symbol="XAUUSD"
        )
        assert row is None


def test_open_rotation_position_unknown_direction_returns_failure(session_factory):
    mt5 = _FakeMT5Client()
    result = open_rotation_position(
        symbol="XAUUSD", direction="flat", volume=0.05,
        atr_at_entry=12.5, risk_usd=24.25,
        mt5_client=mt5, journal_session_factory=session_factory,
        settings=_settings(), now_utc=datetime(2026, 5, 5, 21, 0, tzinfo=UTC),
        strategy="trend_rotation_d1", entry_rebalance_uid=None,
    )
    assert not result.success
    assert result.error_code == "invalid_direction"


# ---------------------------------------------------------------------------
# close_rotation_position
# ---------------------------------------------------------------------------


def test_close_rotation_position_dry_run_does_not_touch_mt5(session_factory):
    mt5 = _FakeMT5Client()
    result = close_rotation_position(
        symbol="XAUUSD", ticket=12345,
        entry_price=2400.5, atr_at_entry=12.5, risk_usd=24.25,
        mt5_client=mt5, journal_session_factory=session_factory,
        now_utc=datetime(2026, 5, 10, 21, 0, tzinfo=UTC),
        exit_rebalance_uid=None, dry_run=True,
    )
    assert result.success
    assert result.ticket == 12345
    assert mt5.calls == []  # no MT5 call


def test_close_rotation_position_uses_history_exit_price_when_available(
    session_factory, engine
):
    # Pre-seed an open rotation row that we'll close.
    with session_scope(engine) as s:
        insert_rotation_position(
            s, strategy="trend_rotation_d1", symbol="XAUUSD",
            mt5_ticket=12345, direction="long", volume=0.05,
            entry_price=2400.5, atr_at_entry=12.5, risk_usd=24.25,
            entry_timestamp_utc=datetime(2026, 5, 5, 21, 0, tzinfo=UTC),
            entry_rebalance_uid=None,
        )

    mt5 = _FakeMT5Client(
        next_close_ok=True,
        close_info={12345: {"exit_price": 2412.5, "profit_usd": 60.0}},
    )
    result = close_rotation_position(
        symbol="XAUUSD", ticket=12345,
        entry_price=2400.5, atr_at_entry=12.5, risk_usd=24.25,
        mt5_client=mt5, journal_session_factory=session_factory,
        now_utc=datetime(2026, 5, 10, 21, 0, tzinfo=UTC),
        exit_rebalance_uid=None,
    )
    assert result.success
    assert result.price == pytest.approx(2412.5)
    with session_scope(engine) as s:
        row = s.execute(
            select(RotationPositionRow).where(
                RotationPositionRow.mt5_ticket == 12345
            )
        ).scalar_one()
        assert row.status == "closed"
        # realized R = (2412.5 - 2400.5) / 12.5 = 0.96
        assert row.realized_r == pytest.approx(0.96)
        # broker P&L preferred over reconstructed when available.
        assert row.realized_pnl_usd == pytest.approx(60.0)


def test_close_rotation_position_falls_back_to_mid_when_history_missing(
    session_factory, engine
):
    with session_scope(engine) as s:
        insert_rotation_position(
            s, strategy="trend_rotation_d1", symbol="XAUUSD",
            mt5_ticket=12346, direction="long", volume=0.05,
            entry_price=2400.0, atr_at_entry=10.0, risk_usd=24.25,
            entry_timestamp_utc=datetime(2026, 5, 5, 21, 0, tzinfo=UTC),
            entry_rebalance_uid=None,
        )
    mt5 = _FakeMT5Client(
        symbol_info={"XAUUSD": _symbol_info(ask=2410.0, bid=2409.0)},
        next_close_ok=True,
        close_info={},  # no history info -> fallback to mid
    )
    result = close_rotation_position(
        symbol="XAUUSD", ticket=12346,
        entry_price=2400.0, atr_at_entry=10.0, risk_usd=24.25,
        mt5_client=mt5, journal_session_factory=session_factory,
        now_utc=datetime(2026, 5, 10, 21, 0, tzinfo=UTC),
        exit_rebalance_uid=None,
    )
    assert result.success
    # Mid = (2410+2409)/2 = 2409.5; R = (2409.5-2400)/10 = 0.95
    assert result.price == pytest.approx(2409.5)
    with session_scope(engine) as s:
        row = s.execute(
            select(RotationPositionRow).where(
                RotationPositionRow.mt5_ticket == 12346
            )
        ).scalar_one()
        assert row.realized_r == pytest.approx(0.95)


def test_close_rotation_position_broker_rejection_returns_failure(session_factory):
    mt5 = _FakeMT5Client(next_close_ok=False)
    result = close_rotation_position(
        symbol="XAUUSD", ticket=12347,
        entry_price=2400.0, atr_at_entry=10.0, risk_usd=24.25,
        mt5_client=mt5, journal_session_factory=session_factory,
        now_utc=datetime(2026, 5, 10, 21, 0, tzinfo=UTC),
        exit_rebalance_uid=None,
    )
    assert not result.success
    assert result.error_code == "close_failed"


# ---------------------------------------------------------------------------
# execute_rebalance_transitions — orchestrator
# ---------------------------------------------------------------------------


def test_execute_rebalance_transitions_closes_then_opens_in_order(
    session_factory, engine
):
    # Seed two open rotation rows; we'll close NDX100, open BTCUSD + GER30.
    with session_scope(engine) as s:
        insert_rebalance_transition(
            s, strategy="trend_rotation_d1",
            timestamp_utc=datetime(2026, 5, 1, 21, 0, tzinfo=UTC),
            basket_before=[], basket_after=["NDX100", "XAUUSD"],
            closed_assets=[], opened_assets=["NDX100", "XAUUSD"],
            capital_at_rebalance_usd=4850.0, risk_per_trade_pct=0.005,
        )
        insert_rotation_position(
            s, strategy="trend_rotation_d1", symbol="NDX100",
            mt5_ticket=1001, direction="long", volume=0.10,
            entry_price=20000.0, atr_at_entry=200.0, risk_usd=24.25,
            entry_timestamp_utc=datetime(2026, 5, 1, 21, 0, tzinfo=UTC),
            entry_rebalance_uid=None,
        )

    closes = [
        RebalanceClose(
            symbol="NDX100", ticket=1001,
            entry_price=20000.0, atr_at_entry=200.0, risk_usd=24.25,
        ),
    ]
    opens = [
        RebalanceOpen(
            symbol="BTCUSD", direction="long", volume=0.001,
            atr_at_entry=2000.0, risk_usd=24.25,
        ),
        RebalanceOpen(
            symbol="GER30", direction="long", volume=0.10,
            atr_at_entry=120.0, risk_usd=24.25,
        ),
    ]

    mt5 = _FakeMT5Client(
        symbol_info={
            "BTCUSD": _symbol_info(
                contract_size=1.0, ask=70000.0, bid=69990.0,
                volume_min=0.001, volume_step=0.001,
            ),
            "GER30": _symbol_info(
                contract_size=1.0, ask=18000.0, bid=17995.0,
                volume_min=0.10, volume_step=0.10,
            ),
        },
        next_market_result=SimpleNamespace(
            retcode=10009, order=0, deal=2002, comment="Done", request_id=1
        ),
        next_close_ok=True,
        close_info={1001: {"exit_price": 21000.0, "profit_usd": 100.0}},
    )

    # The rebalance_transition row must exist before the orchestrator
    # FK-binds opens / closes to it.
    with session_scope(engine) as s:
        rebal_uid = insert_rebalance_transition(
            s, strategy="trend_rotation_d1",
            timestamp_utc=datetime(2026, 5, 5, 21, 0, tzinfo=UTC),
            basket_before=["NDX100", "XAUUSD"],
            basket_after=["BTCUSD", "GER30", "XAUUSD"],
            closed_assets=["NDX100"], opened_assets=["BTCUSD", "GER30"],
            capital_at_rebalance_usd=4850.0, risk_per_trade_pct=0.005,
        )
    result = execute_rebalance_transitions(
        closes=closes, opens=opens,
        mt5_client=mt5, journal_session_factory=session_factory,
        settings=_settings(), now_utc=datetime(2026, 5, 5, 21, 0, tzinfo=UTC),
        strategy="trend_rotation_d1", rebalance_uid=rebal_uid,
    )

    assert result.n_closed_ok == 1
    assert result.n_closed_failed == 0
    assert result.n_opened_ok == 2
    assert result.n_opened_failed == 0

    # Closes must precede opens in the call sequence.
    methods = [c[0] for c in mt5.calls]
    first_open_idx = methods.index("place_market_order")
    last_close_idx = max(
        i for i, m in enumerate(methods) if m == "close_position_at_market"
    )
    assert last_close_idx < first_open_idx

    # Symbols processed alphabetically inside each phase: BTCUSD then GER30.
    open_calls = [c for c in mt5.calls if c[0] == "place_market_order"]
    open_symbols = [c[1]["symbol"] for c in open_calls]
    assert open_symbols == ["BTCUSD", "GER30"]


def test_execute_rebalance_transitions_continues_after_failed_close(
    session_factory, engine
):
    """A broker rejection on one close must not abort subsequent closes / opens."""
    with session_scope(engine) as s:
        insert_rotation_position(
            s, strategy="trend_rotation_d1", symbol="NDX100",
            mt5_ticket=1001, direction="long", volume=0.10,
            entry_price=20000.0, atr_at_entry=200.0, risk_usd=24.25,
            entry_timestamp_utc=datetime(2026, 5, 1, 21, 0, tzinfo=UTC),
            entry_rebalance_uid=None,
        )
        insert_rotation_position(
            s, strategy="trend_rotation_d1", symbol="XAUUSD",
            mt5_ticket=1002, direction="long", volume=0.05,
            entry_price=2400.0, atr_at_entry=10.0, risk_usd=24.25,
            entry_timestamp_utc=datetime(2026, 5, 1, 21, 0, tzinfo=UTC),
            entry_rebalance_uid=None,
        )

    closes = [
        RebalanceClose(
            symbol="NDX100", ticket=1001,
            entry_price=20000.0, atr_at_entry=200.0, risk_usd=24.25,
        ),
        RebalanceClose(
            symbol="XAUUSD", ticket=1002,
            entry_price=2400.0, atr_at_entry=10.0, risk_usd=24.25,
        ),
    ]

    # A scripted failure path is awkward with the FakeMT5Client's
    # single ``next_close_ok``; emulate by having every close succeed
    # *except* the first via a stateful subclass.
    @dataclass
    class _SequencingMt5(_FakeMT5Client):
        close_results: list[bool] = field(default_factory=list)

        def close_position_at_market(self, ticket: int) -> bool:
            self.calls.append(
                ("close_position_at_market", {"ticket": ticket})
            )
            return self.close_results.pop(0) if self.close_results else True

    mt5 = _SequencingMt5(
        symbol_info={"BTCUSD": _symbol_info(volume_min=0.001, volume_step=0.001)},
        close_info={
            1001: {"exit_price": 21000.0, "profit_usd": 100.0},
            1002: {"exit_price": 2410.0, "profit_usd": 50.0},
        },
        next_market_result=SimpleNamespace(
            retcode=10009, order=0, deal=2002, comment="Done", request_id=1
        ),
        close_results=[False, True],  # first close fails, second succeeds
    )

    with session_scope(engine) as s:
        rebal_uid = insert_rebalance_transition(
            s, strategy="trend_rotation_d1",
            timestamp_utc=datetime(2026, 5, 5, 21, 0, tzinfo=UTC),
            basket_before=["NDX100", "XAUUSD"], basket_after=["BTCUSD"],
            closed_assets=["NDX100", "XAUUSD"], opened_assets=["BTCUSD"],
            capital_at_rebalance_usd=4850.0, risk_per_trade_pct=0.005,
        )
    result = execute_rebalance_transitions(
        closes=closes,
        opens=[
            RebalanceOpen(
                symbol="BTCUSD", direction="long", volume=0.001,
                atr_at_entry=2000.0, risk_usd=24.25,
            ),
        ],
        mt5_client=mt5, journal_session_factory=session_factory,
        settings=_settings(), now_utc=datetime(2026, 5, 5, 21, 0, tzinfo=UTC),
        strategy="trend_rotation_d1",
        rebalance_uid=rebal_uid,
    )
    # First close failed, second succeeded; open still attempted.
    assert result.n_closed_ok == 1
    assert result.n_closed_failed == 1
    assert result.n_opened_ok == 1
    assert result.n_opened_failed == 0
