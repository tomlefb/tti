"""D1 bias filter — spec §2.1.

Compare the most recent closed D1 close to the simple moving average
of the last ``ma_period`` D1 closes. Strict comparison; exact equality
returns ``"neutral"`` and the strategy skips that cycle.
"""

from __future__ import annotations

from typing import Literal

import pandas as pd

Bias = Literal["bullish", "bearish", "neutral"]


def bias_d1(close_d1: pd.Series, *, ma_period: int = 50) -> Bias:
    """Compute the D1 bias from a series of D1 closes (spec §2.1).

    Caller contract: ``close_d1`` contains only **closed** D1 candles —
    the production scheduler is responsible for slicing the series so
    the last entry is the last closed-and-published D1 close.

    Args:
        close_d1: pandas Series of D1 closes. Must contain at least
            ``ma_period`` entries; otherwise raises ``ValueError`` so a
            missing-history bug surfaces loudly rather than silently
            returning a wrong bias.
        ma_period: SMA window length. Defaults to 50 per spec §3.1.

    Returns:
        ``"bullish"`` if last close strictly above the SMA, ``"bearish"``
        if strictly below, ``"neutral"`` on exact equality.

    Raises:
        ValueError: if fewer than ``ma_period`` closes are provided.
    """
    if len(close_d1) < ma_period:
        raise ValueError(
            f"bias_d1 requires at least ma_period={ma_period} closes, got {len(close_d1)}"
        )

    last_close = float(close_d1.iloc[-1])
    last_ma = float(close_d1.iloc[-ma_period:].mean())

    if last_close > last_ma:
        return "bullish"
    if last_close < last_ma:
        return "bearish"
    return "neutral"
