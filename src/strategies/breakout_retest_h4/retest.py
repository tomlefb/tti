"""Retest detection — spec §2.4."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

from .breakout import BreakoutEvent


@dataclass(frozen=True)
class RetestEvent:
    """A clean retest of a broken level — see spec §2.4.

    Attributes:
        breakout_event: the parent breakout this retest confirms.
        retest_bar_timestamp: open time of the H4 bar that touched and
            held the broken level.
        retest_bar_low: that bar's low (used for SL on long).
        retest_bar_high: that bar's high (used for SL on short).
        retest_bar_close: that bar's close (used for the entry).
    """

    breakout_event: BreakoutEvent
    retest_bar_timestamp: datetime
    retest_bar_low: float
    retest_bar_high: float
    retest_bar_close: float


def detect_retest(
    ohlc_h4: pd.DataFrame,
    breakout_event: BreakoutEvent,
    n_retest: int,
    retest_tolerance: float,
    *,
    now_utc: datetime | None = None,
    timeframe: timedelta = timedelta(hours=4),
) -> RetestEvent | None:
    """Detect a retest of ``breakout_event.swing.price`` — spec §2.4. Stub."""
    raise NotImplementedError
