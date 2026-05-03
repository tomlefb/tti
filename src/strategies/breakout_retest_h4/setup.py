"""Setup builder — spec §2.5.

Long: entry = retest_close, SL = retest_low - sl_buffer,
      TP = entry + (entry - SL) * rr_target.
Short symmetric.

The bias passed in here is the bias evaluated **at the breakout bar**
and locked into the setup lifecycle (spec §5.6).
"""

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
    """Build a Setup from a confirmed retest (spec §2.5).

    Args:
        retest_event: the retest produced by ``detect_retest``.
        instrument: instrument label, e.g. ``"XAUUSD"``.
        bias_d1: bias to lock into the setup. Per spec §5.6 the
            caller passes the bias evaluated at the breakout bar.
        sl_buffer: instrument-priced buffer added beyond the retest
            extreme.
        rr_target: fixed RR multiple — spec §3.1 anchors at 2.0.

    Returns:
        ``Setup`` with arithmetic per spec §2.5.

    Raises:
        ValueError: if the computed risk is zero or negative (a
            degenerate retest where the close coincides with the
            retest extreme + buffer; the caller should then fail the
            invalidation rule, but we surface it explicitly so a
            silent divide-by-zero never produces a TP arithmetic bug).
    """
    breakout = retest_event.breakout_event
    direction = breakout.direction
    entry = retest_event.retest_bar_close

    if direction == "long":
        sl = retest_event.retest_bar_low - sl_buffer
        risk = entry - sl
    else:
        sl = retest_event.retest_bar_high + sl_buffer
        risk = sl - entry

    if risk <= 0:
        raise ValueError(
            f"build_setup: non-positive risk ({risk}) — degenerate retest. "
            f"entry={entry} sl={sl} direction={direction}"
        )

    if direction == "long":
        tp = entry + risk * rr_target
    else:
        tp = entry - risk * rr_target

    return Setup(
        timestamp_utc=retest_event.retest_bar_timestamp,
        instrument=instrument,
        direction=direction,
        entry_price=float(entry),
        stop_loss=float(sl),
        take_profit=float(tp),
        risk_reward=float(rr_target),
        bias_d1=bias_d1,
        breakout_event=breakout,
        retest_event=retest_event,
    )
