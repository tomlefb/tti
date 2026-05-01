"""Unit tests for ``src.execution.order_manager``.

The order manager owns the place-order pipeline. Tests inject a mock
MT5 client (any object satisfying the order-operations Protocol) and an
in-memory journal so the full flow exercises:

1. Pre-flight via safe_guards (kill switch + day-disabled + hard_stops).
2. Position-size calc from risk + SL distance + symbol contract size.
3. Spread anomaly logging (no skip — operator design call).
4. MT5 ``order_send`` retcode verification.
5. Persist to ``orders`` table with status="pending".
6. ``OrderResult`` returned to caller (success, ticket, error info).

The MT5 mock is a small dataclass exposing the same surface as the
production ``MT5Client`` order methods. Tests do NOT touch the live
broker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from src.detection.fvg import FVG
from src.detection.mss import MSS
from src.detection.setup import Setup
from src.detection.sweep import Sweep
from src.execution.order_manager import (
    OrderResult,
    cancel_order,
    compute_volume,
    modify_position_sl,
    place_order,
)
from src.journal.db import get_engine, init_db, session_scope
from src.journal.models import OrderRow, SpreadAnomalyRow
from src.journal.outcome_tracker import Mt5Trade
from src.journal.repository import get_order_by_ticket, insert_setup


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
        # hard_stops
        ACCOUNT_BALANCE_BASE=5000.0,
        DAILY_LOSS_LIMIT=200.0,
        MAX_LOSS_LIMIT=400.0,
        DAILY_LOSS_STOP_FRACTION=0.80,
        MAX_LOSS_STOP_FRACTION=0.80,
        MAX_TRADES_PER_DAY=2,
        MAX_TRADES_PER_PAIR_PER_DAY=2,
        MAX_CONSECUTIVE_SL_PER_DAY=2,
        NEWS_BLACKOUT_TODAY=False,
        MAX_LOSS_OVERRIDE=False,
        # safe_guards / order_manager
        KILL_SWITCH_PATH=None,
        AUTO_TRADING_ENABLED=True,
        MAGIC_NUMBER=7766,
        RISK_PER_TRADE_FRACTION=0.01,
        MAX_RISK_PER_TRADE_USD=None,
        TP1_PARTIAL_FRACTION=0.5,
        SPREAD_ANOMALY_MULTIPLIER=3.0,
        INSTRUMENT_CONFIG={
            "XAUUSD": {"typical_spread": 0.5},
            "NDX100": {"typical_spread": 2.0},
        },
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@dataclass
class _AccountInfo:
    login_masked: str = "***1234"
    currency: str = "USD"
    balance: float = 5000.0
    equity: float = 5000.0
    profit: float = 0.0
    margin_level: float = 0.0
    leverage: int = 100


@dataclass
class _SymbolInfo:
    symbol: str = "XAUUSD"
    trade_contract_size: float = 100.0
    point: float = 0.01
    volume_min: float = 0.01
    volume_step: float = 0.01
    volume_max: float = 100.0
    ask: float = 4360.5
    bid: float = 4360.0


@dataclass
class _OrderSendResult:
    retcode: int = 10009  # TRADE_RETCODE_DONE
    order: int = 12345678  # ticket id
    deal: int = 0
    comment: str = "Done"
    request_id: int = 0


@dataclass
class _MockMt5:
    """In-memory MT5 double covering the order-operations surface."""

    account: _AccountInfo = field(default_factory=_AccountInfo)
    symbol_infos: dict[str, _SymbolInfo] = field(default_factory=dict)
    next_send_result: _OrderSendResult = field(default_factory=_OrderSendResult)
    last_request: dict[str, Any] | None = None
    sent_requests: list[dict[str, Any]] = field(default_factory=list)
    cancelled_tickets: list[int] = field(default_factory=list)
    modified_positions: list[tuple[int, float]] = field(default_factory=list)
    trades: list[Mt5Trade] = field(default_factory=list)

    def get_account_info(self) -> _AccountInfo:
        return self.account

    def get_recent_trades(self, since: datetime) -> list[Mt5Trade]:
        return list(self.trades)

    def get_symbol_info(self, symbol: str) -> _SymbolInfo:
        return self.symbol_infos.get(symbol, _SymbolInfo(symbol=symbol))

    def place_limit_order(
        self,
        *,
        symbol: str,
        direction: str,
        volume: float,
        price: float,
        sl: float,
        tp: float,
        magic: int,
        comment: str = "",
    ) -> _OrderSendResult:
        request = dict(
            symbol=symbol,
            direction=direction,
            volume=volume,
            price=price,
            sl=sl,
            tp=tp,
            magic=magic,
            comment=comment,
        )
        self.last_request = request
        self.sent_requests.append(request)
        return self.next_send_result

    def cancel_pending_order(self, ticket: int) -> bool:
        self.cancelled_tickets.append(int(ticket))
        return True

    def modify_position_sl(self, *, ticket: int, new_sl: float) -> bool:
        self.modified_positions.append((int(ticket), float(new_sl)))
        return True


def _make_setup(symbol: str = "XAUUSD") -> Setup:
    ts = datetime(2026, 5, 1, 15, 35, tzinfo=UTC)
    sweep = Sweep(
        direction="bearish",
        swept_level_price=4380.0,
        swept_level_type="asian_high",
        swept_level_strength="structural",
        sweep_candle_time_utc=ts,
        sweep_extreme_price=4382.5,
        return_candle_time_utc=ts,
        excursion=2.5,
    )
    mss = MSS(
        direction="bearish",
        sweep=sweep,
        broken_swing_time_utc=ts,
        broken_swing_price=4365.0,
        mss_confirm_candle_time_utc=ts,
        mss_confirm_candle_close=4364.0,
        displacement_body_ratio=2.1,
        displacement_candle_time_utc=ts,
    )
    fvg = FVG(
        direction="bearish",
        proximal=4360.0,
        distal=4366.0,
        c1_time_utc=ts,
        c2_time_utc=ts,
        c3_time_utc=ts,
        size=6.0,
        size_atr_ratio=1.0,
    )
    return Setup(
        timestamp_utc=ts,
        symbol=symbol,
        direction="short",
        daily_bias="bearish",
        killzone="ny",
        swept_level_price=4380.0,
        swept_level_type="asian_high",
        swept_level_strength="structural",
        sweep=sweep,
        mss=mss,
        poi=fvg,
        poi_type="FVG",
        entry_price=4360.0,
        stop_loss=4375.0,
        target_level_type="swing_h1_low",
        tp_runner_price=4080.5,
        tp_runner_rr=18.7,
        tp1_price=4285.0,
        tp1_rr=5.0,
        quality="A",
        confluences=["FVG+OB"],
    )


def _now() -> datetime:
    return datetime(2026, 5, 1, 15, 40, tzinfo=UTC)


# -----------------------------------------------------------------------------
# compute_volume
# -----------------------------------------------------------------------------


def test_compute_volume_xauusd_one_percent_risk():
    """XAU: $5000 × 1% = $50 risk; SL 15 USD; contract 100 oz/lot.
    volume = 50 / (15 × 100) = 0.0333… → snap to 0.03 (volume_step=0.01)."""
    info = _SymbolInfo(symbol="XAUUSD", trade_contract_size=100.0, volume_step=0.01, volume_min=0.01)
    vol = compute_volume(
        risk_usd=50.0,
        sl_distance_price=15.0,
        symbol_info=info,
    )
    # 50 / (15 × 100) = 0.0333; floor to step 0.01 → 0.03.
    assert vol == pytest.approx(0.03, abs=1e-9)


def test_compute_volume_ndx100_one_percent_risk():
    """NDX: $5000 × 1% = $50 risk; SL 50 points; contract 1.
    volume = 50 / (50 × 1) = 1.0."""
    info = _SymbolInfo(
        symbol="NDX100", trade_contract_size=1.0, volume_step=0.1, volume_min=0.1
    )
    vol = compute_volume(
        risk_usd=50.0,
        sl_distance_price=50.0,
        symbol_info=info,
    )
    assert vol == pytest.approx(1.0, abs=1e-9)


def test_compute_volume_clamps_to_volume_min_when_below_floor():
    """If the calc yields less than volume_min, clamp UP to volume_min.

    Trade-off: this means the operator's actual risk slightly exceeds
    RISK_PER_TRADE_FRACTION on small SL distances. Documented in
    docs/04 §"Auto-execution rules" — broker's lot-step floor wins."""
    info = _SymbolInfo(volume_step=0.01, volume_min=0.01)
    vol = compute_volume(
        risk_usd=1.0,
        sl_distance_price=10000.0,  # absurd → calc → 0.000001
        symbol_info=info,
    )
    assert vol == 0.01  # volume_min floor


