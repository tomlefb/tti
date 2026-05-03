"""Unit tests for retest detection — spec §2.4."""

from __future__ import annotations

import pandas as pd

from src.strategies.breakout_retest_h4.breakout import BreakoutEvent
from src.strategies.breakout_retest_h4.retest import detect_retest
from src.strategies.breakout_retest_h4.swings import Swing


def _ohlc(rows: list[tuple[float, float, float]]) -> pd.DataFrame:
    times = pd.date_range("2026-01-01", periods=len(rows), freq="4h", tz="UTC")
    return pd.DataFrame(
        {
            "time": times,
            "open": [r[2] for r in rows],
            "high": [r[0] for r in rows],
            "low": [r[1] for r in rows],
            "close": [r[2] for r in rows],
        }
    )


def _make_long_breakout(df: pd.DataFrame, level: float, break_idx: int) -> BreakoutEvent:
    swing = Swing(
        timestamp_utc=df["time"].iloc[break_idx - 1].to_pydatetime(),
        price=level,
        direction="high",
        bar_index=break_idx - 1,
    )
    return BreakoutEvent(
        swing=swing,
        breakout_bar_timestamp=df["time"].iloc[break_idx].to_pydatetime(),
        breakout_bar_close=float(df["close"].iloc[break_idx]),
        direction="long",
    )


def _make_short_breakout(df: pd.DataFrame, level: float, break_idx: int) -> BreakoutEvent:
    swing = Swing(
        timestamp_utc=df["time"].iloc[break_idx - 1].to_pydatetime(),
        price=level,
        direction="low",
        bar_index=break_idx - 1,
    )
    return BreakoutEvent(
        swing=swing,
        breakout_bar_timestamp=df["time"].iloc[break_idx].to_pydatetime(),
        breakout_bar_close=float(df["close"].iloc[break_idx]),
        direction="short",
    )


def test_retest_long_touches_level_within_n_retest_bars() -> None:
    # Level = 110. Breakout at idx 0 (close=112). Retest at idx 2:
    # low=109.5, close=110.5 — touches 109.5 <= 110+tol, holds 110.5 > 110.
    rows = [
        (113.0, 111.0, 112.0),  # breakout bar
        (114.0, 111.5, 113.0),  # idx 1 — drift up, no retest
        (112.0, 109.5, 110.5),  # idx 2 — RETEST
        (113.0, 110.5, 111.0),
    ]
    df = _ohlc(rows)
    event = _make_long_breakout(df, level=110.0, break_idx=0)
    retest = detect_retest(df, event, n_retest=8, retest_tolerance=1.0)
    assert retest is not None
    assert retest.retest_bar_timestamp == df["time"].iloc[2].to_pydatetime()
    assert retest.retest_bar_low == 109.5
    assert retest.retest_bar_close == 110.5
    assert retest.breakout_event == event


def test_retest_short_touches_level_within_n_retest_bars() -> None:
    # Level = 90. Breakout at idx 0 (close=89). Retest at idx 2:
    # high=90.5, close=89.5 — touches 90.5 >= 90-tol, holds 89.5 < 90.
    rows = [
        (90.0, 88.0, 89.0),  # breakout bar
        (87.5, 86.0, 86.5),  # idx 1 — far from level, no touch
        (90.5, 88.0, 89.5),  # RETEST
        (89.0, 87.0, 88.0),
    ]
    df = _ohlc(rows)
    event = _make_short_breakout(df, level=90.0, break_idx=0)
    retest = detect_retest(df, event, n_retest=8, retest_tolerance=1.0)
    assert retest is not None
    assert retest.retest_bar_high == 90.5
    assert retest.retest_bar_close == 89.5


def test_retest_must_close_above_level_long() -> None:
    # Touch happens but close is BELOW the level → failed retest.
    # Only one bar after the breakout so no second-chance retest.
    rows = [
        (113.0, 111.0, 112.0),
        (112.0, 109.5, 109.5),  # touches but close 109.5 ≤ 110
    ]
    df = _ohlc(rows)
    event = _make_long_breakout(df, level=110.0, break_idx=0)
    assert detect_retest(df, event, n_retest=8, retest_tolerance=1.0) is None


def test_retest_must_close_below_level_short() -> None:
    rows = [
        (90.0, 88.0, 89.0),
        (90.5, 88.0, 90.0),  # touches but close 90.0 not < 90
    ]
    df = _ohlc(rows)
    event = _make_short_breakout(df, level=90.0, break_idx=0)
    assert detect_retest(df, event, n_retest=8, retest_tolerance=1.0) is None


