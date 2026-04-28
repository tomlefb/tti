"""Unit tests for ``src.detection.order_block``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from src.detection.mss import MSS
from src.detection.order_block import detect_order_block
from src.detection.sweep import Sweep


def _times(start: datetime, n: int) -> list[datetime]:
    return [start + timedelta(minutes=5 * i) for i in range(n)]


def _df(times, opens, highs, lows, closes) -> pd.DataFrame:
    return pd.DataFrame({"time": times, "open": opens, "high": highs, "low": lows, "close": closes})


def _stub_sweep() -> Sweep:
    return Sweep(
        direction="bullish",
        swept_level_price=100.0,
        swept_level_type="x",
        swept_level_strength="structural",
        sweep_candle_time_utc=datetime(2025, 7, 14, 9, 0, tzinfo=UTC),
        sweep_extreme_price=99.0,
        return_candle_time_utc=datetime(2025, 7, 14, 9, 0, tzinfo=UTC),
        excursion=1.0,
    )


def _mss(direction: str, displacement_time: datetime) -> MSS:
    return MSS(
        direction=direction,  # type: ignore[arg-type]
        sweep=_stub_sweep(),
        broken_swing_time_utc=displacement_time,
        broken_swing_price=100.0,
        mss_confirm_candle_time_utc=displacement_time,
        mss_confirm_candle_close=100.0,
        displacement_body_ratio=2.0,
        displacement_candle_time_utc=displacement_time,
    )


def test_bullish_ob_found() -> None:
    start = datetime(2025, 7, 14, 9, 0, tzinfo=UTC)
    n = 20
    times = _times(start, n)
    # All bullish candles except idx 12 (a red candle) and idx 16 (the displacement).
    opens = [100.0] * n
    closes = [101.0] * n
    highs = [101.5] * n
    lows = [99.5] * n
    # idx 12: bearish (close < open) — this is the OB.
    opens[12] = 102.0
    closes[12] = 100.5
    highs[12] = 102.2
    lows[12] = 100.3
    # idx 16: displacement candle (bullish, big)
    opens[16] = 102.0
    closes[16] = 105.0
    highs[16] = 105.1
    lows[16] = 101.9
    df = _df(times, opens, highs, lows, closes)
    mss = _mss("bullish", times[16])
    ob = detect_order_block(df, mss)
    assert ob is not None
    assert ob.direction == "bullish"
    assert ob.candle_time_utc == times[12]
    assert ob.proximal == pytest.approx(102.2)  # candle.high
    assert ob.distal == pytest.approx(100.3)  # candle.low


def test_bearish_ob_found() -> None:
    start = datetime(2025, 7, 14, 9, 0, tzinfo=UTC)
    n = 20
    times = _times(start, n)
    # All bearish except idx 14 (bullish — the OB) and idx 16 (displacement).
    opens = [101.0] * n
    closes = [100.0] * n
    highs = [101.5] * n
    lows = [99.5] * n
    opens[14] = 99.5
    closes[14] = 101.0
    highs[14] = 101.2
    lows[14] = 99.4
    opens[16] = 100.0
    closes[16] = 96.0
    highs[16] = 100.1
    lows[16] = 95.9
    df = _df(times, opens, highs, lows, closes)
    mss = _mss("bearish", times[16])
    ob = detect_order_block(df, mss)
    assert ob is not None
    assert ob.direction == "bearish"
    assert ob.candle_time_utc == times[14]
    assert ob.proximal == pytest.approx(99.4)  # bearish OB → candle.low
    assert ob.distal == pytest.approx(101.2)  # candle.high


def test_no_qualifying_candle_returns_none() -> None:
    start = datetime(2025, 7, 14, 9, 0, tzinfo=UTC)
    n = 20
    times = _times(start, n)
    # All bullish candles → no opposite-coloured candle for a bullish setup.
    opens = [100.0] * n
    closes = [101.0] * n
    highs = [101.5] * n
    lows = [99.5] * n
    df = _df(times, opens, highs, lows, closes)
    mss = _mss("bullish", times[16])
    ob = detect_order_block(df, mss)
    assert ob is None


def test_lookback_bounds_search() -> None:
    start = datetime(2025, 7, 14, 9, 0, tzinfo=UTC)
    n = 30
    times = _times(start, n)
    opens = [100.0] * n
    closes = [101.0] * n
    highs = [101.5] * n
    lows = [99.5] * n
    # Bearish candle at idx 0 — outside default 20-candle lookback for displacement at idx 25.
    opens[0] = 102.0
    closes[0] = 100.0
    highs[0] = 102.5
    lows[0] = 99.9
    # displacement at idx 25
    opens[25] = 100.0
    closes[25] = 105.0
    highs[25] = 105.1
    lows[25] = 99.9
    df = _df(times, opens, highs, lows, closes)
    mss = _mss("bullish", times[25])
    assert detect_order_block(df, mss, lookback_candles=20) is None
    assert detect_order_block(df, mss, lookback_candles=30) is not None


def test_invalid_lookback_raises() -> None:
    start = datetime(2025, 7, 14, 9, 0, tzinfo=UTC)
    df = _df(_times(start, 1), [100], [101], [99], [100.5])
    mss = _mss("bullish", start)
    with pytest.raises(ValueError):
        detect_order_block(df, mss, lookback_candles=0)
