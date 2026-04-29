"""Unit tests for src.scheduler.hard_stops.

Use an in-memory SQLite journal + a hand-rolled mock MT5 client so the
tests are deterministic and offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.detection.fvg import FVG
from src.detection.mss import MSS
from src.detection.setup import Setup
from src.detection.sweep import Sweep
from src.journal.db import get_engine, init_db, session_scope
from src.journal.outcome_tracker import Mt5Trade
from src.journal.repository import (
    insert_decision,
    insert_setup,
    setup_uid_for,
    upsert_outcome,
)
from src.scheduler.hard_stops import is_blocked

_TZ_PARIS = ZoneInfo("Europe/Paris")


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
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
class _MockMt5:
    """Minimal MT5 client double for hard-stop tests."""

    account: _AccountInfo = field(default_factory=_AccountInfo)
    trades: list[Mt5Trade] = field(default_factory=list)

    def get_account_info(self) -> _AccountInfo:
        return self.account

    def get_recent_trades(self, since: datetime) -> list[Mt5Trade]:
        return [t for t in self.trades if t.entry_time_utc >= since]


def _make_setup(symbol: str, timestamp_utc: datetime) -> Setup:
    sweep = Sweep(
        direction="bullish",
        swept_level_price=99.5,
        swept_level_type="asian_low",
        swept_level_strength="structural",
        sweep_candle_time_utc=timestamp_utc,
        sweep_extreme_price=99.0,
        return_candle_time_utc=timestamp_utc,
        excursion=0.5,
    )
    fvg = FVG(
        direction="bullish",
        proximal=102.0,
        distal=101.0,
        c1_time_utc=timestamp_utc,
        c2_time_utc=timestamp_utc,
        c3_time_utc=timestamp_utc,
        size=1.0,
        size_atr_ratio=1.0,
    )
    mss = MSS(
        direction="bullish",
        sweep=sweep,
        broken_swing_time_utc=timestamp_utc,
        broken_swing_price=110.0,
        mss_confirm_candle_time_utc=timestamp_utc,
        mss_confirm_candle_close=110.5,
        displacement_body_ratio=2.0,
        displacement_candle_time_utc=timestamp_utc,
    )
    return Setup(
        timestamp_utc=timestamp_utc,
        symbol=symbol,
        direction="long",
        daily_bias="bullish",
        killzone="ny",
        swept_level_price=99.5,
        swept_level_type="asian_low",
        swept_level_strength="structural",
        sweep=sweep,
        mss=mss,
        poi=fvg,
        poi_type="FVG",
        entry_price=102.0,
        stop_loss=99.0,
        target_level_type="asian_high",
        tp_runner_price=120.0,
        tp_runner_rr=6.0,
        tp1_price=117.0,
        tp1_rr=5.0,
        quality="A",
        confluences=[],
    )


def _record_taken_setup(
    session,
    *,
    symbol: str,
    timestamp_utc: datetime,
    sl_hit: bool = False,
) -> str:
    setup = _make_setup(symbol, timestamp_utc)
    insert_setup(session, setup, was_notified=True)
    uid = setup_uid_for(setup)
    insert_decision(session, uid, "taken", decided_at=timestamp_utc)
    if sl_hit:
        upsert_outcome(session, uid, exit_reason="sl_hit")
    return uid


@pytest.fixture
def engine():
    e = get_engine(":memory:")
    init_db(e)
    return e


# A "today" anchor in NY killzone for 2026-04-28 (Paris = UTC+2 summer).
_NOW_UTC = datetime(2026, 4, 28, 14, 0, 0, tzinfo=UTC)
# Paris = 16:00 — well before 23:00 rollover ⇒ trading day = 2026-04-28.


def test_no_block_normal_state(engine):
    mt5 = _MockMt5()
    settings = _settings()
    with session_scope(engine) as s:
        block = is_blocked(s, mt5, settings, pair="XAUUSD", now_utc=_NOW_UTC)
    assert block is None


def test_blocks_on_max_loss_critical(engine):
    # equity 4670 → drawdown 330 ≥ threshold 320 (80% of 400).
    mt5 = _MockMt5(account=_AccountInfo(balance=4670.0, equity=4670.0))
    settings = _settings()
    with session_scope(engine) as s:
        block = is_blocked(s, mt5, settings, pair="XAUUSD", now_utc=_NOW_UTC)
    assert block is not None
    assert block.code == "max_loss_critical"


def test_max_loss_override_unblocks(engine):
    mt5 = _MockMt5(account=_AccountInfo(balance=4670.0, equity=4670.0))
    settings = _settings(MAX_LOSS_OVERRIDE=True)
    with session_scope(engine) as s:
        block = is_blocked(s, mt5, settings, pair="XAUUSD", now_utc=_NOW_UTC)
    assert block is None


def test_blocks_on_daily_loss_reached(engine):
    # Realised loss today = $-170 ≥ $-160 threshold (80% × $200).
    losing_trade = Mt5Trade(
        ticket=1,
        symbol="XAUUSD",
        direction="long",
        entry_time_utc=_NOW_UTC - timedelta(hours=1),
        entry_price=2400.0,
        exit_time_utc=_NOW_UTC - timedelta(minutes=30),
        exit_price=2390.0,
        profit_usd=-170.0,
    )
    mt5 = _MockMt5(account=_AccountInfo(balance=5000.0, equity=4830.0), trades=[losing_trade])
    settings = _settings()
    with session_scope(engine) as s:
        block = is_blocked(s, mt5, settings, pair="XAUUSD", now_utc=_NOW_UTC)
    assert block is not None
    assert block.code == "daily_loss_reached"


def test_blocks_on_news_blackout(engine):
    mt5 = _MockMt5()
    settings = _settings(NEWS_BLACKOUT_TODAY=True)
    with session_scope(engine) as s:
        block = is_blocked(s, mt5, settings, pair="XAUUSD", now_utc=_NOW_UTC)
    assert block is not None
    assert block.code == "news_blackout"


def test_blocks_on_daily_trade_count(engine):
    mt5 = _MockMt5()
    settings = _settings(MAX_TRADES_PER_DAY=2)
    with session_scope(engine) as s:
        # Two taken trades, different pairs.
        _record_taken_setup(s, symbol="XAUUSD", timestamp_utc=_NOW_UTC - timedelta(hours=2))
        _record_taken_setup(s, symbol="EURUSD", timestamp_utc=_NOW_UTC - timedelta(hours=1))
    with session_scope(engine) as s:
        block = is_blocked(s, mt5, settings, pair="GBPUSD", now_utc=_NOW_UTC)
    assert block is not None
    assert block.code == "daily_trade_count"


def test_blocks_on_consecutive_sl(engine):
    mt5 = _MockMt5()
    settings = _settings(MAX_CONSECUTIVE_SL_PER_DAY=2, MAX_TRADES_PER_DAY=10)
    with session_scope(engine) as s:
        _record_taken_setup(
            s,
            symbol="XAUUSD",
            timestamp_utc=_NOW_UTC - timedelta(hours=2),
            sl_hit=True,
        )
        _record_taken_setup(
            s,
            symbol="EURUSD",
            timestamp_utc=_NOW_UTC - timedelta(hours=1),
            sl_hit=True,
        )
    with session_scope(engine) as s:
        block = is_blocked(s, mt5, settings, pair="GBPUSD", now_utc=_NOW_UTC)
    assert block is not None
    assert block.code == "consecutive_sl"


def test_blocks_on_pair_count(engine):
    mt5 = _MockMt5()
    settings = _settings(MAX_TRADES_PER_PAIR_PER_DAY=2, MAX_TRADES_PER_DAY=10)
    with session_scope(engine) as s:
        _record_taken_setup(s, symbol="XAUUSD", timestamp_utc=_NOW_UTC - timedelta(hours=2))
        _record_taken_setup(s, symbol="XAUUSD", timestamp_utc=_NOW_UTC - timedelta(hours=1))
    with session_scope(engine) as s:
        block = is_blocked(s, mt5, settings, pair="XAUUSD", now_utc=_NOW_UTC)
    assert block is not None
    assert block.code == "pair_count"


def test_max_loss_check_short_circuits_before_daily_loss(engine):
    """When both are triggered, max_loss_critical wins (more severe)."""
    losing_trade = Mt5Trade(
        ticket=1,
        symbol="XAUUSD",
        direction="long",
        entry_time_utc=_NOW_UTC - timedelta(hours=1),
        entry_price=2400.0,
        exit_time_utc=_NOW_UTC - timedelta(minutes=30),
        exit_price=2390.0,
        profit_usd=-200.0,
    )
    mt5 = _MockMt5(account=_AccountInfo(balance=4600.0, equity=4600.0), trades=[losing_trade])
    settings = _settings()
    with session_scope(engine) as s:
        block = is_blocked(s, mt5, settings, pair="XAUUSD", now_utc=_NOW_UTC)
    assert block is not None
    assert block.code == "max_loss_critical"


def test_consecutive_sl_only_counts_uninterrupted_streak_from_most_recent(engine):
    """A win in the middle of two SLs resets the streak."""
    mt5 = _MockMt5()
    settings = _settings(
        MAX_CONSECUTIVE_SL_PER_DAY=2, MAX_TRADES_PER_DAY=10, MAX_TRADES_PER_PAIR_PER_DAY=10
    )
    with session_scope(engine) as s:
        # Most recent: SL (will be the trailing streak, len=1).
        _record_taken_setup(
            s, symbol="XAUUSD", timestamp_utc=_NOW_UTC - timedelta(hours=1), sl_hit=True
        )
        # Earlier: TP (breaks the streak).
        uid_tp = _record_taken_setup(
            s, symbol="EURUSD", timestamp_utc=_NOW_UTC - timedelta(hours=2)
        )
        upsert_outcome(s, uid_tp, exit_reason="tp_runner_hit")
        # Earliest: SL.
        _record_taken_setup(
            s, symbol="GBPUSD", timestamp_utc=_NOW_UTC - timedelta(hours=3), sl_hit=True
        )
    with session_scope(engine) as s:
        block = is_blocked(s, mt5, settings, pair="GBPUSD", now_utc=_NOW_UTC)
    # Streak is 1 (only the most-recent SL counts) — under MAX 2.
    assert block is None


def test_account_info_failure_blocks_with_clear_code(engine):
    class _FailingMt5:
        def get_account_info(self):
            raise RuntimeError("MT5 unreachable")

        def get_recent_trades(self, since):  # pragma: no cover — never called
            return []

    settings = _settings()
    with session_scope(engine) as s:
        block = is_blocked(s, _FailingMt5(), settings, pair="XAUUSD", now_utc=_NOW_UTC)
    assert block is not None
    assert block.code == "account_info_unavailable"