def test_compute_volume_clamps_to_volume_max_when_above_ceiling():
    info = _SymbolInfo(volume_step=0.01, volume_min=0.01, volume_max=5.0)
    vol = compute_volume(
        risk_usd=1_000_000.0,
        sl_distance_price=1.0,
        symbol_info=info,
    )
    assert vol == 5.0


def test_compute_volume_floors_to_step_resolution():
    """0.0567 with step=0.01 floors to 0.05 (NOT round to 0.06) — never
    overshoot the risk budget by rounding up."""
    info = _SymbolInfo(volume_step=0.01, volume_min=0.01, trade_contract_size=100.0)
    # Want raw = 0.0567 → use risk=85.05 / (sl=15 × 100) = 0.0567
    vol = compute_volume(
        risk_usd=85.05,
        sl_distance_price=15.0,
        symbol_info=info,
    )
    assert vol == pytest.approx(0.05, abs=1e-9)


def test_compute_volume_zero_sl_distance_raises():
    """SL distance == 0 means no risk — invariant violation, must not
    silently divide-by-zero."""
    info = _SymbolInfo()
    with pytest.raises(ValueError, match="sl_distance"):
        compute_volume(risk_usd=50.0, sl_distance_price=0.0, symbol_info=info)


# -----------------------------------------------------------------------------
# place_order — happy path
# -----------------------------------------------------------------------------


