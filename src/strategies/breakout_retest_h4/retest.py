"""Retest detection — spec §2.4.

Within the ``n_retest`` H4 bars *after* the breakout bar, look for the
first bar that simultaneously **touches** the broken level (wick
within ``retest_tolerance``) and **holds** the breakout side on close.

Spec asymmetry note (deliberate, preserved verbatim): the breakout
scan in §2.3 includes ``now_idx`` whereas the retest scan in §2.4
breaks at ``j >= now_idx`` — so within a single cycle the breakout
bar can be ``now_idx`` itself, but a retest is only observable from
the *next* cycle. This is the literal reading of the pseudo-code and
is reported in the gate-2 deviation log.
"""

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


def _now_idx(
    ohlc_h4: pd.DataFrame,
    now_utc: datetime | None,
    timeframe: timedelta,
) -> int:
    n = len(ohlc_h4)
    if n == 0:
        return -1
    if now_utc is None:
        return n  # Permissive: full frame is observable; spec uses
        # `j >= now_idx: break`, so passing n keeps every j < n eligible.

    times = pd.to_datetime(ohlc_h4["time"], utc=True)
    last_observable = -1
    for i, t in enumerate(times):
        if pd.Timestamp(t).to_pydatetime() + timeframe <= now_utc:
            last_observable = i
        else:
            break
    # The retest scan in spec §2.4 stops at `j >= now_idx`. Setting
    # now_idx = last_observable + 1 means a fully-closed bar at index
    # `last_observable` IS eligible (spec §2.4 reads only closed
    # data); only bars whose close has not yet been observed are
    # excluded.
    return last_observable + 1


def detect_retest(
    ohlc_h4: pd.DataFrame,
    breakout_event: BreakoutEvent,
    n_retest: int,
    retest_tolerance: float,
    *,
    now_utc: datetime | None = None,
    timeframe: timedelta = timedelta(hours=4),
) -> RetestEvent | None:
    """Detect a retest of ``breakout_event.swing.price`` (spec §2.4).

    Args:
        ohlc_h4: OHLC frame indexed identically to the one passed to
            ``detect_breakout`` for this event.
        breakout_event: the parent breakout. The level is
            ``breakout_event.swing.price``; the search starts at
            ``breakout_event.swing.bar_index + 1`` *but* we re-derive
            the breakout bar via ``breakout_bar_timestamp`` to avoid
            relying on the swing's bar_index for the breakout bar
            offset (the breakout may be many bars after the swing).
        n_retest: maximum H4 bars after the breakout bar in which a
            retest may still confirm.
        retest_tolerance: instrument-priced wick buffer; broadens the
            touch test on the wrong side of the level.
        now_utc: production scheduler tick. ``j >= now_idx`` short-
            circuits the scan (spec §2.4).
        timeframe: H4 candle duration.

    Returns:
        ``RetestEvent`` on the first bar that touches and holds; ``None``
        if no such bar in the window.
    """
    if n_retest < 1:
        raise ValueError(f"n_retest must be >= 1, got {n_retest}")

    times = pd.to_datetime(ohlc_h4["time"], utc=True)
    # Locate the breakout bar by timestamp (the BreakoutEvent does not
    # store bar_index; the swing's bar_index is the swing pivot).
    target_ts = pd.Timestamp(breakout_event.breakout_bar_timestamp)
    matches = (times == target_ts).to_numpy().nonzero()[0]
    if len(matches) == 0:
        return None
    break_idx = int(matches[0])

    now_idx = _now_idx(ohlc_h4, now_utc, timeframe)

    highs = ohlc_h4["high"].to_numpy(dtype="float64")
    lows = ohlc_h4["low"].to_numpy(dtype="float64")
    closes = ohlc_h4["close"].to_numpy(dtype="float64")

    level = breakout_event.swing.price

    upper_bound = min(break_idx + 1 + n_retest, len(ohlc_h4))
    for j in range(break_idx + 1, upper_bound):
        if j >= now_idx:
            break
        bar_low = float(lows[j])
        bar_high = float(highs[j])
        bar_close = float(closes[j])
        if breakout_event.direction == "long":
            touched = bar_low <= level + retest_tolerance
            held = bar_close > level
        else:
            touched = bar_high >= level - retest_tolerance
            held = bar_close < level
        if touched and held:
            return RetestEvent(
                breakout_event=breakout_event,
                retest_bar_timestamp=pd.Timestamp(times.iloc[j]).to_pydatetime(),
                retest_bar_low=bar_low,
                retest_bar_high=bar_high,
                retest_bar_close=bar_close,
            )

    return None
