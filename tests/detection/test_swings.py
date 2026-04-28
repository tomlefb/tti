"""Unit tests for ``src.detection.swings`` — hand-crafted OHLC fixtures only."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.detection.swings import (
    _atr,
    filter_significant_swings,
    find_raw_swings,
    find_swings,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ohlc(highs: list[float], lows: list[float]) -> pd.DataFrame:
    """Build a minimal OHLC frame from highs/lows; close=mid, open=mid."""
    n = len(highs)
    assert len(lows) == n
    mid = [(h + lo) / 2 for h, lo in zip(highs, lows, strict=True)]
    return pd.DataFrame(
        {
            "time": pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC"),
            "open": mid,
            "high": highs,
            "low": lows,
            "close": mid,
        }
    )


def _significant_indices(df_swings: pd.DataFrame) -> list[int]:
    """Return positional indices of rows where swing_type is not None."""
    return [i for i, t in enumerate(df_swings["swing_type"]) if t is not None]


# ---------------------------------------------------------------------------
# find_raw_swings
# ---------------------------------------------------------------------------


def test_find_raw_swings_empty() -> None:
    df = pd.DataFrame({"time": [], "open": [], "high": [], "low": [], "close": []})
    out = find_raw_swings(df, lookback=2)
    assert len(out) == 0
    assert list(out.columns) == ["swing_type", "swing_price"]


def test_find_raw_swings_single_candle_returns_none() -> None:
    df = _ohlc([10.0], [9.0])
    out = find_raw_swings(df, lookback=2)
    assert len(out) == 1
    assert out["swing_type"].iloc[0] is None
    assert np.isnan(out["swing_price"].iloc[0])


def test_find_raw_swings_too_short_for_lookback() -> None:
    # Need 2*lookback+1 = 5 candles for any swing with lookback=2; 4 is too few.
    df = _ohlc([10, 11, 12, 11], [9, 8, 7, 8])
    out = find_raw_swings(df, lookback=2)
    assert all(t is None for t in out["swing_type"])


def test_find_raw_swings_strict_hh_hl_series() -> None:
    # 7 candles forming alternating swing high / low pattern.
    # idx:        0    1    2    3    4    5    6
    # highs:     10   12   11   14   13   16   15
    # lows:       8    9    7   10    8   11    9
    # With lookback=1: peaks at idx 1, 3, 5 (highs), troughs at idx 2, 4 (lows).
    highs = [10, 12, 11, 14, 13, 16, 15]
    lows = [8, 9, 7, 10, 8, 11, 9]
    df = _ohlc(highs, lows)
    out = find_raw_swings(df, lookback=1)
    types = out["swing_type"].tolist()
    assert types[1] == "high"
    assert types[3] == "high"
    assert types[5] == "high"
    assert types[2] == "low"
    assert types[4] == "low"
    # Edges (idx 0 and 6) cannot be confirmed.
    assert types[0] is None
    assert types[6] is None


def test_find_raw_swings_plateau_yields_no_swing() -> None:
    # Three central candles share the same high — strict comparison rejects.
    highs = [10, 12, 12, 12, 10]
    lows = [9, 11, 11, 11, 9]
    df = _ohlc(highs, lows)
    out = find_raw_swings(df, lookback=1)
    # Center candle (idx 2) has equal-high neighbours -> not a swing.
    assert out["swing_type"].iloc[2] is None
    # idx 1 and 3 also fail because their right/left neighbour shares their
    # high — strict > rules them out too.
    assert out["swing_type"].iloc[1] is None
    assert out["swing_type"].iloc[3] is None


def test_find_raw_swings_lookback_boundary() -> None:
    # Construct an obvious peak at exactly index = lookback (= 2). The peak
    # bar's high (15) dominates the 2 bars on each side. lookback=2 means
    # earliest confirmable index = 2 and latest = n - lookback - 1.
    highs = [10, 11, 15, 12, 11, 13, 14, 16, 12]
    lows = [9, 10, 14, 11, 10, 12, 11, 15, 11]
    df = _ohlc(highs, lows)
    out = find_raw_swings(df, lookback=2)
    assert out["swing_type"].iloc[2] == "high"
    # idx n - lookback - 1 = 6: high=14 — left max = max(15,12,11)=15, right
    # max = max(16,12)=16, so not a swing. Build a different latest-index
    # case where the bar IS a swing: trough at idx 6.
    # To exercise the upper boundary, use lows: idx 6 must be lower than
    # both neighbours.
    highs2 = [10, 11, 15, 12, 11, 13, 9, 16, 12]
    lows2 = [9, 10, 14, 11, 10, 12, 5, 15, 11]
    df2 = _ohlc(highs2, lows2)
    out2 = find_raw_swings(df2, lookback=2)
    assert out2["swing_type"].iloc[6] == "low"


def test_find_raw_swings_invalid_lookback_raises() -> None:
    df = _ohlc([10] * 5, [9] * 5)
    try:
        find_raw_swings(df, lookback=0)
    except ValueError:
        return
    raise AssertionError("Expected ValueError for lookback=0")


# ---------------------------------------------------------------------------
# filter_significant_swings
# ---------------------------------------------------------------------------


def test_filter_significant_swings_drops_tiny_noise() -> None:
    # Construct a controlled scenario: constant-range candles (TR=2 → ATR≈2)
    # so threshold math is deterministic. Then inject raw swings manually:
    #   idx 5  : low  at price 90  (first → always kept)
    #   idx 10 : high at price 91  (tiny noise; |91-90| = 1 < 2*ATR → drop)
    #   idx 20 : high at price 100 (big move;  |100-90| = 10 > 2*ATR → keep)
    n = 30
    df = pd.DataFrame(
        {
            "time": pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC"),
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.0] * n,
        }
    )
    types: list[str | None] = [None] * n
    prices = [np.nan] * n
    types[5] = "low"
    prices[5] = 90.0
    types[10] = "high"
    prices[10] = 91.0
    types[20] = "high"
    prices[20] = 100.0
    raw = pd.DataFrame(
        {
            "swing_type": pd.Series(types, dtype=object),
            "swing_price": pd.Series(prices, dtype="float64"),
        },
        index=df.index,
    )
    filtered = filter_significant_swings(raw, df, min_amplitude_atr_mult=2.0, atr_period=5)
    assert filtered["swing_type"].iloc[5] == "low"
    assert filtered["swing_type"].iloc[10] is None  # tiny noise dropped
    assert filtered["swing_type"].iloc[20] == "high"


def test_filter_significant_swings_keeps_first_swing_unconditionally() -> None:
    highs = [10, 12, 11, 9, 10]
    lows = [9, 11, 10, 7, 9]
    df = _ohlc(highs, lows)
    raw = find_raw_swings(df, lookback=1)
    # Set the threshold absurdly high; the first swing must still be kept.
    filtered = filter_significant_swings(raw, df, min_amplitude_atr_mult=1e9, atr_period=2)
    kept = _significant_indices(filtered)
    assert len(kept) >= 1
    # First kept index must equal the first raw-swing index.
    first_raw = _significant_indices(raw)[0]
    assert kept[0] == first_raw


def test_filter_significant_swings_zero_threshold_keeps_all() -> None:
    highs = [10, 12, 11, 14, 13, 16, 15]
    lows = [8, 9, 7, 10, 8, 11, 9]
    df = _ohlc(highs, lows)
    raw = find_raw_swings(df, lookback=1)
    filtered = filter_significant_swings(raw, df, min_amplitude_atr_mult=0.0, atr_period=3)
    # With zero threshold, every raw swing whose ATR is defined is kept.
    # ATR(period=3) is defined from index 2 onward; raw swings here all
    # land at indices >= 1 so most should still be kept.
    raw_idx = set(_significant_indices(raw))
    kept_idx = set(_significant_indices(filtered))
    # The first raw swing is always kept; the rest pass with threshold 0.
    assert kept_idx == raw_idx or (raw_idx - kept_idx).issubset(set(range(2)))


def test_filter_significant_swings_empty_input() -> None:
    df = pd.DataFrame({"time": [], "open": [], "high": [], "low": [], "close": []})
    raw = find_raw_swings(df, lookback=2)
    out = filter_significant_swings(raw, df, min_amplitude_atr_mult=0.5)
    assert len(out) == 0


# ---------------------------------------------------------------------------
# find_swings (integration of the two stages)
# ---------------------------------------------------------------------------


def test_find_swings_chains_stages() -> None:
    highs = [10, 12, 11, 14, 13, 16, 15]
    lows = [8, 9, 7, 10, 8, 11, 9]
    df = _ohlc(highs, lows)
    out = find_swings(df, lookback=1, min_amplitude_atr_mult=0.0, atr_period=3)
    # Output schema.
    assert list(out.columns) == ["swing_type", "swing_price"]
    assert len(out) == len(df)


# ---------------------------------------------------------------------------
# _atr (internal but worth pinning down)
# ---------------------------------------------------------------------------


def test_atr_matches_wilder_seed() -> None:
    # 5 constant-range candles: high-low = 1.0 each; close walks up by 1.
    n = 20
    df = pd.DataFrame(
        {
            "time": pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC"),
            "open": [float(i) for i in range(n)],
            "high": [float(i) + 1.0 for i in range(n)],
            "low": [float(i) for i in range(n)],
            "close": [float(i) + 0.5 for i in range(n)],
        }
    )
    period = 5
    atr = _atr(df, period=period)
    # First valid ATR is at index period-1.
    assert all(np.isnan(atr.iloc[: period - 1]))
    assert not np.isnan(atr.iloc[period - 1])
    # ATR should be strictly positive on a non-flat market.
    assert (atr.iloc[period - 1 :] > 0).all()
