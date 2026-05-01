"""Daily bias from H4 + H1 swing structure (see docs/01 §3, docs/07 §1.3).

Public API:

- ``compute_timeframe_bias(swings, bias_swing_count)`` — bias on a single
  timeframe from its significant-swings DataFrame.
- ``compute_daily_bias(df_h4, df_h1, ...)`` — full daily bias requiring H4
  and H1 agreement.

Pure functions, no I/O.
"""

from __future__ import annotations

from typing import Literal

import pandas as pd

from .swings import find_swings

Bias = Literal["bullish", "bearish", "no_trade"]


def _is_strict_bullish_structure(highs: list[float], lows: list[float]) -> bool:
    """Strict HH AND HL: each new high > previous high; each new low > previous low.

    "Broken-structure -> no_trade" is a heuristic per docs/07 §1.3 — by
    requiring the *full* sequence to be strictly increasing on both
    legs, a single late LL or LH instantly invalidates the bias.
    """
    if len(highs) < 2 or len(lows) < 2:
        return False
    for a, b in zip(highs, highs[1:], strict=False):
        if not b > a:
            return False
    for a, b in zip(lows, lows[1:], strict=False):
        if not b > a:
            return False
    return True


def _is_strict_bearish_structure(highs: list[float], lows: list[float]) -> bool:
    """Strict LH AND LL: symmetric to ``_is_strict_bullish_structure``."""
    if len(highs) < 2 or len(lows) < 2:
        return False
    for a, b in zip(highs, highs[1:], strict=False):
        if not b < a:
            return False
    for a, b in zip(lows, lows[1:], strict=False):
        if not b < a:
            return False
    return True


def compute_timeframe_bias(swings: pd.DataFrame, bias_swing_count: int) -> Bias:
    """Determine bias from a single timeframe's significant swings.

    Looks at the **last** ``bias_swing_count`` significant swing rows in
    ``swings`` (i.e., rows where ``swing_type`` is not ``None``).

    - ``"bullish"`` if the sequence shows Higher Highs AND Higher Lows.
    - ``"bearish"`` if it shows Lower Highs AND Lower Lows.
    - ``"no_trade"`` otherwise — including the ``"recent structure break"``
      heuristic from docs/01 §3 / docs/07 §1.3: any single counter-trend
      swing inside the window collapses the strict ordering and yields
      ``"no_trade"``. This may be revisited.

    "Insufficient data" (fewer than ``bias_swing_count`` significant swings,
    or fewer than 2 highs and 2 lows in the window) also returns
    ``"no_trade"``.

    Args:
        swings: DataFrame as produced by
            ``swings.filter_significant_swings`` / ``swings.find_swings``.
            Must contain ``swing_type`` and ``swing_price``.
        bias_swing_count: number of trailing significant swings to consider.

    Returns:
        ``"bullish" | "bearish" | "no_trade"``.
    """
    if bias_swing_count < 2:
        raise ValueError(f"bias_swing_count must be >= 2, got {bias_swing_count}")

    sig = swings[swings["swing_type"].notna()]
    if len(sig) < bias_swing_count:
        return "no_trade"

    window = sig.iloc[-bias_swing_count:]
    types = window["swing_type"].tolist()
    prices = window["swing_price"].tolist()

    highs = [p for t, p in zip(types, prices, strict=False) if t == "high"]
    lows = [p for t, p in zip(types, prices, strict=False) if t == "low"]

    if _is_strict_bullish_structure(highs, lows):
        return "bullish"
    if _is_strict_bearish_structure(highs, lows):
        return "bearish"
    return "no_trade"


def compute_daily_bias(
    df_h4: pd.DataFrame,
    df_h1: pd.DataFrame,
    *,
    swing_lookback_h4: int,
    swing_lookback_h1: int,
    min_amplitude_atr_mult_h4: float,
    min_amplitude_atr_mult_h1: float,
    bias_swing_count: int,
    require_h1_confirmation: bool = False,
    atr_period: int = 14,
) -> Bias:
    """Compute the daily bias.

    By default (``require_h1_confirmation=False``) the bias is determined
    by H4 structure alone — H1 swings are not consulted. Set the flag to
    ``True`` to require H4 AND H1 agreement (Sprint 1's original behaviour).

    Sprint 3 rationale (see ``calibration/runs/FINAL_swing_calibration.md``
    Sprint 3 amendment, plus the diagnostic dive on XAUUSD 2025-10-15):
    H1 swings exhibit lower-order geometric pivots (P=42%, R=75%, F1≈54%
    against operator annotations) which do not represent tradeable major
    liquidity. Empirically, H1 disagreement on clean trending days
    sabotages valid bias signals — e.g. XAUUSD 2025-10-15 has H4 calling
    the day correctly bullish but H1 inserting a noise-driven LL that
    flips H1 bias to no_trade, killing the H4∩H1 intersection.

    Per docs/07 §1.2 / §1.3 this is a heuristic decision, not a free
    parameter — the legacy mode is preserved behind the flag.

    Args:
        df_h4: H4 OHLC frame.
        df_h1: H1 OHLC frame.
        swing_lookback_h4: ``SWING_LOOKBACK_H4`` from settings.
        swing_lookback_h1: ``SWING_LOOKBACK_H1`` from settings.
        min_amplitude_atr_mult_h4: ``MIN_SWING_AMPLITUDE_ATR_MULT_H4``.
        min_amplitude_atr_mult_h1: ``MIN_SWING_AMPLITUDE_ATR_MULT_H1``.
        bias_swing_count: ``BIAS_SWING_COUNT`` from settings.
        require_h1_confirmation: ``BIAS_REQUIRE_H1_CONFIRMATION`` —
            ``False`` (Sprint 3 default) ⇒ H4 alone; ``True`` ⇒ H4 ∧ H1.
        atr_period: ATR window used by the amplitude filter (default 14).

    Returns:
        ``"bullish" | "bearish" | "no_trade"``.
    """
    # NB: deliberately called without ``now_utc``. The orchestrator pre-slices
    # df_h4 / df_h1 to ``time < kz_start_utc`` (see
    # ``setup._slice_frame_until``) before reaching this function, which
    # already enforces the swing-confirmation bound: a pivot at index k is
    # only detectable here if the lookback-after candles also fall within
    # the slice (i.e. before kz_start_utc). Since ``kz_start_utc <= now_utc``
    # for any setup the orchestrator will eventually emit, the bias is
    # unaffected by the swing-confirmation leak and forwarding ``now_utc``
    # would be redundant.
    swings_h4 = find_swings(
        df_h4,
        lookback=swing_lookback_h4,
        min_amplitude_atr_mult=min_amplitude_atr_mult_h4,
        atr_period=atr_period,
    )
    bias_h4 = compute_timeframe_bias(swings_h4, bias_swing_count)

    if not require_h1_confirmation:
        # H4-only mode (Sprint 3 default). Returns H4's own classification
        # — including ``no_trade`` when H4 itself doesn't have a clean
        # HH/HL or LH/LL pattern.
        return bias_h4

    swings_h1 = find_swings(
        df_h1,
        lookback=swing_lookback_h1,
        min_amplitude_atr_mult=min_amplitude_atr_mult_h1,
        atr_period=atr_period,
    )
    bias_h1 = compute_timeframe_bias(swings_h1, bias_swing_count)
    if bias_h4 == "bullish" and bias_h1 == "bullish":
        return "bullish"
    if bias_h4 == "bearish" and bias_h1 == "bearish":
        return "bearish"
    return "no_trade"
