"""Unit tests for ``compute_bollinger`` — spec §2.1."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from src.strategies.mean_reversion_bb_h4.bollinger import compute_bollinger


def test_compute_bollinger_basic_period_20_mult_2() -> None:
    """SMA20 ± 2σ on a 30-bar series; check that the formula matches.

    On a constant series the stddev is 0, so upper == lower == sma.
    On a known series we check one indexed element by hand.
    """
    closes = pd.Series([100.0] * 30)
    bb = compute_bollinger(closes, period=20, multiplier=2.0)

    # First 19 entries are NaN (need 20 values for the rolling SMA / std).
    for i in range(19):
        assert math.isnan(bb.sma.iloc[i])
        assert math.isnan(bb.upper.iloc[i])
        assert math.isnan(bb.lower.iloc[i])

    # From idx 19 onward: SMA = 100, std = 0, so upper = lower = 100.
    for i in range(19, 30):
        assert bb.sma.iloc[i] == pytest.approx(100.0)
        assert bb.upper.iloc[i] == pytest.approx(100.0)
        assert bb.lower.iloc[i] == pytest.approx(100.0)

    assert bb.period == 20
    assert bb.multiplier == 2.0


def test_compute_bollinger_known_values() -> None:
    """A non-constant series — verify SMA + std + bands at index 19.

    Series: 1, 2, 3, ..., 20 (first 20 closes).
    SMA20 at idx 19 = (1+2+...+20)/20 = 10.5.
    Population std (ddof=0) at idx 19 = sqrt((1/20) * sum((x - 10.5)^2))
        = sqrt((1/20) * 665) ≈ sqrt(33.25) ≈ 5.766281
    Upper = 10.5 + 2 * 5.766281 = 22.032562
    Lower = 10.5 - 2 * 5.766281 = -1.032562
    """
    closes = pd.Series([float(i) for i in range(1, 21)])
    bb = compute_bollinger(closes, period=20, multiplier=2.0)

    assert bb.sma.iloc[19] == pytest.approx(10.5)
    expected_std = math.sqrt(33.25)
    assert bb.upper.iloc[19] == pytest.approx(10.5 + 2 * expected_std)
    assert bb.lower.iloc[19] == pytest.approx(10.5 - 2 * expected_std)


def test_compute_bollinger_short_history_raises() -> None:
    """Spec note: a series shorter than ``period`` is an error — the
    caller would otherwise receive an all-NaN frame and silently
    skip the strategy. Surface the issue."""
    closes = pd.Series([100.0] * 10)
    with pytest.raises(ValueError, match="period"):
        compute_bollinger(closes, period=20, multiplier=2.0)


def test_compute_bollinger_bands_symmetric_around_sma() -> None:
    """upper - sma == sma - lower at every defined index."""
    closes = pd.Series(
        [100.0, 101.0, 99.0, 102.0, 98.0] * 5  # 25 bars, varies
    )
    bb = compute_bollinger(closes, period=20, multiplier=2.0)
    for i in range(19, len(closes)):
        upper_dist = bb.upper.iloc[i] - bb.sma.iloc[i]
        lower_dist = bb.sma.iloc[i] - bb.lower.iloc[i]
        assert upper_dist == pytest.approx(lower_dist)


def test_compute_bollinger_no_look_ahead() -> None:
    """The SMA/std at index i must be a function of closes[0..i]
    inclusive only — never of closes[i+1..]. Verify by truncating the
    input and checking the prefix matches the full series' prefix.
    """
    full = pd.Series([float(i) for i in range(1, 31)])
    truncated = full.iloc[:25].copy()

    bb_full = compute_bollinger(full, period=20, multiplier=2.0)
    bb_trunc = compute_bollinger(truncated, period=20, multiplier=2.0)

    # Indices 19..24 of the full series must equal indices 19..24 of
    # the truncated series exactly. If any look-ahead leaked in, the
    # full-series values at idx 19..24 would differ from the truncated.
    for i in range(19, 25):
        assert bb_full.sma.iloc[i] == pytest.approx(bb_trunc.sma.iloc[i])
        assert bb_full.upper.iloc[i] == pytest.approx(bb_trunc.upper.iloc[i])
        assert bb_full.lower.iloc[i] == pytest.approx(bb_trunc.lower.iloc[i])


def test_compute_bollinger_multiplier_scaling() -> None:
    """Doubling the multiplier doubles the band distance."""
    closes = pd.Series([float(i) for i in range(1, 25)])
    bb1 = compute_bollinger(closes, period=20, multiplier=1.0)
    bb2 = compute_bollinger(closes, period=20, multiplier=2.0)
    for i in range(19, 24):
        d1 = bb1.upper.iloc[i] - bb1.sma.iloc[i]
        d2 = bb2.upper.iloc[i] - bb2.sma.iloc[i]
        assert d2 == pytest.approx(2 * d1)
