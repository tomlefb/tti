"""Unit tests for breakout detection — spec §2.3."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from src.strategies.breakout_retest_h4.breakout import detect_breakout
from src.strategies.breakout_retest_h4.swings import Swing


def _ohlc(rows: list[tuple[float, float, float]]) -> pd.DataFrame:
    """Build an H4 OHLC frame from (high, low, close) tuples."""
    times = pd.date_range("2026-01-01", periods=len(rows), freq="4h", tz="UTC")
    highs = [r[0] for r in rows]
    lows = [r[1] for r in rows]
    closes = [r[2] for r in rows]
    return pd.DataFrame(
        {
            "time": times,
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
        }
    )


def _swing_high(df: pd.DataFrame, idx: int) -> Swing:
    return Swing(
        timestamp_utc=df["time"].iloc[idx].to_pydatetime(),
        price=float(df["high"].iloc[idx]),
        direction="high",
        bar_index=idx,
    )


def _swing_low(df: pd.DataFrame, idx: int) -> Swing:
    return Swing(
        timestamp_utc=df["time"].iloc[idx].to_pydatetime(),
        price=float(df["low"].iloc[idx]),
        direction="low",
        bar_index=idx,
    )


def test_breakout_long_when_close_above_swing_high_and_bias_bullish() -> None:
    # 11 bars: pivot high at idx 5 (high=110), confirmed at idx 10
    # (5 + n_swing). Then bar idx 11 closes at 112 > 110 → breakout.
    rows: list[tuple[float, float, float]] = [
        (100.5, 99.0, 100.0),
        (101.5, 100.0, 101.0),
        (102.5, 101.0, 102.0),
        (103.5, 102.0, 103.0),
        (104.5, 103.0, 104.0),
        (110.0, 108.0, 109.0),  # pivot
        (105.5, 104.0, 105.0),
        (104.5, 103.0, 104.0),
        (103.5, 102.0, 103.0),
        (102.5, 101.0, 102.0),
        (101.5, 100.0, 101.0),
        (113.0, 111.0, 112.0),  # breakout bar (idx 11)
    ]
    df = _ohlc(rows)
    swing = _swing_high(df, 5)
    locked: set[Swing] = set()
    event = detect_breakout(df, [swing], [], "bullish", locked, n_swing=5)
    assert event is not None
    assert event.direction == "long"
    assert event.swing == swing
    assert event.breakout_bar_close == 112.0
    assert event.breakout_bar_timestamp == df["time"].iloc[11].to_pydatetime()


def test_breakout_short_when_close_below_swing_low_and_bias_bearish() -> None:
    rows: list[tuple[float, float, float]] = [
        (101.5, 100.0, 101.0),
        (100.5, 99.0, 100.0),
        (99.5, 98.0, 99.0),
        (98.5, 97.0, 98.0),
        (97.5, 96.0, 97.0),
        (92.0, 90.0, 91.0),  # pivot low
        (96.5, 95.0, 96.0),
        (97.5, 96.0, 97.0),
        (98.5, 97.0, 98.0),
        (99.5, 98.0, 99.0),
        (100.5, 99.0, 100.0),
        (90.0, 88.0, 89.0),  # breakout bar — close 89 < swing low 90
    ]
    df = _ohlc(rows)
    swing = _swing_low(df, 5)
    event = detect_breakout(df, [], [swing], "bearish", set(), n_swing=5)
    assert event is not None
    assert event.direction == "short"
    assert event.swing == swing
    assert event.breakout_bar_close == 89.0


def test_no_breakout_if_bias_bearish_and_swing_high_broken() -> None:
    # Same fixture as the long test, but bias filter forbids long-only
    # candidates from being eligible — bearish bias only inspects
    # swing lows.
    rows: list[tuple[float, float, float]] = [
        (100.5, 99.0, 100.0),
        (101.5, 100.0, 101.0),
        (102.5, 101.0, 102.0),
        (103.5, 102.0, 103.0),
        (104.5, 103.0, 104.0),
        (110.0, 108.0, 109.0),
        (105.5, 104.0, 105.0),
        (104.5, 103.0, 104.0),
        (103.5, 102.0, 103.0),
        (102.5, 101.0, 102.0),
        (101.5, 100.0, 101.0),
        (113.0, 111.0, 112.0),
    ]
    df = _ohlc(rows)
    swing = _swing_high(df, 5)
    assert detect_breakout(df, [swing], [], "bearish", set(), n_swing=5) is None


def test_no_breakout_under_neutral_bias() -> None:
    rows = [
        (100.5, 99.0, 100.0),
        (101.5, 100.0, 101.0),
        (102.5, 101.0, 102.0),
        (103.5, 102.0, 103.0),
        (104.5, 103.0, 104.0),
        (110.0, 108.0, 109.0),
        (105.5, 104.0, 105.0),
        (104.5, 103.0, 104.0),
        (103.5, 102.0, 103.0),
        (102.5, 101.0, 102.0),
        (101.5, 100.0, 101.0),
        (113.0, 111.0, 112.0),
    ]
    df = _ohlc(rows)
    swing = _swing_high(df, 5)
    assert detect_breakout(df, [swing], [], "neutral", set(), n_swing=5) is None


def test_swing_lock_prevents_double_breakout_on_same_swing() -> None:
    rows: list[tuple[float, float, float]] = [
        (100.5, 99.0, 100.0),
        (101.5, 100.0, 101.0),
        (102.5, 101.0, 102.0),
        (103.5, 102.0, 103.0),
        (104.5, 103.0, 104.0),
        (110.0, 108.0, 109.0),
        (105.5, 104.0, 105.0),
        (104.5, 103.0, 104.0),
        (103.5, 102.0, 103.0),
        (102.5, 101.0, 102.0),
        (101.5, 100.0, 101.0),
        (113.0, 111.0, 112.0),
    ]
    df = _ohlc(rows)
    swing = _swing_high(df, 5)
    locked: set[Swing] = {swing}
    assert detect_breakout(df, [swing], [], "bullish", locked, n_swing=5) is None


def test_breakout_uses_close_only_not_wick() -> None:
    # Wick pierces above the swing but close stays below → no breakout.
    rows = [
        (100.5, 99.0, 100.0),
        (101.5, 100.0, 101.0),
        (102.5, 101.0, 102.0),
        (103.5, 102.0, 103.0),
        (104.5, 103.0, 104.0),
        (110.0, 108.0, 109.0),
        (105.5, 104.0, 105.0),
        (104.5, 103.0, 104.0),
        (103.5, 102.0, 103.0),
        (102.5, 101.0, 102.0),
        (101.5, 100.0, 101.0),
        (115.0, 105.0, 109.5),  # wick to 115, close 109.5 < 110
    ]
    df = _ohlc(rows)
    swing = _swing_high(df, 5)
    assert detect_breakout(df, [swing], [], "bullish", set(), n_swing=5) is None


def test_breakout_picks_first_close_above_level() -> None:
    # Swing at idx 5 (high=110); two bars after confirmation cross —
    # at idx 11 (close=112) and idx 12 (close=115). First wins.
    rows = [
        (100.5, 99.0, 100.0),
        (101.5, 100.0, 101.0),
        (102.5, 101.0, 102.0),
        (103.5, 102.0, 103.0),
        (104.5, 103.0, 104.0),
        (110.0, 108.0, 109.0),
        (105.5, 104.0, 105.0),
        (104.5, 103.0, 104.0),
        (103.5, 102.0, 103.0),
        (102.5, 101.0, 102.0),
        (101.5, 100.0, 101.0),
        (113.0, 111.0, 112.0),
        (116.0, 114.0, 115.0),
    ]
    df = _ohlc(rows)
    swing = _swing_high(df, 5)
    event = detect_breakout(df, [swing], [], "bullish", set(), n_swing=5)
    assert event is not None
    assert event.breakout_bar_close == 112.0


def test_breakout_picks_most_recent_unlocked_swing() -> None:
    # Two swing highs: idx 5 (price=110) and idx 17 (price=120).
    # Both confirmed; the most recent (idx 17) takes priority. Bar
    # idx 23 closes at 122 > 120 → breakout on the recent swing.
    rows = [
        (100.5, 99.0, 100.0),  # 0
        (101.5, 100.0, 101.0),
        (102.5, 101.0, 102.0),
        (103.5, 102.0, 103.0),
        (104.5, 103.0, 104.0),
        (110.0, 108.0, 109.0),  # 5 — swing high 110
        (105.5, 104.0, 105.0),
        (104.5, 103.0, 104.0),
        (103.5, 102.0, 103.0),
        (102.5, 101.0, 102.0),
        (101.5, 100.0, 101.0),  # 10
        (108.0, 106.0, 107.0),
        (109.0, 107.0, 108.0),
        (110.0, 108.0, 109.0),
        (111.0, 109.0, 110.0),
        (112.0, 110.0, 111.0),
        (113.0, 111.0, 112.0),
        (120.0, 118.0, 119.0),  # 17 — swing high 120
        (115.0, 113.0, 114.0),
        (114.0, 112.0, 113.0),
        (113.0, 111.0, 112.0),
        (112.0, 110.0, 111.0),  # 21
        (111.0, 109.0, 110.0),  # 22
        (123.0, 121.0, 122.0),  # 23 — close above 120
    ]
    df = _ohlc(rows)
    swing_old = _swing_high(df, 5)
    swing_new = _swing_high(df, 17)
    event = detect_breakout(df, [swing_old, swing_new], [], "bullish", set(), n_swing=5)
    assert event is not None
    assert event.swing == swing_new


def test_breakout_falls_back_to_older_swing_when_recent_locked() -> None:
    # Same fixture as the most-recent test but lock the recent swing.
    # However, between idx 6..22 no bar closes above 110, so the old
    # swing's range now starts at idx 11 onward. We need a bar that
    # closes above 110 in that interval, but the construction does
    # close above 110 at idx 23 (close=122). That bar also satisfies
    # the older swing.
    rows = [
        (100.5, 99.0, 100.0),
        (101.5, 100.0, 101.0),
        (102.5, 101.0, 102.0),
        (103.5, 102.0, 103.0),
        (104.5, 103.0, 104.0),
        (110.0, 108.0, 109.0),
        (105.5, 104.0, 105.0),
        (104.5, 103.0, 104.0),
        (103.5, 102.0, 103.0),
        (102.5, 101.0, 102.0),
        (101.5, 100.0, 101.0),
        (108.0, 106.0, 107.0),
        (109.0, 107.0, 108.0),
        (110.0, 108.0, 109.0),
        (111.0, 109.0, 110.0),  # close == 110 not strictly above
        (112.0, 110.0, 110.5),  # close 110.5 > 110 → breakout candidate for OLD swing here
        (113.0, 111.0, 112.0),
        (120.0, 118.0, 119.0),
        (115.0, 113.0, 114.0),
        (114.0, 112.0, 113.0),
        (113.0, 111.0, 112.0),
        (112.0, 110.0, 111.0),
        (111.0, 109.0, 110.0),
        (123.0, 121.0, 122.0),
    ]
    df = _ohlc(rows)
    swing_old = _swing_high(df, 5)
    swing_new = _swing_high(df, 17)
    locked = {swing_new}
    event = detect_breakout(df, [swing_old, swing_new], [], "bullish", locked, n_swing=5)
    assert event is not None
    # Old swing is now the most recent unlocked candidate; first close
    # above 110 after its confirmation is at idx 15 (close=110.5).
    assert event.swing == swing_old
    assert event.breakout_bar_close == 110.5


def test_no_breakout_if_no_close_crosses_yet() -> None:
    rows = [
        (100.5, 99.0, 100.0),
        (101.5, 100.0, 101.0),
        (102.5, 101.0, 102.0),
        (103.5, 102.0, 103.0),
        (104.5, 103.0, 104.0),
        (110.0, 108.0, 109.0),
        (105.5, 104.0, 105.0),
        (104.5, 103.0, 104.0),
        (103.5, 102.0, 103.0),
        (102.5, 101.0, 102.0),
        (101.5, 100.0, 101.0),
        (109.5, 108.0, 109.0),  # close 109 < 110
    ]
    df = _ohlc(rows)
    swing = _swing_high(df, 5)
    assert detect_breakout(df, [swing], [], "bullish", set(), n_swing=5) is None


def test_now_utc_caps_breakout_scan_window() -> None:
    rows = [
        (100.5, 99.0, 100.0),
        (101.5, 100.0, 101.0),
        (102.5, 101.0, 102.0),
        (103.5, 102.0, 103.0),
        (104.5, 103.0, 104.0),
        (110.0, 108.0, 109.0),
        (105.5, 104.0, 105.0),
        (104.5, 103.0, 104.0),
        (103.5, 102.0, 103.0),
        (102.5, 101.0, 102.0),
        (101.5, 100.0, 101.0),
        (113.0, 111.0, 112.0),
    ]
    df = _ohlc(rows)
    swing = _swing_high(df, 5)
    # now_utc set such that bar idx 11 has not closed → breakout is
    # not yet observable.
    breakout_open = df["time"].iloc[11].to_pydatetime()
    not_yet_closed = breakout_open  # breakout candle is "in progress"
    assert (
        detect_breakout(
            df,
            [swing],
            [],
            "bullish",
            set(),
            n_swing=5,
            now_utc=not_yet_closed,
        )
        is None
    )
    # After it closes, the breakout is observable.
    just_closed = breakout_open + pd.Timedelta(hours=4)
    event = detect_breakout(
        df, [swing], [], "bullish", set(), n_swing=5, now_utc=just_closed
    )
    assert event is not None
    assert event.breakout_bar_close == 112.0


def test_unconfirmed_swing_is_ignored() -> None:
    # Swing at idx 5 needs n_swing bars to the right confirmed AND
    # at least one bar after confirmation to host a breakout. Spec
    # pseudo-code: ``s.idx + N_SWING < now_idx`` (strict).
    rows = [
        (100.5, 99.0, 100.0),
        (101.5, 100.0, 101.0),
        (102.5, 101.0, 102.0),
        (103.5, 102.0, 103.0),
        (104.5, 103.0, 104.0),
        (110.0, 108.0, 109.0),
        (105.5, 104.0, 105.0),
        (104.5, 103.0, 104.0),
        (103.5, 102.0, 103.0),
        (102.5, 101.0, 102.0),
        (113.0, 111.0, 112.0),  # idx 10: bar at idx == swing.idx + n_swing
    ]
    df = _ohlc(rows)
    swing = _swing_high(df, 5)
    # The strict inequality requires now_idx > swing.idx + n_swing,
    # i.e. there must be at least one bar BEYOND the confirmation
    # bar in which a breakout could fire. Length 11 means now_idx=10,
    # which equals swing.idx + n_swing → swing not eligible yet.
    assert detect_breakout(df, [swing], [], "bullish", set(), n_swing=5) is None


def _confirm_swing_long(df: pd.DataFrame) -> Swing:
    """Helper used by the now_utc tests above."""
    return _swing_high(df, 5)


# Sanity guard: the helpers above produce timestamps whose tzinfo is UTC.
def test_helpers_return_utc_timestamps() -> None:
    rows = [(100.5, 99.0, 100.0)]
    df = _ohlc(rows)
    sw = _swing_high(df, 0)
    assert isinstance(sw.timestamp_utc, datetime)
    assert sw.timestamp_utc.tzinfo == timezone.utc
