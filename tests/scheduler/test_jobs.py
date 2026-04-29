"""Unit tests for src.scheduler.jobs.

Mock MT5 client + in-memory SQLite + AsyncMock notifier. Pure functions
only; the BlockingScheduler / AsyncIOScheduler main loop is not tested
in pytest (it's a forever-loop).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.detection.fvg import FVG
from src.detection.mss import MSS
from src.detection.setup import RejectedCandidate, Setup
from src.detection.sweep import Sweep
from src.journal.db import get_engine, init_db, session_scope
from src.journal.models import SetupRow
from src.journal.outcome_tracker import Mt5Trade
from src.scheduler import jobs as jobs_module
from src.scheduler.jobs import (
    CycleReport,
    current_killzone,
    run_detection_cycle,
    run_pre_killzone_bias,
    send_killzone_close_heartbeat,
    send_killzone_open_heartbeat,
)

_TZ_PARIS = ZoneInfo("Europe/Paris")
_TZ_UTC = ZoneInfo("UTC")


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        WATCHED_PAIRS=["XAUUSD", "EURUSD"],
        SESSION_ASIA=(2, 0, 6, 0),
        KILLZONE_LONDON=(9, 0, 12, 0),
        KILLZONE_NY=(15, 30, 18, 0),
        SWING_LOOKBACK_H4=2,
        SWING_LOOKBACK_H1=2,
        SWING_LOOKBACK_M5=2,
        MIN_SWING_AMPLITUDE_ATR_MULT_H4=1.0,
        MIN_SWING_AMPLITUDE_ATR_MULT_H1=1.0,
        MIN_SWING_AMPLITUDE_ATR_MULT_M5=1.0,
        BIAS_SWING_COUNT=4,
        BIAS_REQUIRE_H1_CONFIRMATION=False,
        H4_H1_TIME_TOLERANCE_CANDLES_H4=2,
        H4_H1_PRICE_TOLERANCE_FRACTION=0.001,
        SWING_LEVELS_LOOKBACK_COUNT=5,
        SWEEP_RETURN_WINDOW_CANDLES=2,
        SWEEP_DEDUP_TIME_WINDOW_MINUTES=30,
        SWEEP_DEDUP_PRICE_TOLERANCE_FRACTION=0.001,
        MSS_DISPLACEMENT_MULTIPLIER=1.5,
        MSS_DISPLACEMENT_LOOKBACK=20,
        FVG_ATR_PERIOD=14,
        FVG_MIN_SIZE_ATR_MULTIPLIER=0.3,
        MIN_RR=3.0,
        A_PLUS_RR_THRESHOLD=4.0,
        PARTIAL_TP_RR_TARGET=5.0,
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
        CHART_OUTPUT_DIR="/tmp/test_charts_unused",
        CHART_LOOKBACK_CANDLES_M5=80,
        CHART_LOOKFORWARD_CANDLES_M5=10,
        INSTRUMENT_CONFIG={
            "XAUUSD": {"sweep_buffer": 1.0, "equal_hl_tolerance": 0.5, "sl_buffer": 1.0},
            "EURUSD": {"sweep_buffer": 0.0005, "equal_hl_tolerance": 0.0003, "sl_buffer": 0.0005},
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
    """Minimal MT5 double for jobs tests."""

    account: _AccountInfo = field(default_factory=_AccountInfo)
    trades: list[Mt5Trade] = field(default_factory=list)
    ohlc_frames: dict[tuple[str, str], pd.DataFrame] = field(default_factory=dict)
    fail_pair: str | None = None

    def get_account_info(self):
        return self.account

    def get_recent_trades(self, since):
        return list(self.trades)

    def fetch_ohlc(self, symbol, timeframe, n_candles):
        if self.fail_pair == symbol:
            raise RuntimeError(f"forced failure for {symbol}")
        df = self.ohlc_frames.get((symbol, timeframe))
        if df is None:
            # Empty but well-shaped frame so build_setup_candidates short-circuits.
            return pd.DataFrame(
                {
                    "time": pd.to_datetime([], utc=True),
                    "open": [],
                    "high": [],
                    "low": [],
                    "close": [],
                    "volume": [],
                }
            )
        return df


def _make_notifier() -> MagicMock:
    n = MagicMock()
    n.send_setup = AsyncMock(return_value=True)
    n.send_text = AsyncMock(return_value=True)
    n.send_error = AsyncMock(return_value=True)
    return n


@pytest.fixture
def engine():
    e = get_engine(":memory:")
    init_db(e)
    return e


@pytest.fixture
def factory(engine):
    return lambda: session_scope(engine)


# ---------------------------------------------------------------------------
# current_killzone
# ---------------------------------------------------------------------------


def test_current_killzone_inside_london():
    settings = _settings()
    # London = 09:00–12:00 Paris on 2026-04-28 (CEST = UTC+2). UTC range = 07:00–10:00.
    now_utc = datetime(2026, 4, 28, 8, 30, tzinfo=UTC)
    assert current_killzone(now_utc, settings) == "london"


def test_current_killzone_inside_ny():
    settings = _settings()
    # NY = 15:30–18:00 Paris = UTC 13:30–16:00.
    now_utc = datetime(2026, 4, 28, 14, 0, tzinfo=UTC)
    assert current_killzone(now_utc, settings) == "ny"


def test_current_killzone_outside():
    settings = _settings()
    now_utc = datetime(2026, 4, 28, 11, 0, tzinfo=UTC)  # Paris 13:00 — between killzones
    assert current_killzone(now_utc, settings) == "none"


# ---------------------------------------------------------------------------
# run_detection_cycle
# ---------------------------------------------------------------------------


def test_run_detection_cycle_skips_outside_killzone(factory):
    settings = _settings()
    now_utc = datetime(2026, 4, 28, 11, 0, tzinfo=UTC)
    mt5 = _MockMt5()
    notifier = _make_notifier()
    report = run_detection_cycle(mt5, factory, notifier, settings, now_utc=now_utc)
    assert isinstance(report, CycleReport)
    assert report.pairs_processed == 0


def test_run_detection_cycle_skips_when_hard_stop_blocks(factory, monkeypatch):
    settings = _settings(NEWS_BLACKOUT_TODAY=True)
    now_utc = datetime(2026, 4, 28, 14, 0, tzinfo=UTC)  # NY killzone
    mt5 = _MockMt5()
    notifier = _make_notifier()

    report = run_detection_cycle(mt5, factory, notifier, settings, now_utc=now_utc)
    assert report.pairs_processed == 2
    assert report.setups_detected == 0
    assert report.blocks == {"XAUUSD": "news_blackout", "EURUSD": "news_blackout"}


def test_run_detection_cycle_continues_on_per_pair_error(factory):
    settings = _settings()
    now_utc = datetime(2026, 4, 28, 14, 0, tzinfo=UTC)
    mt5 = _MockMt5(fail_pair="XAUUSD")
    notifier = _make_notifier()

    report = run_detection_cycle(mt5, factory, notifier, settings, now_utc=now_utc)
    assert report.pairs_processed == 2
    assert "XAUUSD" in report.errors
    # EURUSD continues (will detect nothing on the empty frames, but no error).
    assert "EURUSD" not in report.errors


def test_run_detection_cycle_persists_setups(factory, monkeypatch):
    """build_setup_candidates returns a setup → it gets journaled and notified."""
    settings = _settings()
    now_utc = datetime(2026, 4, 28, 14, 0, tzinfo=UTC)
    mt5 = _MockMt5()
    notifier = _make_notifier()

    fake_setup = _make_test_setup("XAUUSD", datetime(2026, 4, 28, 14, 0, tzinfo=UTC), kz="ny")
    fake_rejection = RejectedCandidate(
        timestamp_utc=datetime(2026, 4, 28, 14, 5, tzinfo=UTC),
        symbol="XAUUSD",
        rejection_reason="rr_below_threshold",
        sweep_info={"direction": "bearish"},
    )

    def fake_build(*args, **kwargs):
        symbol = kwargs.get("symbol")
        if symbol == "XAUUSD":
            return [fake_setup], [fake_rejection]
        return [], []

    monkeypatch.setattr(jobs_module, "build_setup_candidates", fake_build)
    # Bypass chart rendering — the renderer needs real OHLC.
    captured = []

    def chart_cb(setup, chart_path):
        captured.append(setup)

    report = run_detection_cycle(
        mt5,
        factory,
        notifier,
        settings,
        now_utc=now_utc,
        chart_send_callback=chart_cb,
    )

    assert report.setups_detected == 1
    assert report.setups_notified == 1
    assert report.setups_rejected == 1
    assert len(captured) == 1
    # Setup row + rejected row both persisted.
    with session_scope(get_engine_for(factory)) as s:
        rows = s.query(SetupRow).all()
        uids = sorted(r.setup_uid for r in rows)
        assert any(uid.startswith("rejected:") for uid in uids)
        assert any(not uid.startswith("rejected:") for uid in uids)


def get_engine_for(factory):
    """Re-derive the engine from a session_scope-yielding factory."""
    with factory() as s:
        return s.get_bind()


# ---------------------------------------------------------------------------
# pre_killzone_bias
# ---------------------------------------------------------------------------


def test_pre_killzone_bias_caches_to_daily_state(factory, monkeypatch):
    settings = _settings()
    now_utc = datetime(2026, 4, 28, 6, 55, tzinfo=UTC)  # before London open
    mt5 = _MockMt5()

    monkeypatch.setattr(jobs_module, "compute_daily_bias", lambda **kw: "bullish")

    out = run_pre_killzone_bias(mt5, factory, settings, "london", now_utc=now_utc)
    assert out == {"XAUUSD": "bullish", "EURUSD": "bullish"}

    with factory() as s:
        from src.journal.repository import get_daily_state

        ds = get_daily_state(s, date(2026, 4, 28))
        assert ds is not None
        assert ds.bias_xauusd_london == "bullish"
        assert ds.bias_eurusd_london == "bullish"


# ---------------------------------------------------------------------------
# heartbeats
# ---------------------------------------------------------------------------


def test_killzone_open_heartbeat_format(factory):
    settings = _settings()
    now_utc = datetime(2026, 4, 28, 7, 0, tzinfo=UTC)
    notifier = _make_notifier()

    # Pre-populate daily_state with cached bias.
    with factory() as s:
        from src.journal.repository import upsert_daily_state

        upsert_daily_state(
            s, date(2026, 4, 28), bias_xauusd_london="bullish", bias_eurusd_london="no_trade"
        )

    text = send_killzone_open_heartbeat(notifier, factory, settings, "london", now_utc=now_utc)
    assert "London killzone open" in text
    assert "XAU bullish" in text
    assert "EUR no_trade" in text
    notifier.send_text.assert_awaited_once()


def test_killzone_close_heartbeat_only_if_empty(factory, monkeypatch):
    """When no setup fired during the killzone, the closing heartbeat is sent.

    When a setup did fire, the heartbeat is suppressed.
    """
    settings = _settings()
    now_utc = datetime(2026, 4, 28, 16, 0, tzinfo=UTC)  # NY close
    notifier = _make_notifier()

    # Path A: empty journal → heartbeat sent.
    text = send_killzone_close_heartbeat(notifier, factory, settings, "ny", now_utc=now_utc)
    assert text is not None
    assert "NY killzone closed" in text

    # Path B: insert a notified setup in NY today → suppressed.
    with factory() as s:
        from src.journal.repository import insert_setup

        setup = _make_test_setup("XAUUSD", datetime(2026, 4, 28, 14, 0, tzinfo=UTC), kz="ny")
        insert_setup(s, setup, was_notified=True)

    notifier2 = _make_notifier()
    text2 = send_killzone_close_heartbeat(notifier2, factory, settings, "ny", now_utc=now_utc)
    assert text2 is None
    notifier2.send_text.assert_not_awaited()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_setup(symbol: str, ts: datetime, *, kz: str) -> Setup:
    sweep = Sweep(
        direction="bullish",
        swept_level_price=99.5,
        swept_level_type="asian_low",
        swept_level_strength="structural",
        sweep_candle_time_utc=ts,
        sweep_extreme_price=99.0,
        return_candle_time_utc=ts,
        excursion=0.5,
    )
    fvg = FVG(
        direction="bullish",
        proximal=102.0,
        distal=101.0,
        c1_time_utc=ts,
        c2_time_utc=ts,
        c3_time_utc=ts,
        size=1.0,
        size_atr_ratio=1.0,
    )
    mss = MSS(
        direction="bullish",
        sweep=sweep,
        broken_swing_time_utc=ts,
        broken_swing_price=110.0,
        mss_confirm_candle_time_utc=ts,
        mss_confirm_candle_close=110.5,
        displacement_body_ratio=2.0,
        displacement_candle_time_utc=ts,
    )
    return Setup(
        timestamp_utc=ts,
        symbol=symbol,
        direction="long",
        daily_bias="bullish",
        killzone=kz,  # type: ignore[arg-type]
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
