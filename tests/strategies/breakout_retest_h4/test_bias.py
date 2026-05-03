"""Unit tests for the D1 bias filter — spec §2.1."""

from __future__ import annotations

import pandas as pd
import pytest

from src.strategies.breakout_retest_h4.bias import bias_d1


def _series(values: list[float]) -> pd.Series:
    """Build a pandas Series of D1 closes."""
    return pd.Series(values, dtype="float64")


def test_bias_d1_bullish_when_close_above_ma50() -> None:
    # 49 closes at 100, last close at 110 → SMA50 ≈ 100.2 < 110.
    closes = _series([100.0] * 49 + [110.0])
    assert bias_d1(closes) == "bullish"


def test_bias_d1_bearish_when_close_below_ma50() -> None:
    closes = _series([100.0] * 49 + [80.0])
    assert bias_d1(closes) == "bearish"


def test_bias_d1_neutral_when_equal() -> None:
    # All closes at the same value → SMA50 == last close exactly.
    closes = _series([100.0] * 50)
    assert bias_d1(closes) == "neutral"


def test_bias_d1_raises_on_short_history() -> None:
    closes = _series([100.0] * 49)  # one short
    with pytest.raises(ValueError, match="ma_period"):
        bias_d1(closes)


def test_bias_d1_uses_only_last_ma_period_closes() -> None:
    # Bias must be evaluated on the *last* 50 closes — earlier history
    # is ignored. Build a series where the first 100 closes drag a
    # naive full-history mean down, so a global mean would be < last
    # close, producing the wrong "bullish" verdict; the SMA50 should
    # be flat around 200 with last close at 200 → "neutral".
    early_low = [100.0] * 100
    flat_high = [200.0] * 50
    closes = _series(early_low + flat_high)
    assert bias_d1(closes) == "neutral"


def test_bias_d1_custom_ma_period() -> None:
    # Allow a smaller window for unit tests independent of the
    # production constant.
    closes = _series([1.0, 2.0, 3.0, 4.0, 5.0])  # 5 closes
    # SMA5 = 3.0; last close = 5.0 > 3.0 → bullish.
    assert bias_d1(closes, ma_period=5) == "bullish"
