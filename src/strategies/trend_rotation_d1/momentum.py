"""Momentum score computation — spec §2.2.

A pure function over a close-price series. The score is the
cumulative return over the past ``lookback_days`` D1 closes:

    score = (close[-1] - close[-lookback - 1]) / close[-lookback - 1]

Anti-look-ahead: the caller is responsible for slicing ``close_d1``
so the last value is the most recent observable close at the
decision date — i.e. the close strictly before ``now_utc`` per the
spec's "the close of today is not used for today's decision"
convention.
"""

from __future__ import annotations

import pandas as pd


def compute_momentum(
    close_d1: pd.Series,
    lookback_days: int,
) -> float | None:
    """Return the cumulative-return momentum score, or ``None``.

    Args:
        close_d1: D1 close-price series (chronological, RangeIndex
            or DatetimeIndex). The last value is used as the
            "current" close — the caller is responsible for
            anti-look-ahead slicing.
        lookback_days: lookback window in trading days. The score
            is taken over ``close[-lookback_days - 1] → close[-1]``
            inclusive of both endpoints.

    Returns:
        ``float`` cumulative return, or ``None`` when the series is
        shorter than ``lookback_days + 1`` (insufficient history;
        spec §2.6 hard invalidation).
    """
    if lookback_days < 1:
        raise ValueError(f"lookback_days must be >= 1, got {lookback_days}")
    if len(close_d1) < lookback_days + 1:
        return None
    past = float(close_d1.iloc[-lookback_days - 1])
    now = float(close_d1.iloc[-1])
    if past == 0.0:
        return None
    return (now - past) / past
