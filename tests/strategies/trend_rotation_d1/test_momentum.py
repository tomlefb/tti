"""Unit tests for ``compute_momentum`` — spec §2.2."""

from __future__ import annotations

import pandas as pd
import pytest

from src.strategies.trend_rotation_d1.momentum import compute_momentum


def test_momentum_basic_calculation_63d() -> None:
    """Lookback 63 → score = (close[-1] - close[-64]) / close[-64].

    Synthetic series: 70 closes from 100 to 134 in steps of ~0.5.
    score at 63d = (close[69] - close[6]) / close[6]
                = (133.5 - 103) / 103 ≈ 0.296.
    """
    closes = pd.Series([100.0 + 0.5 * i for i in range(70)])
    score = compute_momentum(closes, lookback_days=63)
    expected = (closes.iloc[-1] - closes.iloc[-64]) / closes.iloc[-64]
    assert score == pytest.approx(expected)


def test_momentum_basic_calculation_126d() -> None:
    """Same shape, 126d lookback — verifies the index arithmetic
    works for the spec's two grid-axis values."""
    closes = pd.Series([100.0 * (1.001**i) for i in range(130)])
    score = compute_momentum(closes, lookback_days=126)
    expected = (closes.iloc[-1] - closes.iloc[-127]) / closes.iloc[-127]
    assert score == pytest.approx(expected)


def test_momentum_returns_none_when_history_insufficient() -> None:
    """Spec §2.2: ``len(close) < lookback + 1`` → ``None``.
    Guards against silently emitting a meaningless score."""
    closes = pd.Series([100.0] * 50)
    assert compute_momentum(closes, lookback_days=63) is None
    # Boundary: exactly lookback bars is still insufficient (need
    # lookback + 1 to compute the diff).
    assert compute_momentum(pd.Series([100.0] * 63), lookback_days=63) is None
    # One more bar → just enough.
    assert compute_momentum(pd.Series([100.0] * 64), lookback_days=63) is not None


def test_momentum_uses_only_past_data() -> None:
    """Anti-look-ahead: truncating the future of a series must NOT
    change the score at the truncation point.

    Compute the score on a 70-bar series, then on the same series
    extended with 50 more (random-looking) future bars, both
    sliced to the same 70-bar prefix. The two scores must match
    exactly — i.e. the score is a pure function of the visible
    prefix."""
    closes_short = pd.Series([100.0 + 0.5 * i for i in range(70)])
    closes_long = pd.Series(
        [100.0 + 0.5 * i for i in range(70)]
        + [99999.0] * 50  # absurd future to maximise contamination risk
    )
    score_short = compute_momentum(closes_short, lookback_days=63)
    score_long = compute_momentum(closes_long.iloc[:70], lookback_days=63)
    assert score_short == score_long


def test_momentum_zero_when_flat() -> None:
    """Constant series → score == 0 exactly."""
    closes = pd.Series([100.0] * 70)
    assert compute_momentum(closes, lookback_days=63) == pytest.approx(0.0)


def test_momentum_negative_when_falling() -> None:
    """Negative cumulative return → negative score."""
    closes = pd.Series([100.0 - 0.5 * i for i in range(70)])
    score = compute_momentum(closes, lookback_days=63)
    assert score is not None and score < 0
