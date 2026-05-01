"""Unit tests for ``src.execution.position_lifecycle``.

The lifecycle owns the post-place-order trajectory:

- ``pending → filled``         when MT5 reports a position with our ticket.
- ``filled → tp1_hit``         when current price crosses TP1 (50% close +
                               SL to break-even).
- ``filled → tp_runner_hit``   when MT5 closes the remainder at TP_runner.
- ``filled → sl_hit``          when MT5 closes the remainder at SL.
- ``pending → cancelled``      at end of killzone if not filled.

Tests use a hand-rolled MT5 mock and an in-memory journal — no real
broker connection.

Convention: ``mt5_ticket`` stored in the journal == initial order ticket
from ``order_send.order``. In hedging-mode accounts (FundedNext default),
the resulting position carries the same identifier so the polling loop
can match journal rows to MT5 positions one-to-one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from src.execution.position_lifecycle import (
    LifecycleReport,
    check_open_positions,
    end_of_killzone_cleanup,
)
from src.journal.db import get_engine, init_db, session_scope
from src.journal.models import OrderRow, SetupRow
from src.journal.repository import (
    get_order_by_ticket,
    insert_order,
)


# -----------------------------------------------------------------------------
# Fixtures
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
    base = dict(
        MAGIC_NUMBER=7766,
        TP1_PARTIAL_FRACTION=0.5,
        INSTRUMENT_CONFIG={
            "XAUUSD": {"typical_spread": 0.5},
            "NDX100": {"typical_spread": 2.0},
        },
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@dataclass
class _PositionSnapshot:
    ticket: int
    symbol: str
    direction: str  # "long" / "short"
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
class _SymbolInfo:
    symbol: str = "XAUUSD"
    bid: float = 4360.0
    ask: float = 4360.5
    point: float = 0.01
    trade_contract_size: float = 100.0
    volume_min: float = 0.01
    volume_step: float = 0.01
    volume_max: float = 100.0


@dataclass
class _MockMt5:
    positions: list[_PositionSnapshot] = field(default_factory=list)
    pending: list[_PendingOrderSnapshot] = field(default_factory=list)
    symbol_info_by_symbol: dict[str, _SymbolInfo] = field(default_factory=dict)
    cancelled: list[int] = field(default_factory=list)
    sl_modifications: list[tuple[int, float]] = field(default_factory=list)
    partial_closes: list[tuple[int, float]] = field(default_factory=list)
    history: dict[int, dict[str, Any]] = field(default_factory=dict)

    def get_open_positions(self, magic: int | None = None) -> list[_PositionSnapshot]:
        if magic is None:
            return list(self.positions)
        return [p for p in self.positions if p.magic == magic]

    def get_pending_orders(self, magic: int | None = None) -> list[_PendingOrderSnapshot]:
        if magic is None:
            return list(self.pending)
        return [o for o in self.pending if o.magic == magic]

    def get_symbol_info(self, symbol: str) -> _SymbolInfo:
        return self.symbol_info_by_symbol.get(symbol, _SymbolInfo(symbol=symbol))

    def cancel_pending_order(self, ticket: int) -> bool:
        self.cancelled.append(int(ticket))
        # Drop from pending list to mimic MT5 state.
        self.pending = [o for o in self.pending if o.ticket != int(ticket)]
        return True

    def modify_position_sl(self, *, ticket: int, new_sl: float) -> bool:
        self.sl_modifications.append((int(ticket), float(new_sl)))
        for p in self.positions:
            if p.ticket == int(ticket):
                p.sl = float(new_sl)
        return True

    def close_partial_position(self, *, ticket: int, volume: float) -> bool:
        self.partial_closes.append((int(ticket), float(volume)))
        for p in self.positions:
            if p.ticket == int(ticket):
                p.volume = max(0.0, p.volume - float(volume))
        return True

    def get_position_close_info(self, ticket: int) -> dict[str, Any] | None:
        """Helper for tp_runner_hit / sl_hit reconciliation. Returns the
        last-known exit data for a closed position (mocked from history)."""
        return self.history.get(int(ticket))


def _add_setup_and_order(
    session,
    *,
    setup_uid: str = "XAUUSD_2026-05-01T15:35:00+00:00",
    direction: str = "short",
    entry: float = 4360.0,
    sl: float = 4375.0,
    tp1: float = 4285.0,
    tp_runner: float = 4080.5,
    volume: float = 0.05,
    ticket: int = 12345678,
    status: str = "pending",
    killzone: str = "ny",
) -> tuple[str, int]:
    session.add(
        SetupRow(
            setup_uid=setup_uid,
            detected_at=datetime(2026, 5, 1, 15, 35, tzinfo=UTC),
            timestamp_utc=datetime(2026, 5, 1, 15, 35, tzinfo=UTC),
            symbol="XAUUSD",
            killzone=killzone,
            direction=direction,
            daily_bias="bearish" if direction == "short" else "bullish",
            swept_level_type="asian_high",
            swept_level_strength="structural",
            swept_level_price=4380.0,
            entry_price=entry,
            stop_loss=sl,
            tp1_price=tp1,
            tp1_rr=5.0,
            tp_runner_price=tp_runner,
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
        direction=direction,
        volume=volume,
        entry_price=entry,
        stop_loss=sl,
        tp1=tp1,
        tp_runner=tp_runner,
        placed_at_utc=datetime(2026, 5, 1, 15, 35, tzinfo=UTC),
        status=status,
    )
    return setup_uid, ticket


def _now() -> datetime:
    return datetime(2026, 5, 1, 16, 0, tzinfo=UTC)


# -----------------------------------------------------------------------------
# pending → filled
# -----------------------------------------------------------------------------


def test_pending_order_marked_filled_when_position_appears(
    engine, session_factory
):
    with session_scope(engine) as s:
        _add_setup_and_order(s, ticket=42, status="pending")

    # MT5 reports a position with our ticket (the order filled).
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
        ],
        symbol_info_by_symbol={"XAUUSD": _SymbolInfo(bid=4365.0, ask=4365.5)},
    )

    report = check_open_positions(
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=_settings(),
        now_utc=_now(),
        notifier=None,
    )

    assert isinstance(report, LifecycleReport)
    assert report.filled == 1

    with session_scope(engine) as s:
        order = get_order_by_ticket(s, 42)
        assert order.status == "filled"
        assert order.filled_at_utc is not None


# -----------------------------------------------------------------------------
# filled → tp1_hit (50% close + SL to BE)
# -----------------------------------------------------------------------------


def test_short_position_tp1_hit_triggers_partial_close_and_be_move(
    engine, session_factory
):
    """Short setup: entry 4360, SL 4375, TP1 4285, TP_runner 4080.5.
    Current ask 4280 (past TP1 down-move) → trigger TP1 partial."""
    with session_scope(engine) as s:
        _add_setup_and_order(
            s, ticket=100, status="filled", direction="short", volume=0.05
        )

    mt5 = _MockMt5(
        positions=[
            _PositionSnapshot(
                ticket=100,
                symbol="XAUUSD",
                direction="short",
                volume=0.05,  # full volume — partial NOT yet executed
                entry_price=4360.0,
                sl=4375.0,
                tp=4080.5,
            )
        ],
        symbol_info_by_symbol={
            "XAUUSD": _SymbolInfo(bid=4279.5, ask=4280.0)
        },  # past TP1 4285 (short hit when ask <= TP1)
    )

    report = check_open_positions(
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=_settings(),
        now_utc=_now(),
        notifier=None,
    )

    assert report.tp1_hit == 1
    # Partial close called with 50% volume.
    assert (100, 0.025) in [(t, round(v, 5)) for t, v in mt5.partial_closes]
    # SL moved to BE (= entry).
    assert (100, 4360.0) in [(t, sl) for t, sl in mt5.sl_modifications]

    with session_scope(engine) as s:
        order = get_order_by_ticket(s, 100)
        assert order.status == "tp1_hit"
        assert (order.notes or "").lower().startswith("tp1")


def test_long_position_tp1_hit_triggers_partial_close_and_be_move(
    engine, session_factory
):
    """Long setup: entry 20000, SL 19990, TP1 20050, TP_runner 20100.
    Current bid 20055 (past TP1) → trigger TP1 partial."""
    with session_scope(engine) as s:
        _add_setup_and_order(
            s,
            ticket=200,
            status="filled",
            direction="long",
            entry=20000.0,
            sl=19990.0,
            tp1=20050.0,
            tp_runner=20100.0,
            volume=1.0,
        )

    mt5 = _MockMt5(
        positions=[
            _PositionSnapshot(
                ticket=200,
                symbol="XAUUSD",
                direction="long",
                volume=1.0,
                entry_price=20000.0,
                sl=19990.0,
                tp=20100.0,
            )
        ],
        symbol_info_by_symbol={"XAUUSD": _SymbolInfo(bid=20055.0, ask=20055.5)},
    )

    report = check_open_positions(
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=_settings(),
        now_utc=_now(),
        notifier=None,
    )

    assert report.tp1_hit == 1
    assert (200, 0.5) in [(t, round(v, 5)) for t, v in mt5.partial_closes]
    assert (200, 20000.0) in mt5.sl_modifications


def test_partial_already_executed_does_not_double_partial(engine, session_factory):
    """If position.volume < order.volume, partial close already done.
    Lifecycle must NOT trigger another partial."""
    with session_scope(engine) as s:
        _add_setup_and_order(s, ticket=300, status="tp1_hit", volume=0.05)

    mt5 = _MockMt5(
        positions=[
            _PositionSnapshot(
                ticket=300,
                symbol="XAUUSD",
                direction="short",
                volume=0.025,  # already half closed
                entry_price=4360.0,
                sl=4360.0,  # already at BE
                tp=4080.5,
            )
        ],
        symbol_info_by_symbol={"XAUUSD": _SymbolInfo(bid=4279.5, ask=4280.0)},
    )

    report = check_open_positions(
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=_settings(),
        now_utc=_now(),
        notifier=None,
    )

    assert report.tp1_hit == 0
    assert mt5.partial_closes == []


def test_position_below_tp1_does_not_partial_close(engine, session_factory):
    """Short with ask still above TP1 → no partial."""
    with session_scope(engine) as s:
        _add_setup_and_order(s, ticket=400, status="filled", volume=0.05)

    mt5 = _MockMt5(
        positions=[
            _PositionSnapshot(
                ticket=400,
                symbol="XAUUSD",
                direction="short",
                volume=0.05,
                entry_price=4360.0,
                sl=4375.0,
                tp=4080.5,
            )
        ],
        symbol_info_by_symbol={"XAUUSD": _SymbolInfo(bid=4350.0, ask=4350.5)},  # > TP1 4285
    )

    report = check_open_positions(
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=_settings(),
        now_utc=_now(),
        notifier=None,
    )

    assert report.tp1_hit == 0
    assert mt5.partial_closes == []
    assert mt5.sl_modifications == []


# -----------------------------------------------------------------------------
# filled → tp_runner_hit / sl_hit (position closed by MT5)
# -----------------------------------------------------------------------------


def test_filled_position_closed_at_tp_runner_marks_status(engine, session_factory):
    """When MT5 has no position with our ticket and history shows exit
    near tp_runner → mark tp_runner_hit. Realized R uses profit-based
    calc (handles blended TP1-partial + runner outcomes correctly).

    Test setup: 0.05 lots × $15 SL × 100 contract = $75 initial risk.
    Profit_usd = $125 (e.g. TP1 partial $50 + runner half $75).
    Blended R = 125 / 75 ≈ 1.67."""
    with session_scope(engine) as s:
        _add_setup_and_order(s, ticket=500, status="filled", volume=0.05)

    mt5 = _MockMt5(
        positions=[],  # gone
        symbol_info_by_symbol={"XAUUSD": _SymbolInfo(trade_contract_size=100.0)},
    )
    mt5.history[500] = dict(
        exit_price=4080.0,
        exit_time_utc=_now(),
        profit_usd=125.0,
    )

    report = check_open_positions(
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=_settings(),
        now_utc=_now(),
        notifier=None,
    )

    assert report.tp_runner_hit == 1
    with session_scope(engine) as s:
        order = get_order_by_ticket(s, 500)
        assert order.status == "tp_runner_hit"
        assert order.realized_r == pytest.approx(125.0 / 75.0, abs=0.01)


def test_filled_position_closed_at_sl_marks_status(engine, session_factory):
    """At SL, full position closes at -1R. profit_usd = -$75 on $75 risk."""
    with session_scope(engine) as s:
        _add_setup_and_order(s, ticket=600, status="filled", volume=0.05)

    mt5 = _MockMt5(
        positions=[],
        symbol_info_by_symbol={"XAUUSD": _SymbolInfo(trade_contract_size=100.0)},
    )
    mt5.history[600] = dict(
        exit_price=4375.0,  # at SL
        exit_time_utc=_now(),
        profit_usd=-75.0,
    )

    report = check_open_positions(
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=_settings(),
        now_utc=_now(),
        notifier=None,
    )

    assert report.sl_hit == 1
    with session_scope(engine) as s:
        order = get_order_by_ticket(s, 600)
        assert order.status == "sl_hit"
        assert order.realized_r == pytest.approx(-1.0, abs=0.01)


# -----------------------------------------------------------------------------
# end_of_killzone_cleanup
# -----------------------------------------------------------------------------


def test_end_of_killzone_cancels_pending_for_that_killzone(
    engine, session_factory
):
    with session_scope(engine) as s:
        _add_setup_and_order(
            s,
            setup_uid="XAUUSD_2026-05-01T11:00:00+00:00",
            ticket=10,
            status="pending",
            killzone="london",
        )
        _add_setup_and_order(
            s,
            setup_uid="XAUUSD_2026-05-01T16:00:00+00:00",
            ticket=20,
            status="pending",
            killzone="ny",
        )

    mt5 = _MockMt5(
        pending=[
            _PendingOrderSnapshot(
                ticket=10,
                symbol="XAUUSD",
                direction="short",
                volume=0.05,
                price_open=4360.0,
                sl=4375.0,
                tp=4080.5,
            ),
            _PendingOrderSnapshot(
                ticket=20,
                symbol="XAUUSD",
                direction="short",
                volume=0.05,
                price_open=4360.0,
                sl=4375.0,
                tp=4080.5,
            ),
        ]
    )

    n = end_of_killzone_cleanup(
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=_settings(),
        killzone="london",
        now_utc=_now(),
        notifier=None,
    )

    assert n == 1
    assert 10 in mt5.cancelled
    assert 20 not in mt5.cancelled

    with session_scope(engine) as s:
        london_order = get_order_by_ticket(s, 10)
        ny_order = get_order_by_ticket(s, 20)
        assert london_order.status == "cancelled"
        assert ny_order.status == "pending"  # untouched


def test_end_of_killzone_skips_already_filled(engine, session_factory):
    """Filled orders must NOT be cancelled by end-of-killzone cleanup."""
    with session_scope(engine) as s:
        _add_setup_and_order(s, ticket=30, status="filled", killzone="ny")

    mt5 = _MockMt5(pending=[])  # no pending — order is filled

    n = end_of_killzone_cleanup(
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=_settings(),
        killzone="ny",
        now_utc=_now(),
        notifier=None,
    )

    assert n == 0
    assert mt5.cancelled == []
    with session_scope(engine) as s:
        order = get_order_by_ticket(s, 30)
        assert order.status == "filled"
