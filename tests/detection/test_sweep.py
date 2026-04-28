"""Unit tests for ``src.detection.sweep``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from src.detection.liquidity import MarkedLevel
from src.detection.sweep import detect_sweeps


def _m5(
    times: list[datetime],
    highs: list[float],
    lows: list[float],
    closes: list[float],
) -> pd.DataFrame:
    n = len(times)
    assert len(highs) == n == len(lows) == len(closes)
    return pd.DataFrame(
        {
            "time": times,
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
        }
    )


def _times(start: datetime, n: int) -> list[datetime]:
    return [start + timedelta(minutes=5 * i) for i in range(n)]


# ---------------------------------------------------------------------------
# Bullish sweep
# ---------------------------------------------------------------------------


def test_bullish_sweep_same_candle_return() -> None:
    start = datetime(2025, 7, 14, 9, 0, tzinfo=UTC)
    # Candle 0: low pierces level 100 by 2 (buffer 1) → wick at 98; close at 101 (above level).
    # Candle 1: above level too — should NOT generate a second sweep on its own.
    df = _m5(
        times=_times(start, 3),
        highs=[101.5, 102.0, 102.5],
        lows=[98.0, 100.5, 101.0],
        closes=[101.0, 101.5, 102.0],
    )
    levels = [MarkedLevel(price=100.0, type="low", label="asian_low", strength="structural")]
    sweeps = detect_sweeps(
        df,
        levels,
        killzone_window_utc=(start, start + timedelta(minutes=15)),
        sweep_buffer=1.0,
        return_window_candles=2,
    )
    assert len(sweeps) == 1
    s = sweeps[0]
    assert s.direction == "bullish"
    assert s.swept_level_price == 100.0
    assert s.sweep_extreme_price == 98.0
    assert s.return_candle_time_utc == s.sweep_candle_time_utc  # same-candle return
    assert s.excursion == 2.0


def test_bullish_sweep_no_return_within_window() -> None:
    start = datetime(2025, 7, 14, 9, 0, tzinfo=UTC)
    # Wick pierces but close stays below level for the entire window.
    df = _m5(
        times=_times(start, 4),
        highs=[99.0, 99.5, 99.5, 99.5],
        lows=[98.0, 98.5, 98.5, 98.5],
        closes=[99.0, 99.0, 99.0, 99.0],
    )
    levels = [MarkedLevel(price=100.0, type="low", label="asian_low", strength="structural")]
    sweeps = detect_sweeps(
        df,
        levels,
        killzone_window_utc=(start, start + timedelta(minutes=20)),
        sweep_buffer=1.0,
        return_window_candles=2,
    )
    assert sweeps == []


def test_bullish_sweep_return_two_candles_later() -> None:
    start = datetime(2025, 7, 14, 9, 0, tzinfo=UTC)
    # Candle 0: pierces level 100 → low 97. Close = 99 (still below).
    # Candle 1: close = 99.5 (still below).
    # Candle 2: close = 100.5 (above level — return!).
    df = _m5(
        times=_times(start, 4),
        highs=[100.5, 100.5, 101.0, 101.0],
        lows=[97.0, 99.5, 99.6, 100.0],  # only candle 0 pierces (level-buffer=99)
        closes=[99.0, 99.5, 100.5, 100.7],
    )
    levels = [MarkedLevel(price=100.0, type="low", label="pdl", strength="structural")]
    sweeps = detect_sweeps(
        df,
        levels,
        killzone_window_utc=(start, start + timedelta(minutes=20)),
        sweep_buffer=1.0,
        return_window_candles=2,
    )
    assert len(sweeps) == 1
    assert sweeps[0].return_candle_time_utc == start + timedelta(minutes=10)


def test_bullish_sweep_buffer_too_small_no_sweep() -> None:
    start = datetime(2025, 7, 14, 9, 0, tzinfo=UTC)
    # Wick goes from 100 to 99.5 (only 0.5 below level), buffer is 1.0 → no sweep.
    df = _m5(
        times=_times(start, 2),
        highs=[101.0, 101.0],
        lows=[99.5, 100.0],
        closes=[100.5, 100.5],
    )
    levels = [MarkedLevel(price=100.0, type="low", label="asian_low", strength="structural")]
    sweeps = detect_sweeps(
        df,
        levels,
        killzone_window_utc=(start, start + timedelta(minutes=10)),
        sweep_buffer=1.0,
        return_window_candles=2,
    )
    assert sweeps == []


# ---------------------------------------------------------------------------
# Bearish sweep
# ---------------------------------------------------------------------------


def test_bearish_sweep_same_candle() -> None:
    start = datetime(2025, 7, 14, 9, 0, tzinfo=UTC)
    df = _m5(
        times=_times(start, 2),
        highs=[102.0, 100.5],
        lows=[99.0, 99.0],
        closes=[99.5, 99.5],
    )
    levels = [MarkedLevel(price=100.0, type="high", label="asian_high", strength="structural")]
    sweeps = detect_sweeps(
        df,
        levels,
        killzone_window_utc=(start, start + timedelta(minutes=10)),
        sweep_buffer=1.0,
        return_window_candles=2,
    )
    assert len(sweeps) == 1
    assert sweeps[0].direction == "bearish"
    assert sweeps[0].sweep_extreme_price == 102.0


# ---------------------------------------------------------------------------
# Multiple levels in same killzone
# ---------------------------------------------------------------------------


def test_multiple_sweeps_returned() -> None:
    start = datetime(2025, 7, 14, 9, 0, tzinfo=UTC)
    df = _m5(
        times=_times(start, 4),
        highs=[103.0, 100.5, 100.5, 100.5],
        lows=[98.0, 99.5, 99.5, 99.5],
        closes=[99.5, 100.5, 100.5, 100.5],
    )
    levels = [
        MarkedLevel(price=100.0, type="low", label="asian_low", strength="structural"),
        MarkedLevel(price=102.0, type="high", label="pdh", strength="structural"),
    ]
    sweeps = detect_sweeps(
        df,
        levels,
        killzone_window_utc=(start, start + timedelta(minutes=20)),
        sweep_buffer=1.0,
        return_window_candles=2,
    )
    # Candle 0 pierces both low (98<=100-1) and high (103>=102+1) and close
    # 99.5 returns above the low and below the high → 2 sweeps on candle 0.
    assert len(sweeps) == 2
    directions = {s.direction for s in sweeps}
    assert directions == {"bullish", "bearish"}


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_inputs_return_empty() -> None:
    start = datetime(2025, 7, 14, 9, 0, tzinfo=UTC)
    df = _m5(_times(start, 0), [], [], [])
    sweeps = detect_sweeps(
        df,
        [MarkedLevel(price=100.0, type="low", label="x", strength="major")],
        killzone_window_utc=(start, start + timedelta(minutes=10)),
        sweep_buffer=1.0,
        return_window_candles=2,
    )
    assert sweeps == []

    df2 = _m5(_times(start, 3), [101] * 3, [99] * 3, [100] * 3)
    assert (
        detect_sweeps(
            df2,
            [],
            killzone_window_utc=(start, start + timedelta(minutes=10)),
            sweep_buffer=1.0,
            return_window_candles=2,
        )
        == []
    )


def test_no_candles_in_killzone() -> None:
    start = datetime(2025, 7, 14, 9, 0, tzinfo=UTC)
    df = _m5(_times(start, 3), [98, 99, 100], [97, 98, 99], [97.5, 98.5, 99.5])
    sweeps = detect_sweeps(
        df,
        [MarkedLevel(price=99.0, type="low", label="x", strength="major")],
        killzone_window_utc=(start + timedelta(hours=4), start + timedelta(hours=5)),
        sweep_buffer=0.5,
        return_window_candles=2,
    )
    assert sweeps == []


def test_negative_params_raise() -> None:
    start = datetime(2025, 7, 14, 9, 0, tzinfo=UTC)
    df = _m5(_times(start, 3), [101] * 3, [99] * 3, [100] * 3)
    with pytest.raises(ValueError):
        detect_sweeps(
            df,
            [],
            (start, start),
            sweep_buffer=-1.0,
            return_window_candles=2,
        )
    with pytest.raises(ValueError):
        detect_sweeps(
            df,
            [],
            (start, start),
            sweep_buffer=1.0,
            return_window_candles=-1,
        )
