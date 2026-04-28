"""Repository CRUD coverage — every public function in
``src.journal.repository`` exercised against an in-memory SQLite engine.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from src.journal.db import session_scope
from src.journal.repository import (
    get_daily_state,
    get_decision,
    get_outcome,
    get_outcomes_to_match,
    get_setup,
    insert_decision,
    insert_setup,
    list_setups,
    setup_uid_for,
    upsert_daily_state,
    upsert_outcome,
)


def _utc(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def test_setup_uid_for_matches_telegram_callback_format(make_setup):
    setup = make_setup()
    assert setup_uid_for(setup) == f"XAUUSD_{setup.timestamp_utc.isoformat()}"


def test_insert_setup_persists_all_fields(engine, make_setup):
    setup = make_setup()
    with session_scope(engine) as s:
        uid = insert_setup(s, setup, was_notified=True)

    with session_scope(engine) as s:
        row = get_setup(s, uid)
        assert row is not None
        assert row.symbol == "XAUUSD"
        assert row.quality == "A"
        assert row.was_notified is True
        assert row.rejection_reason is None
        assert json.loads(row.confluences) == ["structural_sweep", "FVG+OB", "high_rr_runner"]
        assert row.tp_runner_rr == pytest.approx(18.70)
        assert row.tp1_rr == pytest.approx(5.0)


def test_insert_setup_is_idempotent(engine, make_setup):
    setup = make_setup()
    with session_scope(engine) as s:
        uid1 = insert_setup(s, setup, was_notified=True)
        uid2 = insert_setup(s, setup, was_notified=True)
        assert uid1 == uid2
        assert len(list_setups(s)) == 1


def test_insert_setup_rejected_carries_reason(engine, make_setup):
    setup = make_setup()
    with session_scope(engine) as s:
        insert_setup(s, setup, was_notified=False, rejection_reason="killzone_gating")
        row = get_setup(s, setup_uid_for(setup))
        assert row is not None
        assert row.was_notified is False
        assert row.rejection_reason == "killzone_gating"


def test_insert_setup_rejects_reason_with_was_notified_true(engine, make_setup):
    setup = make_setup()
    with pytest.raises(ValueError):
        with session_scope(engine) as s:
            insert_setup(s, setup, was_notified=True, rejection_reason="bug")


def test_list_setups_filters(engine, make_setup):
    s1 = make_setup(timestamp_utc=_utc(2026, 1, 2, 10), symbol="XAUUSD", quality="A")
    s2 = make_setup(timestamp_utc=_utc(2026, 1, 3, 10), symbol="EURUSD", quality="B")
    s3 = make_setup(timestamp_utc=_utc(2026, 1, 4, 10), symbol="XAUUSD", quality="A+")

    with session_scope(engine) as s:
        for setup in (s1, s2, s3):
            insert_setup(s, setup, was_notified=True)

    with session_scope(engine) as s:
        all_rows = list_setups(s)
        assert len(all_rows) == 3
        # Sorted ascending by timestamp_utc.
        assert [r.symbol for r in all_rows] == ["XAUUSD", "EURUSD", "XAUUSD"]

        only_xau = list_setups(s, symbol="XAUUSD")
        assert {r.symbol for r in only_xau} == {"XAUUSD"}
        assert len(only_xau) == 2

        only_a_plus = list_setups(s, quality="A+")
        assert len(only_a_plus) == 1

        in_window = list_setups(s, since=_utc(2026, 1, 2, 12), until=_utc(2026, 1, 3, 12))
        assert [r.symbol for r in in_window] == ["EURUSD"]


def test_insert_decision_happy_path(engine, make_setup):
    setup = make_setup()
    with session_scope(engine) as s:
        uid = insert_setup(s, setup, was_notified=True)
        insert_decision(s, uid, "taken", _utc(2026, 1, 2, 16, 36))

    with session_scope(engine) as s:
        dec = get_decision(s, uid)
        assert dec is not None
        assert dec.decision == "taken"
        assert dec.note is None


def test_insert_decision_rejects_unknown_setup(engine):
    with pytest.raises(ValueError):
        with session_scope(engine) as s:
            insert_decision(s, "ghost", "taken", _utc(2026, 1, 1))


def test_insert_decision_rejects_invalid_value(engine, make_setup):
    setup = make_setup()
    with session_scope(engine) as s:
        uid = insert_setup(s, setup, was_notified=True)
        with pytest.raises(ValueError):
            insert_decision(s, uid, "ignored", _utc(2026, 1, 2))


def test_insert_decision_rejects_duplicate(engine, make_setup):
    setup = make_setup()
    with session_scope(engine) as s:
        uid = insert_setup(s, setup, was_notified=True)
        insert_decision(s, uid, "taken", _utc(2026, 1, 2, 16, 36))

    with pytest.raises(ValueError):
        with session_scope(engine) as s:
            insert_decision(s, uid, "skipped", _utc(2026, 1, 2, 16, 40))


def test_upsert_outcome_inserts_then_updates(engine, make_setup):
    setup = make_setup()
    with session_scope(engine) as s:
        uid = insert_setup(s, setup, was_notified=True)
        insert_decision(s, uid, "taken", _utc(2026, 1, 2, 16, 36))
        upsert_outcome(s, uid, exit_reason="open", mt5_ticket=1234)

    with session_scope(engine) as s:
        row = get_outcome(s, uid)
        assert row is not None
        assert row.exit_reason == "open"
        assert row.mt5_ticket == 1234

    with session_scope(engine) as s:
        upsert_outcome(s, uid, exit_reason="tp_runner_hit", exit_price=4080.5, realized_r=18.7)

    with session_scope(engine) as s:
        row = get_outcome(s, uid)
        assert row.exit_reason == "tp_runner_hit"
        assert row.exit_price == pytest.approx(4080.5)
        # mt5_ticket unchanged from initial insert.
        assert row.mt5_ticket == 1234


def test_upsert_outcome_unknown_field_raises(engine, make_setup):
    setup = make_setup()
    with session_scope(engine) as s:
        uid = insert_setup(s, setup, was_notified=True)
        with pytest.raises(AttributeError):
            upsert_outcome(s, uid, definitely_not_a_column=42)


def test_get_outcomes_to_match_returns_taken_without_outcome(engine, make_setup):
    s1 = make_setup(timestamp_utc=_utc(2026, 1, 2, 10))
    s2 = make_setup(timestamp_utc=_utc(2026, 1, 2, 11))
    s3 = make_setup(timestamp_utc=_utc(2026, 1, 2, 12))

    with session_scope(engine) as s:
        for setup in (s1, s2, s3):
            insert_setup(s, setup, was_notified=True)
        # s1 taken, no outcome yet → pending.
        insert_decision(s, setup_uid_for(s1), "taken", _utc(2026, 1, 2, 10, 5))
        # s2 taken, outcome closed → not pending.
        insert_decision(s, setup_uid_for(s2), "taken", _utc(2026, 1, 2, 11, 5))
        upsert_outcome(s, setup_uid_for(s2), exit_reason="tp1_hit")
        # s3 skipped → never pending.
        insert_decision(s, setup_uid_for(s3), "skipped", _utc(2026, 1, 2, 12, 5))

    with session_scope(engine) as s:
        pending = get_outcomes_to_match(s)
        assert [r.setup_uid for r in pending] == [setup_uid_for(s1)]


def test_get_outcomes_to_match_includes_open_outcomes(engine, make_setup):
    setup = make_setup()
    with session_scope(engine) as s:
        uid = insert_setup(s, setup, was_notified=True)
        insert_decision(s, uid, "taken", _utc(2026, 1, 2, 16, 36))
        upsert_outcome(s, uid, exit_reason="open")

    with session_scope(engine) as s:
        pending = get_outcomes_to_match(s)
        assert [r.setup_uid for r in pending] == [uid]


def test_upsert_daily_state_insert_then_update(engine):
    day = date(2026, 1, 2)
    with session_scope(engine) as s:
        upsert_daily_state(s, day, trades_taken_count=1, daily_loss_usd=-50.0)

    with session_scope(engine) as s:
        row = get_daily_state(s, day)
        assert row is not None
        assert row.trades_taken_count == 1
        assert row.daily_loss_usd == pytest.approx(-50.0)

    with session_scope(engine) as s:
        upsert_daily_state(s, day, trades_taken_count=2)

    with session_scope(engine) as s:
        row = get_daily_state(s, day)
        assert row.trades_taken_count == 2
        # daily_loss_usd preserved across upsert.
        assert row.daily_loss_usd == pytest.approx(-50.0)


def test_upsert_daily_state_unknown_field_raises(engine):
    with pytest.raises(AttributeError):
        with session_scope(engine) as s:
            upsert_daily_state(s, date(2026, 1, 2), nonexistent=True)
