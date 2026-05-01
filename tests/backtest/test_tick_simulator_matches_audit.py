"""Acceptance test for the Phase B tick simulator.

The look-ahead audit at ``calibration/audit_lookahead.py`` defines the
production-truthful contract: at the production scheduler tick
``next_5min_tick_after(mss_confirm)``, the detector observes data with
``time + timeframe <= now_utc`` and emits the setup. The
``simulate_target_date`` simulator iterates ticks and accumulates
setups, locking on first emission.

These two paths must agree bit-identically: for every setup the
simulator emits, re-running ``build_setup_candidates`` on a frame
truncated to the simulator's first-emission tick must reproduce the
same Setup. Any divergence between the simulator and the audit's
truncation rule means the simulator is silently using future data the
audit would catch — defeating the purpose of Phase B.

We anchor the test on a small, committed fixture (NDX100 H1/H4/D1/M5
under ``tests/fixtures/historical/``) and a single trading date with
multiple setups. The test runs the full backtest harness and audit
truncation in one pytest invocation; runtime is a few seconds.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.backtest.tick_simulator import _identity, simulate_target_date
from src.detection.setup import Setup, build_setup_candidates

_TZ_PARIS = ZoneInfo("Europe/Paris")
_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "historical"
# Picked because the simulator emits 3 setups on this date (smoke-tested
# against the committed NDX100 fixture). Any date with multiple setups
# would do; we anchor on one to keep the test deterministic.
_TARGET_DATE = date(2025, 10, 22)
_LOOKBACK_DAYS = 60


def _settings() -> SimpleNamespace:
    ndx_cfg = {"sweep_buffer": 5.0, "equal_hl_tolerance": 3.0, "sl_buffer": 5.0}
    return SimpleNamespace(
        SESSION_ASIA=(2, 0, 6, 0),
        KILLZONE_LONDON=(9, 0, 12, 0),
        KILLZONE_NY=(15, 30, 18, 0),
        SWING_LOOKBACK_H4=2,
        SWING_LOOKBACK_H1=2,
        SWING_LOOKBACK_M5=2,
        MIN_SWING_AMPLITUDE_ATR_MULT_H4=1.3,
        MIN_SWING_AMPLITUDE_ATR_MULT_H1=1.0,
        MIN_SWING_AMPLITUDE_ATR_MULT_M5=1.0,
        BIAS_SWING_COUNT=4,
        BIAS_REQUIRE_H1_CONFIRMATION=False,
        H4_H1_TIME_TOLERANCE_CANDLES_H4=2,
        H4_H1_PRICE_TOLERANCE_FRACTION=0.001,
        SWING_LEVELS_LOOKBACK_COUNT=5,
        SWEEP_RETURN_WINDOW_CANDLES=2,
        SWEEP_DEDUP_TIME_WINDOW_MINUTES=30,
        SWEEP_DEDUP_PRICE_TOLERANCE_FRACTION=0.001,
        MSS_DISPLACEMENT_MULTIPLIER=1.5,
        MSS_DISPLACEMENT_LOOKBACK=20,
        FVG_ATR_PERIOD=14,
        FVG_MIN_SIZE_ATR_MULTIPLIER=0.3,
        MIN_RR=3.0,
        A_PLUS_RR_THRESHOLD=4.0,
        PARTIAL_TP_RR_TARGET=5.0,
        INSTRUMENT_CONFIG={"NDX100": ndx_cfg},
    )


def _next_5min_tick_after(t: datetime) -> datetime:
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    floored = t.replace(second=0, microsecond=0, minute=(t.minute // 5) * 5)
    if floored <= t:
        floored = floored + timedelta(minutes=5)
    return floored


def _load_window(symbol: str, target_d: date) -> dict[str, pd.DataFrame]:
    end_utc = (
        datetime.combine(target_d, time(23, 59))
        .replace(tzinfo=_TZ_PARIS)
        .astimezone(UTC)
        + timedelta(days=1)
    )
    start_utc = end_utc - timedelta(days=_LOOKBACK_DAYS)
    out: dict[str, pd.DataFrame] = {}
    for tf in ("D1", "H4", "H1", "M5"):
        df = pd.read_parquet(_FIXTURE_DIR / f"{symbol}_{tf}.parquet")
        if df["time"].dt.tz is None:
            df["time"] = df["time"].dt.tz_localize("UTC")
        df = df.sort_values("time").reset_index(drop=True)
        df = df.loc[(df["time"] >= start_utc) & (df["time"] <= end_utc)].reset_index(drop=True)
        out[tf] = df
    return out


def _setup_signature(s: Setup) -> dict:
    """Every field whose value should match between simulator emission
    and the audit's truncated re-run."""
    poi_kind = type(s.poi).__name__
    if poi_kind == "FVG":
        poi = {
            "kind": "FVG",
            "proximal": float(s.poi.proximal),
            "distal": float(s.poi.distal),
            "size": float(s.poi.size),
            "size_atr_ratio": float(s.poi.size_atr_ratio),
        }
    else:
        poi = {
            "kind": poi_kind,
            "proximal": float(s.poi.proximal),
            "distal": float(s.poi.distal),
            "candle_time_utc": s.poi.candle_time_utc.isoformat(),
        }
    return {
        "timestamp_utc": s.timestamp_utc.isoformat(),
        "direction": s.direction,
        "daily_bias": s.daily_bias,
        "killzone": s.killzone,
        "swept_level_price": float(s.swept_level_price),
        "swept_level_type": s.swept_level_type,
        "swept_level_strength": s.swept_level_strength,
        "sweep_candle_time_utc": s.sweep.sweep_candle_time_utc.isoformat(),
        "sweep_extreme_price": float(s.sweep.sweep_extreme_price),
        "sweep_return_candle_time_utc": s.sweep.return_candle_time_utc.isoformat(),
        "mss_confirm_candle_time_utc": s.mss.mss_confirm_candle_time_utc.isoformat(),
        "mss_broken_swing_time_utc": s.mss.broken_swing_time_utc.isoformat(),
        "mss_broken_swing_price": float(s.mss.broken_swing_price),
        "mss_displacement_body_ratio": float(s.mss.displacement_body_ratio),
        "poi_type": s.poi_type,
        "poi": poi,
        "entry_price": float(s.entry_price),
        "stop_loss": float(s.stop_loss),
        "target_level_type": s.target_level_type,
        "tp_runner_price": float(s.tp_runner_price),
        "tp_runner_rr": float(s.tp_runner_rr),
        "tp1_price": float(s.tp1_price),
        "tp1_rr": float(s.tp1_rr),
        "quality": s.quality,
        "confluences": list(s.confluences),
    }


