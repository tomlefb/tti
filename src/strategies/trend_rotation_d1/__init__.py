"""Cross-sectional momentum multi-asset rotation D1 strategy.

Pre-specified at ``docs/strategies/trend_rotation_d1.md`` (commit
``889f18c``). This package implements that spec, gate 2 of the
research protocol.

Public API
----------
- ``build_rebalance_candidates`` — cycle-by-cycle pipeline
  orchestration. Returns the ``TradeExit`` records produced this
  cycle (basket positions closed at this rebalance).
- Dataclasses (``types.py``): ``MomentumScore``, ``Basket``,
  ``RebalanceTransition``, ``TradeEntry``, ``TradeExit``,
  ``StrategyParams``, ``StrategyState``.
- Detector primitives (exposed for tests + the gate-3 audit):
  ``compute_momentum``, ``select_top_k``, ``compute_atr``,
  ``passes_volatility_regime``, ``sizing_for_entry``,
  ``detect_rebalance_trades``.

Module layout (one concern per file, no cross-imports between
modules — they all reach into ``types.py`` only):

    types.py          dataclasses
    momentum.py       spec §2.2
    ranking.py        spec §2.3
    volatility.py     spec §3.1 + §2.6
    sizing.py         spec §2.5
    transitions.py    spec §2.4
    pipeline.py       orchestration

Anti-look-ahead invariant: scoring / ATR inputs are sliced
strictly to ``< now_utc``; execution prices (entry / exit) are
the close AT ``now_utc``. The rebalance is the only event that
opens or closes positions — no SL / TP at the position level.
"""

from .momentum import compute_momentum
from .pipeline import build_rebalance_candidates
from .ranking import select_top_k
from .sizing import sizing_for_entry
from .transitions import detect_rebalance_trades
from .types import (
    Basket,
    MomentumScore,
    RebalanceTransition,
    StrategyParams,
    StrategyState,
    TradeEntry,
    TradeExit,
)
from .volatility import compute_atr, passes_volatility_regime

__all__ = [
    "Basket",
    "MomentumScore",
    "RebalanceTransition",
    "StrategyParams",
    "StrategyState",
    "TradeEntry",
    "TradeExit",
    "build_rebalance_candidates",
    "compute_atr",
    "compute_momentum",
    "detect_rebalance_trades",
    "passes_volatility_regime",
    "select_top_k",
    "sizing_for_entry",
]
