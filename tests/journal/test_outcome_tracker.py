"""Outcome tracker — matching strategy + exit classification.

The tracker pairs MT5 trade dicts with journaled ``Taken`` setups by
symbol + direction + ±window minutes around ``timestamp_utc``, then
labels the exit by comparing ``exit_price`` to the setup's
``tp1_price`` / ``tp_runner_price`` / ``stop_loss``. No live MT5.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.journal.db import session_scope
from src.journal.outcome_tracker import Mt5Trade, reconcile_outcomes
from src.journal.repository import (
    get_outcome,
    insert_decision,
    insert_setup,
)


def _utc(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


class FakeMt5Client:
    """Stand-in MT5 wrapper returning a static trade list."""

    def __init__(self, trades: list[Mt5Trade]) -> None:
        self._trades = trades
        self.last_since: datetime | None = None

    def get_recent_trades(self, since: datetime) -> list[Mt5Trade]:
        self.last_since = since
        return list(self._trades)


def _seed_taken_setup(engine, make_setup, **overrides):
    setup = make_setup(**overrides)
    with session_scope(engine) as s:
        uid = insert_setup(s, setup, was_notified=True)
        insert_decision(s, uid, "taken", setup.timestamp_utc)
    return setup, uid


def test_no_pending_returns_zero(engine):
    client = FakeMt5Client([])
    with session_scope(engine) as s:
        assert reconcile_outcomes(s, client, since=_utc(2026, 1, 1)) == 0


def test_exact_match_classified_tp_runner_hit(engine, make_setup):
    setup, uid = _seed_taken_setup(engine, make_setup)
    trade = Mt5Trade(
        ticket=42,
        symbol=setup.symbol,
        direction=setup.direction,
        entry_time_utc=setup.timestamp_utc,
        entry_price=4360.0,
        exit_time_utc=_utc(2026, 1, 2, 17, 30),
        exit_price=setup.tp_runner_price,  # exactly TP_runner
        profit_usd=280.0,
    )
    client = FakeMt5Client([trade])

    with session_scope(engine) as s:
        upserted = reconcile_outcomes(s, client, since=_utc(2026, 1, 1))
        assert upserted == 1

    with session_scope(engine) as s:
        out = get_outcome(s, uid)
        assert out is not None
        assert out.exit_reason == "tp_runner_hit"
        assert out.mt5_ticket == 42
        # short setup hitting TP_runner → ~+18.6R (risk 15, reward 279.5).
        assert out.realized_r == pytest.approx(18.633, abs=0.01)


def test_tp1_hit_classification(engine, make_setup):
    setup, uid = _seed_taken_setup(engine, make_setup)
    trade = Mt5Trade(
        ticket=43,
        symbol=setup.symbol,
        direction=setup.direction,
        entry_time_utc=setup.timestamp_utc,
        entry_price=4360.0,
        exit_time_utc=_utc(2026, 1, 2, 17, 0),
        exit_price=setup.tp1_price,
        profit_usd=75.0,
    )
    client = FakeMt5Client([trade])

    with session_scope(engine) as s:
        reconcile_outcomes(s, client, since=_utc(2026, 1, 1))

    with session_scope(engine) as s:
        out = get_outcome(s, uid)
        assert out is not None
        assert out.exit_reason == "tp1_hit"
        assert out.realized_r == pytest.approx(5.0, abs=0.01)


def test_sl_hit_classification(engine, make_setup):
    setup, uid = _seed_taken_setup(engine, make_setup)
    trade = Mt5Trade(
        ticket=44,
        symbol=setup.symbol,
        direction=setup.direction,
        entry_time_utc=setup.timestamp_utc,
        entry_price=4360.0,
        exit_time_utc=_utc(2026, 1, 2, 16, 50),
        exit_price=setup.stop_loss,
        profit_usd=-50.0,
    )
    client = FakeMt5Client([trade])

    with session_scope(engine) as s:
        reconcile_outcomes(s, client, since=_utc(2026, 1, 1))

    with session_scope(engine) as s:
        out = get_outcome(s, uid)
        assert out is not None
        assert out.exit_reason == "sl_hit"
        assert out.realized_r == pytest.approx(-1.0, abs=0.01)


def test_manual_close_when_exit_far_from_targets(engine, make_setup):
    setup, uid = _seed_taken_setup(engine, make_setup)
    # Halfway between entry and TP1 — well outside the 0.1% tolerance.
    halfway = (setup.entry_price + setup.tp1_price) / 2
    trade = Mt5Trade(
        ticket=45,
        symbol=setup.symbol,
        direction=setup.direction,
        entry_time_utc=setup.timestamp_utc,
        entry_price=4360.0,
        exit_time_utc=_utc(2026, 1, 2, 16, 50),
        exit_price=halfway,
        profit_usd=37.0,
    )
    client = FakeMt5Client([trade])

    with session_scope(engine) as s:
        reconcile_outcomes(s, client, since=_utc(2026, 1, 1))

    with session_scope(engine) as s:
        out = get_outcome(s, uid)
        assert out is not None
        assert out.exit_reason == "manual_close"


def test_unmatched_when_symbol_differs(engine, make_setup):
    setup, uid = _seed_taken_setup(engine, make_setup)
    trade = Mt5Trade(
        ticket=46,
        symbol="EURUSD",  # different symbol → no match
        direction=setup.direction,
        entry_time_utc=setup.timestamp_utc,
        entry_price=1.07,
        exit_time_utc=_utc(2026, 1, 2, 17, 0),
        exit_price=1.075,
        profit_usd=50.0,
    )
    client = FakeMt5Client([trade])

    with session_scope(engine) as s:
        reconcile_outcomes(s, client, since=_utc(2026, 1, 1))

    with session_scope(engine) as s:
        out = get_outcome(s, uid)
        assert out is not None
        assert out.exit_reason == "unmatched"
        assert out.mt5_ticket is None


def test_unmatched_when_direction_differs(engine, make_setup):
    setup, uid = _seed_taken_setup(engine, make_setup)
    trade = Mt5Trade(
        ticket=47,
        symbol=setup.symbol,
        direction="long",  # setup is short → no match
        entry_time_utc=setup.timestamp_utc,
        entry_price=4360.0,
        exit_time_utc=_utc(2026, 1, 2, 17, 0),
        exit_price=4400.0,
        profit_usd=400.0,
    )
    client = FakeMt5Client([trade])

    with session_scope(engine) as s:
        reconcile_outcomes(s, client, since=_utc(2026, 1, 1))

    with session_scope(engine) as s:
        out = get_outcome(s, uid)
        assert out is not None
        assert out.exit_reason == "unmatched"


def test_outside_window_is_unmatched(engine, make_setup):
    setup, uid = _seed_taken_setup(engine, make_setup)
    # Entry 2 hours after MSS confirm — well outside ±30 min default.
    trade = Mt5Trade(
        ticket=48,
        symbol=setup.symbol,
        direction=setup.direction,
        entry_time_utc=_utc(2026, 1, 2, 18, 35),
        entry_price=4360.0,
        exit_time_utc=_utc(2026, 1, 2, 19, 0),
        exit_price=setup.tp_runner_price,
        profit_usd=280.0,
    )
    client = FakeMt5Client([trade])

    with session_scope(engine) as s:
        reconcile_outcomes(s, client, since=_utc(2026, 1, 1))

    with session_scope(engine) as s:
        out = get_outcome(s, uid)
        assert out is not None
        assert out.exit_reason == "unmatched"


def test_multiple_matches_picks_closest_in_time(engine, make_setup):
    setup, uid = _seed_taken_setup(engine, make_setup)
    far = Mt5Trade(
        ticket=100,
        symbol=setup.symbol,
        direction=setup.direction,
        entry_time_utc=_utc(2026, 1, 2, 16, 5),  # 30 min before MSS
        entry_price=4360.0,
        exit_time_utc=_utc(2026, 1, 2, 17, 0),
        exit_price=setup.stop_loss,
        profit_usd=-50.0,
    )
    close = Mt5Trade(
        ticket=200,
        symbol=setup.symbol,
        direction=setup.direction,
        entry_time_utc=_utc(2026, 1, 2, 16, 36),  # 1 min after
        entry_price=4360.0,
        exit_time_utc=_utc(2026, 1, 2, 17, 0),
        exit_price=setup.tp_runner_price,
        profit_usd=280.0,
    )
    client = FakeMt5Client([far, close])

    with session_scope(engine) as s:
        reconcile_outcomes(s, client, since=_utc(2026, 1, 1))

    with session_scope(engine) as s:
        out = get_outcome(s, uid)
        assert out is not None
        assert out.mt5_ticket == 200
        assert out.exit_reason == "tp_runner_hit"


def test_open_trade_records_open_status(engine, make_setup):
    setup, uid = _seed_taken_setup(engine, make_setup)
    trade = Mt5Trade(
        ticket=300,
        symbol=setup.symbol,
        direction=setup.direction,
        entry_time_utc=setup.timestamp_utc,
        entry_price=4360.0,
        exit_time_utc=None,
        exit_price=None,
        profit_usd=None,
    )
    client = FakeMt5Client([trade])

    with session_scope(engine) as s:
        reconcile_outcomes(s, client, since=_utc(2026, 1, 1))

    with session_scope(engine) as s:
        out = get_outcome(s, uid)
        assert out is not None
        assert out.exit_reason == "open"
        assert out.mt5_ticket == 300
        assert out.exit_price is None


def test_settled_outcomes_are_not_re_reconciled(engine, make_setup):
    """Once an outcome is terminal (e.g. tp_runner_hit), it stays put."""
    setup, uid = _seed_taken_setup(engine, make_setup)
    first = Mt5Trade(
        ticket=400,
        symbol=setup.symbol,
        direction=setup.direction,
        entry_time_utc=setup.timestamp_utc,
        entry_price=4360.0,
        exit_time_utc=_utc(2026, 1, 2, 17, 0),
        exit_price=setup.tp_runner_price,
        profit_usd=280.0,
    )
    client = FakeMt5Client([first])

    with session_scope(engine) as s:
        first_pass = reconcile_outcomes(s, client, since=_utc(2026, 1, 1))
        assert first_pass == 1

    # Re-running with no new pending setups should be a no-op.
    with session_scope(engine) as s:
        second_pass = reconcile_outcomes(s, client, since=_utc(2026, 1, 1))
        assert second_pass == 0
