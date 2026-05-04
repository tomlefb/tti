"""Pipeline orchestration for the cross-sectional momentum
rotation D1 strategy.

The detector modules are pure: they read inputs and return
events. The pipeline owns the only mutable state
(``StrategyState``) and is the sole writer to ``current_basket``,
``last_rebalance_date``, and ``open_positions``.

Per-cycle algorithm
-------------------
The pipeline is intended to be called once per D1 close (driver
ticks through panel dates). At each call:

1. **Decide if a rebalance is due** — first call, or
   ``(now_utc - last_rebalance_date).days >= rebalance_frequency_days``.
   If not due, return ``[]``.

2. **Compute scores** — for every asset in the universe:
    a. Slice the asset's close series to ``< now_utc`` (strict
       anti-look-ahead per spec §2.2).
    b. Compute the momentum score on the visible prefix.
    c. Compute ATR(20) on the same visible prefix and apply the
       §2.6 volatility regime filter; if it fails, set score to
       ``None``.

3. **Pick top-K** via ``select_top_k`` on the score map.

4. **Detect transitions** vs ``state.current_basket``:
    - For each closed asset: pull its ``TradeEntry`` from
      ``open_positions``, compute exit price (close at ``now_utc``)
      + return-R, emit a ``TradeExit``.
    - For each opened asset: compute entry price (close at
      ``now_utc``) + ATR-based size, store a ``TradeEntry`` in
      ``open_positions``.

5. **Update state**: ``current_basket``, ``last_rebalance_date``.

6. Return the list of ``TradeExit`` records produced this cycle.
   Empty list when nothing closed (e.g. first rebalance, or
   rotation that only adds without dropping — rare with K fixed).

Anti-look-ahead invariant
-------------------------
- Score and ATR inputs are sliced strictly to ``< now_utc``.
- Execution prices (entry / exit) are the close AT ``now_utc``
  — the trader executes at the close of the decision day, on the
  basis of yesterday's data.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from .momentum import compute_momentum
from .ranking import select_top_k
from .sizing import sizing_for_entry
from .transitions import detect_rebalance_trades
from .types import (
    Basket,
    StrategyParams,
    StrategyState,
    TradeEntry,
    TradeExit,
)
from .volatility import compute_atr, passes_volatility_regime


def _is_rebalance_due(state: StrategyState, now_utc: datetime, freq_days: int) -> bool:
    if state.last_rebalance_date is None:
        return True
    return (now_utc - state.last_rebalance_date) >= timedelta(days=freq_days)


def _score_one_asset(
    df: pd.DataFrame,
    now_utc: datetime,
    params: StrategyParams,
) -> tuple[float | None, float]:
    """Return ``(score, atr_at_decision)``. Score is ``None`` when the
    asset is gated out (insufficient history or volatility regime)."""
    visible = df.loc[df.index < now_utc]
    if "close" not in visible.columns:
        return None, float("nan")
    if len(visible) < params.momentum_lookback_days + 1:
        return None, float("nan")
    score = compute_momentum(visible["close"], params.momentum_lookback_days)
    if score is None:
        return None, float("nan")
    atr = compute_atr(visible, period=params.atr_period)
    if not passes_volatility_regime(
        atr,
        explosive_threshold=params.atr_explosive_threshold,
        regime_lookback=params.atr_regime_lookback,
    ):
        return None, float(atr.iloc[-1]) if len(atr) else float("nan")
    atr_now = float(atr.iloc[-1]) if len(atr) else float("nan")
    return score, atr_now


def _execution_price(df: pd.DataFrame, now_utc: datetime) -> float | None:
    """Return the asset's close at ``now_utc``, or ``None`` if the
    panel has no entry for that date (non-trading day for this asset)."""
    if now_utc not in df.index:
        return None
    return float(df.loc[now_utc, "close"])


def build_rebalance_candidates(
    panel: dict[str, pd.DataFrame],
    params: StrategyParams,
    state: StrategyState,
    *,
    now_utc: datetime,
    capital: float = 100_000.0,
) -> list[TradeExit]:
    """Run one cycle and return any ``TradeExit`` records produced.

    Args:
        panel: per-asset OHLC frame indexed by tz-aware UTC date.
            Only the assets in ``params.universe`` are read; missing
            assets are treated as "no data this cycle".
        params: strategy configuration.
        state: mutable cycle-spanning state. Mutated in-place.
        now_utc: decision date in UTC. Convention: a calendar-day
            00:00 UTC label corresponding to a panel index entry.
            Strict anti-look-ahead: data with timestamp ≥ ``now_utc``
            is NOT used for scoring (only ``< now_utc``); the close
            AT ``now_utc`` is the execution price.
        capital: account capital for risk-parity sizing. Spec §3.1
            default 100,000.

    Returns:
        Zero or more ``TradeExit`` records (rebalance closes). Empty
        when no rebalance fires, or when a rebalance fires but
        nothing exits (e.g. first rebalance — only opens).
    """
    if not _is_rebalance_due(state, now_utc, params.rebalance_frequency_days):
        return []

    # Score every asset in the universe.
    scores: dict[str, float | None] = {}
    atrs_at_decision: dict[str, float] = {}
    for asset in params.universe:
        df = panel.get(asset)
        if df is None:
            scores[asset] = None
            continue
        score, atr_now = _score_one_asset(df, now_utc, params)
        scores[asset] = score
        atrs_at_decision[asset] = atr_now

    # Rank and pick top-K.
    new_top = select_top_k(scores, params.K)
    if not new_top and not state.current_basket:
        # Warmup or hard-filter: no asset to rank, nothing currently
        # held. Skip the rebalance entirely so the frequency clock
        # only starts once we have data.
        return []

    # Build the new basket dataclass for traceability.
    new_basket_obj = Basket(
        timestamp_utc=now_utc,
        assets=tuple(new_top),
        scores={a: s for a, s in scores.items() if s is not None},
    )

    # Compute transitions vs prior basket.
    closed_set, opened_set = detect_rebalance_trades(
        state.current_basket, set(new_top)
    )

    # Process closes: emit TradeExit for each closed asset.
    closed_records: list[TradeExit] = []
    for asset in sorted(closed_set):  # deterministic order for audit
        entry = state.open_positions.get(asset)
        if entry is None:
            # Should not happen — defensive: closed without an open
            # entry means state is corrupt. Skip silently here; the
            # gate-3 audit would surface this as a divergence.
            continue
        exit_price = _execution_price(panel[asset], now_utc)
        if exit_price is None:
            # Asset has no close at now_utc (gap day) — defer the
            # exit. Keep the position in open_positions for the next
            # rebalance.
            continue
        return_r = (
            (exit_price - entry.entry_price) / entry.atr_at_entry
            if entry.atr_at_entry > 0
            else 0.0
        )
        closed_records.append(
            TradeExit(
                asset=asset,
                entry_timestamp_utc=entry.entry_timestamp_utc,
                exit_timestamp_utc=now_utc,
                entry_price=entry.entry_price,
                exit_price=exit_price,
                position_size=entry.position_size,
                atr_at_entry=entry.atr_at_entry,
                return_r=return_r,
            )
        )
        del state.open_positions[asset]

    # Process opens: store a TradeEntry for each new asset.
    for asset in sorted(opened_set):
        entry_price = _execution_price(panel[asset], now_utc)
        if entry_price is None:
            # No close at now_utc — defer the entry, leave the asset
            # out of current_basket for this rebalance.
            continue
        atr = atrs_at_decision.get(asset, float("nan"))
        size = sizing_for_entry(
            capital=capital,
            risk_fraction=params.risk_per_trade_pct / 100.0,
            atr_at_entry=atr,
        )
        if size is None:
            # Cannot size (ATR == 0 / NaN) — skip this entry. The
            # basket reflects only successfully-opened positions.
            continue
        state.open_positions[asset] = TradeEntry(
            asset=asset,
            entry_timestamp_utc=now_utc,
            entry_price=entry_price,
            position_size=size,
            atr_at_entry=atr,
        )

    # Update current_basket: only assets that successfully opened
    # (or stayed) belong; deferred / skipped assets do not.
    state.current_basket = set(state.open_positions.keys())
    state.last_rebalance_date = now_utc
    # ``new_basket_obj`` is computed for traceability; the actual
    # basket may be smaller if some opens were deferred.
    _ = new_basket_obj  # not returned; the audit harness rebuilds it

    return closed_records
