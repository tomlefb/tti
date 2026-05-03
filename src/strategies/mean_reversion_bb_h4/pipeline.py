"""Pipeline orchestration for the mean-reversion BB H4 strategy.

The detection layers are pure: they read inputs and return events.
The pipeline owns the only mutable state (``StrategyState``) and is
the sole writer to ``pending_excesses`` / ``trades_today``.

Per-cycle algorithm:

1. **Resolve pending excesses**. For each excess on the queue, try
   to detect a return within ``max_return_bars`` (spec §2.5). If a
   return fires: build the ``Setup``, run hard invalidation, emit
   if valid, and drop the excess from the queue (one return attempt
   per excess regardless of outcome — no second chance). If no
   return but the window is still open, the excess survives;
   otherwise it is dropped.

2. **Detect a new excess at the just-closed bar**. Run
   ``detect_excess``; on success, apply the §2.3 ATR penetration
   filter and the §2.4 exhaustion filter. Both must pass. The
   filtered excess (with its ``penetration_atr`` populated) is
   appended to the pending queue.

3. Return any setups produced this cycle (typically 0 or 1).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from .bollinger import compute_bollinger
from .excess import detect_excess
from .filters import passes_penetration
from .invalidation import daily_key, is_invalid
from .return_detection import detect_return
from .setup import build_setup
from .types import (
    BollingerBands,
    ExcessEvent,
    Setup,
    StrategyParams,
    StrategyState,
)

_H4 = timedelta(hours=4)


def _compute_atr(ohlc_h4: pd.DataFrame, period: int) -> pd.Series:
    """Simple ``SMA(TR, period)`` ATR — spec §2.3.

    True Range[i] = ``max(high[i] - low[i], |high[i] - close[i-1]|,
    |close[i-1] - low[i]|)``. Bar 0 has no ``close[-1]`` so its TR
    falls back to ``high[0] - low[0]``.

    A simple-MA implementation (vs Wilder's EMA) is deterministic and
    spec-aligned: the strategy spec calls for "ATR(14)" without
    specifying the smoothing.
    """
    high = ohlc_h4["high"].astype("float64")
    low = ohlc_h4["low"].astype("float64")
    close = ohlc_h4["close"].astype("float64")
    prev_close = close.shift(1)

    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (prev_close - low).abs(),
        ],
        axis=1,
    ).max(axis=1)
    # Bar 0: no prev_close → only (high - low) is defined; .max(axis=1)
    # of [hl, NaN, NaN] returns hl thanks to skipna default.
    return tr.rolling(window=period, min_periods=period).mean()


def _now_bar_index(
    ohlc_h4: pd.DataFrame,
    now_utc: datetime,
    timeframe: timedelta = _H4,
) -> int:
    """Return the highest bar index whose H4 close has been observed
    by ``now_utc``. ``-1`` if no bar has closed yet."""
    times = pd.to_datetime(ohlc_h4["time"], utc=True)
    last = -1
    for i, t in enumerate(times):
        if pd.Timestamp(t).to_pydatetime() + timeframe <= now_utc:
            last = i
        else:
            break
    return last


def _replace_penetration(excess: ExcessEvent, pen_atr: float) -> ExcessEvent:
    """Frozen-dataclass copy with ``penetration_atr`` filled in."""
    return ExcessEvent(
        timestamp_utc=excess.timestamp_utc,
        bar_index=excess.bar_index,
        direction=excess.direction,
        close=excess.close,
        high=excess.high,
        low=excess.low,
        bb_level=excess.bb_level,
        penetration_atr=float(pen_atr),
    )


def build_setup_candidates(
    ohlc_h4: pd.DataFrame,
    instrument: str,
    params: StrategyParams,
    state: StrategyState,
    *,
    now_utc: datetime,
    bb: BollingerBands | None = None,
    atr: pd.Series | None = None,
) -> list[Setup]:
    """Run one detection cycle and return any new setups produced.

    Args:
        ohlc_h4: H4 OHLC frame with ``time, open, high, low, close``;
            ``time`` UTC tz-aware. The frame can be longer than the
            current observable window — the pipeline truncates via
            ``now_utc``.
        instrument: instrument label, e.g. ``"XAUUSD"``.
        params: strategy parameters.
        state: mutable cycle-spanning state. Mutated in-place: pending
            excesses are queued / drained, ``trades_today`` is
            incremented for every emitted setup.
        now_utc: production scheduler tick (UTC, tz-aware). Bars whose
            close has not yet occurred at ``now_utc`` are not visible
            to the cycle.
        bb: optional pre-computed Bollinger bands. The audit harness
            (gate 3) injects these so streaming and full-history runs
            share the same band computation. When ``None`` (the
            default), the function recomputes bands over the full
            ``ohlc_h4`` — same result thanks to causal rolling.
        atr: optional pre-computed ATR series. Same role as ``bb``.

    Returns:
        ``list[Setup]`` — typically 0 or 1 setups per cycle. Multiple
        setups can occur if more than one pending excess returns on
        the same cycle (rare).
    """
    setups: list[Setup] = []

    now_bar_idx = _now_bar_index(ohlc_h4, now_utc)
    if now_bar_idx < 0:
        return setups

    # Need at least ``bb_period`` bars before any band can be defined.
    # The gate-3 audit drives this via physically-truncated frames at
    # early cycles; production cycles always have warmup, so this is
    # an audit-supporting guard rather than a code-path active in
    # live runs.
    if len(ohlc_h4) < params.bb_period:
        return setups

    if bb is None:
        bb = compute_bollinger(
            ohlc_h4["close"], period=params.bb_period, multiplier=params.bb_multiplier
        )
    if atr is None:
        atr = _compute_atr(ohlc_h4, period=params.atr_period)

    kz_kwargs = {
        "killzone_london_start_utc": params.killzone_london_start_utc,
        "killzone_london_end_utc": params.killzone_london_end_utc,
        "killzone_ny_start_utc": params.killzone_ny_start_utc,
        "killzone_ny_end_utc": params.killzone_ny_end_utc,
    }

    # ---- Step 1: resolve pending excesses ----------------------------
    pending = state.pending_excesses.setdefault(instrument, [])
    surviving: list[ExcessEvent] = []
    for excess in pending:
        ret = detect_return(
            ohlc_h4,
            bb,
            excess,
            max_return_bars=params.max_return_bars,
            now_bar_index=now_bar_idx,
            **kz_kwargs,
        )
        if ret is not None:
            try:
                setup = build_setup(
                    ret, instrument=instrument, sl_buffer=params.sl_buffer
                )
            except ValueError:
                # Degenerate (zero risk). Drop silently — the excess
                # is consumed regardless.
                continue
            key = daily_key(setup)
            already_today = state.trades_today.get(key, 0)
            if not is_invalid(
                setup,
                min_rr=params.min_rr,
                max_risk_distance=params.max_risk_distance,
                daily_count=already_today,
                max_trades_per_day=params.max_trades_per_day,
            ):
                setups.append(setup)
                state.trades_today[key] = already_today + 1
            # One return attempt per excess (spec §2.5): drop.
            continue

        # No return this cycle. Keep the excess if the window is still
        # open, drop otherwise.
        if now_bar_idx < excess.bar_index + params.max_return_bars:
            surviving.append(excess)
        # else: window expired — drop.
    state.pending_excesses[instrument] = surviving

    # ---- Step 2: detect a new excess at the just-closed bar ----------
    new_excess = detect_excess(
        ohlc_h4,
        bb,
        bar_index=now_bar_idx,
        **kz_kwargs,
    )
    if new_excess is None:
        return setups

    atr_value = atr.iloc[now_bar_idx]
    if pd.isna(atr_value):
        # ATR not yet defined — drop the excess. Spec §2.3 cannot run.
        return setups

    passes, pen_atr = passes_penetration(
        new_excess,
        atr_at_bar=float(atr_value),
        min_pen_atr_mult=params.min_penetration_atr_mult,
    )
    if not passes:
        return setups

    # v1.1 (commit ae61f70): the §2.4 exhaustion-candle filter is
    # NOT applied. The diagnostic measured 3.7 % retention at this
    # gate (NDX train), making it the steepest single-step drop in
    # the chain and reducing the final setup count to 1 over 60
    # months. The function ``is_exhaustion_candle`` is kept in
    # ``filters.py`` as a v2/v3 candidate; the ``StrategyParams``
    # fields ``exhaustion_min_wick_ratio`` / ``exhaustion_max_body_ratio``
    # are kept for the same reason, but read by neither pipeline
    # nor any audit harness in v1.1.

    state.pending_excesses.setdefault(instrument, []).append(
        _replace_penetration(new_excess, pen_atr)
    )
    return setups
