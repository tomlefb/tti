"""Regression tests for the breakout-retest H4 look-ahead audit harness.

Two checks:

- The gate-2 hand-built fixtures pass the streaming-vs-truncated
  diff (Mode A == Mode B). If this regresses, either the audit
  itself broke or the strategy started reading future data.
- ``diff_setups`` correctly flags asymmetric setup lists (A-only /
  B-only / field-divergent). Used as a sanity check on the diff
  logic itself.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from calibration.audit_breakout_retest_h4 import (
    diff_setups,
    run_streaming,
    run_streaming_truncated,
    smoke_test,
)
from src.strategies.breakout_retest_h4 import (
    BreakoutEvent,
    RetestEvent,
    Setup,
    StrategyParams,
    Swing,
)
from tests.strategies.breakout_retest_h4.test_pipeline_integration import (
    _bullish_d1_close,
    _long_fixture,
)


def test_smoke_test_passes() -> None:
    """The bundled smoke test must pass on the gate-2 fixtures.

    A regression here means either:
      - the audit harness broke,
      - or the strategy started leaking future data.
    """
    assert smoke_test() is True


def test_streaming_full_frame_matches_truncated_on_long_fixture() -> None:
    """Direct A == B check with no smoke-test wrapping."""
    df_h4 = _long_fixture()
    close_d1 = _bullish_d1_close()
    params = StrategyParams(retest_tolerance=1.0, sl_buffer=0.5, max_risk_distance=10.0)
    a = run_streaming(df_h4, close_d1, "XAUUSD", params)
    b = run_streaming_truncated(df_h4, close_d1, "XAUUSD", params)
    diff = diff_setups(a, b)
    assert diff["identical"], (
        f"A-only={len(diff['a_only'])} B-only={len(diff['b_only'])} "
        f"field_diffs={len(diff['field_diffs'])}"
    )
    assert len(a) == 1


# ---------------------------------------------------------------------------
# diff_setups self-checks — we want a clear, structured signal when divergence
# does happen. Build hand-rolled Setup instances to avoid running the pipeline
# (the goal here is to test the diff harness itself).
# ---------------------------------------------------------------------------


def _make_setup(*, entry: float, sl: float, tp: float, ts_hour: int = 0) -> Setup:
    swing = Swing(
        timestamp_utc=datetime(2026, 1, 1, tzinfo=UTC),
        price=110.0,
        direction="high",
        bar_index=5,
    )
    breakout = BreakoutEvent(
        swing=swing,
        breakout_bar_timestamp=datetime(2026, 1, 1, 4, tzinfo=UTC),
        breakout_bar_close=112.0,
        direction="long",
    )
    retest = RetestEvent(
        breakout_event=breakout,
        retest_bar_timestamp=datetime(2026, 1, 1, ts_hour, tzinfo=UTC),
        retest_bar_low=109.5,
        retest_bar_high=111.0,
        retest_bar_close=110.5,
    )
    return Setup(
        timestamp_utc=retest.retest_bar_timestamp,
        instrument="XAUUSD",
        direction="long",
        entry_price=entry,
        stop_loss=sl,
        take_profit=tp,
        risk_reward=2.0,
        bias_d1="bullish",
        breakout_event=breakout,
        retest_event=retest,
    )


def test_diff_setups_identical_lists() -> None:
    s = _make_setup(entry=110.0, sl=109.0, tp=112.0)
    diff = diff_setups([s], [s])
    assert diff["identical"]
    assert diff["n_a"] == diff["n_b"] == diff["n_shared"] == 1


def test_diff_setups_flags_a_only() -> None:
    s1 = _make_setup(entry=110.0, sl=109.0, tp=112.0, ts_hour=12)
    diff = diff_setups([s1], [])
    assert not diff["identical"]
    assert len(diff["a_only"]) == 1
    assert diff["a_only"][0] == s1


def test_diff_setups_flags_b_only() -> None:
    s1 = _make_setup(entry=110.0, sl=109.0, tp=112.0, ts_hour=12)
    diff = diff_setups([], [s1])
    assert not diff["identical"]
    assert len(diff["b_only"]) == 1
    assert diff["b_only"][0] == s1


def test_diff_setups_flags_field_divergence_on_shared_key() -> None:
    """Two setups with the same canonical key but a different field must
    be flagged as a field-level divergence — that is the most subtle
    leak signature (same trade detected, but with different SL/TP)."""
    s1 = _make_setup(entry=110.0, sl=109.0, tp=112.0, ts_hour=12)
    s2 = _make_setup(entry=110.0, sl=108.5, tp=113.0, ts_hour=12)
    diff = diff_setups([s1], [s2])
    assert not diff["identical"]
    assert len(diff["field_diffs"]) == 1
    fields = diff["field_diffs"][0]["fields"]
    assert "stop_loss" in fields
    assert fields["stop_loss"] == (109.0, 108.5)
    assert "take_profit" in fields


@pytest.mark.parametrize(
    "a_count,b_count,expected_identical",
    [(0, 0, True), (1, 1, False), (5, 0, False), (0, 5, False)],
)
def test_diff_setups_counts_consistency(
    a_count: int, b_count: int, expected_identical: bool
) -> None:
    """Quick sanity matrix for the counts."""
    a = [_make_setup(entry=110.0 + i, sl=109.0, tp=112.0, ts_hour=i) for i in range(a_count)]
    b = [_make_setup(entry=120.0 + i, sl=119.0, tp=122.0, ts_hour=i) for i in range(b_count)]
    diff = diff_setups(a, b)
    assert (diff["identical"]) == expected_identical
