"""Regression tests for the mean-reversion BB H4 look-ahead audit harness.

Two checks:

- The gate-2 hand-built fixtures pass the truncated-vs-full-frame
  diff (Mode A == Mode B). If this regresses, either the audit
  itself broke or the strategy started reading future data.
- ``diff_setups`` correctly flags asymmetric setup lists (A-only /
  B-only / field-divergent). Used as a sanity check on the diff
  logic itself.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from calibration.audit_mean_reversion_bb_h4 import (
    diff_setups,
    run_streaming,
    run_streaming_truncated,
    smoke_test,
)
from src.strategies.mean_reversion_bb_h4 import (
    ExcessEvent,
    ReturnEvent,
    Setup,
    StrategyParams,
)
from tests.strategies.mean_reversion_bb_h4.test_pipeline_integration import (
    _fixture_long,
    _params,
)


def test_smoke_test_passes() -> None:
    """The bundled smoke test must pass on the gate-2 fixtures.

    A regression here means either the audit harness broke or the
    strategy started leaking future data.
    """
    assert smoke_test() is True


def test_truncated_matches_full_frame_on_long_fixture() -> None:
    """Direct A == B check with no smoke-test wrapping."""
    df_h4 = _fixture_long()
    params = _params()
    a = run_streaming_truncated(df_h4, "XAUUSD", params)
    b = run_streaming(df_h4, "XAUUSD", params)
    diff = diff_setups(a, b)
    assert diff["identical"], (
        f"A-only={len(diff['a_only'])} B-only={len(diff['b_only'])} "
        f"field_diffs={len(diff['field_diffs'])}"
    )
    assert len(a) == 1


# ---------------------------------------------------------------------------
# diff_setups self-checks — verify the diff harness itself flags divergences
# correctly, without going through the pipeline.
# ---------------------------------------------------------------------------


def _make_setup(
    *,
    entry: float,
    sl: float,
    tp: float,
    rr: float = 1.0,
    ts_hour: int = 12,
) -> Setup:
    excess = ExcessEvent(
        timestamp_utc=datetime(2026, 1, 1, 8, tzinfo=UTC),
        bar_index=20,
        direction="lower",
        close=95.0,
        high=96.0,
        low=94.0,
        bb_level=97.0,
        penetration_atr=0.5,
    )
    ret = ReturnEvent(
        excess_event=excess,
        return_bar_timestamp=datetime(2026, 1, 1, ts_hour, tzinfo=UTC),
        return_bar_index=21,
        return_bar_close=entry,
        return_bar_high=entry + 0.5,
        return_bar_low=entry - 0.5,
        sma_at_return=tp,
    )
    return Setup(
        timestamp_utc=ret.return_bar_timestamp,
        instrument="XAUUSD",
        direction="long",
        entry_price=entry,
        stop_loss=sl,
        take_profit=tp,
        risk_reward=rr,
        excess_event=excess,
        return_event=ret,
    )


def test_diff_setups_identical_lists() -> None:
    s = _make_setup(entry=100.0, sl=99.0, tp=102.0)
    diff = diff_setups([s], [s])
    assert diff["identical"]
    assert diff["n_a"] == diff["n_b"] == diff["n_shared"] == 1


def test_diff_setups_flags_a_only() -> None:
    s = _make_setup(entry=100.0, sl=99.0, tp=102.0, ts_hour=12)
    diff = diff_setups([s], [])
    assert not diff["identical"]
    assert len(diff["a_only"]) == 1
    assert diff["a_only"][0] == s


def test_diff_setups_flags_b_only() -> None:
    s = _make_setup(entry=100.0, sl=99.0, tp=102.0, ts_hour=12)
    diff = diff_setups([], [s])
    assert not diff["identical"]
    assert len(diff["b_only"]) == 1
    assert diff["b_only"][0] == s


def test_diff_setups_flags_field_divergence_on_shared_key() -> None:
    """Two setups with the same canonical key but different SL/TP must
    be flagged as a field-level divergence — the most subtle leak
    signature (same trade detected with different geometry)."""
    s1 = _make_setup(entry=100.0, sl=99.0, tp=102.0, ts_hour=12)
    s2 = _make_setup(entry=100.0, sl=98.5, tp=103.0, ts_hour=12)
    diff = diff_setups([s1], [s2])
    assert not diff["identical"]
    assert len(diff["field_diffs"]) == 1
    fields = diff["field_diffs"][0]["fields"]
    assert "stop_loss" in fields
    assert fields["stop_loss"] == (99.0, 98.5)
    assert "take_profit" in fields


@pytest.mark.parametrize(
    "a_count,b_count,expected_identical",
    [(0, 0, True), (1, 1, False), (5, 0, False), (0, 5, False)],
)
def test_diff_setups_counts_consistency(
    a_count: int, b_count: int, expected_identical: bool
) -> None:
    """Quick sanity matrix for the counts."""
    a = [
        _make_setup(entry=100.0 + i, sl=99.0, tp=102.0, ts_hour=i)
        for i in range(a_count)
    ]
    b = [
        _make_setup(entry=110.0 + i, sl=109.0, tp=112.0, ts_hour=i)
        for i in range(b_count)
    ]
    diff = diff_setups(a, b)
    assert diff["identical"] == expected_identical


def test_pipeline_returns_empty_on_short_frame() -> None:
    """Direct guard test: the pipeline early-returns ``[]`` when the
    H4 frame has fewer rows than ``params.bb_period``. Without this
    guard, ``compute_bollinger`` raises and the truncated audit mode
    cannot run on early cycles. Asserting it here so a future change
    that removes the guard breaks loudly."""
    import pandas as pd

    from src.strategies.mean_reversion_bb_h4 import (
        StrategyState,
        build_setup_candidates,
    )

    times = pd.date_range("2026-01-01 00:00", periods=5, freq="4h", tz="UTC")
    short_df = pd.DataFrame(
        {
            "time": times,
            "open": [100.0] * 5,
            "high": [100.5] * 5,
            "low": [99.5] * 5,
            "close": [100.0] * 5,
        }
    )
    params = StrategyParams(
        min_penetration_atr_mult=0.3, sl_buffer=1.0, max_risk_distance=1e9
    )
    state = StrategyState()
    setups = build_setup_candidates(
        short_df,
        "XAUUSD",
        params,
        state,
        now_utc=datetime(2026, 1, 1, 20, 0, tzinfo=UTC),
    )
    assert setups == []
