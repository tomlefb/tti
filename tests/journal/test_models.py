"""Schema sanity checks.

Ensure each table accepts a happy-path insert, that PRAGMA foreign_keys
is actually enforced (SQLite defaults to OFF — the connect hook in
``db.py`` flips it ON), and that cascade delete on ``setups`` removes
related ``decisions`` and ``outcomes`` rows.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.journal.db import session_scope
from src.journal.models import DailyStateRow, DecisionRow, OutcomeRow, SetupRow


def _now() -> datetime:
    return datetime.now(tz=UTC)


def test_create_all_creates_four_tables(engine):
    from sqlalchemy import inspect

    names = set(inspect(engine).get_table_names())
    assert {"setups", "decisions", "outcomes", "daily_state"} <= names


def test_insert_setup_row_round_trip(engine):
    with session_scope(engine) as s:
        s.add(
            SetupRow(
                setup_uid="XAUUSD_2026-01-02T16:35:00+00:00",
                detected_at=_now(),
                timestamp_utc=datetime(2026, 1, 2, 16, 35, tzinfo=UTC),
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

    with session_scope(engine) as s:
        row = s.execute(select(SetupRow)).scalar_one()
        assert row.symbol == "XAUUSD"
        assert row.was_notified is True
        assert row.confluences == '["FVG+OB"]'


def test_decision_requires_existing_setup_uid(engine):
    """FK constraint must fire when decisions.setup_uid does not match."""
    with pytest.raises(IntegrityError):
        with session_scope(engine) as s:
            s.add(
                DecisionRow(
                    setup_uid="ghost_2026-01-02T00:00:00+00:00",
                    decision="taken",
                    decided_at=_now(),
                )
            )


def test_outcome_requires_existing_setup_uid(engine):
    with pytest.raises(IntegrityError):
        with session_scope(engine) as s:
            s.add(
                OutcomeRow(
                    setup_uid="ghost_2026-01-02T00:00:00+00:00",
                    exit_reason="open",
                )
            )


def test_cascade_delete_setup_removes_decision_and_outcome(engine):
    uid = "EURUSD_2026-01-02T10:00:00+00:00"
    with session_scope(engine) as s:
        s.add(
            SetupRow(
                setup_uid=uid,
                detected_at=_now(),
                timestamp_utc=datetime(2026, 1, 2, 10, 0, tzinfo=UTC),
                symbol="EURUSD",
                killzone="london",
                direction="long",
                daily_bias="bullish",
                swept_level_type="pdl",
                swept_level_strength="major",
                swept_level_price=1.07,
                entry_price=1.0710,
                stop_loss=1.0690,
                tp1_price=1.0760,
                tp1_rr=2.5,
                tp_runner_price=1.0800,
                tp_runner_rr=4.5,
                target_level_type="pdh",
                poi_type="OrderBlock",
                quality="A",
                confluences="[]",
                was_notified=True,
            )
        )
        s.add(DecisionRow(setup_uid=uid, decision="taken", decided_at=_now()))
        s.add(OutcomeRow(setup_uid=uid, exit_reason="open"))

    with session_scope(engine) as s:
        setup = s.execute(select(SetupRow).where(SetupRow.setup_uid == uid)).scalar_one()
        s.delete(setup)

    with session_scope(engine) as s:
        assert s.execute(select(DecisionRow).where(DecisionRow.setup_uid == uid)).first() is None
        assert s.execute(select(OutcomeRow).where(OutcomeRow.setup_uid == uid)).first() is None


def test_daily_state_round_trip(engine):
    with session_scope(engine) as s:
        s.add(
            DailyStateRow(
                date=date(2026, 1, 2),
                bias_xauusd_london="bearish",
                trades_taken_count=1,
                consecutive_sl_count=0,
                daily_loss_usd=0.0,
                daily_stop_triggered=False,
                updated_at=_now(),
            )
        )
    with session_scope(engine) as s:
        row = s.get(DailyStateRow, date(2026, 1, 2))
        assert row is not None
        assert row.bias_xauusd_london == "bearish"
        assert row.trades_taken_count == 1
