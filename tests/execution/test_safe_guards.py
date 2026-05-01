"""Unit tests for ``src.execution.safe_guards``.

The safe-guards layer stacks on top of Sprint 6's ``hard_stops`` module:

- ``check_pre_trade`` delegates the financial checks (account info,
  daily loss, max loss, trade count, consecutive SL, per-pair count) to
  ``hard_stops.is_blocked``. It only adds two NEW checks on top:

  1. ``KILL_SWITCH`` file existence (manual hard-disable).
  2. ``daily_state.auto_trading_disabled`` flag (set by the safe-guards
     layer itself when something went wrong earlier today).

- ``log_spread_anomaly`` writes to the ``spread_anomalies`` table when
  the live spread exceeds ``typical × multiplier``. It NEVER blocks the
  trade — operator's design call (see docs/04 §"Auto-execution rules").

- ``disable_for_day`` is the kill-flag setter — used by the order
  manager when a critical fault is observed mid-cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.detection.fvg import FVG
from src.detection.mss import MSS
from src.detection.setup import Setup
from src.detection.sweep import Sweep
from src.execution.safe_guards import (
    check_pre_trade,
    disable_for_day,
    kill_switch_active,
    log_spread_anomaly,
    should_log_spread_anomaly,
)
from src.journal.db import get_engine, init_db, session_scope
from src.journal.models import SpreadAnomalyRow
from src.journal.outcome_tracker import Mt5Trade
from src.journal.repository import (
    disable_auto_trading_for_day,
    insert_setup,
    setup_uid_for,
)
from sqlalchemy import select

_TZ_PARIS = ZoneInfo("Europe/Paris")


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def engine():
    eng = get_engine(":memory:")
    init_db(eng)
    return eng


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        # hard_stops surface
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
        # safe_guards surface
        KILL_SWITCH_PATH=None,  # tests override
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
class _MockMt5:
    account: _AccountInfo = field(default_factory=_AccountInfo)
    trades: list[Mt5Trade] = field(default_factory=list)

    def get_account_info(self) -> _AccountInfo:
        return self.account

    def get_recent_trades(self, since: datetime) -> list[Mt5Trade]:
        return [t for t in self.trades if t.entry_time_utc >= since]


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
# kill_switch_active
# -----------------------------------------------------------------------------


def test_kill_switch_active_returns_false_when_path_missing(tmp_path):
    path = tmp_path / "KILL_SWITCH"
    assert not path.exists()
    assert kill_switch_active(path) is False


def test_kill_switch_active_returns_true_when_path_exists(tmp_path):
    path = tmp_path / "KILL_SWITCH"
    path.write_text("manual stop, see ops chat 2026-05-01")
    assert kill_switch_active(path) is True


def test_kill_switch_active_default_path(monkeypatch, tmp_path):
    """When called with no argument, the default project-root path is used."""
    monkeypatch.chdir(tmp_path)
    assert kill_switch_active() is False
    (tmp_path / "KILL_SWITCH").touch()
    assert kill_switch_active() is True


# -----------------------------------------------------------------------------
# check_pre_trade — delegates to hard_stops + adds kill switch / disabled flag
# -----------------------------------------------------------------------------


def test_check_pre_trade_passes_when_nothing_blocks(engine, tmp_path):
    setup = _make_setup()
    settings = _settings(KILL_SWITCH_PATH=tmp_path / "KILL_SWITCH")
    mt5 = _MockMt5()

    with session_scope(engine) as s:
        allowed, reason = check_pre_trade(
            s, mt5, settings, setup=setup, now_utc=_now()
        )
    assert allowed is True
    assert reason is None


def test_check_pre_trade_blocks_when_kill_switch_present(engine, tmp_path):
    kill_path = tmp_path / "KILL_SWITCH"
    kill_path.touch()
    setup = _make_setup()
    settings = _settings(KILL_SWITCH_PATH=kill_path)
    mt5 = _MockMt5()

    with session_scope(engine) as s:
        allowed, reason = check_pre_trade(
            s, mt5, settings, setup=setup, now_utc=_now()
        )
    assert allowed is False
    assert reason == "kill_switch"


def test_check_pre_trade_blocks_when_auto_trading_disabled_for_day(engine, tmp_path):
    setup = _make_setup()
    settings = _settings(KILL_SWITCH_PATH=tmp_path / "KILL_SWITCH")
    mt5 = _MockMt5()
    today_paris = _now().astimezone(_TZ_PARIS).date()

    with session_scope(engine) as s:
        disable_auto_trading_for_day(s, day=today_paris, reason="manual")

    with session_scope(engine) as s:
        allowed, reason = check_pre_trade(
            s, mt5, settings, setup=setup, now_utc=_now()
        )
    assert allowed is False
    assert reason == "auto_trading_disabled"


def test_check_pre_trade_delegates_to_hard_stops_for_daily_loss(engine, tmp_path):
    """When hard_stops.is_blocked fires, check_pre_trade returns its block code."""
    setup = _make_setup()
    settings = _settings(KILL_SWITCH_PATH=tmp_path / "KILL_SWITCH")
    # equity drop of $200 → drawdown $200 ≥ 80% of $200 daily limit → blocks.
    mt5 = _MockMt5(account=_AccountInfo(balance=5000.0, equity=4800.0, profit=-200.0))

    with session_scope(engine) as s:
        allowed, reason = check_pre_trade(
            s, mt5, settings, setup=setup, now_utc=_now()
        )
    assert allowed is False
    assert reason == "daily_loss_reached"


def test_check_pre_trade_kill_switch_short_circuits_before_hard_stops(
    engine, tmp_path
):
    """Kill switch is checked first — even if account info would also block."""
    kill_path = tmp_path / "KILL_SWITCH"
    kill_path.touch()
    setup = _make_setup()
    settings = _settings(KILL_SWITCH_PATH=kill_path)

    # A broken MT5 client that would otherwise raise → must not be touched.
    class _BrokenMt5:
        def get_account_info(self):  # pragma: no cover — must not be called
            raise RuntimeError("MT5 unreachable")

    with session_scope(engine) as s:
        allowed, reason = check_pre_trade(
            s, _BrokenMt5(), settings, setup=setup, now_utc=_now()
        )
    assert allowed is False
    assert reason == "kill_switch"


# -----------------------------------------------------------------------------
# Spread anomaly
# -----------------------------------------------------------------------------


def test_should_log_spread_anomaly_threshold():
    # 3.0 multiplier (default).
    assert should_log_spread_anomaly(current=1.6, typical=0.5, multiplier=3.0) is True
    assert should_log_spread_anomaly(current=1.5, typical=0.5, multiplier=3.0) is False
    assert should_log_spread_anomaly(current=0.7, typical=0.5, multiplier=3.0) is False


def test_should_log_spread_anomaly_no_typical_returns_false():
    """Without a typical_spread baseline, no anomaly judgment is possible."""
    assert should_log_spread_anomaly(current=10.0, typical=None) is False


def test_log_spread_anomaly_persists_to_journal(engine):
    setup = _make_setup()
    with session_scope(engine) as s:
        uid = insert_setup(s, setup, was_notified=True, detected_at=_now())

    with session_scope(engine) as s:
        log_spread_anomaly(
            s,
            symbol="XAUUSD",
            current_spread=2.5,
            typical_spread=0.5,
            setup_uid=uid,
            detected_at_utc=_now(),
            action_taken="executed_anyway",
        )

    with session_scope(engine) as s:
        rows = list(s.execute(select(SpreadAnomalyRow)).scalars().all())
        assert len(rows) == 1
        assert rows[0].setup_uid == uid
        assert rows[0].action_taken == "executed_anyway"
        assert rows[0].spread == 2.5


# -----------------------------------------------------------------------------
# disable_for_day
# -----------------------------------------------------------------------------


def test_disable_for_day_flips_flag(engine):
    today = date(2026, 5, 1)
    with session_scope(engine) as s:
        disable_for_day(s, day=today, reason="circuit_breaker_test")

    with session_scope(engine) as s:
        from src.journal.repository import is_auto_trading_disabled

        assert is_auto_trading_disabled(s, day=today) is True
