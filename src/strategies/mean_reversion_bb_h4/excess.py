"""Excess detection — spec §2.2.

A pure per-bar function. The pipeline calls ``detect_excess`` for
the candidate bar (typically the just-closed H4 at ``now_utc``) and
either receives an ``ExcessEvent`` or ``None``. The killzone filter
is applied here, structurally — an off-session excess is dropped
before any subsequent stage runs.

The §2.3 ATR-penetration filter is intentionally NOT applied inside
``detect_excess``: keeping the two filters separate makes it
trivial for the gate-3 audit harness to differ-by-component, and
matches the spec's modular pseudo-code (one function per filter).
"""

from __future__ import annotations

import math
from datetime import time, timedelta

import pandas as pd

from .types import BollingerBands, ExcessEvent

_H4 = timedelta(hours=4)


def _is_in_killzone(
    bar_close_time: time,
    *,
    london_start: time,
    london_end: time,
    ny_start: time,
    ny_end: time,
) -> bool:
    """``True`` iff the bar's **close** time-of-day is in either window
    `[start, end]` (both ends inclusive).

    Spec §2.2 (Option A): the killzone gate is evaluated at the close
    timestamp because the detection decision is taken at the close.
    With the H4-grid defaults from ``types.py``, this rule yields the
    in-killzone close set ``{08:00, 12:00, 16:00}`` (3 bars per UTC
    day — same convention as the archived breakout-retest spec).
    """
    london_in = london_start <= bar_close_time <= london_end
    ny_in = ny_start <= bar_close_time <= ny_end
    return london_in or ny_in


def _bar_close_time(bar_open_ts: pd.Timestamp) -> time:
    """Return the time-of-day of the H4 bar's CLOSE in UTC."""
    close_ts = bar_open_ts + _H4
    if close_ts.tzinfo is not None:
        close_ts = close_ts.tz_convert("UTC")
    return close_ts.time()


def detect_excess(
    ohlc_h4: pd.DataFrame,
    bb: BollingerBands,
    *,
    bar_index: int,
    killzone_london_start_utc: time,
    killzone_london_end_utc: time,
    killzone_ny_start_utc: time,
    killzone_ny_end_utc: time,
) -> ExcessEvent | None:
    """Detect a Bollinger excess at ``bar_index`` (spec §2.2).

    Args:
        ohlc_h4: OHLC frame with columns ``time, open, high, low, close``;
            ``time`` is UTC tz-aware. Read by position (RangeIndex
            assumed).
        bb: Bollinger bands as returned by ``compute_bollinger``;
            same index as ``ohlc_h4["close"]``.
        bar_index: positional index of the bar to test. The caller
            (pipeline) supplies the just-closed bar's index.
        killzone_*: window bounds — see ``StrategyParams`` and the
            module-level docstring of ``types.py``.

    Returns:
        ``ExcessEvent`` if (a) the bar is in killzone and (b) its
        close pierces a band strictly. ``None`` otherwise.

    Raises:
        IndexError: if ``bar_index`` is out of bounds for ``ohlc_h4``.
            (Surfaced so a caller bug never silently no-ops.)
    """
    if bar_index < 0 or bar_index >= len(ohlc_h4):
        raise IndexError(
            f"detect_excess: bar_index {bar_index} out of range "
            f"[0, {len(ohlc_h4)})"
        )

    bar_ts = pd.Timestamp(ohlc_h4["time"].iloc[bar_index])
    if not _is_in_killzone(
        _bar_close_time(bar_ts),
        london_start=killzone_london_start_utc,
        london_end=killzone_london_end_utc,
        ny_start=killzone_ny_start_utc,
        ny_end=killzone_ny_end_utc,
    ):
        return None

    close = float(ohlc_h4["close"].iloc[bar_index])
    upper = float(bb.upper.iloc[bar_index])
    lower = float(bb.lower.iloc[bar_index])
    if math.isnan(upper) or math.isnan(lower):
        # Bands not yet defined (bar_index < period - 1).
        return None

    if close > upper:
        direction: str = "upper"
        bb_level = upper
    elif close < lower:
        direction = "lower"
        bb_level = lower
    else:
        return None

    high = float(ohlc_h4["high"].iloc[bar_index])
    low = float(ohlc_h4["low"].iloc[bar_index])
    # ``penetration_atr`` is filled by the caller after the §2.3
    # filter has been applied; at the bare-detection layer we only
    # know the raw |close - level|. Store NaN here and let the
    # filter populate the final figure if it accepts the excess.
    return ExcessEvent(
        timestamp_utc=bar_ts.to_pydatetime(),
        bar_index=bar_index,
        direction=direction,  # type: ignore[arg-type]
        close=close,
        high=high,
        low=low,
        bb_level=bb_level,
        penetration_atr=float("nan"),
    )
