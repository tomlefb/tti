"""Unit tests for ``is_invalid`` / ``daily_key`` — spec §2.7."""

from __future__ import annotations

from datetime import date, datetime

from src.strategies.mean_reversion_bb_h4.invalidation import (
    daily_key,
    is_invalid,
)
from src.strategies.mean_reversion_bb_h4.types import ExcessEvent, ReturnEvent, Setup


def _setup(
    *,
    direction: str = "short",
    entry_price: float = 105.0,
    stop_loss: float = 110.5,
    take_profit: float = 100.0,
    risk_reward: float = 0.91,
    instrument: str = "XAUUSD",
    timestamp: datetime | None = None,
) -> Setup:
    excess = ExcessEvent(
        timestamp_utc=datetime(2026, 1, 5, 8, 0),
        bar_index=20,
        direction="upper",
        close=110.0,
        high=110.0,
        low=104.0,
        bb_level=108.0,
        penetration_atr=0.5,
    )
    ret = ReturnEvent(
        excess_event=excess,
        return_bar_timestamp=datetime(2026, 1, 5, 12, 0),
        return_bar_index=21,
        return_bar_close=entry_price,
        return_bar_high=entry_price + 0.5,
        return_bar_low=entry_price - 0.5,
        sma_at_return=take_profit,
    )
    return Setup(
        timestamp_utc=timestamp or datetime(2026, 1, 5, 12, 0),
        instrument=instrument,
        direction=direction,  # type: ignore[arg-type]
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        risk_reward=risk_reward,
        excess_event=excess,
        return_event=ret,
    )


def test_invalid_when_rr_below_min_rr() -> None:
    """RR floor (spec §2.7 first bullet)."""
    s = _setup(risk_reward=0.5)
    assert is_invalid(
        s,
        min_rr=1.0,
        max_risk_distance=100.0,
        daily_count=0,
        max_trades_per_day=2,
    ) is True


def test_invalid_when_risk_distance_too_large() -> None:
    """Risk-distance cap (spec §2.7 second bullet)."""
    # entry=105, SL=110.5 → risk = 5.5. Cap at 5.0 → invalid.
    s = _setup(entry_price=105.0, stop_loss=110.5)
    assert is_invalid(
        s,
        min_rr=0.5,
        max_risk_distance=5.0,
        daily_count=0,
        max_trades_per_day=2,
    ) is True


def test_invalid_when_daily_count_exceeded() -> None:
    """Per-day cap (spec §2.7 third bullet)."""
    s = _setup(risk_reward=2.0)  # OK on RR
    assert is_invalid(
        s,
        min_rr=1.0,
        max_risk_distance=100.0,
        daily_count=2,
        max_trades_per_day=2,
    ) is True


def test_valid_setup_passes() -> None:
    """All checks pass → returns False."""
    s = _setup(risk_reward=1.5, entry_price=105.0, stop_loss=110.5)
    assert is_invalid(
        s,
        min_rr=1.0,
        max_risk_distance=10.0,
        daily_count=0,
        max_trades_per_day=2,
    ) is False


def test_invalid_rr_at_threshold_passes() -> None:
    """RR == min_rr should pass (the floor is inclusive of the threshold)."""
    s = _setup(risk_reward=1.0, entry_price=105.0, stop_loss=110.5)
    assert is_invalid(
        s,
        min_rr=1.0,
        max_risk_distance=10.0,
        daily_count=0,
        max_trades_per_day=2,
    ) is False


def test_daily_key_uses_utc_date_of_setup() -> None:
    """Key for the per-day cap uses the setup timestamp's UTC date."""
    s = _setup(timestamp=datetime(2026, 3, 15, 10, 0), instrument="NDX100")
    assert daily_key(s) == ("NDX100", date(2026, 3, 15))
