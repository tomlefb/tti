"""Setup builder — spec §2.5."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from .bias import Bias
from .breakout import BreakoutEvent
from .retest import RetestEvent


@dataclass(frozen=True)
class Setup:
    """Final trade plan — spec §2.5.

    Attributes:
        timestamp_utc: open time of the retest bar (the bar whose close
            triggers the entry).
        instrument: e.g. ``"XAUUSD"`` / ``"NDX100"``.
        direction: ``"long"`` or ``"short"``.
        entry_price: retest bar's close.
        stop_loss: ``retest.low - sl_buffer`` (long) or
            ``retest.high + sl_buffer`` (short).
        take_profit: ``entry + (entry - sl) * rr_target`` (long), symmetric short.
        risk_reward: realised RR (== ``rr_target`` by construction).
        bias_d1: D1 bias evaluated at the breakout bar (spec §5.6).
        breakout_event: parent breakout.
        retest_event: parent retest.
    """

    timestamp_utc: datetime
    instrument: str
    direction: Literal["long", "short"]
    entry_price: float
    stop_loss: float
    take_profit: float
    risk_reward: float
    bias_d1: Bias
    breakout_event: BreakoutEvent
    retest_event: RetestEvent


def build_setup(
    retest_event: RetestEvent,
    *,
    instrument: str,
    bias_d1: Bias,
    sl_buffer: float,
    rr_target: float,
) -> Setup:
    """Build a Setup from a confirmed retest — see spec §2.5. Stub."""
    raise NotImplementedError
