"""Unit tests for ``src.detection.fvg``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from src.detection.fvg import detect_fvgs_in_window


def _times(start: datetime, n: int) -> list[datetime]:
    return [start + timedelta(minutes=5 * i) for i in range(n)]


def _df(times, opens, highs, lows, closes) -> pd.DataFrame:
    return pd.DataFrame({"time": times, "open": opens, "high": highs, "low": lows, "close": closes})


def _baseline(n: int):
    """Helper: ATR-warmable baseline of small bodies/ranges around price 100."""
    opens = [100.0] * n
    highs = [100.5] * n
    lows = [99.5] * n
    closes = [100.0] * n
    return opens, highs, lows, closes


def test_bullish_fvg_passes_size_filter() -> None:
    start = datetime(2025, 7, 14, 9, 0, tzinfo=UTC)
    n = 30
    times = _times(start, n)
    opens, highs, lows, closes = _baseline(n)
    # Insert bullish FVG at indices 25, 26, 27 (c1, c2, c3).
    # c1.high = 101.0; c3.low = 102.0 ⇒ gap [101.0, 102.0] size 1.0 ≫ ATR≈1.0.
    highs[25] = 101.0
    closes[25] = 100.8
    opens[25] = 100.5
    lows[25] = 100.3
    # c2: aggressive bull body
    opens[26] = 101.0
    closes[26] = 102.5
    highs[26] = 102.6
    lows[26] = 100.9
    # c3
    opens[27] = 102.5
    closes[27] = 102.7
    highs[27] = 102.8
    lows[27] = 102.0
    df = _df(times, opens, highs, lows, closes)
    fvgs = detect_fvgs_in_window(
        df,
        start,
        start + timedelta(hours=10),
        "bullish",
        min_size_atr_mult=0.3,
    )
    # The structural FVG we care about: c1=25, c2=26, c3=27 with size 1.0.
    target = next(f for f in fvgs if f.size == pytest.approx(1.0))
    assert target.proximal == pytest.approx(102.0)  # c3.low
    assert target.distal == pytest.approx(101.0)  # c1.high
    assert target.size_atr_ratio >= 0.3


def test_fvg_below_size_filter_dropped() -> None:
    start = datetime(2025, 7, 14, 9, 0, tzinfo=UTC)
    n = 30
    times = _times(start, n)
    opens, highs, lows, closes = _baseline(n)
    # Tiny FVG: c1.high=100.0, c3.low=100.05 ⇒ size 0.05 ≪ ATR≈1.0.
    highs[25] = 100.0
    opens[25] = 99.8
    closes[25] = 99.9
    lows[25] = 99.7
    opens[26] = 100.0
    closes[26] = 100.1
    highs[26] = 100.15
    lows[26] = 99.95
    opens[27] = 100.10
    closes[27] = 100.20
    highs[27] = 100.25
    lows[27] = 100.05
    df = _df(times, opens, highs, lows, closes)
    fvgs = detect_fvgs_in_window(
        df,
        start,
        start + timedelta(hours=10),
        "bullish",
        min_size_atr_mult=0.3,
    )
    assert fvgs == []


def test_bearish_fvg_passes() -> None:
    start = datetime(2025, 7, 14, 9, 0, tzinfo=UTC)
    n = 30
    times = _times(start, n)
    opens, highs, lows, closes = _baseline(n)
    # Bearish FVG: c1.low > c3.high.
    lows[25] = 100.5
    opens[25] = 100.8
    closes[25] = 100.7
    highs[25] = 100.9
    # c2 aggressive bear body
    opens[26] = 100.5
    closes[26] = 99.0
    highs[26] = 100.6
    lows[26] = 98.9
    # c3
    opens[27] = 99.0
    closes[27] = 98.7
    highs[27] = 99.0
    lows[27] = 98.6
    df = _df(times, opens, highs, lows, closes)
    fvgs = detect_fvgs_in_window(
        df,
        start,
        start + timedelta(hours=10),
        "bearish",
        min_size_atr_mult=0.3,
    )
    assert len(fvgs) == 1
    fvg = fvgs[0]
    assert fvg.proximal == pytest.approx(99.0)  # c3.high
    assert fvg.distal == pytest.approx(100.5)  # c1.low
    assert fvg.size == pytest.approx(1.5)


def test_direction_filter_only_returns_requested() -> None:
    start = datetime(2025, 7, 14, 9, 0, tzinfo=UTC)
    n = 40
    times = _times(start, n)
    opens, highs, lows, closes = _baseline(n)
    # Bullish FVG at 18..20 (ATR(14) is warm by then)
    highs[18] = 101.0
    opens[19] = 101.0
    closes[19] = 102.5
    highs[19] = 102.6
    lows[19] = 100.9
    lows[20] = 102.0
    # Bearish FVG at 28..30
    lows[28] = 100.5
    opens[29] = 100.5
    closes[29] = 99.0
    highs[29] = 100.6
    lows[29] = 98.9
    highs[30] = 99.0
    df = _df(times, opens, highs, lows, closes)
    bull = detect_fvgs_in_window(
        df,
        start,
        start + timedelta(hours=10),
        "bullish",
        min_size_atr_mult=0.0,
    )
    bear = detect_fvgs_in_window(
        df,
        start,
        start + timedelta(hours=10),
        "bearish",
        min_size_atr_mult=0.0,
    )
    # At least the structural FVGs (size 1.0 / 1.5) must appear.
    assert any(f.size == pytest.approx(1.0) for f in bull)
    assert any(f.size == pytest.approx(1.5) for f in bear)
    assert all(f.direction == "bullish" for f in bull)
    assert all(f.direction == "bearish" for f in bear)


def test_window_filtering() -> None:
    start = datetime(2025, 7, 14, 9, 0, tzinfo=UTC)
    n = 40
    times = _times(start, n)
    opens, highs, lows, closes = _baseline(n)
    # bullish FVG at 18..20 — c2 at index 19 (ATR warm)
    highs[18] = 101.0
    opens[19] = 101.0
    closes[19] = 102.5
    highs[19] = 102.6
    lows[19] = 100.9
    lows[20] = 102.0
    df = _df(times, opens, highs, lows, closes)

    # Window past the FVG → none.
    out = detect_fvgs_in_window(
        df,
        times[25],
        times[35],
        "bullish",
        min_size_atr_mult=0.0,
    )
    assert out == []

    # Window covering the FVG c2 → the structural one (size 1.0) must appear.
    out = detect_fvgs_in_window(
        df,
        times[18],
        times[20],
        "bullish",
        min_size_atr_mult=0.0,
    )
    assert any(f.size == pytest.approx(1.0) for f in out)


def test_fvg_negative_min_size_raises() -> None:
    start = datetime(2025, 7, 14, 9, 0, tzinfo=UTC)
    df = _df(
        _times(start, 3),
        [100, 100, 100],
        [100.5, 100.5, 100.5],
        [99.5, 99.5, 99.5],
        [100, 100, 100],
    )
    with pytest.raises(ValueError):
        detect_fvgs_in_window(
            df, start, start + timedelta(hours=1), "bullish", min_size_atr_mult=-0.1
        )
    with pytest.raises(ValueError):
        detect_fvgs_in_window(df, start, start + timedelta(hours=1), "diagonal", min_size_atr_mult=0.1)  # type: ignore[arg-type]


def test_fvg_too_short_returns_empty() -> None:
    start = datetime(2025, 7, 14, 9, 0, tzinfo=UTC)
    df = _df(_times(start, 2), [100, 100], [101, 101], [99, 99], [100, 100])
    assert (
        detect_fvgs_in_window(
            df, start, start + timedelta(hours=1), "bullish", min_size_atr_mult=0.0
        )
        == []
    )
