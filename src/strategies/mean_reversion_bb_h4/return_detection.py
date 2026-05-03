"""Return-inside-bands detection — spec §2.5.

Within the ``max_return_bars`` H4 bars after a confirmed excess,
look for the first **in-killzone** bar that closes strictly inside
both Bollinger bands. That bar is the setup trigger.

Anti-look-ahead: the scan stops at ``now_bar_index`` (inclusive of
``now_bar_index`` itself, i.e. the just-closed bar at the current
cycle is eligible). The pipeline supplies the index that matches
its ``now_utc`` cutoff so that streaming and full-history runs
produce identical event lists.
"""

from __future__ import annotations

import math
from datetime import time

import pandas as pd

from .excess import _bar_close_time, _is_in_killzone
from .types import BollingerBands, ExcessEvent, ReturnEvent


def detect_return(
    ohlc_h4: pd.DataFrame,
    bb: BollingerBands,
    excess: ExcessEvent,
    *,
    max_return_bars: int,
    now_bar_index: int,
    killzone_london_start_utc: time,
    killzone_london_end_utc: time,
    killzone_ny_start_utc: time,
    killzone_ny_end_utc: time,
) -> ReturnEvent | None:
    """Scan for a return-inside close in the post-excess window (spec §2.5).

    Args:
        ohlc_h4: OHLC frame (RangeIndex assumed); same shape as the
            one passed to ``detect_excess`` for this excess.
        bb: Bollinger bands, same index.
        excess: parent excess.
        max_return_bars: max H4 bars after the excess in which a
            return may still fire (spec §3.2 anchored at 3).
        now_bar_index: positional cutoff — bars with index >
            ``now_bar_index`` are not yet observable. Pipeline pegs
            this to the just-closed bar at ``now_utc``.
        killzone_*: window bounds — same as ``detect_excess``.

    Returns:
        ``ReturnEvent`` on the first eligible in-killzone return-inside
        bar; ``None`` if the window expires (or is fully out-of-killzone)
        without one.
    """
    if max_return_bars < 1:
        raise ValueError(f"max_return_bars must be >= 1, got {max_return_bars}")

    n = len(ohlc_h4)
    last_idx = min(excess.bar_index + max_return_bars, n - 1, now_bar_index)
    closes = ohlc_h4["close"].to_numpy(dtype="float64")
    times = pd.to_datetime(ohlc_h4["time"], utc=True)

    upper = bb.upper.to_numpy(dtype="float64")
    lower = bb.lower.to_numpy(dtype="float64")
    sma = bb.sma.to_numpy(dtype="float64")

    for j in range(excess.bar_index + 1, last_idx + 1):
        bar_ts = pd.Timestamp(times.iloc[j])
        if not _is_in_killzone(
            _bar_close_time(bar_ts),
            london_start=killzone_london_start_utc,
            london_end=killzone_london_end_utc,
            ny_start=killzone_ny_start_utc,
            ny_end=killzone_ny_end_utc,
        ):
            continue

        bar_upper = upper[j]
        bar_lower = lower[j]
        if math.isnan(bar_upper) or math.isnan(bar_lower):
            continue

        bar_close = float(closes[j])
        # Spec §2.5: strictly inside both bands.
        if bar_lower < bar_close < bar_upper:
            return ReturnEvent(
                excess_event=excess,
                return_bar_timestamp=bar_ts.to_pydatetime(),
                return_bar_index=j,
                return_bar_close=bar_close,
                return_bar_high=float(ohlc_h4["high"].iloc[j]),
                return_bar_low=float(ohlc_h4["low"].iloc[j]),
                sma_at_return=float(sma[j]),
            )

    return None
