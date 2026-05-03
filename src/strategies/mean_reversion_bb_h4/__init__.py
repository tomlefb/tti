"""Mean-reversion Bollinger H4 bidirectional strategy.

Pre-specified at ``docs/strategies/mean_reversion_bb_h4.md`` (commit
``91cb2a2``). This package implements that spec, gate 2 of the
research protocol.

Public API
----------
- ``build_setup_candidates`` — cycle-by-cycle pipeline orchestration.
- ``Setup`` — final trade plan returned to the caller.
- ``ExcessEvent`` / ``ReturnEvent`` — intermediate events emitted by
  the detection layers (exposed for audit + tests).
- ``BollingerBands`` — output of ``compute_bollinger`` (exposed
  similarly for tests + the gate-3 audit harness).
- ``StrategyParams`` / ``StrategyState`` — configuration and the
  cycle-spanning state container.

Module layout (one detector per file, no cross-imports between
detectors — they all reach into ``types.py`` only):

    types.py             dataclasses
    bollinger.py         spec §2.1
    excess.py            spec §2.2
    filters.py           spec §2.3 + §2.4
    return_detection.py  spec §2.5
    setup.py             spec §2.6
    invalidation.py      spec §2.7
    pipeline.py          orchestration

Every detector takes a ``now_utc`` cutoff (or its index analogue) so
the whole pipeline can be audited against look-ahead leakage at
gate 3 of the protocol.
"""

from .bollinger import compute_bollinger
from .excess import detect_excess
from .filters import is_exhaustion_candle, passes_penetration
from .invalidation import daily_key, is_invalid
from .pipeline import build_setup_candidates
from .return_detection import detect_return
from .setup import build_setup
from .types import (
    BollingerBands,
    ExcessEvent,
    ReturnEvent,
    Setup,
    StrategyParams,
    StrategyState,
)

__all__ = [
    "BollingerBands",
    "ExcessEvent",
    "ReturnEvent",
    "Setup",
    "StrategyParams",
    "StrategyState",
    "build_setup",
    "build_setup_candidates",
    "compute_bollinger",
    "daily_key",
    "detect_excess",
    "detect_return",
    "is_exhaustion_candle",
    "is_invalid",
    "passes_penetration",
]