def test_retest_window_expires_after_n_retest() -> None:
    # Retest happens at idx 4 but n_retest=2 only inspects idx 1, 2.
    rows = [
        (113.0, 111.0, 112.0),
        (114.0, 112.0, 113.0),
        (115.0, 113.0, 114.0),
        (116.0, 114.0, 115.0),
        (112.0, 109.5, 110.5),  # idx 4 — would be retest if window were larger
    ]
    df = _ohlc(rows)
    event = _make_long_breakout(df, level=110.0, break_idx=0)
    assert detect_retest(df, event, n_retest=2, retest_tolerance=1.0) is None
    # With n_retest >= 4 it does fire.
    retest = detect_retest(df, event, n_retest=4, retest_tolerance=1.0)
    assert retest is not None


def test_retest_tolerance_buffer_long() -> None:
    # Wick reaches 110.4 (just above 110). Without tolerance the touch
    # condition `low <= 110` fails. With tolerance >= 0.4 it succeeds.
    rows = [
        (113.0, 111.0, 112.0),
        (113.0, 111.0, 112.0),
        (112.0, 110.4, 111.0),  # low 110.4
    ]
    df = _ohlc(rows)
    event = _make_long_breakout(df, level=110.0, break_idx=0)
    assert detect_retest(df, event, n_retest=8, retest_tolerance=0.0) is None
    retest = detect_retest(df, event, n_retest=8, retest_tolerance=0.5)
    assert retest is not None


def test_retest_tolerance_buffer_short() -> None:
    rows = [
        (90.0, 88.0, 89.0),
        (89.0, 87.5, 88.0),
        (89.6, 88.0, 89.0),  # high 89.6 below 90
    ]
    df = _ohlc(rows)
    event = _make_short_breakout(df, level=90.0, break_idx=0)
    assert detect_retest(df, event, n_retest=8, retest_tolerance=0.0) is None
    retest = detect_retest(df, event, n_retest=8, retest_tolerance=0.5)
    assert retest is not None


def test_retest_picks_first_valid_bar() -> None:
    # Two valid retest bars — first one wins.
    rows = [
        (113.0, 111.0, 112.0),
        (114.0, 111.5, 113.0),
        (112.0, 109.5, 110.5),  # first valid
        (112.0, 109.8, 110.8),  # also valid
    ]
    df = _ohlc(rows)
    event = _make_long_breakout(df, level=110.0, break_idx=0)
    retest = detect_retest(df, event, n_retest=8, retest_tolerance=1.0)
    assert retest is not None
    assert retest.retest_bar_timestamp == df["time"].iloc[2].to_pydatetime()


def test_retest_starts_after_breakout_bar() -> None:
    # The breakout bar itself must NOT be considered a retest, even if
    # its own wick touches and its close holds. No subsequent bar
    # retouches → expected None.
    rows = [
        (115.0, 109.5, 110.5),  # break bar — low 109.5, close 110.5
        (114.0, 112.5, 113.0),  # bar 1 — far above level, no touch
    ]
    df = _ohlc(rows)
    event = _make_long_breakout(df, level=110.0, break_idx=0)
    assert detect_retest(df, event, n_retest=8, retest_tolerance=0.0) is None


def test_now_utc_excludes_in_progress_bar() -> None:
    # Per spec §2.4 pseudo-code: ``if j >= now_idx: break``. The retest
    # scan stops STRICTLY before now_idx (whereas the breakout scan
    # includes now_idx). Reported as a deliberate spec asymmetry, see
    # the strategies/breakout_retest_h4/__init__.py docstring.
    rows = [
        (113.0, 111.0, 112.0),
        (114.0, 111.5, 113.0),
        (112.0, 109.5, 110.5),
    ]
    df = _ohlc(rows)
    event = _make_long_breakout(df, level=110.0, break_idx=0)
    # now_utc set so idx 2 is exactly the "current" bar (not yet
    # treated as observable by the retest scan per the spec).
    now_utc = df["time"].iloc[2].to_pydatetime()
    assert detect_retest(df, event, n_retest=8, retest_tolerance=1.0, now_utc=now_utc) is None
    # After idx 2 closes (now_utc covers idx 3+), retest is observable.
    later = df["time"].iloc[2].to_pydatetime() + pd.Timedelta(hours=8)
    retest = detect_retest(df, event, n_retest=8, retest_tolerance=1.0, now_utc=later)
    assert retest is not None
