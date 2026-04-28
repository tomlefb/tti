"""Unit tests for ``src.detection.mss``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from src.detection.mss import _mean_body, detect_mss
from src.detection.sweep import Sweep


def _times(start: datetime, n: int) -> list[datetime]:
    return [start + timedelta(minutes=5 * i) for i in range(n)]


def _df(times, opens, highs, lows, closes) -> pd.DataFrame:
    return pd.DataFrame({"time": times, "open": opens, "high": highs, "low": lows, "close": closes})


def _sweep(time_utc: datetime, direction: str, extreme: float, level_price: float) -> Sweep:
    return Sweep(
        direction=direction,  # type: ignore[arg-type]
        swept_level_price=level_price,
        swept_level_type="asian_low" if direction == "bullish" else "asian_high",
        swept_level_strength="structural",
        sweep_candle_time_utc=time_utc,
        sweep_extreme_price=extreme,
        return_candle_time_utc=time_utc,
        excursion=abs(level_price - extreme),
    )


# ---------------------------------------------------------------------------
# Bullish MSS
# ---------------------------------------------------------------------------


def test_bullish_mss_happy_path() -> None:
    """Build a frame: long quiet baseline, then sweep low + impulsive break of swing high."""
    start = datetime(2025, 7, 14, 7, 0, tzinfo=UTC)
    n = 60
    times = _times(start, n)

    # Phase 1 (0..29): quiet baseline around 100, small bodies (≈0.2).
    opens = [100.0] * n
    highs = [100.5] * n
    lows = [99.5] * n
    closes = [100.2] * n

    # Insert a swing HIGH at index 18: high 102.5 (lookback=2 + amplitude filter ⇒ pivot).
    # Ensure low at indexes 16-17 dipped to make amplitude vs prior-low big enough.
    lows[10] = 97.0
    closes[10] = 99.0
    opens[10] = 99.0
    highs[10] = 99.5
    # Pivot HIGH:
    highs[18] = 102.5
    closes[18] = 102.4
    opens[18] = 100.5
    # bring its neighbours below it
    for k in (16, 17, 19, 20):
        highs[k] = 100.5
    # Phase 2 (30..32): sweep of low 97.0 (already structural in Phase 1):
    # Actually let's place the swept level at 97 (manufactured).
    swept_level = 97.0
    sweep_idx = 30
    lows[sweep_idx] = 96.0
    closes[sweep_idx] = 97.5  # returns above
    opens[sweep_idx] = 97.5
    highs[sweep_idx] = 97.5

    # Phase 3 (31..36): impulsive bull move that closes above 102.5.
    for k in range(31, 36):
        opens[k] = 100.0
        closes[k] = 100.0
        highs[k] = 100.5
        lows[k] = 99.5
    # MSS candle at index 35: big bullish body 100.0 → 103.0 (body 3.0 vs ~0.2 baseline ⇒ ratio ≈ 15).
    opens[35] = 100.0
    closes[35] = 103.0
    highs[35] = 103.1
    lows[35] = 99.9

    df = _df(times, opens, highs, lows, closes)
    sweep = _sweep(times[sweep_idx], "bullish", extreme=96.0, level_price=swept_level)

    mss = detect_mss(
        df,
        sweep,
        swing_lookback_m5=2,
        min_swing_amplitude_atr_mult=1.0,
        displacement_multiplier=1.5,
        displacement_lookback=20,
        max_lookforward_minutes=120,
    )
    assert mss is not None
    assert mss.direction == "bullish"
    assert mss.broken_swing_price == pytest.approx(102.5)
    assert mss.mss_confirm_candle_time_utc == times[35]
    assert mss.displacement_body_ratio >= 1.5


def test_bullish_mss_no_swing_high_returns_none() -> None:
    """Boring flat market — no significant swing high to break."""
    start = datetime(2025, 7, 14, 7, 0, tzinfo=UTC)
    n = 50
    times = _times(start, n)
    opens = [100.0] * n
    highs = [100.5] * n
    lows = [99.5] * n
    closes = [100.0] * n
    # sweep at index 30
    lows[30] = 98.0
    closes[30] = 100.5
    df = _df(times, opens, highs, lows, closes)
    sweep = _sweep(times[30], "bullish", extreme=98.0, level_price=99.0)

    mss = detect_mss(
        df,
        sweep,
        swing_lookback_m5=2,
        min_swing_amplitude_atr_mult=1.0,
        displacement_multiplier=1.5,
        displacement_lookback=20,
    )
    assert mss is None


def test_bullish_mss_weak_displacement_returns_none() -> None:
    """Same setup as the happy path but the breakout candle is tiny."""
    start = datetime(2025, 7, 14, 7, 0, tzinfo=UTC)
    n = 60
    times = _times(start, n)
    opens = [100.0] * n
    highs = [102.0] * n  # uniform highs — large baseline body context
    lows = [99.0] * n
    closes = [101.0] * n  # body 1.0 each candle (uniform)
    # Pivot high at idx 18: 105.
    highs[18] = 105.0
    closes[18] = 104.5
    opens[18] = 102.5
    for k in (16, 17, 19, 20):
        highs[k] = 103.0
    # Make lows differ enough for amplitude
    lows[10] = 95.0
    # sweep at idx 30
    lows[30] = 94.0
    closes[30] = 96.0
    opens[30] = 96.0
    highs[30] = 96.0
    # Tiny break at idx 35: close 105.5 (above pivot) but body only 0.5 ≪ 1.5 × ~1.0 baseline.
    opens[35] = 105.0
    closes[35] = 105.5
    highs[35] = 105.6
    lows[35] = 104.9

    df = _df(times, opens, highs, lows, closes)
    sweep = _sweep(times[30], "bullish", extreme=94.0, level_price=95.0)
    mss = detect_mss(
        df,
        sweep,
        swing_lookback_m5=2,
        min_swing_amplitude_atr_mult=1.0,
        displacement_multiplier=1.5,
        displacement_lookback=20,
    )
    assert mss is None


# ---------------------------------------------------------------------------
# Bearish MSS — symmetric
# ---------------------------------------------------------------------------


def test_bearish_mss_happy_path() -> None:
    start = datetime(2025, 7, 14, 7, 0, tzinfo=UTC)
    n = 60
    times = _times(start, n)
    opens = [100.0] * n
    highs = [100.5] * n
    lows = [99.5] * n
    closes = [99.8] * n
    # Swing LOW at idx 18 at 97.5
    lows[18] = 97.5
    closes[18] = 97.6
    opens[18] = 99.5
    for k in (16, 17, 19, 20):
        lows[k] = 99.5
    # Big high beforehand for amplitude
    highs[10] = 103.0
    closes[10] = 101.0
    # Sweep of high 103 at idx 30 (sweep of high ⇒ bearish)
    highs[30] = 104.0
    closes[30] = 102.5
    opens[30] = 102.5
    lows[30] = 102.5
    # Quiet candles 31..34
    for k in range(31, 35):
        opens[k] = 100.0
        highs[k] = 100.5
        lows[k] = 99.5
        closes[k] = 100.0
    # MSS candle idx 35: big bearish body, closes 96.5 (below 97.5)
    opens[35] = 100.0
    closes[35] = 96.5
    highs[35] = 100.1
    lows[35] = 96.4
    df = _df(times, opens, highs, lows, closes)
    sweep = _sweep(times[30], "bearish", extreme=104.0, level_price=103.0)

    mss = detect_mss(
        df,
        sweep,
        swing_lookback_m5=2,
        min_swing_amplitude_atr_mult=1.0,
        displacement_multiplier=1.5,
        displacement_lookback=20,
    )
    assert mss is not None
    assert mss.direction == "bearish"
    assert mss.broken_swing_price == pytest.approx(97.5)


def test_mss_outside_lookforward_returns_none() -> None:
    """If the breakout happens beyond max_lookforward, no MSS is reported."""
    start = datetime(2025, 7, 14, 7, 0, tzinfo=UTC)
    n = 80
    times = _times(start, n)
    opens = [100.0] * n
    highs = [100.5] * n
    lows = [99.5] * n
    closes = [100.0] * n
    highs[18] = 102.5
    closes[18] = 102.4
    opens[18] = 100.5
    lows[10] = 97.0
    # sweep at idx 30
    lows[30] = 96.0
    closes[30] = 97.5
    # Breakout at idx 70 — far past max_lookforward of 60 minutes (= 12 candles after idx 30).
    opens[70] = 100.0
    closes[70] = 105.0
    highs[70] = 105.0
    lows[70] = 99.9
    df = _df(times, opens, highs, lows, closes)
    sweep = _sweep(times[30], "bullish", extreme=96.0, level_price=97.0)

    mss = detect_mss(
        df,
        sweep,
        swing_lookback_m5=2,
        min_swing_amplitude_atr_mult=1.0,
        displacement_multiplier=1.5,
        displacement_lookback=20,
        max_lookforward_minutes=60,
    )
    assert mss is None


def test_mean_body_helper() -> None:
    n = 5
    times = _times(datetime(2025, 7, 14, 7, 0, tzinfo=UTC), n)
    opens = [100, 100, 100, 100, 100]
    closes = [100.5, 99.5, 101.0, 99.0, 102.0]
    highs = [c + 0.1 for c in closes]
    lows = [min(o, c) - 0.1 for o, c in zip(opens, closes, strict=False)]
    df = _df(times, opens, highs, lows, closes)
    # bodies = [0.5, 0.5, 1.0, 1.0, 2.0]; mean of last 3 ending at idx 5 = mean(1.0, 1.0, 2.0) = 4/3
    assert _mean_body(df, end_idx=5, lookback=3) == pytest.approx(4.0 / 3.0)


def test_mss_negative_params_raise() -> None:
    df = _df(_times(datetime(2025, 7, 14, 7, 0, tzinfo=UTC), 1), [1], [1], [1], [1])
    sweep = _sweep(datetime(2025, 7, 14, 7, 0, tzinfo=UTC), "bullish", 0.0, 1.0)
    with pytest.raises(ValueError):
        detect_mss(
            df,
            sweep,
            swing_lookback_m5=2,
            min_swing_amplitude_atr_mult=1.0,
            displacement_multiplier=1.5,
            displacement_lookback=0,
        )
    with pytest.raises(ValueError):
        detect_mss(
            df,
            sweep,
            swing_lookback_m5=2,
            min_swing_amplitude_atr_mult=1.0,
            displacement_multiplier=0.0,
            displacement_lookback=20,
        )
