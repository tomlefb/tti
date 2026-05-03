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
from datetime import time

import pandas as pd

from .types import BollingerBands, ExcessEvent


def _is_in_killzone(
    bar_time: time,
    *,
    london_start: time,
    london_end: time,
    ny_start: time,
    ny_end: time,
) -> bool:
    """``True`` iff the bar's open time falls in either killzone window.

    Spec §2.2 narrative is the source: with the H4-grid-derived
    defaults from ``types.py``, this rule reproduces the in-killzone
    set ``{08:00, 12:00}``.
    """
    london_in = london_start <= bar_time < london_end
    ny_in = ny_start <= bar_time < ny_end
    return london_in or ny_in


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
    bar_time_utc = bar_ts.tz_convert("UTC").time() if bar_ts.tzinfo else bar_ts.time()
    if not _is_in_killzone(
        bar_time_utc,
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
