"""Breakout detection — spec §2.3."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import pandas as pd

from .bias import Bias
from .swings import Swing


@dataclass(frozen=True)
class BreakoutEvent:
    """A swing that has been broken on H4 close — see spec §2.3.

    Attributes:
        swing: the swing that was broken (its level is the breakout
            level).
        breakout_bar_timestamp: open time of the H4 bar whose close
            crossed the swing.
        breakout_bar_close: close of that bar.
        direction: ``"long"`` (broken high under bullish bias) or
            ``"short"`` (broken low under bearish bias).
    """

    swing: Swing
    breakout_bar_timestamp: datetime
    breakout_bar_close: float
    direction: Literal["long", "short"]


def detect_breakout(
    ohlc_h4: pd.DataFrame,
    swings_high: list[Swing],
    swings_low: list[Swing],
    bias: Bias,
    locked_swings: set[Swing],
    *,
    now_utc: datetime | None = None,
    n_swing: int = 5,
    timeframe: timedelta = timedelta(hours=4),
) -> BreakoutEvent | None:
    """Detect a breakout — see spec §2.3. Stub."""
    raise NotImplementedError
