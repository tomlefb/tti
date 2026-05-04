"""ATR computation + volatility regime filter — spec §3.1 + §2.6.

Two pure functions:

- ``compute_atr``: SMA(True Range, period). Same convention as the
  MR BB H4 pipeline; the True Range at bar 0 falls back to
  ``high - low`` (no prior close).
- ``passes_volatility_regime``: tail ATR ≤ ``explosive_threshold`` ×
  median(ATR over the past ``regime_lookback`` days). Returns
  ``True`` (pass) when the median is undefined (warmup) or when
  the tail ATR is NaN — "unknown regime" is treated as
  non-exclusion to avoid spurious filtering during warmup.
"""

from __future__ import annotations

import math

import pandas as pd


def compute_atr(ohlc: pd.DataFrame, period: int = 20) -> pd.Series:
    """Average True Range — spec §3.1.

    Args:
        ohlc: D1 OHLC frame with at least ``high, low, close``
            columns. The frame's index must be chronological.
        period: ATR window in bars (spec default 20).

    Returns:
        ATR series, same index as ``ohlc``. Bars ``< period - 1``
        carry NaN (rolling-mean warmup).
    """
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")

    high = ohlc["high"].astype("float64")
    low = ohlc["low"].astype("float64")
    close = ohlc["close"].astype("float64")
    prev_close = close.shift(1)

    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (prev_close - low).abs(),
        ],
        axis=1,
    ).max(axis=1)
    # Bar 0: no prev_close → only (high - low) is defined; max with
    # NaN siblings yields the (high - low) value via skipna.
    return tr.rolling(window=period, min_periods=period).mean()


def passes_volatility_regime(
    atr: pd.Series,
    *,
    explosive_threshold: float,
    regime_lookback: int,
) -> bool:
    """Spec §2.6: exclude an asset when its tail ATR is more than
    ``explosive_threshold`` × the rolling median ATR over the past
    ``regime_lookback`` days.

    Args:
        atr: ATR series for one asset, chronological. The last
            value is the "tail" — the ATR at the decision date.
        explosive_threshold: spec default 5.0.
        regime_lookback: spec default 90.

    Returns:
        ``True`` if the asset passes (regime normal or undefined).
        ``False`` only when both the tail ATR and the rolling
        median are defined AND the tail exceeds the threshold.
    """
    if len(atr) == 0:
        return True
    tail = atr.iloc[-1]
    if pd.isna(tail):
        # Tail unknown → no basis to exclude; pass.
        return True
    if len(atr) < regime_lookback:
        # Insufficient history for the regime baseline → pass.
        return True
    window = atr.iloc[-regime_lookback:].dropna()
    if len(window) == 0:
        return True
    median = float(window.median())
    if math.isnan(median) or median <= 0:
        return True
    return float(tail) <= explosive_threshold * median
