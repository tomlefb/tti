"""Pipeline orchestration — wires bias → swings → breakout → retest → setup.

The detection layers are pure: they read inputs and return events. The
pipeline owns the only mutable state (``StrategyState``) and is the
sole writer to ``locked_swings`` / ``trades_today``.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from .setup import Setup
from .types import StrategyParams, StrategyState


def build_setup_candidates(
    ohlc_h4: pd.DataFrame,
    close_d1: pd.Series,
    instrument: str,
    params: StrategyParams,
    state: StrategyState,
    *,
    now_utc: datetime,
) -> list[Setup]:
    """Run one detection cycle — see spec §2 and pipeline.py docstring. Stub."""
    raise NotImplementedError
