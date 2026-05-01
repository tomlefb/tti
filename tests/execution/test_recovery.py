"""Unit tests for ``src.execution.recovery``.

Recovery runs once at scheduler startup and reconciles MT5 state vs
the journal:

- **Orphan position**: MT5 has a position with our magic that is NOT
  in the journal (or is in the journal with status ``cancelled``,
  ``sl_hit``, etc — i.e. should not be open). Close it at market for
  safety + emit a CRITICAL Telegram alert.

- **Lost pending order**: journal has a ``pending`` row but MT5 has
  no matching pending order. The order was probably cancelled
  manually or the broker rolled it. Mark journal as ``lost`` +
  inform Telegram.

- **Lost filled order**: journal has a ``filled`` row but MT5 has no
  matching open position. Try to reconcile from history; if found,
  classify as ``tp_runner_hit`` / ``sl_hit``. If history empty, mark
  as ``lost``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from src.execution.recovery import RecoveryReport, reconcile_orphan_positions
from src.journal.db import get_engine, init_db, session_scope
from src.journal.models import OrderRow, SetupRow
from src.journal.repository import (
    get_order_by_ticket,
    insert_order,
)


# -----------------------------------------------------------------------------
# Fixtures (shared shape with lifecycle tests)
# -----------------------------------------------------------------------------


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
    base = dict(MAGIC_NUMBER=7766)
    base.update(overrides)
    return SimpleNamespace(**base)


@dataclass
class _PositionSnapshot:
    ticket: int
    symbol: str
    direction: str
    volume: float
    entry_price: float
    sl: float
    tp: float
    magic: int = 7766
    time_open_utc: datetime = field(
        default_factory=lambda: datetime(2026, 5, 1, 15, 36, tzinfo=UTC)
    )
    profit: float = 0.0


@dataclass
class _PendingOrderSnapshot:
    ticket: int
    symbol: str
    direction: str
    volume: float
    price_open: float
    sl: float
    tp: float
    magic: int = 7766
    time_setup_utc: datetime = field(
        default_factory=lambda: datetime(2026, 5, 1, 15, 36, tzinfo=UTC)
    )


@dataclass
class _MockMt5:
    positions: list[_PositionSnapshot] = field(default_factory=list)
    pending: list[_PendingOrderSnapshot] = field(default_factory=list)
    closed_at_market: list[int] = field(default_factory=list)

    def get_open_positions(self, magic=None):
        if magic is None:
            return list(self.positions)
        return [p for p in self.positions if p.magic == magic]

    def get_pending_orders(self, magic=None):
        if magic is None:
            return list(self.pending)
        return [o for o in self.pending if o.magic == magic]

    def close_position_at_market(self, ticket: int) -> bool:
        self.closed_at_market.append(int(ticket))
        self.positions = [p for p in self.positions if p.ticket != int(ticket)]
        return True


def _add_setup_and_order(
    session,
    *,
    setup_uid: str = "XAUUSD_2026-05-01T15:35:00+00:00",
    ticket: int = 12345678,
    status: str = "filled",
):
    session.add(
        SetupRow(
            setup_uid=setup_uid,
            detected_at=datetime(2026, 5, 1, 15, 35, tzinfo=UTC),
            timestamp_utc=datetime(2026, 5, 1, 15, 35, tzinfo=UTC),
            symbol="XAUUSD",
            killzone="ny",
            direction="short",
            daily_bias="bearish",
            swept_level_type="asian_high",
            swept_level_strength="structural",
            swept_level_price=4380.0,
            entry_price=4360.0,
            stop_loss=4375.0,
            tp1_price=4285.0,
            tp1_rr=5.0,
            tp_runner_price=4080.5,
            tp_runner_rr=18.7,
            target_level_type="swing_h1_low",
            poi_type="FVG",
            quality="A",
            confluences='["FVG+OB"]',
            was_notified=True,
        )
    )
    session.flush()
    insert_order(
        session,
        setup_uid=setup_uid,
        mt5_ticket=ticket,
        symbol="XAUUSD",
        direction="short",
        volume=0.05,
        entry_price=4360.0,
        stop_loss=4375.0,
        tp1=4285.0,
        tp_runner=4080.5,
        placed_at_utc=datetime(2026, 5, 1, 15, 35, tzinfo=UTC),
        status=status,
    )


def _now() -> datetime:
    return datetime(2026, 5, 1, 18, 30, tzinfo=UTC)


# -----------------------------------------------------------------------------
# Orphan positions
# -----------------------------------------------------------------------------


def test_orphan_position_in_mt5_but_not_in_journal_is_closed_at_market(
    engine, session_factory
):
    """A position with our magic that the journal doesn't know about
    must be closed at market for safety + critical Telegram alert."""
    mt5 = _MockMt5(
        positions=[
            _PositionSnapshot(
                ticket=99,
                symbol="NDX100",
                direction="long",
                volume=1.0,
                entry_price=20000.0,
                sl=19990.0,
                tp=20100.0,
            )
        ]
    )

    notifier = SimpleNamespace(
        send_orphan_alert=lambda **kw: None,
    )

    report = reconcile_orphan_positions(
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=_settings(),
        now_utc=_now(),
        notifier=notifier,
    )

    assert isinstance(report, RecoveryReport)
    assert report.orphan_positions == 1
    assert 99 in mt5.closed_at_market


def test_position_in_both_mt5_and_journal_is_not_orphan(engine, session_factory):
    with session_scope(engine) as s:
        _add_setup_and_order(s, ticket=42, status="filled")

    mt5 = _MockMt5(
        positions=[
            _PositionSnapshot(
                ticket=42,
                symbol="XAUUSD",
                direction="short",
                volume=0.05,
                entry_price=4360.0,
                sl=4375.0,
                tp=4080.5,
            )
        ]
    )

    report = reconcile_orphan_positions(
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=_settings(),
        now_utc=_now(),
        notifier=None,
    )
    assert report.orphan_positions == 0
    assert mt5.closed_at_market == []


def test_position_for_already_closed_journal_status_treated_as_orphan(
    engine, session_factory
):
    """If the journal records the order as already ``sl_hit`` but MT5
    still has it open, this is a desync — close at market."""
    with session_scope(engine) as s:
        _add_setup_and_order(s, ticket=55, status="sl_hit")

    mt5 = _MockMt5(
        positions=[
            _PositionSnapshot(
                ticket=55,
                symbol="XAUUSD",
                direction="short",
                volume=0.05,
                entry_price=4360.0,
                sl=4375.0,
                tp=4080.5,
            )
        ]
    )

    report = reconcile_orphan_positions(
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=_settings(),
        now_utc=_now(),
        notifier=None,
    )
    assert report.orphan_positions == 1
    assert 55 in mt5.closed_at_market


# -----------------------------------------------------------------------------
# Lost pending orders
# -----------------------------------------------------------------------------


def test_lost_pending_order_marked_lost_in_journal(engine, session_factory):
    """Journal has 'pending' but MT5 has no matching pending → mark lost."""
    with session_scope(engine) as s:
        _add_setup_and_order(s, ticket=10, status="pending")

    mt5 = _MockMt5(positions=[], pending=[])  # MT5 doesn't know our ticket

    report = reconcile_orphan_positions(
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=_settings(),
        now_utc=_now(),
        notifier=None,
    )

    assert report.lost_orders == 1
    with session_scope(engine) as s:
        order = get_order_by_ticket(s, 10)
        assert order.status == "lost"


def test_pending_order_present_in_mt5_is_not_lost(engine, session_factory):
    with session_scope(engine) as s:
        _add_setup_and_order(s, ticket=20, status="pending")

    mt5 = _MockMt5(
        pending=[
            _PendingOrderSnapshot(
                ticket=20,
                symbol="XAUUSD",
                direction="short",
                volume=0.05,
                price_open=4360.0,
                sl=4375.0,
                tp=4080.5,
            )
        ]
    )

    report = reconcile_orphan_positions(
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=_settings(),
        now_utc=_now(),
        notifier=None,
    )
    assert report.lost_orders == 0
    with session_scope(engine) as s:
        order = get_order_by_ticket(s, 20)
        assert order.status == "pending"  # unchanged


# -----------------------------------------------------------------------------
# Lost filled orders
# -----------------------------------------------------------------------------


def test_lost_filled_order_marked_lost_when_no_position_and_no_history(
    engine, session_factory
):
    """Journal 'filled' but MT5 position gone — mark lost (history layer
    will reconcile retroactively if/when info appears)."""
    with session_scope(engine) as s:
        _add_setup_and_order(s, ticket=30, status="filled")

    mt5 = _MockMt5(positions=[])
    # No get_position_close_info → recovery treats as lost.

    report = reconcile_orphan_positions(
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=_settings(),
        now_utc=_now(),
        notifier=None,
    )

    assert report.lost_orders == 1
    with session_scope(engine) as s:
        order = get_order_by_ticket(s, 30)
        assert order.status == "lost"
