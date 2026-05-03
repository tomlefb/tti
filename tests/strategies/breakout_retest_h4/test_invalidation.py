"""Unit tests for hard invalidation — spec §2.6."""

from __future__ import annotations

from datetime import UTC, datetime

from src.strategies.breakout_retest_h4.breakout import BreakoutEvent
from src.strategies.breakout_retest_h4.invalidation import daily_key, is_invalid
from src.strategies.breakout_retest_h4.retest import RetestEvent
from src.strategies.breakout_retest_h4.setup import Setup, build_setup
from src.strategies.breakout_retest_h4.swings import Swing


def _setup(*, sl_buffer: float = 0.5) -> Setup:
    swing = Swing(
        timestamp_utc=datetime(2026, 1, 1, tzinfo=UTC),
        price=110.0,
        direction="high",
        bar_index=5,
    )
    breakout = BreakoutEvent(
        swing=swing,
        breakout_bar_timestamp=datetime(2026, 1, 1, 4, tzinfo=UTC),
        breakout_bar_close=112.0,
        direction="long",
    )
    retest = RetestEvent(
        breakout_event=breakout,
        retest_bar_timestamp=datetime(2026, 1, 1, 12, tzinfo=UTC),
        retest_bar_low=109.5,
        retest_bar_high=111.0,
        retest_bar_close=110.5,
    )
    return build_setup(
        retest, instrument="XAUUSD", bias_d1="bullish", sl_buffer=sl_buffer, rr_target=2.0
    )


def test_valid_setup_passes() -> None:
    s = _setup(sl_buffer=0.5)  # risk = 1.5
    assert is_invalid(s, max_risk_distance=5.0, daily_count=0, max_trades_per_day=2) is False


def test_invalid_when_risk_distance_too_large() -> None:
    # Push the SL deep below the retest low so risk grows past the cap.
    s = _setup(sl_buffer=10.0)  # risk = 11.0, cap = 5.0
    assert is_invalid(s, max_risk_distance=5.0, daily_count=0, max_trades_per_day=2) is True


def test_invalid_when_daily_count_at_cap() -> None:
    s = _setup()
    assert is_invalid(s, max_risk_distance=5.0, daily_count=2, max_trades_per_day=2) is True


def test_invalid_when_daily_count_exceeded() -> None:
    s = _setup()
    assert is_invalid(s, max_risk_distance=5.0, daily_count=3, max_trades_per_day=2) is True


def test_valid_when_daily_count_below_cap() -> None:
    s = _setup()
    assert is_invalid(s, max_risk_distance=5.0, daily_count=1, max_trades_per_day=2) is False


def test_daily_key_uses_instrument_and_utc_date() -> None:
    s = _setup()
    key = daily_key(s)
    assert key == ("XAUUSD", datetime(2026, 1, 1, tzinfo=UTC).date())
