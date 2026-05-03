"""Excess filters — spec §2.3 (ATR penetration), §2.4 (exhaustion, REMOVED v1.1).

In v1.1 the pipeline applies only ``passes_penetration``;
``is_exhaustion_candle`` is kept here for reference (and still
covered by ``test_filters.py``) but never called. See the
function's docstring + spec §2.4 "Removal rationale" for the
gate-3 attrition data that drove the v1.1 deactivation.

Both functions are pure. ``passes_penetration`` is consumed by
the pipeline immediately after ``detect_excess`` accepts a bar.
"""

from __future__ import annotations

import math
from typing import Literal

from .types import ExcessEvent


def passes_penetration(
    excess: ExcessEvent,
    *,
    atr_at_bar: float,
    min_pen_atr_mult: float,
) -> tuple[bool, float]:
    """ATR-relative penetration test — spec §2.3.

    Args:
        excess: the excess event from ``detect_excess``.
        atr_at_bar: ATR value at ``excess.bar_index``. The pipeline
            computes the ATR series once and supplies the bar-aligned
            value here.
        min_pen_atr_mult: minimum ``penetration / atr`` to accept.
            Calibrated per ``StrategyParams`` §3.2.

    Returns:
        ``(passes, penetration_in_atr_units)``. ``penetration_in_atr_units``
        is the metric to record on the ``ExcessEvent`` for audit /
        debug — the pipeline will rebuild the dataclass with this
        value substituted in.
    """
    if excess.direction == "upper":
        penetration = excess.close - excess.bb_level
    else:
        penetration = excess.bb_level - excess.close

    threshold = min_pen_atr_mult * atr_at_bar

    if atr_at_bar > 0:
        pen_atr = penetration / atr_at_bar
    else:
        # ATR == 0 (degenerate flat fixture). Threshold is 0 so the
        # raw inequality still answers correctly, and we surface a
        # sentinel pen_atr (inf if pen > 0, NaN if pen == 0).
        if penetration > 0:
            pen_atr = math.inf
        else:
            pen_atr = math.nan

    return penetration >= threshold, pen_atr


def is_exhaustion_candle(
    *,
    direction: Literal["upper", "lower"],
    bar_open: float,
    bar_high: float,
    bar_low: float,
    bar_close: float,
    min_wick_ratio: float,
    max_body_ratio: float,
) -> bool:
    """Rejection-wick / exhaustion test — spec §2.4 (REMOVED v1.1).

    **Deprecated v1.1 (commit ae61f70)**: kept for reference, no
    longer applied by the pipeline. See spec §2.4 "Removal rationale"
    — the gate-3 attrition diagnostic measured 3.7 % retention at
    this gate (the steepest in the chain), making the n_closed >= 50
    admission floor unreachable on every grid cell. The function and
    its tests stay in the codebase as a v2 / v3 candidate filter.

    For an upper-side excess, the rejection wick is the **upper** wick:
    ``high - max(open, close)``. For a lower-side excess, it is the
    **lower** wick: ``min(open, close) - low``.

    Args:
        direction: the excess direction.
        bar_open / bar_high / bar_low / bar_close: the H4 bar's OHLC.
        min_wick_ratio: minimum ``wick / range`` to accept (v1.0
            spec §3.1: 0.4).
        max_body_ratio: maximum ``body / range`` to accept (v1.0
            spec §3.1: 0.5).

    Returns:
        ``True`` iff both ratio thresholds are satisfied.
    """
    rng = bar_high - bar_low
    if rng <= 0:
        return False

    body = abs(bar_close - bar_open)

    if direction == "upper":
        wick = bar_high - max(bar_close, bar_open)
    else:
        wick = min(bar_close, bar_open) - bar_low

    body_ratio = body / rng
    wick_ratio = wick / rng

    return wick_ratio >= min_wick_ratio and body_ratio <= max_body_ratio
