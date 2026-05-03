"""Unit tests for ``build_setup`` — spec §2.6.

Key spec property: RR is **computed** from the SMA-target (not pinned
at a fixed multiple), so ``setup.risk_reward`` may legitimately span
0.5–2.5. The §2.7 floor at ``min_rr`` is enforced in
``invalidation.is_invalid``, NOT in ``build_setup``.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from src.strategies.mean_reversion_bb_h4.setup import build_setup
from src.strategies.mean_reversion_bb_h4.types import ExcessEvent, ReturnEvent


def _events(
    *,
    direction: str,
    excess_high: float,
    excess_low: float,
    return_close: float,
    sma_at_return: float,
) -> tuple[ExcessEvent, ReturnEvent]:
    excess = ExcessEvent(
        timestamp_utc=datetime(2026, 1, 5, 8, 0),
        bar_index=20,
        direction=direction,  # type: ignore[arg-type]
        close=excess_high if direction == "upper" else excess_low,
        high=excess_high,
        low=excess_low,
        bb_level=0.0,
        penetration_atr=float("nan"),
    )
    ret = ReturnEvent(
        excess_event=excess,
        return_bar_timestamp=datetime(2026, 1, 5, 12, 0),
        return_bar_index=21,
        return_bar_close=return_close,
        return_bar_high=return_close + 0.5,
        return_bar_low=return_close - 0.5,
        sma_at_return=sma_at_return,
    )
    return excess, ret


def test_setup_short_arithmetic_excess_upper() -> None:
    """Excess upper → SHORT setup.

    excess.high = 110, sl_buffer = 0.5 → SL = 110.5.
    return_close = 105 → entry = 105.
    sma_at_return = 100 → TP = 100.
    risk = SL - entry = 5.5; reward = entry - TP = 5; RR = 5/5.5 ≈ 0.909.
    """
    _, ret = _events(
        direction="upper",
        excess_high=110.0,
        excess_low=104.0,
        return_close=105.0,
        sma_at_return=100.0,
    )
    s = build_setup(ret, instrument="XAUUSD", sl_buffer=0.5)

    assert s.direction == "short"
    assert s.instrument == "XAUUSD"
    assert s.entry_price == pytest.approx(105.0)
    assert s.stop_loss == pytest.approx(110.5)
    assert s.take_profit == pytest.approx(100.0)
    assert s.risk_reward == pytest.approx(5.0 / 5.5)


def test_setup_long_arithmetic_excess_lower() -> None:
    """Excess lower → LONG setup.

    excess.low = 90, sl_buffer = 0.5 → SL = 89.5.
    return_close = 95 → entry = 95.
    sma_at_return = 100 → TP = 100.
    risk = entry - SL = 5.5; reward = TP - entry = 5; RR ≈ 0.909.
    """
    _, ret = _events(
        direction="lower",
        excess_high=96.0,
        excess_low=90.0,
        return_close=95.0,
        sma_at_return=100.0,
    )
    s = build_setup(ret, instrument="XAUUSD", sl_buffer=0.5)

    assert s.direction == "long"
    assert s.entry_price == pytest.approx(95.0)
    assert s.stop_loss == pytest.approx(89.5)
    assert s.take_profit == pytest.approx(100.0)
    assert s.risk_reward == pytest.approx(5.0 / 5.5)


def test_setup_rr_calculated_from_sma_target() -> None:
    """RR depends only on the relative geometry of (entry, SL, TP),
    NOT on a fixed multiple. Vary the SMA position to vary the RR."""
    _, ret_close_to_sma = _events(
        direction="upper",
        excess_high=110.0,
        excess_low=104.0,
        return_close=109.0,
        sma_at_return=108.0,  # close to entry → small reward → low RR
    )
    s_low_rr = build_setup(ret_close_to_sma, instrument="X", sl_buffer=0.5)

    _, ret_far_from_sma = _events(
        direction="upper",
        excess_high=110.0,
        excess_low=104.0,
        return_close=109.0,
        sma_at_return=100.0,  # far from entry → large reward → high RR
    )
    s_high_rr = build_setup(ret_far_from_sma, instrument="X", sl_buffer=0.5)

    assert s_high_rr.risk_reward > s_low_rr.risk_reward
    # Sanity check the actual numbers.
    # s_low_rr: risk = 110.5 - 109 = 1.5; reward = 109 - 108 = 1; RR = 1/1.5 ≈ 0.667.
    assert s_low_rr.risk_reward == pytest.approx(1.0 / 1.5)
    # s_high_rr: risk = 1.5; reward = 109 - 100 = 9; RR = 6.0.
    assert s_high_rr.risk_reward == pytest.approx(9.0 / 1.5)


def test_setup_rr_can_be_below_1() -> None:
    """Spec §2.6: computed RR may legitimately be < 1.0. The min_rr
    floor lives in invalidation, not here."""
    _, ret = _events(
        direction="upper",
        excess_high=110.0,
        excess_low=104.0,
        return_close=109.0,
        sma_at_return=108.5,  # tiny reward
    )
    s = build_setup(ret, instrument="X", sl_buffer=0.5)
    # risk = 1.5; reward = 0.5; RR ≈ 0.333.
    assert s.risk_reward == pytest.approx(0.5 / 1.5)
    assert s.risk_reward < 1.0


def test_setup_timestamp_is_return_bar() -> None:
    """The setup is timestamped at the return bar (entry triggers there)."""
    _, ret = _events(
        direction="upper",
        excess_high=110.0,
        excess_low=104.0,
        return_close=105.0,
        sma_at_return=100.0,
    )
    s = build_setup(ret, instrument="X", sl_buffer=0.5)
    assert s.timestamp_utc == datetime(2026, 1, 5, 12, 0)


def test_setup_zero_risk_raises() -> None:
    """Degenerate case: return_close == excess_extreme + sl_buffer.

    If ``return_close == excess.high + sl_buffer`` on a short, then
    risk = 0 → divide-by-zero / invalid setup. Surface explicitly so
    a silent NaN does not propagate to the audit trail.
    """
    _, ret = _events(
        direction="upper",
        excess_high=110.0,
        excess_low=104.0,
        return_close=110.5,  # exactly equals SL = excess.high + 0.5
        sma_at_return=100.0,
    )
    with pytest.raises(ValueError, match="risk"):
        build_setup(ret, instrument="X", sl_buffer=0.5)
