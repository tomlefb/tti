"""H4 swing detection — spec §2.2."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import pandas as pd


@dataclass(frozen=True)
class Swing:
    """A confirmed fractal pivot on H4 (spec §2.2).

    Attributes:
        timestamp_utc: open time of the pivot bar (UTC, tz-aware).
        price: pivot price (high for ``direction="high"``, low otherwise).
        direction: ``"high"`` or ``"low"``.
        bar_index: positional index into the OHLC frame supplied to
            ``detect_swings_h4``. The caller's frame must be 0-indexed
            consecutively (default ``RangeIndex``).
    """

    timestamp_utc: datetime
    price: float
    direction: Literal["high", "low"]
    bar_index: int


def detect_swings_h4(
    ohlc_h4: pd.DataFrame,
    n_swing: int,
    *,
    now_utc: datetime | None = None,
    timeframe: timedelta = timedelta(hours=4),
) -> tuple[list[Swing], list[Swing]]:
    """Detect confirmed swing highs / lows — see spec §2.2. Stub."""
    raise NotImplementedError
