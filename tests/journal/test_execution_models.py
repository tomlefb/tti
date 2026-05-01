"""Schema sanity checks for Sprint 7 execution-tracking tables.

Three additions to the journal schema:

- ``OrderRow``           — every limit order placed by the system.
- ``SpreadAnomalyRow``   — every spread anomaly observed at place_order time.
- ``DailyStateRow``      — extended with ``auto_trading_disabled`` /
                           ``disabled_reason`` (existing columns untouched).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from src.journal.db import session_scope
from src.journal.models import (
    DailyStateRow,
    OrderRow,
    SetupRow,
    SpreadAnomalyRow,
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
# Schema creation
# -----------------------------------------------------------------------------


def test_create_all_includes_orders_and_spread_anomalies(engine):
    names = set(inspect(engine).get_table_names())
    assert {"orders", "spread_anomalies"} <= names


def test_orders_table_columns_match_spec(engine):
    cols = {c["name"] for c in inspect(engine).get_columns("orders")}
    expected = {
        "id",
        "setup_uid",
        "mt5_ticket",
        "symbol",
        "direction",
        "volume",
        "entry_price",
        "stop_loss",
        "tp1",
        "tp_runner",
        "placed_at_utc",
        "status",
        "filled_at_utc",
        "closed_at_utc",
        "realized_r",
        "notes",
    }
    assert expected <= cols


def test_spread_anomalies_table_columns_match_spec(engine):
    cols = {c["name"] for c in inspect(engine).get_columns("spread_anomalies")}
    expected = {
        "id",
        "detected_at_utc",
        "symbol",
        "spread",
        "typical_spread",
        "setup_uid",
        "action_taken",
    }
    assert expected <= cols


def test_daily_state_extended_with_auto_trading_disabled(engine):
    cols = {c["name"] for c in inspect(engine).get_columns("daily_state")}
    assert "auto_trading_disabled" in cols
    assert "disabled_reason" in cols
    # Existing columns untouched.
    assert {
        "trades_taken_count",
        "consecutive_sl_count",
        "daily_loss_usd",
        "daily_stop_triggered",
    } <= cols


# -----------------------------------------------------------------------------
# OrderRow round-trip
# -----------------------------------------------------------------------------


def test_order_round_trip(engine):
    with session_scope(engine) as s:
        uid = _add_setup(s)
        s.add(
            OrderRow(
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
        )

    with session_scope(engine) as s:
        row = s.execute(select(OrderRow).where(OrderRow.mt5_ticket == 12345678)).scalar_one()
        assert row.symbol == "XAUUSD"
        assert row.status == "pending"
        assert row.volume == 0.05
        assert row.filled_at_utc is None
        assert row.realized_r is None


def test_order_mt5_ticket_unique(engine):
    """mt5_ticket is UNIQUE — two orders cannot share the same broker ticket."""
    with session_scope(engine) as s:
        uid = _add_setup(s)
        s.add(
            OrderRow(
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
        )

    with pytest.raises(IntegrityError):
        with session_scope(engine) as s:
            uid2 = _add_setup(s, uid="NDX100_2026-05-01T15:40:00+00:00")
            s.add(
                OrderRow(
                    setup_uid=uid2,
                    mt5_ticket=42,  # duplicate
                    symbol="NDX100",
                    direction="long",
                    volume=0.1,
                    entry_price=20000.0,
                    stop_loss=19990.0,
                    tp1=20050.0,
                    tp_runner=20100.0,
                    placed_at_utc=_now(),
                    status="pending",
                )
            )


def test_order_setup_fk_enforced(engine):
    """orders.setup_uid → setups.setup_uid must be honored."""
    with pytest.raises(IntegrityError):
        with session_scope(engine) as s:
            s.add(
                OrderRow(
                    setup_uid="ghost_uid",
                    mt5_ticket=99,
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
            )


# -----------------------------------------------------------------------------
# SpreadAnomalyRow round-trip
# -----------------------------------------------------------------------------


def test_spread_anomaly_round_trip(engine):
    with session_scope(engine) as s:
        uid = _add_setup(s)
        s.add(
            SpreadAnomalyRow(
                detected_at_utc=_now(),
                symbol="XAUUSD",
                spread=2.5,
                typical_spread=0.5,
                setup_uid=uid,
                action_taken="executed_anyway",
            )
        )

    with session_scope(engine) as s:
        row = s.execute(select(SpreadAnomalyRow)).scalar_one()
        assert row.symbol == "XAUUSD"
        assert row.spread == 2.5
        assert row.action_taken == "executed_anyway"


def test_spread_anomaly_setup_uid_optional(engine):
    """A spread anomaly observed outside any setup context (e.g. periodic
    health check) is still loggable — setup_uid can be NULL."""
    with session_scope(engine) as s:
        s.add(
            SpreadAnomalyRow(
                detected_at_utc=_now(),
                symbol="NDX100",
                spread=20.0,
                typical_spread=2.0,
                setup_uid=None,
                action_taken="logged",
            )
        )
    with session_scope(engine) as s:
        row = s.execute(select(SpreadAnomalyRow)).scalar_one()
        assert row.setup_uid is None


# -----------------------------------------------------------------------------
# DailyStateRow extension
# -----------------------------------------------------------------------------


def test_daily_state_auto_trading_disabled_default_false(engine):
    """New row defaults auto_trading_disabled to False (not None)."""
    with session_scope(engine) as s:
        s.add(DailyStateRow(date=date(2026, 5, 1), updated_at=_now()))
    with session_scope(engine) as s:
        row = s.get(DailyStateRow, date(2026, 5, 1))
        assert row is not None
        assert row.auto_trading_disabled is False
        assert row.disabled_reason is None


def test_daily_state_disable_for_day_round_trip(engine):
    """disable_for_day() updates auto_trading_disabled + disabled_reason."""
    with session_scope(engine) as s:
        s.add(
            DailyStateRow(
                date=date(2026, 5, 2),
                auto_trading_disabled=True,
                disabled_reason="daily_loss_circuit_breaker",
                updated_at=_now(),
            )
        )
    with session_scope(engine) as s:
        row = s.get(DailyStateRow, date(2026, 5, 2))
        assert row.auto_trading_disabled is True
        assert row.disabled_reason == "daily_loss_circuit_breaker"
