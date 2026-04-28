"""Unit tests for ``src.detection.liquidity``."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from src.detection.liquidity import (
    AsianRange,
    DailyLevels,
    EqualLevel,
    SwingLevel,
    asian_range_to_marked_levels,
    daily_levels_to_marked_levels,
    equal_level_to_marked_level,
    find_equal_highs_lows,
    mark_asian_range,
    mark_pdh_pdl,
    mark_swing_levels,
    paris_session_to_utc,
    swing_level_to_marked_level,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _m5_frame(start_utc: datetime, n: int, *, base: float = 100.0, step: float = 0.1):
    """Build a synthetic M5 frame starting at ``start_utc`` with ``n`` candles."""
    times = [start_utc + timedelta(minutes=5 * i) for i in range(n)]
    highs = [base + i * step + 0.5 for i in range(n)]
    lows = [base + i * step - 0.5 for i in range(n)]
    return pd.DataFrame(
        {
            "time": times,
            "open": [base + i * step for i in range(n)],
            "high": highs,
            "low": lows,
            "close": [base + i * step for i in range(n)],
        }
    )


def _h1_frame(start_utc: datetime, highs: list[float], lows: list[float]):
    n = len(highs)
    return pd.DataFrame(
        {
            "time": [start_utc + timedelta(hours=i) for i in range(n)],
            "open": [(h + lo) / 2 for h, lo in zip(highs, lows, strict=True)],
            "high": highs,
            "low": lows,
            "close": [(h + lo) / 2 for h, lo in zip(highs, lows, strict=True)],
        }
    )


def _h4_frame(start_utc: datetime, highs: list[float], lows: list[float]):
    n = len(highs)
    return pd.DataFrame(
        {
            "time": [start_utc + timedelta(hours=4 * i) for i in range(n)],
            "open": [(h + lo) / 2 for h, lo in zip(highs, lows, strict=True)],
            "high": highs,
            "low": lows,
            "close": [(h + lo) / 2 for h, lo in zip(highs, lows, strict=True)],
        }
    )


# ---------------------------------------------------------------------------
# paris_session_to_utc
# ---------------------------------------------------------------------------


def test_paris_session_to_utc_summer() -> None:
    # Mid-July: Paris is UTC+2, so 02:00 Paris = 00:00 UTC.
    s, e = paris_session_to_utc(date(2025, 7, 14), (2, 0, 6, 0))
    assert s == datetime(2025, 7, 14, 0, 0, tzinfo=UTC)
    assert e == datetime(2025, 7, 14, 4, 0, tzinfo=UTC)


def test_paris_session_to_utc_winter() -> None:
    # Mid-December: Paris is UTC+1, so 02:00 Paris = 01:00 UTC.
    s, e = paris_session_to_utc(date(2025, 12, 26), (2, 0, 6, 0))
    assert s == datetime(2025, 12, 26, 1, 0, tzinfo=UTC)
    assert e == datetime(2025, 12, 26, 5, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# mark_asian_range
# ---------------------------------------------------------------------------


def test_mark_asian_range_basic() -> None:
    # Build M5 spanning 23:00 UTC the previous day to 06:00 UTC of the day.
    # Asia 02:00–06:00 Paris in summer = 00:00–04:00 UTC, so we want candles
    # at those times to be in-window.
    start = datetime(2025, 7, 13, 23, 55, tzinfo=UTC)
    df = _m5_frame(start, 80)  # 80 × 5min = 6h40min, covers our window.

    result = mark_asian_range(df, date(2025, 7, 14), session_asia=(2, 0, 6, 0))
    assert result is not None
    assert result.date == date(2025, 7, 14)
    # The window has the highest 'high' at the latest candle in window
    # (because base+i*step+0.5 grows with i).
    assert result.asian_high > result.asian_low


def test_mark_asian_range_no_data_returns_none() -> None:
    # M5 frame entirely outside the Asia window for the target date.
    start = datetime(2025, 7, 14, 12, 0, tzinfo=UTC)
    df = _m5_frame(start, 12)  # 12:00–13:00 UTC; well past Asia window.

    result = mark_asian_range(df, date(2025, 7, 14), session_asia=(2, 0, 6, 0))
    assert result is None


# ---------------------------------------------------------------------------
# mark_pdh_pdl
# ---------------------------------------------------------------------------


def _d1_frame(rows: list[tuple[date, float, float]]):
    return pd.DataFrame(
        {
            "time": [datetime(d.year, d.month, d.day, 0, 0, tzinfo=UTC) for d, _, _ in rows],
            "open": [(h + lo) / 2 for _, h, lo in rows],
            "high": [h for _, h, _ in rows],
            "low": [lo for _, _, lo in rows],
            "close": [(h + lo) / 2 for _, h, lo in rows],
        }
    )


def test_mark_pdh_pdl_normal_tuesday() -> None:
    df = _d1_frame(
        [
            (date(2025, 7, 7), 100.0, 95.0),  # Mon
            (date(2025, 7, 8), 102.0, 96.0),  # Tue
            (date(2025, 7, 9), 105.0, 99.0),  # Wed (target = 9, source = 8)
        ]
    )
    out = mark_pdh_pdl(df, date(2025, 7, 9))
    assert out is not None
    assert out.target_date == date(2025, 7, 9)
    assert out.source_date == date(2025, 7, 8)
    assert out.pdh == 102.0
    assert out.pdl == 96.0


def test_mark_pdh_pdl_walks_back_over_weekend() -> None:
    df = _d1_frame(
        [
            (date(2025, 7, 11), 100.0, 95.0),  # Fri
            # Sat, Sun missing
            (date(2025, 7, 14), 105.0, 99.0),  # Mon
        ]
    )
    out = mark_pdh_pdl(df, date(2025, 7, 14))
    assert out is not None
    # Sunday 13 missing → walks back to Saturday 12 missing → Friday 11.
    assert out.source_date == date(2025, 7, 11)
    assert out.pdh == 100.0
    assert out.pdl == 95.0


def test_mark_pdh_pdl_returns_none_when_no_data() -> None:
    df = _d1_frame([(date(2024, 1, 1), 100.0, 95.0)])
    out = mark_pdh_pdl(df, date(2025, 7, 14), max_walkback_days=3)
    assert out is None


# ---------------------------------------------------------------------------
# mark_swing_levels — multi-TF confluence
# ---------------------------------------------------------------------------


def test_mark_swing_levels_h4_h1_confluence_promotes_to_major() -> None:
    # Hand-craft H4 series with a clear high at index 2.
    # 5 H4 candles starting 2025-07-14T00:00 UTC.
    h4 = _h4_frame(
        datetime(2025, 7, 14, 0, 0, tzinfo=UTC),
        highs=[100, 102, 110, 103, 100],
        lows=[95, 97, 105, 98, 94],
    )
    # H1 series with a high at the same time as the H4 high (within tolerance).
    # H4[2].time = 2025-07-14T08:00 UTC. Place an H1 high at 09:00 UTC
    # (1h apart, within ±2 H4 candles = ±8h).
    h1 = _h1_frame(
        datetime(2025, 7, 14, 0, 0, tzinfo=UTC),
        highs=[100, 101, 102, 103, 105, 108, 109, 111, 110, 109, 105, 100],
        lows=[95, 96, 97, 98, 100, 103, 104, 106, 105, 104, 100, 95],
    )

    levels = mark_swing_levels(
        h4,
        h1,
        as_of_utc=datetime(2025, 7, 15, 0, 0, tzinfo=UTC),
        lookback_h4=1,
        lookback_h1=2,
        min_amplitude_atr_mult=0.0,
        n_swings=5,
        h4_h1_time_tolerance_h4_candles=2,
        h4_h1_price_tolerance_fraction=0.05,  # generous — synthetic data
        atr_period=3,
    )

    # We expect at least one MAJOR swing reflecting the H4 high promoted by
    # the nearby H1 high.
    assert any(lvl.strength == "major" and lvl.type == "high" for lvl in levels)


def test_mark_swing_levels_filters_by_as_of_utc() -> None:
    h4 = _h4_frame(
        datetime(2025, 7, 14, 0, 0, tzinfo=UTC),
        highs=[100, 105, 110, 105, 100],
        lows=[95, 100, 105, 100, 95],
    )
    h1 = _h1_frame(
        datetime(2025, 7, 14, 0, 0, tzinfo=UTC),
        highs=[100, 102, 110, 108, 105],
        lows=[95, 97, 105, 103, 100],
    )
    # as_of cuts off before the peak.
    out = mark_swing_levels(
        h4,
        h1,
        as_of_utc=datetime(2025, 7, 14, 4, 0, tzinfo=UTC),
        lookback_h4=1,
        lookback_h1=1,
        min_amplitude_atr_mult=0.0,
        n_swings=5,
    )
    for lvl in out:
        assert lvl.time_utc <= datetime(2025, 7, 14, 4, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# find_equal_highs_lows
# ---------------------------------------------------------------------------


def _swing(price: float, swing_type: str = "high") -> SwingLevel:
    return SwingLevel(
        type=swing_type,  # type: ignore[arg-type]
        price=price,
        time_utc=datetime(2025, 1, 1, tzinfo=UTC),
        timeframe="H4",
        strength="major_h4_only",
        touches=1,
    )


def test_find_equal_highs_lows_basic_cluster() -> None:
    swings = [_swing(3450.0, "high"), _swing(3450.3, "high"), _swing(3458.0, "high")]
    out = find_equal_highs_lows(swings, equal_hl_tolerance=0.5)
    assert len(out) == 1
    cluster = out[0]
    assert cluster.type == "high"
    assert {m.price for m in cluster.member_levels} == {3450.0, 3450.3}
    # Singleton 3458 is rejected (cluster of size 1).


def test_find_equal_highs_lows_partitions_by_type() -> None:
    swings = [
        _swing(100.0, "high"),
        _swing(100.2, "high"),
        _swing(100.0, "low"),
        _swing(100.2, "low"),
    ]
    out = find_equal_highs_lows(swings, equal_hl_tolerance=0.5)
    types = {c.type for c in out}
    assert types == {"high", "low"}
    assert len(out) == 2


def test_find_equal_highs_lows_no_cluster_when_singletons() -> None:
    swings = [_swing(100.0), _swing(110.0), _swing(120.0)]
    out = find_equal_highs_lows(swings, equal_hl_tolerance=0.5)
    assert out == []


def test_find_equal_highs_lows_negative_tolerance_raises() -> None:
    with pytest.raises(ValueError):
        find_equal_highs_lows([], equal_hl_tolerance=-0.1)


# ---------------------------------------------------------------------------
# Converters
# ---------------------------------------------------------------------------


def test_asian_range_to_marked_levels_returns_two() -> None:
    ar = AsianRange(
        date=date(2025, 7, 14),
        asian_high=105.0,
        asian_low=99.0,
        asian_high_time_utc=datetime(2025, 7, 14, 1, tzinfo=UTC),
        asian_low_time_utc=datetime(2025, 7, 14, 2, tzinfo=UTC),
    )
    out = asian_range_to_marked_levels(ar)
    assert len(out) == 2
    assert {lvl.type for lvl in out} == {"high", "low"}
    assert all(lvl.strength == "structural" for lvl in out)


def test_asian_range_to_marked_levels_handles_none() -> None:
    assert asian_range_to_marked_levels(None) == []


def test_daily_levels_to_marked_levels_basic() -> None:
    d = DailyLevels(
        target_date=date(2025, 7, 14),
        pdh=110.0,
        pdl=95.0,
        source_date=date(2025, 7, 11),
    )
    out = daily_levels_to_marked_levels(d)
    assert {lvl.label for lvl in out} == {"pdh", "pdl"}


def test_swing_level_to_marked_level_label_format() -> None:
    s = SwingLevel(
        type="high",
        price=4218.4,
        time_utc=datetime(2025, 10, 15, tzinfo=UTC),
        timeframe="H4",
        strength="major",
        touches=2,
    )
    out = swing_level_to_marked_level(s)
    assert out.label == "swing_h4_high"
    assert out.strength == "major"


def test_equal_level_to_marked_level_uses_avg_price() -> None:
    eq = EqualLevel(
        type="high",
        cluster_avg_price=3450.15,
        member_levels=[_swing(3450.0), _swing(3450.3)],
        cluster_min_price=3450.0,
        cluster_max_price=3450.3,
    )
    out = equal_level_to_marked_level(eq)
    assert out.price == 3450.15
    assert out.strength == "major"
