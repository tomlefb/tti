"""H4 swing detection — spec §2.2.

Fractal-style: a pivot at index ``i`` is a swing high iff its high is
strictly greater than the highs of the ``n_swing`` bars on each side
(symmetric for swing lows on lows). Confirmation requires ``n_swing``
bars to the *right* of the pivot, so a pivot at index ``i`` is only
observable from index ``i + n_swing`` onward.

Anti-look-ahead contract: when ``now_utc`` is supplied, a pivot at
index ``i`` is only emitted if the **confirmation candle** (the bar at
index ``i + n_swing``) has already closed by ``now_utc`` — i.e.
``ohlc_h4["time"].iloc[i + n_swing] + timeframe <= now_utc``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import pandas as pd


@dataclass(frozen=True)
class Swing:
    """A confirmed fractal pivot on H4 (spec §2.2).

    Attributes:
        timestamp_utc: open time of the pivot bar (UTC, tz-aware).
        price: pivot price (high for ``direction="high"``, low otherwise).
        direction: ``"high"`` or ``"low"``.
        bar_index: positional index into the OHLC frame supplied to
            ``detect_swings_h4``. The caller's frame must be 0-indexed
            consecutively (default ``RangeIndex``).
    """

    timestamp_utc: datetime
    price: float
    direction: Literal["high", "low"]
    bar_index: int


def detect_swings_h4(
    ohlc_h4: pd.DataFrame,
    n_swing: int,
    *,
    now_utc: datetime | None = None,
    timeframe: timedelta = timedelta(hours=4),
) -> tuple[list[Swing], list[Swing]]:
    """Detect confirmed swing highs / lows on H4 (spec §2.2).

    Args:
        ohlc_h4: OHLC frame with columns ``time, open, high, low, close``.
            ``time`` must be tz-aware UTC. Index is read by position.
        n_swing: bars on each side that must be strictly lower (high) /
            higher (low) for a pivot to count.
        now_utc: optional production scheduler tick. When set, a pivot
            is only emitted if its confirmation candle has closed by
            ``now_utc``. ``None`` runs the unconstrained mode (used by
            full-history tests + the to-be-audited reference path).
        timeframe: H4 candle duration. Parameterised so the same
            implementation can be exercised on other timeframes from
            unit tests, but the strategy is H4-only in production.

    Returns:
        ``(swings_high, swings_low)`` — two lists in chronological
        (ascending bar-index) order.
    """
    if n_swing < 1:
        raise ValueError(f"n_swing must be >= 1, got {n_swing}")

    n = len(ohlc_h4)
    if n < 2 * n_swing + 1:
        return [], []

    high = ohlc_h4["high"].to_numpy(dtype="float64")
    low = ohlc_h4["low"].to_numpy(dtype="float64")
    times = pd.to_datetime(ohlc_h4["time"], utc=True)
    times_py = [pd.Timestamp(t).to_pydatetime() for t in times]

    swings_high: list[Swing] = []
    swings_low: list[Swing] = []

    for i in range(n_swing, n - n_swing):
        if now_utc is not None:
            confirm_close = times_py[i + n_swing] + timeframe
            if confirm_close > now_utc:
                # Confirmation candle hasn't closed yet — pivot is not
                # observable in real time. Spec §2.2 anti-look-ahead.
                continue

        h = high[i]
        left_h = high[i - n_swing : i]
        right_h = high[i + 1 : i + 1 + n_swing]
        if h > left_h.max() and h > right_h.max():
            swings_high.append(
                Swing(
                    timestamp_utc=times_py[i],
                    price=float(h),
                    direction="high",
                    bar_index=i,
                )
            )
            continue

        ll = low[i]
        left_l = low[i - n_swing : i]
        right_l = low[i + 1 : i + 1 + n_swing]
        if ll < left_l.min() and ll < right_l.min():
            swings_low.append(
                Swing(
                    timestamp_utc=times_py[i],
                    price=float(ll),
                    direction="low",
                    bar_index=i,
                )
            )

    return swings_high, swings_low
