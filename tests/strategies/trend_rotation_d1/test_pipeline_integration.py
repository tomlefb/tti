"""End-to-end integration tests for the trend_rotation_d1 pipeline.

CRITICAL: this file is reused by gate 3 of the research protocol
(audit look-ahead via streaming-vs-full-history diff). The
fixtures are minimal hand-built panels where the expected basket
transitions are known by construction.

Convention reminder:
- now_utc must be a panel index entry (driver iterates trading days).
- Score / ATR use closes strictly before now_utc.
- Execution price (entry / exit) = close AT now_utc.

Synthetic-fixture parameters: lookback=5, rebalance=5, K=2.
This shrinks the warmup so the integration tests can drive the
pipeline through a few rebalances on hand-built price series.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from src.strategies.trend_rotation_d1 import (
    StrategyParams,
    StrategyState,
    build_rebalance_candidates,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_ohlc_from_closes(
    closes: list[float],
    *,
    start: str = "2025-01-01",
    range_size: float = 0.5,
) -> pd.DataFrame:
    """Synthetic D1 OHLC: open=prev close, high=close+r, low=close-r."""
    times = pd.date_range(start, periods=len(closes), freq="1D", tz="UTC")
    s = pd.Series(closes, index=times)
    df = pd.DataFrame(
        {
            "open": s.shift(1).fillna(s.iloc[0]),
            "high": s + range_size,
            "low": s - range_size,
            "close": s,
        }
    )
    return df


def _drive_pipeline(
    panel: dict[str, pd.DataFrame],
    params: StrategyParams,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> tuple[list, StrategyState]:
    """Iterate every panel date and call build_rebalance_candidates."""
    # Panel dates = union of indices.
    all_dates = sorted(
        set().union(*(df.index for df in panel.values()))
    )
    if start is not None:
        all_dates = [d for d in all_dates if d >= start]
    if end is not None:
        all_dates = [d for d in all_dates if d <= end]
    state = StrategyState()
    exits = []
    for d in all_dates:
        new_exits = build_rebalance_candidates(
            panel, params, state, now_utc=d
        )
        exits.extend(new_exits)
    return exits, state


# ---------------------------------------------------------------------------
# Fixture A — basket transition
# ---------------------------------------------------------------------------


def _fixture_basket_transition() -> dict[str, pd.DataFrame]:
    """5 assets where leadership swaps between two rebalances.

    Days 0..29: warm-up (need ≥ lookback=5 + 1 = 6 closes before any
    score is computed).

    Asset A: rises days 0..14, then flat → strong momentum at day
        ~10, weak after.
    Asset B: similar but slightly less.
    Asset C: flat days 0..14, rises 15..29 → weak then strong.
    Asset D: similar to C.
    Asset E: flat throughout → low rank always.

    Rebalance every 5 days, K=2:
    - First rebalance with valid scores: day ~10 → top-2 = {A, B}.
    - Two rebalances later (day ~20): top-2 should rotate to {C, D}
      because A/B's 5-day momentum has gone flat while C/D have
      taken the lead.
    """
    n = 30

    def _series(values: list[float]) -> list[float]:
        assert len(values) == n
        return values

    asset_a = _series([100.0 + i for i in range(15)] + [114.0] * 15)
    asset_b = _series([100.0 + 0.8 * i for i in range(15)] + [111.2] * 15)
    asset_c = _series([100.0] * 15 + [100.0 + i for i in range(15)])
    asset_d = _series([100.0] * 15 + [100.0 + 0.8 * i for i in range(15)])
    asset_e = _series([100.0] * n)

    return {
        "A": _build_ohlc_from_closes(asset_a),
        "B": _build_ohlc_from_closes(asset_b),
        "C": _build_ohlc_from_closes(asset_c),
        "D": _build_ohlc_from_closes(asset_d),
        "E": _build_ohlc_from_closes(asset_e),
    }


def _short_params(lookback: int = 5, K: int = 2, rebal: int = 5) -> StrategyParams:
    """Spec-compliant params shrunk for synthetic fixtures."""
    return StrategyParams(
        universe=("A", "B", "C", "D", "E"),
        momentum_lookback_days=lookback,
        K=K,
        rebalance_frequency_days=rebal,
        risk_per_trade_pct=1.0,
        atr_period=5,
        atr_explosive_threshold=5.0,
        atr_regime_lookback=10,
    )


def test_fixture_a_basket_rotates_from_AB_to_CD() -> None:
    panel = _fixture_basket_transition()
    params = _short_params(lookback=5, K=2, rebal=5)

    exits, state = _drive_pipeline(panel, params)

    # Initial basket — once history is sufficient, A and B should
    # dominate (rising). After the leadership swap, C and D should
    # take over and A/B should produce TradeExit records.
    closed_assets = {e.asset for e in exits}
    assert "A" in closed_assets, f"A should close after C/D take lead; exits={[e.asset for e in exits]}"
    assert "B" in closed_assets, f"B should close after C/D take lead; exits={[e.asset for e in exits]}"
    # C and D should currently be in the basket (open positions).
    assert "C" in state.current_basket
    assert "D" in state.current_basket
    # E never enters (always flat → lowest score).
    assert "E" not in state.current_basket


def test_fixture_a_returns_are_meaningful() -> None:
    """For each TradeExit, return_r = (exit - entry) / atr_at_entry.
    Verify the sign is consistent with the synthetic price moves."""
    panel = _fixture_basket_transition()
    params = _short_params(lookback=5, K=2, rebal=5)

    exits, _ = _drive_pipeline(panel, params)

    for e in exits:
        # Sanity: return_r matches the formula.
        if e.atr_at_entry > 0:
            expected = (e.exit_price - e.entry_price) / e.atr_at_entry
            assert e.return_r == pytest.approx(expected)
        # Direction is long-only.
        assert e.direction == "long"


# ---------------------------------------------------------------------------
# Fixture B — no transition
# ---------------------------------------------------------------------------


def _fixture_stable_ranking() -> dict[str, pd.DataFrame]:
    """3 assets with monotonic, persistent leadership: A > B > C
    every day across the 30-day fixture. The top-2 should be {A, B}
    at every rebalance — no transitions, no closed trades."""
    n = 30
    asset_a = [100.0 + 1.0 * i for i in range(n)]
    asset_b = [100.0 + 0.6 * i for i in range(n)]
    asset_c = [100.0 + 0.2 * i for i in range(n)]
    return {
        "A": _build_ohlc_from_closes(asset_a),
        "B": _build_ohlc_from_closes(asset_b),
        "C": _build_ohlc_from_closes(asset_c),
    }


def test_fixture_b_no_transitions_when_ranking_stable() -> None:
    panel = _fixture_stable_ranking()
    params = StrategyParams(
        universe=("A", "B", "C"),
        momentum_lookback_days=5,
        K=2,
        rebalance_frequency_days=5,
        atr_period=5,
        atr_regime_lookback=10,
    )
    exits, state = _drive_pipeline(panel, params)
    # Top-2 stays {A, B} every rebalance → zero TradeExit emitted.
    assert exits == [], f"expected 0 exits, got {len(exits)}"
    # Final basket = {A, B}.
    assert state.current_basket == {"A", "B"}


# ---------------------------------------------------------------------------
# Fixture C — volatility regime filter excludes explosive asset
# ---------------------------------------------------------------------------


def _fixture_explosive_asset() -> dict[str, pd.DataFrame]:
    """4 assets: A, B, C ranked normally (B highest), plus D which
    has a huge volatility spike at the decision day. D would
    otherwise rank #1 by raw momentum but the §2.6 filter excludes
    it."""
    n = 30
    asset_a = [100.0 + 0.3 * i for i in range(n)]
    asset_b = [100.0 + 0.6 * i for i in range(n)]
    asset_c = [100.0 + 0.1 * i for i in range(n)]
    # Asset D: flat for 28 days, then a massive flash spike on day
    # 28-29 → momentum looks great but ATR explodes.
    asset_d_closes = [100.0] * 28 + [200.0, 220.0]
    df_d = _build_ohlc_from_closes(asset_d_closes, range_size=0.5)
    # Inject the explosive range on the last bar.
    df_d.loc[df_d.index[-1], "high"] = 250.0
    df_d.loc[df_d.index[-1], "low"] = 50.0
    return {
        "A": _build_ohlc_from_closes(asset_a),
        "B": _build_ohlc_from_closes(asset_b),
        "C": _build_ohlc_from_closes(asset_c),
        "D": df_d,
    }


def test_fixture_c_volatility_filter_excludes_explosive() -> None:
    panel = _fixture_explosive_asset()
    params = StrategyParams(
        universe=("A", "B", "C", "D"),
        momentum_lookback_days=5,
        K=2,
        rebalance_frequency_days=5,
        atr_period=5,
        atr_explosive_threshold=5.0,
        atr_regime_lookback=10,
    )

    exits, state = _drive_pipeline(panel, params)

    # The final cycle is on the explosive day. D should NOT be in
    # the final basket despite its strong momentum signal.
    # Note: D may have been in the basket on earlier rebalances when
    # it was still flat (score 0, possibly entering by alpha
    # tie-break), so we only check the FINAL state which spans the
    # explosive day.
    assert "D" not in state.current_basket, (
        f"D should be excluded by volatility filter; "
        f"basket={state.current_basket}"
    )


# ---------------------------------------------------------------------------
# Fixture D — insufficient history excludes asset
# ---------------------------------------------------------------------------


def _fixture_short_history() -> dict[str, pd.DataFrame]:
    """4 assets, but D has < lookback + 1 closes."""
    n = 30
    asset_a = [100.0 + 0.6 * i for i in range(n)]
    asset_b = [100.0 + 0.4 * i for i in range(n)]
    asset_c = [100.0 + 0.2 * i for i in range(n)]
    df_d = _build_ohlc_from_closes([100.0 + 1.0 * i for i in range(3)])
    # D has only 3 bars — well under lookback=5+1.
    return {
        "A": _build_ohlc_from_closes(asset_a),
        "B": _build_ohlc_from_closes(asset_b),
        "C": _build_ohlc_from_closes(asset_c),
        "D": df_d,
    }


def test_fixture_d_short_history_excludes_asset_from_basket() -> None:
    panel = _fixture_short_history()
    params = StrategyParams(
        universe=("A", "B", "C", "D"),
        momentum_lookback_days=5,
        K=2,
        rebalance_frequency_days=5,
        atr_period=5,
        atr_regime_lookback=10,
    )
    _, state = _drive_pipeline(panel, params)
    # D never enters — its score is None at every rebalance.
    assert "D" not in state.current_basket
    # Top-2 = A, B (highest momentum after exclusion).
    assert state.current_basket == {"A", "B"}
