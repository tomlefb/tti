"""Dataclasses for the mean-reversion BB H4 strategy.

Pre-specified at ``docs/strategies/mean_reversion_bb_h4.md`` (commit
``91cb2a2``); this module hosts every shared dataclass referenced by
spec §2 / §3.

Per the gate-2 brief, **all** dataclasses live in ``types.py`` so the
per-detector modules can stay function-only and import from here
without cross-importing each other. The detectors are pure
functions, the dataclasses are the only shared surface.

Killzone defaults
-----------------

Spec §2.2 / §3.1: London = ``[08:00, 12:00]`` UTC, NY =
``[13:00, 18:00]`` UTC. Filter rule (Option A): a bar is
in-killzone iff its **close timestamp** ∈ window (both-ends
inclusive). The close timestamp is the natural comparison point
because the strategy's detection decision is taken at the close.

On the H4 grid anchored at UTC midnight (closes ∈ {04, 08, 12,
16, 20, 00}):

| Bar open | Bar close | London ⊆ [08, 12] | NY ⊆ [13, 18] | In-killzone? |
|----------|-----------|:------:|:----:|:--:|
| 04:00    | 08:00     | ✓      |      | YES (London) |
| 08:00    | 12:00     | ✓      |      | YES (London) |
| 12:00    | 16:00     |        | ✓    | YES (NY)     |
| 16:00    | 20:00     |        |      | NO           |
| 20:00    | 00:00     |        |      | NO           |
| 00:00    | 04:00     |        |      | NO           |

Net = 3 in-killzone H4 bars per UTC day. Same definition as the
archived breakout-retest H4 strategy — cross-strategy
comparability holds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Literal

import pandas as pd


@dataclass(frozen=True)
class BollingerBands:
    """Output of ``compute_bollinger`` — spec §2.1.

    Attributes:
        sma: simple moving average of close over ``period`` bars,
            same index as the source ``close`` series.
        upper: ``sma + multiplier * std``.
        lower: ``sma - multiplier * std``.
        period: BB period used to compute these bands.
        multiplier: BB stddev multiplier used to compute these bands.
    """

    sma: pd.Series
    upper: pd.Series
    lower: pd.Series
    period: int
    multiplier: float


@dataclass(frozen=True)
class ExcessEvent:
    """A confirmed Bollinger band excess in-killzone — spec §2.2.

    Attributes:
        timestamp_utc: open time of the excess H4 bar (UTC, tz-aware).
        bar_index: positional index into the OHLC frame supplied to
            ``detect_excess``. Frames must be 0-indexed consecutively
            (default ``RangeIndex``).
        direction: ``"upper"`` (close > upper band; long-bias for the
            subsequent reversion → SHORT setup) or ``"lower"`` (close
            < lower band → LONG setup). The mapping is in
            ``build_setup``.
        close: that bar's close.
        high: that bar's high (used for SL on a future short setup).
        low: that bar's low (used for SL on a future long setup).
        bb_level: the band value at the excess bar (upper for
            ``direction="upper"``, lower otherwise).
        penetration_atr: ``|close - bb_level| / atr_at_excess``.
            Recorded for debug / audit (the §2.3 filter has already
            cleared this excess by the time the dataclass is built).
    """

    timestamp_utc: datetime
    bar_index: int
    direction: Literal["upper", "lower"]
    close: float
    high: float
    low: float
    bb_level: float
    penetration_atr: float


@dataclass(frozen=True)
class ReturnEvent:
    """A return-inside-bands close that triggers the setup — spec §2.5.

    Attributes:
        excess_event: the parent excess this return resolves.
        return_bar_timestamp: open time of the return H4 bar.
        return_bar_index: positional index of the return bar.
        return_bar_close: the entry price for the setup.
        return_bar_high: bar high (kept for symmetry / audit).
        return_bar_low: bar low (kept for symmetry / audit).
        sma_at_return: the BB midline at the return bar — i.e. the
            mean-reversion TP target (spec §2.6).
    """

    excess_event: ExcessEvent
    return_bar_timestamp: datetime
    return_bar_index: int
    return_bar_close: float
    return_bar_high: float
    return_bar_low: float
    sma_at_return: float


@dataclass(frozen=True)
class Setup:
    """Final mean-reversion trade plan — spec §2.6.

    Attributes:
        timestamp_utc: open time of the return bar (the bar whose
            close triggers the entry).
        instrument: e.g. ``"XAUUSD"`` / ``"NDX100"``.
        direction: ``"long"`` (excess on lower band → reversion up)
            or ``"short"`` (excess on upper band → reversion down).
        entry_price: return bar's close.
        stop_loss: just beyond the excess bar extreme:
            ``excess.low - sl_buffer`` (long) or
            ``excess.high + sl_buffer`` (short).
        take_profit: ``sma_at_return`` — pinned at the BB midline,
            **not** a fixed multiple of risk. RR is therefore variable.
        risk_reward: COMPUTED ``|tp - entry| / |entry - sl|``. Spec
            §2.6 makes this explicit: not pinned at a target, may
            span 0.5–2.5; the §2.7 floor at ``min_rr`` trims the
            worst.
        excess_event: parent excess (kept for audit + post-mortem).
        return_event: parent return (kept for audit + post-mortem).
    """

    timestamp_utc: datetime
    instrument: str
    direction: Literal["long", "short"]
    entry_price: float
    stop_loss: float
    take_profit: float
    risk_reward: float
    excess_event: ExcessEvent
    return_event: ReturnEvent


@dataclass(frozen=True)
class StrategyParams:
    """Static configuration for one run of the strategy.

    See spec §3.1–§3.2 for the rationale on each value.
    ``min_penetration_atr_mult``, ``sl_buffer`` and
    ``max_risk_distance`` are deliberately default-less: they are
    the calibrated instrument-specific axes from §3.2 and must be
    supplied explicitly by the caller.

    Attributes:
        bb_period: Bollinger period (spec §3.1: 20).
        bb_multiplier: Bollinger stddev multiplier (spec §3.1: 2.0).
        atr_period: ATR period for the §2.3 penetration filter
            (spec §3.1: 14).
        min_penetration_atr_mult: ATR multiplier threshold for the
            §2.3 penetration filter — calibrated per §3.2 grid.
        max_return_bars: max H4 bars after the excess in which a
            return-inside close still produces a setup (spec §3.2
            anchored at 3).
        sl_buffer: instrument-priced buffer beyond the excess extreme
            for the SL — calibrated per §3.2 grid.
        min_rr: hard floor on computed RR (spec §3.1: 1.0).
        max_risk_distance: instrument-priced cap on ``|entry - sl|``
            (spec §3.2 — anti-degenerate-trade guardrail).
        max_trades_per_day: per-instrument per-day cap on emitted
            setups (spec §3.1: 2).
        killzone_*_start_utc / killzone_*_end_utc: see module
            docstring for the H4-grid-derived defaults.
        exhaustion_min_wick_ratio / exhaustion_max_body_ratio: spec
            §2.4 discriminator constants — fixed at 0.4 / 0.5 per
            §3.1, exposed as fields so unit tests can probe edges
            without reaching into module-level constants.
    """

    min_penetration_atr_mult: float
    sl_buffer: float
    max_risk_distance: float
    bb_period: int = 20
    bb_multiplier: float = 2.0
    atr_period: int = 14
    max_return_bars: int = 3
    min_rr: float = 1.0
    max_trades_per_day: int = 2
    exhaustion_min_wick_ratio: float = 0.4
    exhaustion_max_body_ratio: float = 0.5
    killzone_london_start_utc: time = time(8, 0)
    killzone_london_end_utc: time = time(12, 0)
    killzone_ny_start_utc: time = time(13, 0)
    killzone_ny_end_utc: time = time(18, 0)


@dataclass
class StrategyState:
    """Mutable state carried across cycles of the same run.

    Instances are created once at the start of a backtest / live
    session and passed to every ``build_setup_candidates`` call. The
    pipeline is the only writer; individual detectors are pure and
    receive views as inputs.

    Attributes:
        pending_excesses: per-instrument list of excess events that
            have already passed the §2.3 / §2.4 filters but have not
            yet seen a return-inside close. Entries are dropped from
            this list when (a) a return fires (setup attempt) or
            (b) the ``max_return_bars`` window expires. Spec §3.4.
        trades_today: per-(instrument, calendar-date-UTC) counter of
            setups emitted so far today. Used by the per-day cap
            invalidation rule (spec §2.7).
    """

    pending_excesses: dict[str, list[ExcessEvent]] = field(default_factory=dict)
    trades_today: dict[tuple[str, date], int] = field(default_factory=dict)
