"""CRUD tests for the Sprint 7 execution-tracking repository surface."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from src.journal.db import session_scope
from src.journal.models import (
    DailyStateRow,
    OrderRow,
    SetupRow,
    SpreadAnomalyRow,
)
from src.journal.repository import (
    disable_auto_trading_for_day,
    get_order_by_setup_uid,
    get_order_by_ticket,
    insert_order,
    insert_spread_anomaly,
    is_auto_trading_disabled,
    list_open_orders_with_status,
    update_order_status,
)


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _add_setup(session, uid: str = "XAUUSD_2026-05-01T15:35:00+00:00") -> str:
    session.add(
        SetupRow(
            setup_uid=uid,
            detected_at=_now(),
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
    return uid


# -----------------------------------------------------------------------------
# insert_order / get_order_*
# -----------------------------------------------------------------------------


def test_insert_order_round_trip(engine):
    with session_scope(engine) as s:
        uid = _add_setup(s)
        row = insert_order(
            s,
            setup_uid=uid,
            mt5_ticket=12345678,
            symbol="XAUUSD",
            direction="short",
            volume=0.05,
            entry_price=4360.0,
            stop_loss=4375.0,
            tp1=4285.0,
            tp_runner=4080.5,
            placed_at_utc=_now(),
            status="pending",
        )
        assert row.id is not None
        assert row.mt5_ticket == 12345678

    with session_scope(engine) as s:
        fetched = get_order_by_ticket(s, 12345678)
        assert fetched is not None
        assert fetched.symbol == "XAUUSD"
        assert fetched.status == "pending"


def test_insert_order_rejects_duplicate_ticket(engine):
    with session_scope(engine) as s:
        uid = _add_setup(s)
        insert_order(
            s,
            setup_uid=uid,
            mt5_ticket=42,
            symbol="XAUUSD",
            direction="short",
            volume=0.05,
            entry_price=4360.0,
            stop_loss=4375.0,
            tp1=4285.0,
            tp_runner=4080.5,
            placed_at_utc=_now(),
            status="pending",
        )

    with pytest.raises(ValueError, match="ticket"):
        with session_scope(engine) as s:
            insert_order(
                s,
                setup_uid="XAUUSD_2026-05-01T15:35:00+00:00",
                mt5_ticket=42,
                symbol="XAUUSD",
                direction="short",
                volume=0.05,
                entry_price=4360.0,
                stop_loss=4375.0,
                tp1=4285.0,
                tp_runner=4080.5,
                placed_at_utc=_now(),
                status="pending",
            )


def test_get_order_by_ticket_returns_none_when_missing(engine):
    with session_scope(engine) as s:
        assert get_order_by_ticket(s, 999999) is None


def test_get_order_by_setup_uid_round_trip(engine):
    with session_scope(engine) as s:
        uid = _add_setup(s)
        insert_order(
            s,
            setup_uid=uid,
            mt5_ticket=100,
            symbol="XAUUSD",
            direction="short",
            volume=0.05,
            entry_price=4360.0,
            stop_loss=4375.0,
            tp1=4285.0,
            tp_runner=4080.5,
            placed_at_utc=_now(),
            status="pending",
        )

    with session_scope(engine) as s:
        fetched = get_order_by_setup_uid(s, uid)
        assert fetched is not None
        assert fetched.mt5_ticket == 100


# -----------------------------------------------------------------------------
# update_order_status
# -----------------------------------------------------------------------------


def test_update_order_status_simple_transition(engine):
    with session_scope(engine) as s:
        uid = _add_setup(s)
        insert_order(
            s,
            setup_uid=uid,
            mt5_ticket=200,
            symbol="XAUUSD",
            direction="short",
            volume=0.05,
            entry_price=4360.0,
            stop_loss=4375.0,
            tp1=4285.0,
            tp_runner=4080.5,
            placed_at_utc=_now(),
            status="pending",
        )

    fill_time = _now()
    with session_scope(engine) as s:
        update_order_status(s, ticket=200, status="filled", filled_at_utc=fill_time)

    with session_scope(engine) as s:
        row = get_order_by_ticket(s, 200)
        assert row.status == "filled"
        assert row.filled_at_utc is not None


def test_update_order_status_records_outcome(engine):
    with session_scope(engine) as s:
        uid = _add_setup(s)
        insert_order(
            s,
            setup_uid=uid,
            mt5_ticket=300,
            symbol="XAUUSD",
            direction="short",
            volume=0.05,
            entry_price=4360.0,
            stop_loss=4375.0,
            tp1=4285.0,
            tp_runner=4080.5,
            placed_at_utc=_now(),
            status="filled",
        )

    with session_scope(engine) as s:
        update_order_status(
            s,
            ticket=300,
            status="tp_runner_hit",
            closed_at_utc=_now(),
            realized_r=2.5,
            notes="full runner",
        )

    with session_scope(engine) as s:
        row = get_order_by_ticket(s, 300)
        assert row.status == "tp_runner_hit"
        assert row.realized_r == 2.5
        assert row.notes == "full runner"


def test_update_order_status_raises_when_ticket_missing(engine):
    with pytest.raises(ValueError, match="no order"):
        with session_scope(engine) as s:
            update_order_status(s, ticket=999, status="filled")


def test_update_order_status_rejects_unknown_field(engine):
    with session_scope(engine) as s:
        uid = _add_setup(s)
        insert_order(
            s,
            setup_uid=uid,
            mt5_ticket=400,
            symbol="XAUUSD",
            direction="short",
            volume=0.05,
            entry_price=4360.0,
            stop_loss=4375.0,
            tp1=4285.0,
            tp_runner=4080.5,
            placed_at_utc=_now(),
            status="pending",
        )

    with pytest.raises(AttributeError, match="bogus_field"):
        with session_scope(engine) as s:
            update_order_status(s, ticket=400, status="filled", bogus_field=42)


# -----------------------------------------------------------------------------
# list_open_orders_with_status
# -----------------------------------------------------------------------------


def test_list_open_orders_filters_by_status(engine):
    with session_scope(engine) as s:
        uid1 = _add_setup(s, "XAUUSD_2026-05-01T10:00:00+00:00")
        uid2 = _add_setup(s, "XAUUSD_2026-05-01T11:00:00+00:00")
        uid3 = _add_setup(s, "XAUUSD_2026-05-01T12:00:00+00:00")
        for uid, ticket, status in [
            (uid1, 1, "pending"),
            (uid2, 2, "filled"),
            (uid3, 3, "sl_hit"),
        ]:
            insert_order(
                s,
                setup_uid=uid,
                mt5_ticket=ticket,
                symbol="XAUUSD",
                direction="short",
                volume=0.05,
                entry_price=4360.0,
                stop_loss=4375.0,
                tp1=4285.0,
                tp_runner=4080.5,
                placed_at_utc=_now(),
                status=status,
            )

    with session_scope(engine) as s:
        pending = list_open_orders_with_status(s, statuses=["pending"])
        assert {o.mt5_ticket for o in pending} == {1}

        active = list_open_orders_with_status(s, statuses=["pending", "filled"])
        assert {o.mt5_ticket for o in active} == {1, 2}


# -----------------------------------------------------------------------------
# insert_spread_anomaly
# -----------------------------------------------------------------------------


def test_insert_spread_anomaly_round_trip(engine):
    with session_scope(engine) as s:
        uid = _add_setup(s)
        row = insert_spread_anomaly(
            s,
            detected_at_utc=_now(),
            symbol="XAUUSD",
            spread=2.5,
            typical_spread=0.5,
            setup_uid=uid,
            action_taken="executed_anyway",
        )
        assert row.id is not None
        assert row.spread == 2.5

    with session_scope(engine) as s:
        all_rows = list(s.execute(select(SpreadAnomalyRow)).scalars().all())
        assert len(all_rows) == 1
        assert all_rows[0].symbol == "XAUUSD"


def test_insert_spread_anomaly_without_setup_uid(engine):
    with session_scope(engine) as s:
        insert_spread_anomaly(
            s,
            detected_at_utc=_now(),
            symbol="NDX100",
            spread=20.0,
            typical_spread=2.0,
            setup_uid=None,
            action_taken="logged_no_setup",
        )
    with session_scope(engine) as s:
        row = s.execute(select(SpreadAnomalyRow)).scalar_one()
        assert row.setup_uid is None


# -----------------------------------------------------------------------------
# disable_auto_trading_for_day / is_auto_trading_disabled
# -----------------------------------------------------------------------------


def test_disable_auto_trading_for_day_creates_or_updates_row(engine):
    """Disabling on a day with no daily_state row creates it."""
    with session_scope(engine) as s:
        disable_auto_trading_for_day(
            s, day=date(2026, 5, 1), reason="daily_loss_circuit_breaker"
        )

    with session_scope(engine) as s:
        row = s.get(DailyStateRow, date(2026, 5, 1))
        assert row is not None
        assert row.auto_trading_disabled is True
        assert row.disabled_reason == "daily_loss_circuit_breaker"


def test_disable_auto_trading_for_day_preserves_other_columns(engine):
    """Disabling must not stomp existing bias/loss fields."""
    with session_scope(engine) as s:
        s.add(
            DailyStateRow(
                date=date(2026, 5, 2),
                bias_xauusd_ny="bearish",
                trades_taken_count=1,
                daily_loss_usd=120.0,
                updated_at=_now(),
            )
        )

    with session_scope(engine) as s:
        disable_auto_trading_for_day(
            s, day=date(2026, 5, 2), reason="kill_switch"
        )

    with session_scope(engine) as s:
        row = s.get(DailyStateRow, date(2026, 5, 2))
        assert row.auto_trading_disabled is True
        assert row.disabled_reason == "kill_switch"
        assert row.bias_xauusd_ny == "bearish"
        assert row.trades_taken_count == 1
        assert row.daily_loss_usd == 120.0


def test_is_auto_trading_disabled_reads_back_state(engine):
    """Returns False when no row exists; True after disable."""
    with session_scope(engine) as s:
        assert is_auto_trading_disabled(s, day=date(2026, 5, 3)) is False

    with session_scope(engine) as s:
        disable_auto_trading_for_day(s, day=date(2026, 5, 3), reason="manual")

    with session_scope(engine) as s:
        assert is_auto_trading_disabled(s, day=date(2026, 5, 3)) is True
