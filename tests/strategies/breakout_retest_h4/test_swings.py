"""Unit tests for H4 swing detection — spec §2.2."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from src.strategies.breakout_retest_h4.swings import detect_swings_h4


def _ohlc(highs: list[float], lows: list[float]) -> pd.DataFrame:
    """Build an H4-spaced OHLC frame from highs/lows; close=mid, open=mid."""
    n = len(highs)
    assert len(lows) == n
    mid = [(h + lo) / 2 for h, lo in zip(highs, lows, strict=True)]
    return pd.DataFrame(
        {
            "time": pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC"),
            "open": mid,
            "high": highs,
            "low": lows,
            "close": mid,
        }
    )


def test_detect_swing_high_basic_fractal() -> None:
    # 11 bars; pivot at idx 5 (high=110), needs 5 bars each side strictly
    # lower.
    highs = [100, 101, 102, 103, 104, 110, 105, 104, 103, 102, 101]
    lows = [h - 2 for h in highs]
    df = _ohlc(highs, lows)
    swings_high, swings_low = detect_swings_h4(df, n_swing=5)
    assert len(swings_high) == 1
    assert swings_high[0].bar_index == 5
    assert swings_high[0].price == 110.0
    assert swings_high[0].direction == "high"
    # No swing low in a strictly hill-shaped series.
    assert swings_low == []


def test_detect_swing_low_basic_fractal() -> None:
    lows = [100, 99, 98, 97, 96, 90, 95, 96, 97, 98, 99]
    highs = [lo + 2 for lo in lows]
    df = _ohlc(highs, lows)
    swings_high, swings_low = detect_swings_h4(df, n_swing=5)
    assert len(swings_low) == 1
    assert swings_low[0].bar_index == 5
    assert swings_low[0].price == 90.0
    assert swings_low[0].direction == "low"


def test_swing_requires_n_bars_after_for_confirmation() -> None:
    # Pivot at idx 5 (high=110). With now_utc set to the time the
    # confirmation candle (idx 10) has NOT yet closed, the pivot must
    # not be emitted (spec §2.2 + anti-look-ahead contract).
    highs = [100, 101, 102, 103, 104, 110, 105, 104, 103, 102, 101]
    lows = [h - 2 for h in highs]
    df = _ohlc(highs, lows)

    # Confirmation candle is bar at idx 10 (the 5th bar after pivot
    # idx 5). Its open time + 4h is the moment it has closed.
    conf_open = df["time"].iloc[10].to_pydatetime()
    just_before_close = conf_open + timedelta(hours=3, minutes=59)
    just_at_close = conf_open + timedelta(hours=4)

    swings_high_before, _ = detect_swings_h4(df, n_swing=5, now_utc=just_before_close)
    assert swings_high_before == []

    swings_high_after, _ = detect_swings_h4(df, n_swing=5, now_utc=just_at_close)
    assert len(swings_high_after) == 1
    assert swings_high_after[0].bar_index == 5


def test_no_swing_in_monotonic_series() -> None:
    highs = list(range(20))
    lows = [h - 1 for h in highs]
    df = _ohlc([float(h) for h in highs], [float(lo) for lo in lows])
    swings_high, swings_low = detect_swings_h4(df, n_swing=5)
    assert swings_high == []
    assert swings_low == []


def test_handles_n_swing_3_5_7() -> None:
    # 21 bars centered hill; n_swing=3 / 5 / 7 should all detect the
    # central pivot. With n_swing=7 the frame must extend 7 bars on
    # each side of the pivot.
    n = 21
    highs = [100 + min(i, n - 1 - i) for i in range(n)]
    lows = [h - 2 for h in highs]
    df = _ohlc([float(h) for h in highs], [float(lo) for lo in lows])
    for n_swing in (3, 5, 7):
        swings_high, _ = detect_swings_h4(df, n_swing=n_swing)
        assert len(swings_high) >= 1
        # The strict-fractal definition does not emit a pivot when the
        # bar shares its high with adjacent bars; a centered hill
        # made from `min(i, n-1-i)` is strictly increasing then
        # strictly decreasing → unique pivot at i = (n-1)//2.
        peak_idx = (n - 1) // 2
        assert any(s.bar_index == peak_idx for s in swings_high)


def test_short_frame_returns_empty() -> None:
    # n=2*n_swing, not enough room for a confirmed pivot.
    df = _ohlc([100.0] * 10, [99.0] * 10)
    assert detect_swings_h4(df, n_swing=5) == ([], [])


def test_plateau_yields_no_swing() -> None:
    # Strict comparison rule: equal highs do not produce a swing.
    highs = [100, 101, 102, 103, 104, 110, 110, 104, 103, 102, 101]
    lows = [h - 2 for h in highs]
    df = _ohlc(highs, lows)
    swings_high, _ = detect_swings_h4(df, n_swing=5)
    assert swings_high == []


def test_now_utc_filters_only_observable_pivots() -> None:
    # Two pivots: a high at idx 5 (high=110), a high at idx 17 (high=120).
    # Set now_utc so only the first is observable.
    highs = (
        [100, 101, 102, 103, 104, 110, 105, 104, 103, 102, 101]
        + [102, 103, 104, 105, 106, 107, 120, 108, 107, 106, 105, 104]
    )
    lows = [h - 2 for h in highs]
    df = _ohlc(highs, lows)
    # Confirmation for second pivot is at idx 17+5=22; capping now_utc
    # before that bar's close excludes the second pivot.
    conf2_open = df["time"].iloc[22].to_pydatetime()
    swings_high, _ = detect_swings_h4(
        df, n_swing=5, now_utc=conf2_open + timedelta(minutes=1)
    )
    assert [s.bar_index for s in swings_high] == [5]


def test_swing_timestamp_is_pivot_bar_open_time() -> None:
    highs = [100, 101, 102, 103, 104, 110, 105, 104, 103, 102, 101]
    lows = [h - 2 for h in highs]
    df = _ohlc(highs, lows)
    swings_high, _ = detect_swings_h4(df, n_swing=5)
    expected_ts = df["time"].iloc[5].to_pydatetime()
    assert swings_high[0].timestamp_utc == expected_ts
    assert swings_high[0].timestamp_utc.tzinfo == timezone.utc
