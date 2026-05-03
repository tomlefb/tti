"""D1 bias filter — spec §2.1."""

from __future__ import annotations

from typing import Literal

import pandas as pd

Bias = Literal["bullish", "bearish", "neutral"]


def bias_d1(close_d1: pd.Series, *, ma_period: int = 50) -> Bias:
    """Compute the D1 bias — see spec §2.1. Stub; tests drive the body."""
    raise NotImplementedError
