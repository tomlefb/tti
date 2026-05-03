"""Unit tests for the §2.3 penetration filter and §2.4 exhaustion filter."""

from __future__ import annotations

import math
from datetime import datetime

import pandas as pd
import pytest

from src.strategies.mean_reversion_bb_h4.filters import (
    is_exhaustion_candle,
    passes_penetration,
)
from src.strategies.mean_reversion_bb_h4.types import ExcessEvent


def _excess(
    *,
    direction: str = "upper",
    close: float = 110.0,
    high: float = 112.0,
    low: float = 108.0,
    bb_level: float = 105.0,
) -> ExcessEvent:
    return ExcessEvent(
        timestamp_utc=datetime(2026, 1, 5, 8, 0),
        bar_index=20,
        direction=direction,  # type: ignore[arg-type]
        close=close,
        high=high,
        low=low,
        bb_level=bb_level,
        penetration_atr=float("nan"),
    )


# ---------------------------------------------------------------------------
# §2.3 ATR penetration
# ---------------------------------------------------------------------------


def test_penetration_passes_when_above_atr_threshold() -> None:
    """penetration = close - bb_level = 110 - 105 = 5; atr = 10;
    threshold = 0.3 * atr = 3 → 5 >= 3 → passes."""
    ev = _excess(direction="upper", close=110.0, bb_level=105.0)
    ok, pen_atr = passes_penetration(ev, atr_at_bar=10.0, min_pen_atr_mult=0.3)
    assert ok is True
    assert pen_atr == pytest.approx(0.5)  # 5 / 10


def test_penetration_fails_when_too_shallow() -> None:
    """penetration = 1.0; atr = 10; threshold = 0.3 * 10 = 3 → fails."""
    ev = _excess(direction="upper", close=106.0, bb_level=105.0)
    ok, pen_atr = passes_penetration(ev, atr_at_bar=10.0, min_pen_atr_mult=0.3)
    assert ok is False
    assert pen_atr == pytest.approx(0.1)


def test_penetration_lower_direction_uses_correct_sign() -> None:
    """For 'lower': penetration = bb_level - close. Must be positive."""
    ev = _excess(direction="lower", close=95.0, bb_level=100.0)
    ok, pen_atr = passes_penetration(ev, atr_at_bar=10.0, min_pen_atr_mult=0.3)
    assert ok is True
    assert pen_atr == pytest.approx(0.5)


def test_penetration_zero_atr_is_safe() -> None:
    """ATR can be zero on a flat fixture — must not divide-by-zero."""
    ev = _excess(direction="upper", close=110.0, bb_level=105.0)
    ok, pen_atr = passes_penetration(ev, atr_at_bar=0.0, min_pen_atr_mult=0.3)
    # With ATR == 0 the threshold is 0, so any positive penetration
    # passes; pen_atr is +inf or NaN — both are acceptable as long as
    # the function does not crash.
    assert ok is True
    assert math.isinf(pen_atr) or math.isnan(pen_atr)


# ---------------------------------------------------------------------------
# §2.4 exhaustion candle
# ---------------------------------------------------------------------------


def test_exhaustion_upper_long_upper_wick_short_body() -> None:
    """Upper-side excess wants a long upper wick + short body.

    Bar: open=110, close=109, high=120, low=108.
    body = |109-110| = 1; range = 120 - 108 = 12.
    upper_wick = 120 - max(109, 110) = 10.
    body_ratio = 1/12 ≈ 0.083 (≤ 0.5 ✓)
    wick_ratio = 10/12 ≈ 0.833 (≥ 0.4 ✓)
    """
    assert is_exhaustion_candle(
        direction="upper",
        bar_open=110.0,
        bar_high=120.0,
        bar_low=108.0,
        bar_close=109.0,
        min_wick_ratio=0.4,
        max_body_ratio=0.5,
    ) is True


def test_exhaustion_lower_long_lower_wick_short_body() -> None:
    """Lower-side excess wants long lower wick + short body."""
    # open=90, close=91, low=80, high=92. body=1, range=12.
    # lower_wick = min(91, 90) - 80 = 90 - 80 = 10. wick_ratio ≈ 0.833.
    assert is_exhaustion_candle(
        direction="lower",
        bar_open=90.0,
        bar_high=92.0,
        bar_low=80.0,
        bar_close=91.0,
        min_wick_ratio=0.4,
        max_body_ratio=0.5,
    ) is True


def test_exhaustion_fails_for_full_body_candle() -> None:
    """Marubozu — body equals range → wick_ratio = 0 → fails."""
    # open=100, close=110, high=110, low=100.
    # body = 10, range = 10, body_ratio = 1.0 (> 0.5 ✗)
    # upper_wick = 110 - max(110, 100) = 0 → wick_ratio = 0 (< 0.4 ✗)
    assert is_exhaustion_candle(
        direction="upper",
        bar_open=100.0,
        bar_high=110.0,
        bar_low=100.0,
        bar_close=110.0,
        min_wick_ratio=0.4,
        max_body_ratio=0.5,
    ) is False


def test_exhaustion_fails_when_body_too_large() -> None:
    """Wick OK but body > max_body_ratio."""
    # open=100, close=108, high=112, low=99. body=8, range=13.
    # body_ratio = 8/13 ≈ 0.615 > 0.5 → fails.
    assert is_exhaustion_candle(
        direction="upper",
        bar_open=100.0,
        bar_high=112.0,
        bar_low=99.0,
        bar_close=108.0,
        min_wick_ratio=0.4,
        max_body_ratio=0.5,
    ) is False


def test_exhaustion_fails_when_wick_too_short() -> None:
    """Body OK but wick < min_wick_ratio."""
    # open=100, close=101, high=102, low=99. body=1, range=3.
    # upper_wick = 102 - max(101, 100) = 1. wick_ratio = 1/3 ≈ 0.333 < 0.4
    assert is_exhaustion_candle(
        direction="upper",
        bar_open=100.0,
        bar_high=102.0,
        bar_low=99.0,
        bar_close=101.0,
        min_wick_ratio=0.4,
        max_body_ratio=0.5,
    ) is False


def test_exhaustion_zero_range_returns_false() -> None:
    """Bar with zero range (high == low) → undefined ratios, fail safely."""
    assert is_exhaustion_candle(
        direction="upper",
        bar_open=100.0,
        bar_high=100.0,
        bar_low=100.0,
        bar_close=100.0,
        min_wick_ratio=0.4,
        max_body_ratio=0.5,
    ) is False


def test_exhaustion_lower_with_full_body_marubozu_down_fails() -> None:
    """Bearish marubozu on a lower-side excess → no rejection wick."""
    # open=110, close=100, high=110, low=100.
    # lower_wick = min(100, 110) - 100 = 0 → wick_ratio = 0 < 0.4
    assert is_exhaustion_candle(
        direction="lower",
        bar_open=110.0,
        bar_high=110.0,
        bar_low=100.0,
        bar_close=100.0,
        min_wick_ratio=0.4,
        max_body_ratio=0.5,
    ) is False
