"""End-to-end integration tests for the breakout-retest H4 pipeline.

CRITICAL: this test file is reused by gate 3 of the research protocol
(audit look-ahead via streaming-vs-full-history diff). The fixtures
are minimal hand-built H4 + D1 frames where the expected setup count
is known by construction.

Three scenarios:

- A long-bias fixture producing exactly one long setup with known
  entry / SL / TP values.
- A short-bias fixture producing exactly one short setup, symmetric.
- A "failed retest" fixture (breakout fires, but the would-be retest
  bar closes back below the level) producing zero setups.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from src.strategies.breakout_retest_h4 import (
    StrategyParams,
    StrategyState,
    build_setup_candidates,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_h4(
    rows: list[tuple[float, float, float]],
    *,
    start: str = "2026-01-01 00:00",
) -> pd.DataFrame:
    """Build an H4 frame from (high, low, close) rows. open := close (sufficient
    for the strategy, which only reads high/low/close and time)."""
    times = pd.date_range(start, periods=len(rows), freq="4h", tz="UTC")
    return pd.DataFrame(
        {
            "time": times,
            "open": [r[2] for r in rows],
            "high": [r[0] for r in rows],
            "low": [r[1] for r in rows],
            "close": [r[2] for r in rows],
        }
    )


def _bullish_d1_close(level: float = 1000.0) -> pd.Series:
    """Build 60 D1 closes such that SMA50 < last close (bullish bias)."""
    closes = [level] * 49 + [level + 100, level + 110, level + 120] + [level + 100] * 8
    return pd.Series(closes, dtype="float64")


def _bearish_d1_close(level: float = 1000.0) -> pd.Series:
    """Build 60 D1 closes such that SMA50 > last close (bearish bias)."""
    closes = [level] * 49 + [level - 100, level - 110, level - 120] + [level - 100] * 8
    return pd.Series(closes, dtype="float64")


def _drive_pipeline(
    df_h4: pd.DataFrame,
    close_d1: pd.Series,
    instrument: str,
    params: StrategyParams,
) -> list:
    """Run the pipeline cycle-by-cycle on every H4 close in the frame.

    Returns the accumulated list of Setups produced. Mirrors how the
    production scheduler calls ``build_setup_candidates`` once per H4
    close — this is also exactly the input shape gate 3 uses for the
    streaming-vs-full-history audit.
    """
    state = StrategyState()
    setups: list = []
    for i in range(len(df_h4)):
        bar_open = df_h4["time"].iloc[i].to_pydatetime()
        # now_utc := the moment bar i has just closed.
        now_utc = bar_open + timedelta(hours=4)
        new_setups = build_setup_candidates(
            df_h4,
            close_d1,
            instrument,
            params,
            state,
            now_utc=now_utc,
        )
        setups.extend(new_setups)
    return setups


# ---------------------------------------------------------------------------
# Long scenario
# ---------------------------------------------------------------------------


def _long_fixture() -> pd.DataFrame:
    """Hand-built H4 frame with exactly one valid long setup.

    Layout:
      idx 0-4  : ramp-up (highs strictly increasing under 1020)
      idx 5    : pivot high = 1020 (the swing the breakout targets)
      idx 6-10 : ramp-down (highs strictly decreasing under 1020)
      idx 11   : breakout bar — close = 1025 > 1020
      idx 12   : drift bar (no touch)
      idx 13   : RETEST — low = 1019.0 (touch within tol), close = 1022 (held)
      idx 14-19: filler bars staying above 1020 (no second retest in
                 window)
    """
    rows = [
        # idx 0-4: rising
        (1004.0, 1000.0, 1002.0),
        (1006.0, 1002.0, 1004.0),
        (1008.0, 1004.0, 1006.0),
        (1010.0, 1006.0, 1008.0),
        (1012.0, 1008.0, 1010.0),
        # idx 5: pivot
        (1020.0, 1015.0, 1018.0),
        # idx 6-10: falling
        (1015.0, 1010.0, 1012.0),
        (1013.0, 1008.0, 1010.0),
        (1011.0, 1006.0, 1008.0),
        (1009.0, 1004.0, 1006.0),
        (1007.0, 1002.0, 1004.0),
        # idx 11: BREAKOUT (close 1025 > 1020)
        (1027.0, 1022.0, 1025.0),
        # idx 12: stays above, no touch
        (1028.0, 1024.0, 1026.0),
        # idx 13: RETEST — low 1019 (touch 1019 <= 1020 + tol=1.0), close 1022
        (1023.0, 1019.0, 1022.0),
        # idx 14-19: filler (stay above level, no second retest)
        (1024.0, 1022.5, 1023.0),
        (1025.0, 1023.0, 1024.0),
        (1026.0, 1024.0, 1025.0),
        (1027.0, 1025.0, 1026.0),
        (1028.0, 1026.0, 1027.0),
        (1029.0, 1027.0, 1028.0),
    ]
    return _build_h4(rows)


def test_pipeline_produces_one_long_setup_on_known_fixture() -> None:
    df_h4 = _long_fixture()
    close_d1 = _bullish_d1_close()
    params = StrategyParams(
        retest_tolerance=1.0,
        sl_buffer=0.5,
        max_risk_distance=10.0,
        n_swing=5,
        n_retest=8,
        rr_target=2.0,
        max_trades_per_day=2,
    )

    setups = _drive_pipeline(df_h4, close_d1, "XAUUSD", params)

    assert len(setups) == 1, f"expected exactly 1 setup, got {len(setups)}"
    s = setups[0]
    assert s.direction == "long"
    assert s.instrument == "XAUUSD"
    assert s.bias_d1 == "bullish"

    # Entry = retest close = 1022.0
    assert s.entry_price == pytest.approx(1022.0)
    # SL = retest low - sl_buffer = 1019.0 - 0.5 = 1018.5
    assert s.stop_loss == pytest.approx(1018.5)
    # Risk = 3.5; TP = 1022.0 + 3.5 * 2.0 = 1029.0
    assert s.take_profit == pytest.approx(1029.0)
    assert s.risk_reward == pytest.approx(2.0)

    # Setup timestamped at the retest bar (idx 13).
    expected_ts = df_h4["time"].iloc[13].to_pydatetime()
    assert s.timestamp_utc == expected_ts


# ---------------------------------------------------------------------------
# Short scenario
# ---------------------------------------------------------------------------


def _short_fixture() -> pd.DataFrame:
    """Symmetric short-bias fixture with exactly one valid short setup."""
    rows = [
        # idx 0-4: falling (lows strictly decreasing above 980)
        (1000.0, 996.0, 998.0),
        (998.0, 994.0, 996.0),
        (996.0, 992.0, 994.0),
        (994.0, 990.0, 992.0),
        (992.0, 988.0, 990.0),
        # idx 5: pivot LOW = 980
        (985.0, 980.0, 982.0),
        # idx 6-10: rising (lows strictly increasing above 980)
        (990.0, 985.0, 988.0),
        (992.0, 987.0, 990.0),
        (994.0, 989.0, 992.0),
        (996.0, 991.0, 994.0),
        (998.0, 993.0, 996.0),
        # idx 11: BREAKOUT — close 975 < 980
        (978.0, 973.0, 975.0),
        # idx 12: stays below
        (976.0, 972.0, 974.0),
        # idx 13: RETEST — high 981 (touch 981 >= 980 - tol=1.0), close 978
        (981.0, 977.0, 978.0),
        # idx 14-19: filler (stay below level)
        (977.5, 976.0, 977.0),
        (977.0, 975.0, 976.0),
        (976.0, 974.0, 975.0),
        (975.0, 973.0, 974.0),
        (974.0, 972.0, 973.0),
        (973.0, 971.0, 972.0),
    ]
    return _build_h4(rows)


def test_pipeline_produces_one_short_setup_on_symmetric_fixture() -> None:
    df_h4 = _short_fixture()
    close_d1 = _bearish_d1_close()
    params = StrategyParams(
        retest_tolerance=1.0,
        sl_buffer=0.5,
        max_risk_distance=10.0,
        n_swing=5,
        n_retest=8,
        rr_target=2.0,
        max_trades_per_day=2,
    )

    setups = _drive_pipeline(df_h4, close_d1, "XAUUSD", params)

    assert len(setups) == 1, f"expected exactly 1 setup, got {len(setups)}"
    s = setups[0]
    assert s.direction == "short"
    assert s.bias_d1 == "bearish"
    # Entry = retest close = 978
    assert s.entry_price == pytest.approx(978.0)
    # SL = retest high + sl_buffer = 981 + 0.5 = 981.5
    assert s.stop_loss == pytest.approx(981.5)
    # Risk = 3.5; TP = 978 - 3.5*2 = 971.0
    assert s.take_profit == pytest.approx(971.0)


# ---------------------------------------------------------------------------
# Failed retest scenario
# ---------------------------------------------------------------------------


def _failed_retest_fixture() -> pd.DataFrame:
    """Long-bias fixture with a breakout but a failing retest.

    Same shape as the long scenario but the would-be retest bar
    closes BELOW the broken level — touch ✓ but hold ✗ → no setup.
    No subsequent bar in the n_retest window retests successfully.
    """
    rows = [
        # idx 0-4
        (1004.0, 1000.0, 1002.0),
        (1006.0, 1002.0, 1004.0),
        (1008.0, 1004.0, 1006.0),
        (1010.0, 1006.0, 1008.0),
        (1012.0, 1008.0, 1010.0),
        # idx 5: pivot
        (1020.0, 1015.0, 1018.0),
        # idx 6-10
        (1015.0, 1010.0, 1012.0),
        (1013.0, 1008.0, 1010.0),
        (1011.0, 1006.0, 1008.0),
        (1009.0, 1004.0, 1006.0),
        (1007.0, 1002.0, 1004.0),
        # idx 11: BREAKOUT
        (1027.0, 1022.0, 1025.0),
        # idx 12: drift
        (1028.0, 1024.0, 1026.0),
        # idx 13: failing retest — low 1019 (touch ✓), close 1019 (hold ✗,
        # 1019 not strictly > 1020)
        (1023.0, 1019.0, 1019.0),
        # idx 14-19: stay BELOW 1020, no second-chance retest possible
        (1019.5, 1015.0, 1018.0),
        (1018.0, 1014.0, 1016.0),
        (1017.0, 1013.0, 1015.0),
        (1016.0, 1012.0, 1014.0),
        (1015.0, 1011.0, 1013.0),
        (1014.0, 1010.0, 1012.0),
    ]
    return _build_h4(rows)


def test_pipeline_produces_zero_setups_on_failed_retest() -> None:
    df_h4 = _failed_retest_fixture()
    close_d1 = _bullish_d1_close()
    params = StrategyParams(
        retest_tolerance=1.0,
        sl_buffer=0.5,
        max_risk_distance=10.0,
        n_swing=5,
        n_retest=8,
        rr_target=2.0,
        max_trades_per_day=2,
    )

    setups = _drive_pipeline(df_h4, close_d1, "XAUUSD", params)

    assert setups == [], f"expected 0 setups, got {len(setups)}: {setups}"


# ---------------------------------------------------------------------------
# Sanity: state container is reusable across symbols, daily-cap honoured
# ---------------------------------------------------------------------------


def test_state_locks_swing_after_breakout() -> None:
    """After the first breakout, the swing is in state.locked_swings;
    subsequent calls do not re-emit on the same level (anti
    double-dip, spec §5.1)."""
    df_h4 = _long_fixture()
    close_d1 = _bullish_d1_close()
    params = StrategyParams(
        retest_tolerance=1.0,
        sl_buffer=0.5,
        max_risk_distance=10.0,
    )
    state = StrategyState()
    # Drive only up to the breakout bar's close.
    breakout_close_utc = df_h4["time"].iloc[11].to_pydatetime() + timedelta(hours=4)
    build_setup_candidates(df_h4, close_d1, "XAUUSD", params, state, now_utc=breakout_close_utc)
    assert len(state.locked_swings) == 1
    # Second call at the same now_utc must not re-emit.
    build_setup_candidates(df_h4, close_d1, "XAUUSD", params, state, now_utc=breakout_close_utc)
    assert len(state.locked_swings) == 1


def test_neutral_bias_short_circuits_to_no_setup() -> None:
    df_h4 = _long_fixture()
    # Constant D1 → SMA50 == last close → neutral bias.
    close_d1 = pd.Series([1000.0] * 60, dtype="float64")
    params = StrategyParams(
        retest_tolerance=1.0,
        sl_buffer=0.5,
        max_risk_distance=10.0,
    )
    setups = _drive_pipeline(df_h4, close_d1, "XAUUSD", params)
    assert setups == []


def test_short_d1_history_skips_cycle_without_error() -> None:
    df_h4 = _long_fixture()
    short_d1 = pd.Series([1000.0] * 30, dtype="float64")  # < 50 closes
    params = StrategyParams(
        retest_tolerance=1.0,
        sl_buffer=0.5,
        max_risk_distance=10.0,
    )
    state = StrategyState()
    now_utc = datetime(2026, 1, 5, tzinfo=UTC)
    setups = build_setup_candidates(df_h4, short_d1, "XAUUSD", params, state, now_utc=now_utc)
    assert setups == []
