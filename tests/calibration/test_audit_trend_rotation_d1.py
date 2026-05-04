"""Regression tests for the trend_rotation_d1 look-ahead audit harness.

Two checks:

- The gate-2 hand-built fixtures pass the truncated-vs-full-frame
  diff (Mode A == Mode B). If this regresses, either the audit
  itself broke or the strategy started reading future data.
- ``diff_exits`` correctly flags asymmetric exit lists (A-only /
  B-only / field-divergent). Used as a sanity check on the diff
  logic itself.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from calibration.audit_trend_rotation_d1 import (
    diff_exits,
    diff_final_state,
    run_streaming,
    run_streaming_truncated,
    smoke_test,
)
from src.strategies.trend_rotation_d1 import (
    StrategyParams,
    StrategyState,
    TradeEntry,
    TradeExit,
)
from tests.strategies.trend_rotation_d1.test_pipeline_integration import (
    _fixture_basket_transition,
    _short_params,
)


def test_smoke_test_passes() -> None:
    """The bundled smoke test must pass on the gate-2 fixtures."""
    assert smoke_test() is True


def test_truncated_matches_full_frame_on_basket_transition() -> None:
    """Direct A == B check with no smoke-test wrapping."""
    panel = _fixture_basket_transition()
    params = _short_params(lookback=5, K=2, rebal=5)
    dates = sorted(set().union(*(df.index for df in panel.values())))
    a, state_a = run_streaming_truncated(panel, params, dates)
    b, state_b = run_streaming(panel, params, dates)
    diff_e = diff_exits(a, b)
    diff_s = diff_final_state(state_a, state_b)
    assert diff_e["identical"], (
        f"A-only={len(diff_e['a_only'])} B-only={len(diff_e['b_only'])} "
        f"field={len(diff_e['field_diffs'])}"
    )
    assert diff_s["identical"]
    assert len(a) >= 1, "fixture A should produce at least one TradeExit"


# ---------------------------------------------------------------------------
# diff_exits self-checks — verify the diff harness flags divergences
# correctly without going through the pipeline.
# ---------------------------------------------------------------------------


def _make_exit(
    *,
    asset: str = "EURUSD",
    entry_h: int = 8,
    exit_h: int = 12,
    entry_price: float = 1.10,
    exit_price: float = 1.12,
    return_r: float = 0.5,
) -> TradeExit:
    return TradeExit(
        asset=asset,
        entry_timestamp_utc=datetime(2025, 1, 1, entry_h, tzinfo=UTC),
        exit_timestamp_utc=datetime(2025, 1, 2, exit_h, tzinfo=UTC),
        entry_price=entry_price,
        exit_price=exit_price,
        position_size=1000.0,
        atr_at_entry=0.04,
        return_r=return_r,
    )


def test_diff_exits_identical_lists() -> None:
    e = _make_exit()
    diff = diff_exits([e], [e])
    assert diff["identical"]
    assert diff["n_a"] == diff["n_b"] == diff["n_shared"] == 1


def test_diff_exits_flags_a_only() -> None:
    e = _make_exit()
    diff = diff_exits([e], [])
    assert not diff["identical"]
    assert len(diff["a_only"]) == 1
    assert diff["a_only"][0] == e


def test_diff_exits_flags_b_only() -> None:
    e = _make_exit()
    diff = diff_exits([], [e])
    assert not diff["identical"]
    assert len(diff["b_only"]) == 1


def test_diff_exits_flags_field_divergence() -> None:
    """Same key (asset + entry/exit ts) but different prices →
    field-level divergence flagged."""
    e1 = _make_exit(entry_price=1.10, exit_price=1.12, return_r=0.5)
    e2 = _make_exit(entry_price=1.10, exit_price=1.15, return_r=1.25)
    diff = diff_exits([e1], [e2])
    assert not diff["identical"]
    assert len(diff["field_diffs"]) == 1
    fields = diff["field_diffs"][0]["fields"]
    assert "exit_price" in fields
    assert "return_r" in fields


@pytest.mark.parametrize(
    "a_count,b_count,expected_identical",
    [(0, 0, True), (3, 3, False), (2, 0, False), (0, 5, False)],
)
def test_diff_exits_counts_consistency(
    a_count: int, b_count: int, expected_identical: bool
) -> None:
    a = [_make_exit(asset=f"A{i}") for i in range(a_count)]
    b = [_make_exit(asset=f"B{i}") for i in range(b_count)]
    diff = diff_exits(a, b)
    assert diff["identical"] == expected_identical


# ---------------------------------------------------------------------------
# diff_final_state self-checks
# ---------------------------------------------------------------------------


def test_diff_final_state_identical_when_baskets_match() -> None:
    s1 = StrategyState(current_basket={"A", "B"})
    s2 = StrategyState(current_basket={"A", "B"})
    assert diff_final_state(s1, s2)["identical"]


def test_diff_final_state_flags_basket_divergence() -> None:
    s1 = StrategyState(current_basket={"A", "B"})
    s2 = StrategyState(current_basket={"A", "C"})
    diff = diff_final_state(s1, s2)
    assert not diff["identical"]
    assert set(diff["basket_symmetric_diff"]) == {"B", "C"}


def test_diff_final_state_flags_open_positions_divergence() -> None:
    entry_a = TradeEntry(
        asset="A",
        entry_timestamp_utc=datetime(2025, 1, 1, tzinfo=UTC),
        entry_price=100.0,
        position_size=10.0,
        atr_at_entry=1.0,
    )
    entry_a_diff = TradeEntry(
        asset="A",
        entry_timestamp_utc=datetime(2025, 1, 1, tzinfo=UTC),
        entry_price=101.0,  # different price → flagged
        position_size=10.0,
        atr_at_entry=1.0,
    )
    s1 = StrategyState(current_basket={"A"}, open_positions={"A": entry_a})
    s2 = StrategyState(current_basket={"A"}, open_positions={"A": entry_a_diff})
    diff = diff_final_state(s1, s2)
    assert not diff["identical"]
    assert "A" in diff["open_positions_diff"]