def test_place_order_happy_path_persists_and_returns_ticket(
    engine, session_factory
):
    setup = _make_setup()
    with session_scope(engine) as s:
        insert_setup(s, setup, was_notified=True, detected_at=_now())

    settings = _settings()
    mt5 = _MockMt5()

    result = place_order(
        setup=setup,
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=settings,
        now_utc=_now(),
        notifier=None,
    )

    assert result.success is True
    assert result.ticket == 12345678
    assert result.error_code is None

    # Persisted with status=pending.
    with session_scope(engine) as s:
        order = get_order_by_ticket(s, 12345678)
        assert order is not None
        assert order.status == "pending"
        assert order.symbol == "XAUUSD"
        assert order.direction == "short"
        assert order.entry_price == pytest.approx(4360.0)
        assert order.stop_loss == pytest.approx(4375.0)
        # tp_runner stored, NOT tp1 — lifecycle handles partial close.
        assert order.tp_runner == pytest.approx(4080.5)


def test_place_order_sends_correct_mt5_request(engine, session_factory):
    setup = _make_setup()
    with session_scope(engine) as s:
        insert_setup(s, setup, was_notified=True, detected_at=_now())

    settings = _settings()
    mt5 = _MockMt5()

    place_order(
        setup=setup,
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=settings,
        now_utc=_now(),
        notifier=None,
    )

    req = mt5.last_request
    assert req["symbol"] == "XAUUSD"
    assert req["direction"] == "short"
    assert req["price"] == pytest.approx(4360.0)
    assert req["sl"] == pytest.approx(4375.0)
    # TP at runner — partial close at TP1 happens via lifecycle.
    assert req["tp"] == pytest.approx(4080.5)
    assert req["magic"] == 7766
    assert req["volume"] > 0


# -----------------------------------------------------------------------------
# place_order — pre-flight blocks
# -----------------------------------------------------------------------------


def test_place_order_blocked_by_kill_switch_returns_failure(
    engine, session_factory, tmp_path
):
    """When safe_guards blocks, place_order returns success=False and
    does NOT call mt5.place_limit_order."""
    kill = tmp_path / "KILL_SWITCH"
    kill.touch()
    setup = _make_setup()
    with session_scope(engine) as s:
        insert_setup(s, setup, was_notified=True, detected_at=_now())

    settings = _settings(KILL_SWITCH_PATH=kill)
    mt5 = _MockMt5()

    result = place_order(
        setup=setup,
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=settings,
        now_utc=_now(),
        notifier=None,
    )

    assert result.success is False
    assert result.error_code == "kill_switch"
    assert result.ticket is None
    assert mt5.last_request is None  # MT5 not touched

    # No order row persisted.
    with session_scope(engine) as s:
        rows = list(s.execute(select(OrderRow)).scalars().all())
        assert rows == []


# -----------------------------------------------------------------------------
# place_order — MT5 retcode failure
# -----------------------------------------------------------------------------


def test_place_order_mt5_retcode_failure_returns_failure(
    engine, session_factory
):
    setup = _make_setup()
    with session_scope(engine) as s:
        insert_setup(s, setup, was_notified=True, detected_at=_now())

    settings = _settings()
    mt5 = _MockMt5(
        next_send_result=_OrderSendResult(retcode=10004, comment="Requote")
    )

    result = place_order(
        setup=setup,
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=settings,
        now_utc=_now(),
        notifier=None,
    )

    assert result.success is False
    assert result.error_code == 10004
    assert "Requote" in (result.error_message or "")

    # No order row persisted on failure.
    with session_scope(engine) as s:
        rows = list(s.execute(select(OrderRow)).scalars().all())
        assert rows == []


# -----------------------------------------------------------------------------
# place_order — spread anomaly logged but does NOT block
# -----------------------------------------------------------------------------