def test_simulator_setups_match_audit_truncation():
    """For each setup the tick simulator emits on a known multi-setup
    date, the audit's truncated re-run must produce the same Setup.

    Concretely: take the simulator's full-data result with
    ``now_utc=tick``, and verify that running
    ``build_setup_candidates`` on a frame truncated to ``time <=
    mss_confirm`` (with the same ``now_utc``) returns a Setup with the
    same identity AND the same downstream fields.
    """
    settings = _settings()
    window = _load_window("NDX100", _TARGET_DATE)

    simulated: list[Setup] = simulate_target_date(
        df_h4=window["H4"],
        df_h1=window["H1"],
        df_m5=window["M5"],
        df_d1=window["D1"],
        target_date=_TARGET_DATE,
        symbol="NDX100",
        settings=settings,
    )
    assert len(simulated) > 0, (
        "expected at least one simulated setup on the test date; "
        "fixture or detector behaviour has drifted"
    )

    for original in simulated:
        T = original.mss.mss_confirm_candle_time_utc
        tick = _next_5min_tick_after(T)
        truncated = {
            tf: df.loc[df["time"] <= T].reset_index(drop=True)
            for tf, df in window.items()
        }
        re_setups = build_setup_candidates(
            df_h4=truncated["H4"],
            df_h1=truncated["H1"],
            df_m5=truncated["M5"],
            df_d1=truncated["D1"],
            target_date=_TARGET_DATE,
            symbol="NDX100",
            settings=settings,
            now_utc=tick,
        )

        original_id = _identity(original)
        match = next((s for s in re_setups if _identity(s) == original_id), None)
        assert match is not None, (
            f"simulator emitted {original_id} but the truncated re-run found no "
            f"matching setup. re-run identities: "
            f"{[_identity(s) for s in re_setups]}"
        )

        sig_a = _setup_signature(original)
        sig_b = _setup_signature(match)
        assert sig_a == sig_b, (
            f"simulator vs truncated re-run signature mismatch for {original_id}.\n"
            f"simulator: {sig_a}\nre-run:    {sig_b}"
        )


def test_simulator_first_emission_is_locked():
    """A setup that surfaces at multiple ticks (e.g. with a stronger POI
    once more candles close) must be emitted with the **first-tick**
    fields. This is the production-parity invariant: production locks
    the setup at the moment of notification.

    We exercise this by re-running the simulator with a tick interval
    of 5 min (the production cadence) and an interval of 1 min, then
    asserting that for every identity present in both, the 5-min
    emission's downstream fields match the 1-min emission's.
    """
    settings = _settings()
    window = _load_window("NDX100", _TARGET_DATE)

    base = simulate_target_date(
        df_h4=window["H4"],
        df_h1=window["H1"],
        df_m5=window["M5"],
        df_d1=window["D1"],
        target_date=_TARGET_DATE,
        symbol="NDX100",
        settings=settings,
        tick_interval_minutes=5,
    )
    fine = simulate_target_date(
        df_h4=window["H4"],
        df_h1=window["H1"],
        df_m5=window["M5"],
        df_d1=window["D1"],
        target_date=_TARGET_DATE,
        symbol="NDX100",
        settings=settings,
        tick_interval_minutes=1,
    )

    by_id_base = {_identity(s): s for s in base}
    by_id_fine = {_identity(s): s for s in fine}

    common = set(by_id_base) & set(by_id_fine)
    assert common, "test fixture must produce at least one common setup"

    for identity in common:
        sig_base = _setup_signature(by_id_base[identity])
        sig_fine = _setup_signature(by_id_fine[identity])
        assert sig_base == sig_fine, (
            f"first-emission lock broken for {identity}: 5-min and 1-min "
            f"simulators disagree.\n5min: {sig_base}\n1min: {sig_fine}"
        )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
