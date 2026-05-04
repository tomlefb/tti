"""Unit tests for ``sizing_for_entry`` — spec §2.5 risk parity."""

from __future__ import annotations

import math

import pytest

from src.strategies.trend_rotation_d1.sizing import sizing_for_entry


def test_risk_parity_sizing_inverse_to_atr() -> None:
    """size = risk_dollars / atr → halving ATR doubles size."""
    capital = 100_000.0
    risk = 0.01  # 1 %
    size_low_vol = sizing_for_entry(
        capital=capital, risk_fraction=risk, atr_at_entry=10.0
    )
    size_high_vol = sizing_for_entry(
        capital=capital, risk_fraction=risk, atr_at_entry=20.0
    )
    assert size_low_vol == pytest.approx(2 * size_high_vol)


def test_risk_parity_sizing_known_value() -> None:
    """Hand-check: $100k × 1 % / ATR=$5 = 200 units."""
    size = sizing_for_entry(capital=100_000.0, risk_fraction=0.01, atr_at_entry=5.0)
    assert size == pytest.approx(200.0)


def test_sizing_handles_zero_atr() -> None:
    """ATR == 0 (degenerate, never seen on real data but possible
    on a synthetic flat fixture) → return ``None`` to signal the
    asset cannot be sized; caller (pipeline) skips this entry."""
    assert (
        sizing_for_entry(capital=100_000.0, risk_fraction=0.01, atr_at_entry=0.0)
        is None
    )


def test_sizing_handles_negative_atr() -> None:
    """Negative ATR is a programming error in the upstream
    computation, surface explicitly."""
    with pytest.raises(ValueError, match="atr"):
        sizing_for_entry(capital=100_000.0, risk_fraction=0.01, atr_at_entry=-1.0)


def test_sizing_consistent_across_assets_with_same_volatility() -> None:
    """Two assets with the same ATR → same position size, regardless
    of price level. This is the *risk parity* property: identical
    dollar risk per asset."""
    size_btc = sizing_for_entry(
        capital=100_000.0, risk_fraction=0.01, atr_at_entry=1500.0
    )
    size_eur = sizing_for_entry(
        capital=100_000.0, risk_fraction=0.01, atr_at_entry=1500.0
    )
    assert size_btc == size_eur


def test_sizing_handles_nan_atr() -> None:
    """NaN ATR (warmup edge) → ``None``."""
    assert (
        sizing_for_entry(
            capital=100_000.0, risk_fraction=0.01, atr_at_entry=float("nan")
        )
        is None
    )
