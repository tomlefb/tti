"""Market Structure Shift (MSS) detection on M5 — calibrated rule.

Per docs/01 §5 Step 2 and docs/07 §1.2: after a valid sweep, watch M5
for a body-close break of the most recent significant opposite-side swing,
combined with an impulsive ("displacement") candle.

Two parameters are calibrated (kept at defaults until Sprint 3 integration
data drives a tuning pass):

- ``MSS_DISPLACEMENT_MULTIPLIER`` — how much body size counts as impulsive.
- ``MSS_DISPLACEMENT_LOOKBACK`` — how many M5 candles to mean over.

Heuristics documented inline (docs/07 §1.3):

- "Most recent" swing high to break = highest-time confirmed swing of the
  opposite type whose price strictly dominates the sweep extreme. An
  alternative would be "highest-priced significant swing" or "swing with
  most touches".
- The displacement-satisfying candle may be the MSS candle itself OR one
  of the 3 preceding candles. Alternative: strict "MSS candle itself"
  (more conservative, kills clean breakouts that consolidate immediately).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import pandas as pd

from .sweep import Sweep
from .swings import find_swings

_DISPLACEMENT_TRAILING_LOOKBACK = 3
"""Number of M5 candles BEFORE the MSS-confirming candle that may also
satisfy the displacement check. Keep equal to the value documented in
the module docstring; 3 is the default per spec."""


@dataclass(frozen=True)
class MSS:
    """One detected Market Structure Shift on M5.

    ``displacement_body_ratio`` is the actual body size of the candle
    that satisfied the displacement check, divided by the mean body of
    the previous ``MSS_DISPLACEMENT_LOOKBACK`` M5 candles. By construction
    ``>= MSS_DISPLACEMENT_MULTIPLIER``.
    """

    direction: Literal["bullish", "bearish"]
    sweep: Sweep
    broken_swing_time_utc: datetime
    broken_swing_price: float
    mss_confirm_candle_time_utc: datetime
    mss_confirm_candle_close: float
    displacement_body_ratio: float
    displacement_candle_time_utc: datetime


def detect_mss(
    df_m5: pd.DataFrame,
    sweep: Sweep,
    *,
    swing_lookback_m5: int,
    min_swing_amplitude_atr_mult: float,
    displacement_multiplier: float,
    displacement_lookback: int,
    max_lookforward_minutes: int = 120,
    atr_period: int = 14,
) -> MSS | None:
    """Detect a Market Structure Shift on M5 after ``sweep``.

    Bullish flow (``sweep.direction == "bullish"``, i.e. a low was swept):

    - Identify the **most recent significant swing high** on M5 whose
      ``time_utc < candidate_close_time`` AND whose ``price > sweep
      .sweep_extreme_price`` (it must be a real opposite pivot to break,
      not a random nearby high). Significance uses the same
      ``find_swings(SWING_LOOKBACK_M5, MIN_SWING_AMPLITUDE_ATR_MULT)``
      pipeline as Sprint 1.
    - MSS confirmed when an M5 candle **closes** (body close, not just
      wick) strictly above that swing high.
    - The MSS-confirming candle OR one of the
      ``_DISPLACEMENT_TRAILING_LOOKBACK`` preceding candles must have a
      body ``>= displacement_multiplier × mean_body`` of the previous
      ``displacement_lookback`` M5 candles ending immediately before the
      candidate.

    Bearish flow is symmetric on highs.

    Search window is ``(sweep.return_candle_time_utc, sweep
    .return_candle_time_utc + max_lookforward_minutes]``. The sweep's
    return candle itself is NOT a valid MSS candidate — MSS is the
    *next* structural event.

    Args:
        df_m5: M5 OHLC frame (UTC ``time`` column).
        sweep: triggering sweep.
        swing_lookback_m5: ``SWING_LOOKBACK_M5`` from settings.
        min_swing_amplitude_atr_mult: ``MIN_SWING_AMPLITUDE_ATR_MULT``.
        displacement_multiplier: ``MSS_DISPLACEMENT_MULTIPLIER``.
        displacement_lookback: ``MSS_DISPLACEMENT_LOOKBACK`` (the N for
            the mean-body baseline).
        max_lookforward_minutes: cap on the forward search window.
            Default 120 (= 24 M5 candles).
        atr_period: ATR period for the swing amplitude filter (14).

    Returns:
        ``MSS`` on first confirmation; ``None`` if no MSS within window.
    """
    if displacement_lookback < 1:
        raise ValueError(f"displacement_lookback must be >= 1, got {displacement_lookback}")
    if displacement_multiplier <= 0:
        raise ValueError(f"displacement_multiplier must be > 0, got {displacement_multiplier}")
    if max_lookforward_minutes < 0:
        raise ValueError(f"max_lookforward_minutes must be >= 0, got {max_lookforward_minutes}")
    if len(df_m5) == 0:
        return None

    times = pd.to_datetime(df_m5["time"], utc=True)
    times_py = [pd.Timestamp(t).to_pydatetime() for t in times]
    n = len(df_m5)
    opens = df_m5["open"].to_numpy(dtype="float64")
    closes = df_m5["close"].to_numpy(dtype="float64")

    # Significant M5 swings — needed to identify the level whose break
    # constitutes the structure shift. Deliberately called WITHOUT
    # ``now_utc``: the swing-confirmation leak that affects
    # ``mark_swing_levels`` (post-MSS-time data inflating the trailing-N
    # significant-swing window) does NOT apply here, because the MSS
    # iteration below caps each candidate's pivot index at
    # ``i - swing_confirmation_offset``. That guarantees every pivot the
    # MSS detector reads has been confirmed by candle ``i`` (the MSS
    # candidate itself), so any pivot data past ``i`` is naturally
    # ignored. Adding ``now_utc`` here would be a no-op and only obscure
    # the contract.
    sig_swings = find_swings(
        df_m5,
        lookback=swing_lookback_m5,
        min_amplitude_atr_mult=min_swing_amplitude_atr_mult,
        atr_period=atr_period,
    )
    swing_types = sig_swings["swing_type"].to_numpy()
    swing_prices = sig_swings["swing_price"].to_numpy(dtype="float64")
    # Pivots are confirmed only ``swing_lookback_m5`` candles AFTER the
    # pivot bar. We capture that confirmation time so we never use a
    # pivot before it would have been visible to the system in real time.
    swing_confirmation_offset = swing_lookback_m5

    search_start = sweep.return_candle_time_utc
    search_end = search_start + timedelta(minutes=max_lookforward_minutes)

    if sweep.direction == "bullish":
        opposite_pivot_type = "high"
        sweep_extreme = sweep.sweep_extreme_price

        def is_break(close: float, pivot_price: float) -> bool:
            return close > pivot_price

        def pivot_passes_geometry(pivot_price: float) -> bool:
            return pivot_price > sweep_extreme

    elif sweep.direction == "bearish":
        opposite_pivot_type = "low"
        sweep_extreme = sweep.sweep_extreme_price

        def is_break(close: float, pivot_price: float) -> bool:
            return close < pivot_price

        def pivot_passes_geometry(pivot_price: float) -> bool:
            return pivot_price < sweep_extreme

    else:  # pragma: no cover — Literal narrows this in the type system
        raise ValueError(f"unexpected sweep.direction: {sweep.direction!r}")

    for i in range(n):
        candle_time = times_py[i]
        if candle_time <= search_start:
            continue
        if candle_time > search_end:
            break

        # Find the most recent (highest-time) opposite pivot CONFIRMED
        # before this candle's close. "Confirmed before" means the
        # pivot bar is at least ``swing_confirmation_offset`` candles
        # before ``i``.
        latest_pivot_idx: int | None = None
        latest_pivot_price: float | None = None
        max_pivot_idx = i - swing_confirmation_offset
        for k in range(max_pivot_idx, -1, -1):
            if swing_types[k] != opposite_pivot_type:
                continue
            price = float(swing_prices[k])
            if not pivot_passes_geometry(price):
                continue
            latest_pivot_idx = k
            latest_pivot_price = price
            break

        if latest_pivot_idx is None or latest_pivot_price is None:
            continue

        if not is_break(float(closes[i]), latest_pivot_price):
            continue

        # Displacement check — current candle and the
        # ``_DISPLACEMENT_TRAILING_LOOKBACK`` preceding candles are eligible.
        disp = _check_displacement(
            opens=opens,
            closes=closes,
            times_py=times_py,
            mss_idx=i,
            displacement_multiplier=displacement_multiplier,
            displacement_lookback=displacement_lookback,
        )
        if disp is None:
            continue

        return MSS(
            direction=sweep.direction,
            sweep=sweep,
            broken_swing_time_utc=times_py[latest_pivot_idx],
            broken_swing_price=latest_pivot_price,
            mss_confirm_candle_time_utc=candle_time,
            mss_confirm_candle_close=float(closes[i]),
            displacement_body_ratio=disp[0],
            displacement_candle_time_utc=disp[1],
        )

    return None


def _check_displacement(
    opens,
    closes,
    times_py: list[datetime],
    mss_idx: int,
    displacement_multiplier: float,
    displacement_lookback: int,
) -> tuple[float, datetime] | None:
    """Return ``(body_ratio, candle_time)`` for the first qualifying candle.

    The MSS candle is checked first, then walks backwards through the
    ``_DISPLACEMENT_TRAILING_LOOKBACK`` preceding candles. Each candidate
    is compared against the mean body of the ``displacement_lookback``
    candles ending immediately before that candidate.
    """
    candidates = [
        c for c in range(mss_idx, mss_idx - _DISPLACEMENT_TRAILING_LOOKBACK - 1, -1) if c >= 0
    ]
    for cand in candidates:
        baseline_start = cand - displacement_lookback
        if baseline_start < 0:
            continue
        mean_body = _mean_body_range(opens, closes, baseline_start, cand)
        if mean_body <= 0:
            continue
        body = abs(float(closes[cand]) - float(opens[cand]))
        ratio = body / mean_body
        if ratio >= displacement_multiplier:
            return (ratio, times_py[cand])
    return None


def _mean_body(df_m5: pd.DataFrame, end_idx: int, lookback: int) -> float:
    """Mean ``abs(close - open)`` over the ``lookback`` candles ending at ``end_idx-1``.

    Public-helper variant used by tests / debugging. The detector itself
    uses the inlined-numpy ``_mean_body_range`` for speed.

    Args:
        df_m5: M5 OHLC frame.
        end_idx: index AFTER the last candle to include in the mean. The
            window is ``[end_idx - lookback, end_idx)``.
        lookback: how many candles to average. Must be ``>= 1``.

    Returns:
        Mean absolute body. ``0.0`` if the requested window is invalid.
    """
    if lookback < 1:
        raise ValueError(f"lookback must be >= 1, got {lookback}")
    start = end_idx - lookback
    if start < 0 or end_idx > len(df_m5):
        return 0.0
    bodies = (df_m5["close"].iloc[start:end_idx] - df_m5["open"].iloc[start:end_idx]).abs()
    return float(bodies.mean()) if len(bodies) > 0 else 0.0


def _mean_body_range(opens, closes, start: int, end: int) -> float:
    """Mean ``|close - open|`` over ``[start, end)``. ``end`` exclusive."""
    if end <= start:
        return 0.0
    total = 0.0
    for k in range(start, end):
        total += abs(float(closes[k]) - float(opens[k]))
    return total / (end - start)
