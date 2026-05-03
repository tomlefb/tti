"""Pipeline orchestration for the breakout-retest H4 strategy.

The detection layers are pure: they read inputs and return events. The
pipeline owns the only mutable state (``StrategyState``) and is the
sole writer to ``locked_swings`` / ``trades_today`` /
``in_flight_breakouts``.

Per-cycle algorithm:

1. **Resolve in-flight breakouts**. For each breakout already detected
   on previous cycles, attempt a retest with the **bias frozen at
   breakout time** (spec §5.6). If the retest fires, build the
   ``Setup`` and run hard invalidation; if the retest window has
   expired (current observable bar > ``break_idx + n_retest``), drop
   the breakout from the in-flight queue. Successful or expired,
   the breakout leaves the queue.

2. **Detect a new breakout**. Compute today's D1 bias, run swing
   detection on H4 truncated to data observable at ``now_utc``, and
   call ``detect_breakout`` excluding swings already in
   ``locked_swings``. If a breakout fires: add the swing to
   ``locked_swings`` and queue ``(breakout, bias)`` for retest in
   subsequent cycles. Within the same cycle a brand-new breakout
   cannot produce a retest (spec §2.4 ``j >= now_idx`` short-circuit
   — see retest.py docstring), so we do not attempt retest on it
   immediately.

3. Return every confirmed Setup the cycle produced (typically 0 or 1).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from .bias import bias_d1
from .breakout import BreakoutEvent, detect_breakout
from .invalidation import daily_key, is_invalid
from .retest import detect_retest
from .setup import Setup, build_setup
from .swings import detect_swings_h4
from .types import StrategyParams, StrategyState

_H4 = timedelta(hours=4)


def build_setup_candidates(
    ohlc_h4: pd.DataFrame,
    close_d1: pd.Series,
    instrument: str,
    params: StrategyParams,
    state: StrategyState,
    *,
    now_utc: datetime,
) -> list[Setup]:
    """Run one detection cycle and return any new setups produced.

    Args:
        ohlc_h4: H4 OHLC frame with ``time, open, high, low, close``;
            ``time`` UTC tz-aware. Must contain at least 2*n_swing+1
            bars before any swing can be confirmed.
        close_d1: D1 closes (only the closes are needed for the bias
            filter). The caller is responsible for slicing so the last
            entry is the last D1 close observable at ``now_utc``.
        instrument: instrument label, e.g. ``"XAUUSD"``.
        params: strategy parameters.
        state: mutable cycle-spanning state. Mutated in-place by this
            function: locked swings are added, in-flight breakouts are
            queued / dropped, trades_today is incremented for every
            setup emitted.
        now_utc: production scheduler tick (UTC, tz-aware).

    Returns:
        ``list[Setup]`` — typically 0 or 1 setups per cycle. Multiple
        setups can occur if more than one in-flight breakout retests
        on the same cycle (rare).
    """
    setups: list[Setup] = []

    # ---- Step 1: resolve in-flight breakouts ------------------------
    queue = state.in_flight_breakouts.setdefault(instrument, [])
    surviving: list[tuple[BreakoutEvent, str]] = []
    for breakout, frozen_bias in queue:
        retest = detect_retest(
            ohlc_h4,
            breakout,
            n_retest=params.n_retest,
            retest_tolerance=params.retest_tolerance,
            now_utc=now_utc,
        )
        if retest is not None:
            setup = build_setup(
                retest,
                instrument=instrument,
                bias_d1=frozen_bias,  # type: ignore[arg-type]
                sl_buffer=params.sl_buffer,
                rr_target=params.rr_target,
            )
            key = daily_key(setup)
            already_today = state.trades_today.get(key, 0)
            if not is_invalid(
                setup,
                max_risk_distance=params.max_risk_distance,
                daily_count=already_today,
                max_trades_per_day=params.max_trades_per_day,
            ):
                setups.append(setup)
                state.trades_today[key] = already_today + 1
            # Whether valid or invalid, this breakout's lifecycle is
            # over (one retest at most per breakout — spec §2.4).
            continue

        # No retest on this cycle: keep the breakout in the queue if
        # the retest window is still open.
        if _retest_window_still_open(
            ohlc_h4, breakout, params.n_retest, now_utc=now_utc, timeframe=_H4
        ):
            surviving.append((breakout, frozen_bias))
        # else: window expired — drop.
    state.in_flight_breakouts[instrument] = surviving

    # ---- Step 2: detect a new breakout ------------------------------
    if len(close_d1) < 50:  # SMA50 needs 50 closes
        return setups
    bias = bias_d1(close_d1, ma_period=50)
    if bias == "neutral":
        return setups

    swings_high, swings_low = detect_swings_h4(
        ohlc_h4, n_swing=params.n_swing, now_utc=now_utc
    )
    breakout = detect_breakout(
        ohlc_h4,
        swings_high,
        swings_low,
        bias,
        state.locked_swings,
        now_utc=now_utc,
        n_swing=params.n_swing,
    )
    if breakout is not None:
        state.locked_swings.add(breakout.swing)
        state.in_flight_breakouts.setdefault(instrument, []).append((breakout, bias))
        # Within the same cycle, the retest cannot fire on a
        # just-detected breakout (spec §2.4). It will be inspected on
        # the next cycle as part of step 1.

    return setups


def _retest_window_still_open(
    ohlc_h4: pd.DataFrame,
    breakout: BreakoutEvent,
    n_retest: int,
    *,
    now_utc: datetime,
    timeframe: timedelta,
) -> bool:
    """Return True if at least one bar in [break_idx+1, break_idx+n_retest]
    has not yet closed at ``now_utc`` (i.e. retest is still possible).
    """
    times = pd.to_datetime(ohlc_h4["time"], utc=True)
    target_ts = pd.Timestamp(breakout.breakout_bar_timestamp)
    matches = (times == target_ts).to_numpy().nonzero()[0]
    if len(matches) == 0:
        return False
    break_idx = int(matches[0])
    last_eligible = break_idx + n_retest

    if last_eligible >= len(ohlc_h4):
        # Frame doesn't even cover the full retest window yet → still
        # open by definition (we just don't have enough data).
        return True

    last_open = pd.Timestamp(times.iloc[last_eligible]).to_pydatetime()
    last_close = last_open + timeframe
    return last_close > now_utc
