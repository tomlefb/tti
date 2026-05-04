"""Unit tests for ``compute_atr`` + ``passes_volatility_regime`` —
spec §3.1 (ATR(20)) and §2.6 (5x avg 90j filter)."""

from __future__ import annotations

import pandas as pd
import pytest

from src.strategies.trend_rotation_d1.volatility import (
    compute_atr,
    passes_volatility_regime,
)


def _build_ohlc(closes: list[float], range_size: float = 1.0) -> pd.DataFrame:
    """Synthetic OHLC: open=close-shift, high=close+r/2, low=close-r/2."""
    closes_s = pd.Series(closes)
    return pd.DataFrame(
        {
            "open": closes_s.shift(1).fillna(closes_s.iloc[0]),
            "high": closes_s + range_size / 2,
            "low": closes_s - range_size / 2,
            "close": closes_s,
        }
    )


def test_compute_atr_period_20() -> None:
    """ATR(20) = SMA(20) of True Range. On a constant-range fixture
    (range = 1.0 every bar, no overnight gap), TR = 1.0 and so
    ATR = 1.0 from index 19 onwards."""
    df = _build_ohlc([100.0] * 30, range_size=1.0)
    atr = compute_atr(df, period=20)
    # Bars 0..18 are NaN (need 20 for the SMA).
    assert atr.iloc[:19].isna().all()
    # Bars 19..29 = 1.0 (constant TR).
    for i in range(19, 30):
        assert atr.iloc[i] == pytest.approx(1.0)


def test_atr_uses_only_past_data() -> None:
    """Anti-look-ahead: truncating the future of an OHLC frame must
    not change the ATR at the truncation point."""
    closes = [100.0 + (i % 3) for i in range(40)]
    df_full = _build_ohlc(closes)
    df_short = df_full.iloc[:30].copy()
    atr_full = compute_atr(df_full, period=20)
    atr_short = compute_atr(df_short, period=20)
    for i in range(19, 30):
        assert atr_full.iloc[i] == pytest.approx(atr_short.iloc[i])


def test_volatility_regime_filter_passes_normal_assets() -> None:
    """Asset with ATR ≈ median(ATR over 90 d) → passes."""
    df = _build_ohlc([100.0 + (i % 5) for i in range(120)], range_size=1.0)
    atr = compute_atr(df, period=20)
    assert passes_volatility_regime(
        atr,
        explosive_threshold=5.0,
        regime_lookback=90,
    )


def test_volatility_regime_filter_excludes_explosive_assets() -> None:
    """Asset with last ATR > threshold × median → blocked."""
    closes = [100.0] * 100
    df = _build_ohlc(closes, range_size=1.0)
    # Inject a flash-crash bar at the end: huge range relative to
    # the constant 1.0 baseline. Override the high/low directly.
    df.loc[df.index[-1], "high"] = 200.0
    df.loc[df.index[-1], "low"] = 0.0
    df.loc[df.index[-1], "close"] = 50.0
    atr = compute_atr(df, period=20)
    # ATR jump should drag the last value well above 5×median.
    assert not passes_volatility_regime(
        atr,
        explosive_threshold=5.0,
        regime_lookback=90,
    )


def test_volatility_regime_filter_short_history_passes_safely() -> None:
    """When the regime-lookback (90 d) cannot be computed (history <
    period + lookback), the filter passes by default — no spurious
    exclusion at strategy warmup."""
    df = _build_ohlc([100.0] * 40, range_size=1.0)
    atr = compute_atr(df, period=20)
    # Only ATR values available; not enough for 90-d median.
    # Convention: pass when the median is undefined.
    assert passes_volatility_regime(
        atr,
        explosive_threshold=5.0,
        regime_lookback=90,
    )


def test_volatility_regime_filter_handles_nan_at_tail() -> None:
    """An ATR series whose last value is NaN (rare; bar removed from
    the source) is treated as 'unknown regime' → pass (do not
    exclude on missing data)."""
    df = _build_ohlc([100.0] * 120, range_size=1.0)
    atr = compute_atr(df, period=20)
    atr.iloc[-1] = float("nan")
    assert passes_volatility_regime(
        atr,
        explosive_threshold=5.0,
        regime_lookback=90,
    )
