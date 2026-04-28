"""Fair Value Gap (FVG) detection on M5.

FVG geometric detection is **pure logic** (docs/07 §1.1); the size
threshold is a **calibrated rule** (docs/07 §1.2).

The 3-candle definition is the most common in SMC literature; alternative
definitions (5-candle implied FVG, etc.) are explicitly out of scope for
v1 — see docs/01 §8.

Per docs/01 §5 Step 3:

- Bullish FVG: ``c1.high < c3.low`` ⇒ gap region is ``[c1.high, c3.low]``.
- Bearish FVG: ``c1.low  > c3.high`` ⇒ gap region is ``[c3.high, c1.low]``.

Proximal/distal convention (matches docs/01 §5 Step 4 entry rule):

- Bullish setup: limit BUY at the **upper edge** of the gap → that's
  ``c3.low``. So ``proximal = c3.low`` (closer to current price after a
  bullish displacement, hit first on a pullback) and ``distal = c1.high``.
- Bearish setup: limit SELL at the **lower edge** → that's ``c3.high``.
  So ``proximal = c3.high`` and ``distal = c1.low``.

The size filter divides the FVG's geometric ``size`` by ATR computed at
the index of c2 (the middle candle). This keeps the threshold scale-free
across instruments without re-tuning.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import pandas as pd

from .swings import _atr


@dataclass(frozen=True)
class FVG:
    """One detected Fair Value Gap.

    ``proximal`` is the entry-side edge (closer to where price will be
    after the displacement); ``distal`` is the SL-side edge.

    ``size_atr_ratio`` is ``size / ATR(atr_period)`` evaluated at c2.
    By construction ``>= min_size_atr_mult``.
    """

    direction: Literal["bullish", "bearish"]
    proximal: float
    distal: float
    c1_time_utc: datetime
    c2_time_utc: datetime
    c3_time_utc: datetime
    size: float
    size_atr_ratio: float


def detect_fvgs_in_window(
    df_m5: pd.DataFrame,
    start_time_utc: datetime,
    end_time_utc: datetime,
    direction: Literal["bullish", "bearish"],
    *,
    min_size_atr_mult: float,
    atr_period: int = 14,
) -> list[FVG]:
    """Detect every FVG of ``direction`` whose c2 falls inside the window.

    The window is ``[start_time_utc, end_time_utc]`` inclusive on both
    sides. We anchor on c2's timestamp because that is the moment the
    gap structurally appears (c1 has happened; c3 is the candle that
    confirms the gap by leaving it open). Any FVG whose c2 is in the
    window AND whose c3 is also present in ``df_m5`` is returned.

    Args:
        df_m5: M5 OHLC frame (UTC ``time``).
        start_time_utc: window start (inclusive).
        end_time_utc: window end (inclusive).
        direction: ``"bullish"`` or ``"bearish"`` — pre-filters to gaps
            of the side the caller cares about. The orchestrator passes
            the side aligned with the daily bias.
        min_size_atr_mult: ``FVG_MIN_SIZE_ATR_MULTIPLIER``. ``0`` disables
            the filter (returns every geometric FVG).
        atr_period: ``FVG_ATR_PERIOD`` (default 14).

    Returns:
        ``list[FVG]`` sorted by ``c2_time_utc`` ascending (oldest first).
    """
    if min_size_atr_mult < 0:
        raise ValueError(f"min_size_atr_mult must be >= 0, got {min_size_atr_mult}")
    if direction not in ("bullish", "bearish"):
        raise ValueError(f"direction must be 'bullish' or 'bearish', got {direction!r}")
    n = len(df_m5)
    if n < 3:
        return []

    times = pd.to_datetime(df_m5["time"], utc=True)
    times_py = [pd.Timestamp(t).to_pydatetime() for t in times]
    highs = df_m5["high"].to_numpy(dtype="float64")
    lows = df_m5["low"].to_numpy(dtype="float64")

    atr_series = _atr(df_m5, atr_period).to_numpy(dtype="float64")

    out: list[FVG] = []
    for j in range(1, n - 1):  # j is the c2 index
        c2_time = times_py[j]
        if c2_time < start_time_utc or c2_time > end_time_utc:
            continue
        c1, c3 = j - 1, j + 1
        if direction == "bullish":
            if not (highs[c1] < lows[c3]):
                continue
            proximal = float(lows[c3])
            distal = float(highs[c1])
            size = proximal - distal  # > 0
        else:
            if not (lows[c1] > highs[c3]):
                continue
            proximal = float(highs[c3])
            distal = float(lows[c1])
            size = distal - proximal  # > 0

        atr_here = atr_series[j]
        if atr_here != atr_here or atr_here <= 0:  # NaN guard
            # If ATR isn't defined yet we cannot apply a size-vs-ATR
            # filter — drop conservatively.
            continue
        ratio = size / atr_here
        if ratio < min_size_atr_mult:
            continue

        out.append(
            FVG(
                direction=direction,
                proximal=proximal,
                distal=distal,
                c1_time_utc=times_py[c1],
                c2_time_utc=c2_time,
                c3_time_utc=times_py[c3],
                size=size,
                size_atr_ratio=float(ratio),
            )
        )

    return out
