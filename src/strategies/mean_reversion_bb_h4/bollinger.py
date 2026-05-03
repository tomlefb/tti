"""Bollinger bands computation — spec §2.1.

A pure function over a close-price series. The rolling SMA and
population stddev are anchored: ``sma[i]`` and ``std[i]`` are
computed from ``close[i - period + 1 .. i]`` inclusive. Bars
``i < period - 1`` carry NaN, which is the natural Pandas behaviour
for rolling windows with ``min_periods == period``.
"""

from __future__ import annotations

import pandas as pd

from .types import BollingerBands


def compute_bollinger(
    close: pd.Series,
    period: int = 20,
    multiplier: float = 2.0,
) -> BollingerBands:
    """Return ``BollingerBands`` for the given close series — spec §2.1.

    Args:
        close: close prices, RangeIndex assumed (the position of each
            value is its bar index — same convention as the OHLC
            frames consumed downstream).
        period: BB period — spec §3.1 anchors at 20.
        multiplier: stddev multiplier — spec §3.1 anchors at 2.0.

    Returns:
        ``BollingerBands`` with ``sma``, ``upper``, ``lower`` series
        sharing the same index as ``close``. Indices ``< period - 1``
        carry NaN.

    Raises:
        ValueError: if ``len(close) < period`` (the resulting bands
            would be all-NaN, and silently skipping the strategy on
            partial data would be a debug hazard at gate 4).
    """
    if period < 2:
        raise ValueError(f"period must be >= 2, got {period}")
    if len(close) < period:
        raise ValueError(
            f"compute_bollinger: close has {len(close)} bars, "
            f"need at least period={period}"
        )

    sma = close.rolling(window=period, min_periods=period).mean()
    # Population stddev (ddof=0): same convention as the cadence
    # pre-measure and the spec narrative "captures ≈ 95 % of the
    # in-distribution moves" — that figure assumes population std.
    std = close.rolling(window=period, min_periods=period).std(ddof=0)

    upper = sma + multiplier * std
    lower = sma - multiplier * std

    return BollingerBands(
        sma=sma,
        upper=upper,
        lower=lower,
        period=period,
        multiplier=multiplier,
    )