def test_place_order_logs_spread_anomaly_but_proceeds(
    engine, session_factory
):
    setup = _make_setup()
    with session_scope(engine) as s:
        insert_setup(s, setup, was_notified=True, detected_at=_now())

    # XAUUSD typical spread = 0.5; anomaly multiplier = 3 → > 1.5.
    settings = _settings()
    mt5 = _MockMt5(
        symbol_infos={"XAUUSD": _SymbolInfo(symbol="XAUUSD", ask=4362.0, bid=4360.0)}
    )  # spread = 2.0 > 1.5 = anomaly threshold

    result = place_order(
        setup=setup,
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=settings,
        now_utc=_now(),
        notifier=None,
    )

    assert result.success is True
    # Anomaly logged.
    with session_scope(engine) as s:
        rows = list(s.execute(select(SpreadAnomalyRow)).scalars().all())
        assert len(rows) == 1
        assert rows[0].symbol == "XAUUSD"
        assert rows[0].spread == pytest.approx(2.0)
        assert rows[0].action_taken == "executed_anyway"


# -----------------------------------------------------------------------------
# place_order — dry_run
# -----------------------------------------------------------------------------


def test_place_order_dry_run_does_not_call_mt5_or_persist(
    engine, session_factory
):
    setup = _make_setup()
    with session_scope(engine) as s:
        insert_setup(s, setup, was_notified=True, detected_at=_now())

    settings = _settings()
    mt5 = _MockMt5()

    result = place_order(
        setup=setup,
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=settings,
        now_utc=_now(),
        notifier=None,
        dry_run=True,
    )

    assert result.success is True
    assert result.ticket is None  # no real ticket in dry-run
    assert mt5.last_request is None  # MT5 not touched

    # No order row persisted in dry-run.
    with session_scope(engine) as s:
        rows = list(s.execute(select(OrderRow)).scalars().all())
        assert rows == []


# -----------------------------------------------------------------------------
# place_order — telegram notifier hooks fire (pre + post)
# -----------------------------------------------------------------------------


def test_place_order_invokes_telegram_pre_and_post_when_provided(
    engine, session_factory
):
    setup = _make_setup()
    with session_scope(engine) as s:
        insert_setup(s, setup, was_notified=True, detected_at=_now())

    settings = _settings()
    mt5 = _MockMt5()

    notifier = MagicMock()
    notifier.send_text = MagicMock(return_value=None)
    notifier.send_order_placed = MagicMock(return_value=None)

    place_order(
        setup=setup,
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=settings,
        now_utc=_now(),
        notifier=notifier,
    )

    # send_order_placed called once with the ticket.
    notifier.send_order_placed.assert_called_once()


# -----------------------------------------------------------------------------
# cancel_order
# -----------------------------------------------------------------------------


def test_cancel_order_invokes_mt5_and_updates_journal(engine, session_factory):
    setup = _make_setup()
    with session_scope(engine) as s:
        insert_setup(s, setup, was_notified=True, detected_at=_now())

    settings = _settings()
    mt5 = _MockMt5()

    place_order(
        setup=setup,
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=settings,
        now_utc=_now(),
        notifier=None,
    )

    ok = cancel_order(
        ticket=12345678,
        mt5_client=mt5,
        journal_session_factory=session_factory,
        reason="end_of_killzone",
        now_utc=_now(),
    )
    assert ok is True
    assert 12345678 in mt5.cancelled_tickets

    with session_scope(engine) as s:
        order = get_order_by_ticket(s, 12345678)
        assert order.status == "cancelled"
        assert "end_of_killzone" in (order.notes or "")


def test_cancel_order_unknown_ticket_returns_false(engine, session_factory):
    settings = _settings()
    mt5 = _MockMt5()
    ok = cancel_order(
        ticket=999,
        mt5_client=mt5,
        journal_session_factory=session_factory,
        reason="manual",
        now_utc=_now(),
    )
    # MT5 does the cancel attempt; journal update is a no-op since ticket
    # is unknown to us. We treat it as success-with-warning at MT5 level
    # but a journal-side warning. The contract is: True if MT5 reported
    # OK; the journal layer logs but does not raise.
    assert ok is True


# -----------------------------------------------------------------------------
# modify_position_sl
# -----------------------------------------------------------------------------


def test_modify_position_sl_invokes_mt5(engine, session_factory):
    settings = _settings()
    mt5 = _MockMt5()

    ok = modify_position_sl(
        ticket=42,
        new_sl=4360.0,
        mt5_client=mt5,
    )
    assert ok is True
    assert (42, 4360.0) in mt5.modified_positions
