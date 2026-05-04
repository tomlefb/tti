"""Dataclasses for the cross-sectional momentum rotation D1 strategy.

Pre-specified at ``docs/strategies/trend_rotation_d1.md`` (commit
``889f18c``); this module hosts every shared dataclass referenced
by spec §2 / §3.

All dataclasses live in ``types.py`` so the per-module functions can
stay function-only and import only from here, preserving the
"no cross-imports between modules" architectural rule used by the
prior strategies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class MomentumScore:
    """Output of ``compute_momentum`` — spec §2.2.

    Attributes:
        asset: instrument label (e.g. ``"NDX100"``).
        timestamp_utc: decision date in UTC. The score is computed
            from closes strictly before this date (anti-look-ahead).
        score: cumulative return ``(close[-1] - close[-lookback-1])
            / close[-lookback-1]`` over the visible window.
        lookback_days: lookback used (``63`` or ``126`` per §3.2).
    """

    asset: str
    timestamp_utc: datetime
    score: float
    lookback_days: int


@dataclass(frozen=True)
class Basket:
    """The top-K basket selected at a rebalance — spec §2.3.

    Attributes:
        timestamp_utc: rebalance date in UTC.
        assets: ordered tuple of K asset labels, top-score first.
        scores: per-asset score map for the rebalance — kept for
            audit and traceability of the ranking decision.
    """

    timestamp_utc: datetime
    assets: tuple[str, ...]
    scores: dict[str, float]


@dataclass(frozen=True)
class RebalanceTransition:
    """A complete rebalance: prior basket, new basket, transitions.

    Attributes:
        timestamp_utc: rebalance date in UTC.
        previous_basket: the pre-rebalance basket. ``None`` on the
            very first rebalance of a run (no prior basket exists).
        new_basket: the post-rebalance basket.
        closed_assets: assets dropping out of the basket — their
            position from the prior cycle is now closed at this
            rebalance close. Empty on the first rebalance.
        opened_assets: assets entering the basket — new positions
            opened at this rebalance close. On the first rebalance,
            equals ``new_basket.assets``.
    """

    timestamp_utc: datetime
    previous_basket: Basket | None
    new_basket: Basket
    closed_assets: tuple[str, ...]
    opened_assets: tuple[str, ...]


@dataclass(frozen=True)
class TradeEntry:
    """An individual basket-entry — spec §2.5.

    Attributes:
        asset: instrument label.
        entry_timestamp_utc: rebalance date the position is opened.
        entry_price: D1 close at the rebalance date (per the v1
            execution convention: decisions on prior closes,
            executed at the rebalance-date close).
        position_size: in instrument units. ``risk_dollars /
            atr_at_entry`` per the §2.5 risk-parity rule.
        atr_at_entry: ATR(20) value at the rebalance date — kept
            for traceability and for the exit-time R computation.
    """

    asset: str
    entry_timestamp_utc: datetime
    entry_price: float
    position_size: float
    atr_at_entry: float


@dataclass(frozen=True)
class TradeExit:
    """A closed basket-entry — used to feed ``BacktestResult``.

    Attributes:
        asset: instrument label.
        entry_timestamp_utc / exit_timestamp_utc: position lifetime.
        entry_price / exit_price: prices at the two endpoints.
        position_size: copy from the parent ``TradeEntry``.
        atr_at_entry: copy from the parent ``TradeEntry`` — used to
            compute ``return_r``.
        return_r: realised return in R units. For long-only
            risk-parity sizing, ``return_r = (exit_price -
            entry_price) / atr_at_entry`` — independent of the
            account's absolute capital. Always carries the sign of
            the move.
        direction: always ``"long"`` in v1 (no short side).
    """

    asset: str
    entry_timestamp_utc: datetime
    exit_timestamp_utc: datetime
    entry_price: float
    exit_price: float
    position_size: float
    atr_at_entry: float
    return_r: float
    direction: Literal["long"] = "long"


@dataclass(frozen=True)
class StrategyParams:
    """Static configuration for one run — spec §3.1 / §3.2.

    Attributes:
        universe: tuple of 15 asset labels (FundedNext-tradable per
            §1).
        momentum_lookback_days: ``{63, 126}`` per §3.2 grid.
        K: basket size, ``{3, 4}`` per §3.2 grid.
        rebalance_frequency_days: ``{10, 21}`` per §3.2 grid.
        risk_per_trade_pct: spec §3.1 default ``1.0`` (1 %).
        atr_period: ATR window, spec §3.1 default ``20``.
        atr_explosive_threshold: ATR-multiplier above the 90-day
            median that triggers the §2.6 / §3.1 volatility regime
            filter. Spec default ``5.0``.
        atr_regime_lookback: lookback for the rolling-median ATR
            baseline, spec default ``90`` days.
    """

    universe: tuple[str, ...]
    momentum_lookback_days: int
    K: int
    rebalance_frequency_days: int
    risk_per_trade_pct: float = 1.0
    atr_period: int = 20
    atr_explosive_threshold: float = 5.0
    atr_regime_lookback: int = 90


@dataclass
class StrategyState:
    """Mutable cycle-spanning state.

    Attributes:
        current_basket: the assets currently held. ``set`` for
            cheap membership checks; the basket dataclass surfaces
            the ordered tuple.
        last_rebalance_date: the date the most recent rebalance was
            executed. ``None`` until the first rebalance fires.
        open_positions: per-asset entry record for the assets in
            ``current_basket``. Keyed by asset for lookup at exit
            time.
    """

    current_basket: set[str] = field(default_factory=set)
    last_rebalance_date: datetime | None = None
    open_positions: dict[str, TradeEntry] = field(default_factory=dict)
