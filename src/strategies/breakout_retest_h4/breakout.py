"""Breakout detection — spec §2.3.

A breakout is the first H4 close that crosses a confirmed swing in the
bias direction. Only the **most recent** confirmed swing in that
direction is considered; once a swing has produced a breakout, the
caller is expected to add it to ``locked_swings`` so this function
never re-emits on the same level (anti false-breakout pitfall, §5.1).
"""

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


def _now_idx(
    ohlc_h4: pd.DataFrame,
    now_utc: datetime | None,
    timeframe: timedelta,
) -> int:
    """Return the highest index whose bar has closed by ``now_utc``.

    With ``now_utc=None``, the entire frame is observable and the
    answer is ``len(df) - 1`` (legacy unconstrained mode).
    """
    n = len(ohlc_h4)
    if n == 0:
        return -1
    if now_utc is None:
        return n - 1

    times = pd.to_datetime(ohlc_h4["time"], utc=True)
    last_observable = -1
    for i, t in enumerate(times):
        if pd.Timestamp(t).to_pydatetime() + timeframe <= now_utc:
            last_observable = i
        else:
            break
    return last_observable


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
    """Detect a breakout per spec §2.3.

    Args:
        ohlc_h4: OHLC frame with columns ``time, open, high, low, close``;
            ``time`` UTC tz-aware. Index is read by position.
        swings_high: swing highs detected by ``detect_swings_h4``.
        swings_low: swing lows.
        bias: D1 bias from ``bias_d1``. ``"neutral"`` short-circuits to
            ``None``.
        locked_swings: swings already used by a prior breakout. The
            caller (pipeline) is expected to add ``event.swing`` to
            this set after a positive return — this function is pure
            and does not mutate.
        now_utc: optional production scheduler tick — caps the bar
            scan window.
        n_swing: bars-each-side parameter from spec §3.2 (controls
            confirmation eligibility, not a scan input here).
        timeframe: H4 candle duration.

    Returns:
        ``BreakoutEvent`` if a confirmed swing in the bias direction has
        been crossed by an H4 close in the observable window;
        ``None`` otherwise.
    """
    if bias == "neutral":
        return None

    now_idx = _now_idx(ohlc_h4, now_utc, timeframe)
    if now_idx < 0:
        return None

    if bias == "bullish":
        candidates = [s for s in swings_high if s not in locked_swings]
        cmp = lambda close, level: close > level  # noqa: E731
        direction: Literal["long", "short"] = "long"
    else:  # bearish
        candidates = [s for s in swings_low if s not in locked_swings]
        cmp = lambda close, level: close < level  # noqa: E731
        direction = "short"

    # Spec §2.3: only confirmed swings whose confirmation bar lies
    # strictly before now_idx are eligible — leaves at least one bar
    # in which a breakout can fire.
    candidates = [s for s in candidates if s.bar_index + n_swing < now_idx]
    if not candidates:
        return None

    last = max(candidates, key=lambda s: s.bar_index)

    closes = ohlc_h4["close"].to_numpy(dtype="float64")
    times = pd.to_datetime(ohlc_h4["time"], utc=True)

    # Scan from the bar after confirmation through now_idx (inclusive).
    for j in range(last.bar_index + n_swing + 1, now_idx + 1):
        if cmp(float(closes[j]), last.price):
            return BreakoutEvent(
                swing=last,
                breakout_bar_timestamp=pd.Timestamp(times.iloc[j]).to_pydatetime(),
                breakout_bar_close=float(closes[j]),
                direction=direction,
            )

    return None
