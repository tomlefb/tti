"""Breakout-retest H4 trend-following strategy.

Pre-specified at ``docs/strategies/breakout_retest_h4.md`` (commit ``b14e054``).
This package implements that spec, gate 2 of the research protocol.

Public API
----------
- ``build_setup_candidates`` — cycle-by-cycle pipeline orchestration.
- ``Setup`` — final trade plan returned to the caller.
- ``BreakoutEvent`` / ``RetestEvent`` — intermediate events emitted by
  the detection layers (exposed for audit + tests).
- ``StrategyParams`` / ``StrategyState`` — configuration and the
  cycle-spanning state container.

Module layout (one detector per file, no cross-imports beyond the
shared dataclasses):

    bias.py          spec §2.1
    swings.py        spec §2.2
    breakout.py      spec §2.3
    retest.py        spec §2.4
    setup.py         spec §2.5
    invalidation.py  spec §2.6
    pipeline.py      orchestration

Every detector takes a ``now_utc`` cutoff so the whole pipeline can be
audited against look-ahead leakage at gate 3 of the protocol.
"""

from .breakout import BreakoutEvent, detect_breakout
from .pipeline import build_setup_candidates
from .retest import RetestEvent, detect_retest
from .setup import Setup, build_setup
from .swings import Swing
from .types import StrategyParams, StrategyState

__all__ = [
    "BreakoutEvent",
    "RetestEvent",
    "Setup",
    "StrategyParams",
    "StrategyState",
    "Swing",
    "build_setup",
    "build_setup_candidates",
    "detect_breakout",
    "detect_retest",
]
