"""Integration tests on the committed historical OHLC fixtures.

These are sanity checks, NOT correctness tests against ground truth — that's
what the calibration harness (``calibration/run_swing_calibration.py``) is for.
The goals here are only:

1. The detector runs end-to-end on each real fixture without raising.
2. The output schema is preserved.
3. The number of significant swings on ~1 year of H4/H1 data falls inside a
   plausible band — guards against gross regressions (e.g. a bug that
   detects every bar or none).
4. Detected swings sit inside the fixture time range.
5. Significant swings mostly alternate high/low, as expected by construction
   of the amplitude filter.

Default config values mirror ``config/settings.py.example``. Hardcoded here
because ``config/settings.py`` is gitignored and not present on the dev Mac;
keep them in sync if the example changes.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from src.detection.bias import compute_daily_bias, compute_timeframe_bias
from src.detection.swings import find_swings

# Defaults that mirror config/settings.py.example. Not imported because
# config.settings imports config.secrets (gitignored).
_SWING_LOOKBACK_H4 = 2
_SWING_LOOKBACK_H1 = 2
_MIN_SWING_AMPLITUDE_ATR_MULT_H4 = 0.5
_MIN_SWING_AMPLITUDE_ATR_MULT_H1 = 0.5
_BIAS_SWING_COUNT = 4
_ATR_PERIOD = 14

_PAIRS = ["XAUUSD", "NDX100", "EURUSD", "GBPUSD"]
_FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "historical"


def _load(symbol: str, tf: str) -> pd.DataFrame:
    path = _FIXTURE_DIR / f"{symbol}_{tf}.parquet"
    if not path.exists():
        pytest.skip(f"fixture missing: {path}")
    return pd.read_parquet(path)


@pytest.mark.parametrize("symbol", _PAIRS)
@pytest.mark.parametrize(
    ("tf", "lookback"),
    [("H4", _SWING_LOOKBACK_H4), ("H1", _SWING_LOOKBACK_H1)],
)
def test_find_swings_runs_on_fixtures(symbol: str, tf: str, lookback: int) -> None:
    df = _load(symbol, tf)
    swings = find_swings(
        df,
        lookback=lookback,
        min_amplitude_atr_mult=(
            _MIN_SWING_AMPLITUDE_ATR_MULT_H4 if tf == "H4" else _MIN_SWING_AMPLITUDE_ATR_MULT_H1
        ),
        atr_period=_ATR_PERIOD,
    )

    # 1. Schema preserved
    assert list(swings.columns) == ["swing_type", "swing_price"]
    assert len(swings) == len(df)

    sig = swings[swings["swing_type"].notna()]

    # 2. Plausible swing count, scaled to fixture depth.
    # The amplitude filter at ATR×0.5 with lookback=2 keeps roughly
    # 25-30% of bars on H1/H4 of the validated portfolio (XAU/NDX/
    # EUR/GBP). Bounds are parametric so the test stays valid across
    # fixture re-exports of any depth — this is a regression guard,
    # not a precision test. Tighten after Sprint 1 calibration.
    n_sig = len(sig)
    n_bars = len(df)
    min_swings = max(5, n_bars // 200)
    max_swings = n_bars // 3
    assert min_swings <= n_sig <= max_swings, (
        f"{symbol} {tf}: {n_sig} swings outside [{min_swings}, {max_swings}] "
        f"on {n_bars} bars"
    )

    # 3. All swing rows fall inside the fixture's time range.
    times = df.loc[sig.index, "time"]
    assert times.min() >= df["time"].min()
    assert times.max() <= df["time"].max()

    # 4. Mostly alternating types: count adjacent same-type pairs in the
    # significant series. With the amplitude filter active this should be
    # the minority case.
    types = sig["swing_type"].tolist()
    if len(types) >= 2:
        same = sum(1 for a, b in zip(types, types[1:], strict=False) if a == b)
        ratio = same / (len(types) - 1)
        # Same threshold caveat as the swing-count band: pre-calibration the
        # rate sits around 20-25% on this fixture set; treat >35% as a
        # regression signal.
        assert ratio <= 0.35, (
            f"{symbol} {tf}: {ratio:.2%} adjacent same-type swings " f"(>35% suggests a regression)"
        )


@pytest.mark.parametrize("symbol", _PAIRS)
def test_compute_timeframe_bias_returns_valid_label(symbol: str) -> None:
    df_h4 = _load(symbol, "H4")
    swings = find_swings(
        df_h4,
        lookback=_SWING_LOOKBACK_H4,
        min_amplitude_atr_mult=_MIN_SWING_AMPLITUDE_ATR_MULT_H4,
        atr_period=_ATR_PERIOD,
    )
    bias = compute_timeframe_bias(swings, _BIAS_SWING_COUNT)
    assert bias in {"bullish", "bearish", "no_trade"}


@pytest.mark.parametrize("symbol", _PAIRS)
def test_compute_daily_bias_runs_end_to_end(symbol: str) -> None:
    df_h4 = _load(symbol, "H4")
    df_h1 = _load(symbol, "H1")
    bias = compute_daily_bias(
        df_h4=df_h4,
        df_h1=df_h1,
        swing_lookback_h4=_SWING_LOOKBACK_H4,
        swing_lookback_h1=_SWING_LOOKBACK_H1,
        min_amplitude_atr_mult_h4=_MIN_SWING_AMPLITUDE_ATR_MULT_H4,
        min_amplitude_atr_mult_h1=_MIN_SWING_AMPLITUDE_ATR_MULT_H1,
        bias_swing_count=_BIAS_SWING_COUNT,
        atr_period=_ATR_PERIOD,
    )
    assert bias in {"bullish", "bearish", "no_trade"}
