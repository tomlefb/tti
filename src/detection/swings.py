"""Swing high / swing low detection — calibrated rule (see docs/07 § 1.2).

Two stages:

1. ``find_raw_swings`` — N-bar fractal detection (the geometric definition).
2. ``filter_significant_swings`` — ATR-based amplitude filter; drops swings
   whose excursion vs the previous opposite-type significant swing is below
   ``min_amplitude_atr_mult * ATR(period)``.

``find_swings`` chains the two for convenience.

All functions are pure: they take ``pandas.DataFrame`` input with the
canonical OHLC schema (``time, open, high, low, close[, ...]``) and return
``DataFrame`` results indexed identically to the input. No I/O, no global
state, no MT5 imports.

ATR uses Wilder's smoothing — implemented inline (``_atr``) to avoid a
dependency on pandas-ta (per CLAUDE.md tech-stack note).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _empty_swings(index: pd.Index) -> pd.DataFrame:
    """Return a properly typed, empty swings DataFrame aligned to ``index``."""
    return pd.DataFrame(
        {
            "swing_type": pd.Series([None] * len(index), dtype=object, index=index),
            "swing_price": pd.Series(np.nan, index=index, dtype="float64"),
        }
    )


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    """Compute Wilder's ATR over ``df`` for the given ``period``.

    True Range:
        TR_t = max(high_t - low_t, |high_t - close_{t-1}|, |low_t - close_{t-1}|)

    Wilder smoothing (effectively EMA with alpha=1/period, seeded by the
    simple mean of the first ``period`` TR values):
        ATR_{period-1} = mean(TR_0 .. TR_{period-1})
        ATR_t         = (ATR_{t-1} * (period - 1) + TR_t) / period       (t >= period)

    Args:
        df: OHLC frame with at least ``high``, ``low``, ``close`` columns.
        period: smoothing window (e.g. 14).

    Returns:
        A ``pd.Series`` aligned to ``df.index``. Indices before the first
        valid ATR are ``NaN``.
    """
    if period <= 0:
        raise ValueError(f"ATR period must be positive, got {period}")

    high = df["high"].astype("float64")
    low = df["low"].astype("float64")
    close = df["close"].astype("float64")
    prev_close = close.shift(1)

    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1, skipna=True)

    atr = pd.Series(np.nan, index=df.index, dtype="float64")
    n = len(tr)
    if n < period:
        return atr

    seed = float(tr.iloc[:period].mean())
    atr.iloc[period - 1] = seed
    for i in range(period, n):
        prev = atr.iloc[i - 1]
        atr.iloc[i] = (prev * (period - 1) + tr.iloc[i]) / period
    return atr


def find_raw_swings(df: pd.DataFrame, lookback: int) -> pd.DataFrame:
    """Detect raw swing highs and lows using an N-bar fractal definition.

    A candle at index ``i`` is a swing high iff its ``high`` is strictly
    greater than the highs of the ``lookback`` candles immediately before
    AND the ``lookback`` candles immediately after. Swing lows are symmetric
    on ``low``.

    Plateaus (e.g. three adjacent candles sharing the same high) yield no
    swing point, since the comparison is strict — by design, see docs/07 §1.2.

    Candles within ``lookback`` of either edge of ``df`` cannot be confirmed
    and have ``swing_type=None``.

    Args:
        df: OHLC frame indexed however the caller prefers; must contain the
            ``high`` and ``low`` columns. May be empty.
        lookback: number of candles each side that must be lower (for a
            high) / higher (for a low). Must be ``>= 1``.

    Returns:
        DataFrame with the same index as ``df`` and columns:
            - ``swing_type``: ``"high" | "low" | None``
            - ``swing_price``: ``float`` (the candle's high or low at swing
              points; ``NaN`` elsewhere).
    """
    if lookback < 1:
        raise ValueError(f"lookback must be >= 1, got {lookback}")

    out = _empty_swings(df.index)
    n = len(df)
    if n < 2 * lookback + 1:
        return out

    high = df["high"].to_numpy(dtype="float64")
    low = df["low"].to_numpy(dtype="float64")

    swing_type = np.full(n, None, dtype=object)
    swing_price = np.full(n, np.nan, dtype="float64")

    for i in range(lookback, n - lookback):
        h = high[i]
        ll = low[i]
        left_h = high[i - lookback : i]
        right_h = high[i + 1 : i + 1 + lookback]
        if h > left_h.max() and h > right_h.max():
            swing_type[i] = "high"
            swing_price[i] = h
            continue
        left_l = low[i - lookback : i]
        right_l = low[i + 1 : i + 1 + lookback]
        if ll < left_l.min() and ll < right_l.min():
            swing_type[i] = "low"
            swing_price[i] = ll

    out["swing_type"] = swing_type
    out["swing_price"] = swing_price
    return out


def filter_significant_swings(
    raw_swings: pd.DataFrame,
    df: pd.DataFrame,
    min_amplitude_atr_mult: float,
    atr_period: int = 14,
) -> pd.DataFrame:
    """Filter raw swings by an ATR-based amplitude threshold.

    A candidate swing is kept iff:

    - It is the first kept swing of the series (no prior to compare), OR
    - There is no prior **opposite-type** kept swing yet, OR
    - The absolute price distance to the most recent kept swing of the
      *opposite* type is ``>= min_amplitude_atr_mult * ATR(atr_period)``,
      where ATR is evaluated at the candidate swing's own bar.

    Indices with ``swing_type=None`` in ``raw_swings`` stay ``None`` in the
    output.

    Args:
        raw_swings: output of ``find_raw_swings``; index must be identical
            to ``df.index``.
        df: same OHLC frame used to produce ``raw_swings``; needed for ATR.
        min_amplitude_atr_mult: minimum amplitude multiplier (e.g. ``0.5``).
        atr_period: ATR window (default ``14``).

    Returns:
        DataFrame with the same schema as ``raw_swings``: kept swings
        retain their ``swing_type``/``swing_price``; dropped swings (and
        non-swing rows) have ``swing_type=None`` and ``swing_price=NaN``.
    """
    if min_amplitude_atr_mult < 0:
        raise ValueError(f"min_amplitude_atr_mult must be >= 0, got {min_amplitude_atr_mult}")
    if not raw_swings.index.equals(df.index):
        raise ValueError("raw_swings.index must equal df.index")

    out = _empty_swings(df.index)
    if len(df) == 0:
        return out

    atr = _atr(df, atr_period)

    types = raw_swings["swing_type"].to_numpy()
    prices = raw_swings["swing_price"].to_numpy(dtype="float64")
    out_type = np.full(len(df), None, dtype=object)
    out_price = np.full(len(df), np.nan, dtype="float64")

    last_opposite_price: dict[str, float | None] = {"high": None, "low": None}
    has_kept_any = False
    atr_arr = atr.to_numpy(dtype="float64")

    for i in range(len(df)):
        t = types[i]
        if t is None:
            continue

        opposite = "low" if t == "high" else "high"
        opp_price = last_opposite_price[opposite]

        if not has_kept_any or opp_price is None:
            keep = True
        else:
            atr_here = atr_arr[i]
            if np.isnan(atr_here):
                # ATR not yet defined — drop conservatively. Only affects
                # the first ~atr_period bars, which rarely contain swings.
                keep = False
            else:
                threshold = min_amplitude_atr_mult * atr_here
                keep = abs(prices[i] - opp_price) >= threshold

        if keep:
            out_type[i] = t
            out_price[i] = prices[i]
            last_opposite_price[t] = float(prices[i])
            has_kept_any = True

    out["swing_type"] = out_type
    out["swing_price"] = out_price
    return out


def find_swings(
    df: pd.DataFrame,
    lookback: int,
    min_amplitude_atr_mult: float,
    atr_period: int = 14,
) -> pd.DataFrame:
    """Convenience wrapper: ``find_raw_swings`` then ``filter_significant_swings``.

    Args:
        df: OHLC frame.
        lookback: bars each side for the fractal definition.
        min_amplitude_atr_mult: amplitude filter multiplier.
        atr_period: ATR window for the amplitude filter.

    Returns:
        DataFrame of significant swings (same schema as the two stages).
    """
    raw = find_raw_swings(df, lookback)
    return filter_significant_swings(raw, df, min_amplitude_atr_mult, atr_period)
