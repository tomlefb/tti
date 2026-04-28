"""Unit tests for ``src.detection.bias`` — hand-crafted swings DataFrames."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.detection.bias import compute_daily_bias, compute_timeframe_bias


def _swings_from_seq(seq: list[tuple[str, float]]) -> pd.DataFrame:
    """Build a swings DataFrame from a list of (type, price) pairs.

    Indices are sequential; gaps (non-swing rows) are not represented since
    ``compute_timeframe_bias`` only filters by ``swing_type.notna()``.
    """
    types = [t for t, _ in seq]
    prices = [p for _, p in seq]
    return pd.DataFrame(
        {
            "swing_type": pd.Series(types, dtype=object),
            "swing_price": pd.Series(prices, dtype="float64"),
        }
    )


# ---------------------------------------------------------------------------
# compute_timeframe_bias
# ---------------------------------------------------------------------------


def test_bias_bullish_4_hh_hl() -> None:
    seq = [
        ("low", 100.0),
        ("high", 105.0),
        ("low", 102.0),
        ("high", 110.0),  # window of 4 ends here
    ]
    swings = _swings_from_seq(seq)
    assert compute_timeframe_bias(swings, bias_swing_count=4) == "bullish"


def test_bias_bearish_4_lh_ll() -> None:
    seq = [
        ("high", 110.0),
        ("low", 102.0),
        ("high", 108.0),
        ("low", 100.0),
        ("high", 106.0),
        ("low", 95.0),
    ]
    # Last 4: high=108, low=100, high=106, low=95 → LH (108>106) and LL (100>95).
    swings = _swings_from_seq(seq)
    assert compute_timeframe_bias(swings, bias_swing_count=4) == "bearish"


def test_bias_mixed_returns_no_trade() -> None:
    seq = [
        ("low", 100.0),
        ("high", 110.0),
        ("low", 105.0),
        ("high", 108.0),  # high not strictly higher (108 < 110) → not bullish
    ]
    swings = _swings_from_seq(seq)
    assert compute_timeframe_bias(swings, bias_swing_count=4) == "no_trade"


def test_bias_recent_break_collapses_to_no_trade() -> None:
    # 3 HH/HL then a sudden lower-low → neutral (heuristic per docs/01 §3).
    seq = [
        ("low", 100.0),
        ("high", 105.0),
        ("low", 102.0),
        ("high", 110.0),
        ("low", 90.0),  # break: lower than the prior low (102)
        ("high", 112.0),  # but window of 4 = (high, low_break, high, ...) wait
    ]
    # Last 4: low=102, high=110, low=90, high=112.
    # Highs: 110, 112 → HH ok. Lows: 102, 90 → not HL (90 < 102) → not bullish.
    swings = _swings_from_seq(seq)
    assert compute_timeframe_bias(swings, bias_swing_count=4) == "no_trade"


def test_bias_insufficient_swings() -> None:
    seq = [
        ("low", 100.0),
        ("high", 110.0),
        ("low", 102.0),
    ]
    swings = _swings_from_seq(seq)
    assert compute_timeframe_bias(swings, bias_swing_count=4) == "no_trade"


def test_bias_ignores_none_rows() -> None:
    # Mix in non-swing rows (None) — they must be ignored.
    types: list[str | None] = [
        None,
        "low",
        None,
        "high",
        None,
        "low",
        None,
        "high",
    ]
    prices = [np.nan, 100.0, np.nan, 105.0, np.nan, 102.0, np.nan, 110.0]
    swings = pd.DataFrame(
        {
            "swing_type": pd.Series(types, dtype=object),
            "swing_price": pd.Series(prices, dtype="float64"),
        }
    )
    assert compute_timeframe_bias(swings, bias_swing_count=4) == "bullish"


def test_bias_invalid_count_raises() -> None:
    seq = [("low", 100.0)]
    swings = _swings_from_seq(seq)
    try:
        compute_timeframe_bias(swings, bias_swing_count=1)
    except ValueError:
        return
    raise AssertionError("Expected ValueError for bias_swing_count<2")


# ---------------------------------------------------------------------------
# compute_daily_bias
# ---------------------------------------------------------------------------


def _trending_up_ohlc(n: int = 60) -> pd.DataFrame:
    """Synthetic clean uptrend with regular pullbacks → bullish swings on
    H4 and H1 alike."""
    # Sawtooth: +3, -1, +3, -1, ... yields HH/HL.
    rng = np.random.default_rng(0)
    base = 100.0
    highs = []
    lows = []
    closes = []
    for i in range(n):
        if i % 4 < 2:
            base += 3.0
        else:
            base -= 1.0
        # add tiny noise so highs/lows differ
        noise = float(rng.uniform(0.0, 0.5))
        highs.append(base + 1.0 + noise)
        lows.append(base - 1.0 - noise)
        closes.append(base)
    return pd.DataFrame(
        {
            "time": pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC"),
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
        }
    )


def _trending_down_ohlc(n: int = 60) -> pd.DataFrame:
    df = _trending_up_ohlc(n)
    # Mirror around 200 to invert the trend.
    df = df.copy()
    df["high"], df["low"] = (200 - df["low"]), (200 - df["high"])
    df["open"] = 200 - df["open"]
    df["close"] = 200 - df["close"]
    return df


def test_compute_daily_bias_bullish_when_both_agree() -> None:
    df = _trending_up_ohlc(80)
    bias = compute_daily_bias(
        df_h4=df,
        df_h1=df,
        swing_lookback_h4=2,
        swing_lookback_h1=2,
        min_amplitude_atr_mult=0.0,  # accept everything for the test
        bias_swing_count=4,
        atr_period=14,
    )
    assert bias == "bullish"


def test_compute_daily_bias_bearish_when_both_agree() -> None:
    df = _trending_down_ohlc(80)
    bias = compute_daily_bias(
        df_h4=df,
        df_h1=df,
        swing_lookback_h4=2,
        swing_lookback_h1=2,
        min_amplitude_atr_mult=0.0,
        bias_swing_count=4,
        atr_period=14,
    )
    assert bias == "bearish"


def test_compute_daily_bias_no_trade_when_disagree() -> None:
    up = _trending_up_ohlc(80)
    down = _trending_down_ohlc(80)
    bias = compute_daily_bias(
        df_h4=up,
        df_h1=down,
        swing_lookback_h4=2,
        swing_lookback_h1=2,
        min_amplitude_atr_mult=0.0,
        bias_swing_count=4,
        atr_period=14,
    )
    assert bias == "no_trade"
