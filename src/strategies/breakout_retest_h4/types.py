"""Configuration and cycle-spanning state for the breakout-retest H4 strategy.

Kept in a separate module so the per-detector files can import these
without pulling each other in (preserves the "no cross-imports between
detectors" rule from the spec).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class StrategyParams:
    """Static configuration for one run of the strategy.

    See spec §3 for the rationale on each value. ``retest_tolerance``,
    ``sl_buffer`` and ``max_risk_distance`` are deliberately default-less:
    they are instrument-specific and must be supplied explicitly by the
    caller.

    Attributes:
        n_swing: bars on each side of a fractal pivot. Spec §3.2 anchors
            this at 5.
        n_retest: max H4 bars after a breakout in which a retest may
            still confirm the setup. Spec §3.2 anchors this at 8.
        retest_tolerance: instrument-priced buffer added to the broken
            level when checking the wick touch (long: low <= level +
            tol; short: high >= level - tol). Spec §3.2.
        sl_buffer: instrument-priced buffer added beyond the retest
            extreme for the stop-loss. Spec §2.5.
        rr_target: fixed risk-reward multiple. Spec §3.1: 2.0.
        max_risk_distance: instrument-priced cap on ``|entry - sl|``.
            A retest with a deep wick would otherwise produce a
            giant-stop trade. Spec §2.6.
        max_trades_per_day: per-instrument per-day cap on emitted
            setups. Spec §3.1: 2.
    """

    retest_tolerance: float
    sl_buffer: float
    max_risk_distance: float
    n_swing: int = 5
    n_retest: int = 8
    rr_target: float = 2.0
    max_trades_per_day: int = 2


@dataclass
class StrategyState:
    """Mutable state carried across cycles of the same run.

    Instances are created once at the start of a backtest / live
    session and passed to every ``build_setup_candidates`` call. The
    pipeline is the only writer; individual detectors are pure and
    receive views (e.g. ``locked_swings``) as inputs.

    Attributes:
        locked_swings: swings that have already produced a breakout
            event. A swing is added the first time
            ``detect_breakout`` returns it, and never re-emits — see
            spec §2.3 + §5.1 (anti-double-dip on the same level).
        trades_today: per (instrument, calendar-date-UTC) counter of
            setups emitted so far today. Used by the hard
            invalidation rule in spec §2.6.
    """

    locked_swings: set = field(default_factory=set)
    trades_today: dict[tuple[str, date], int] = field(default_factory=dict)
