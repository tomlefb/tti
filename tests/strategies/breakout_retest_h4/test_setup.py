"""Unit tests for the setup builder — spec §2.5."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.strategies.breakout_retest_h4.breakout import BreakoutEvent
from src.strategies.breakout_retest_h4.retest import RetestEvent
from src.strategies.breakout_retest_h4.setup import build_setup
from src.strategies.breakout_retest_h4.swings import Swing


def _swing(direction: str, level: float) -> Swing:
    return Swing(
        timestamp_utc=datetime(2026, 1, 1, tzinfo=UTC),
        price=level,
        direction=direction,  # type: ignore[arg-type]
        bar_index=5,
    )


def _make_long_retest(
    *,
    level: float = 110.0,
    retest_low: float = 109.5,
    retest_close: float = 110.5,
) -> RetestEvent:
    breakout = BreakoutEvent(
        swing=_swing("high", level),
        breakout_bar_timestamp=datetime(2026, 1, 1, 4, tzinfo=UTC),
        breakout_bar_close=112.0,
        direction="long",
    )
    return RetestEvent(
        breakout_event=breakout,
        retest_bar_timestamp=datetime(2026, 1, 1, 12, tzinfo=UTC),
        retest_bar_low=retest_low,
        retest_bar_high=retest_close + 1.0,
        retest_bar_close=retest_close,
    )


def _make_short_retest(
    *,
    level: float = 90.0,
    retest_high: float = 90.5,
    retest_close: float = 89.5,
) -> RetestEvent:
    breakout = BreakoutEvent(
        swing=_swing("low", level),
        breakout_bar_timestamp=datetime(2026, 1, 1, 4, tzinfo=UTC),
        breakout_bar_close=89.0,
        direction="short",
    )
    return RetestEvent(
        breakout_event=breakout,
        retest_bar_timestamp=datetime(2026, 1, 1, 12, tzinfo=UTC),
        retest_bar_low=retest_close - 1.0,
        retest_bar_high=retest_high,
        retest_bar_close=retest_close,
    )


def test_setup_long_entry_sl_tp_arithmetic() -> None:
    retest = _make_long_retest(retest_low=109.5, retest_close=110.5)
    setup = build_setup(
        retest, instrument="XAUUSD", bias_d1="bullish", sl_buffer=0.5, rr_target=2.0
    )
    # Entry = retest close = 110.5
    # SL = retest_low - sl_buffer = 109.0
    # Risk = 1.5
    # TP = entry + risk * rr = 110.5 + 3.0 = 113.5
    assert setup.entry_price == pytest.approx(110.5)
    assert setup.stop_loss == pytest.approx(109.0)
    assert setup.take_profit == pytest.approx(113.5)
    assert setup.direction == "long"
    assert setup.instrument == "XAUUSD"
    assert setup.bias_d1 == "bullish"


def test_setup_short_entry_sl_tp_arithmetic() -> None:
    retest = _make_short_retest(retest_high=90.5, retest_close=89.5)
    setup = build_setup(
        retest, instrument="NDX100", bias_d1="bearish", sl_buffer=0.5, rr_target=2.0
    )
    # Entry = 89.5; SL = 90.5 + 0.5 = 91.0; Risk = 1.5; TP = 89.5 - 3.0 = 86.5
    assert setup.entry_price == pytest.approx(89.5)
    assert setup.stop_loss == pytest.approx(91.0)
    assert setup.take_profit == pytest.approx(86.5)
    assert setup.direction == "short"
    assert setup.bias_d1 == "bearish"


def test_setup_rr_matches_rr_target() -> None:
    retest = _make_long_retest(retest_low=109.0, retest_close=110.0)
    setup = build_setup(
        retest, instrument="XAUUSD", bias_d1="bullish", sl_buffer=0.0, rr_target=3.0
    )
    assert setup.risk_reward == pytest.approx(3.0)


def test_setup_timestamp_is_retest_bar() -> None:
    retest = _make_long_retest()
    setup = build_setup(
        retest, instrument="XAUUSD", bias_d1="bullish", sl_buffer=0.5, rr_target=2.0
    )
    assert setup.timestamp_utc == retest.retest_bar_timestamp


def test_setup_carries_breakout_and_retest_events() -> None:
    retest = _make_long_retest()
    setup = build_setup(
        retest, instrument="XAUUSD", bias_d1="bullish", sl_buffer=0.5, rr_target=2.0
    )
    assert setup.retest_event == retest
    assert setup.breakout_event == retest.breakout_event


def test_setup_sl_buffer_can_be_zero() -> None:
    retest = _make_long_retest(retest_low=109.5, retest_close=110.5)
    setup = build_setup(
        retest, instrument="XAUUSD", bias_d1="bullish", sl_buffer=0.0, rr_target=2.0
    )
    assert setup.stop_loss == pytest.approx(109.5)
    # Risk = 1.0; TP = 110.5 + 2.0 = 112.5
    assert setup.take_profit == pytest.approx(112.5)


def test_setup_raises_on_zero_risk() -> None:
    # Retest close == retest low - 0 (sl_buffer 0) → risk = 0; degenerate.
    retest = _make_long_retest(retest_low=110.5, retest_close=110.5)
    with pytest.raises(ValueError, match="risk"):
        build_setup(
            retest,
            instrument="XAUUSD",
            bias_d1="bullish",
            sl_buffer=0.0,
            rr_target=2.0,
        )
