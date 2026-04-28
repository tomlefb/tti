"""Unit tests for ``src.detection.setup``.

The orchestrator is mostly glue. We mock the heavy upstream detectors via
hand-crafted DataFrames where possible; for the OTE / TP selection logic
we test the helpers directly to avoid building 200-candle fixtures.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from src.detection.fvg import FVG
from src.detection.liquidity import MarkedLevel
from src.detection.order_block import OrderBlock
from src.detection.setup import (
    Setup,
    _compute_tp1,
    _ote_overlaps_poi,
    _select_take_profit,
    build_setup_candidates,
)
from src.detection.sweep import Sweep


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        SESSION_ASIA=(2, 0, 6, 0),
        KILLZONE_LONDON=(9, 0, 12, 0),
        KILLZONE_NY=(15, 30, 18, 0),
        SWING_LOOKBACK_H4=2,
        SWING_LOOKBACK_H1=2,
        SWING_LOOKBACK_M5=2,
        MIN_SWING_AMPLITUDE_ATR_MULT_H4=1.0,
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
        INSTRUMENT_CONFIG={
            "TEST": {"sweep_buffer": 0.5, "equal_hl_tolerance": 0.5, "sl_buffer": 0.5},
        },
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _stub_sweep(extreme: float, level_price: float, direction: str) -> Sweep:
    return Sweep(
        direction=direction,  # type: ignore[arg-type]
        swept_level_price=level_price,
        swept_level_type="asian_low" if direction == "bullish" else "asian_high",
        swept_level_strength="structural",
        sweep_candle_time_utc=datetime(2025, 7, 14, 9, 0, tzinfo=UTC),
        sweep_extreme_price=extreme,
        return_candle_time_utc=datetime(2025, 7, 14, 9, 0, tzinfo=UTC),
        excursion=abs(level_price - extreme),
    )


def _stub_fvg(direction: str, proximal: float, distal: float) -> FVG:
    t = datetime(2025, 7, 14, 9, 0, tzinfo=UTC)
    return FVG(
        direction=direction,  # type: ignore[arg-type]
        proximal=proximal,
        distal=distal,
        c1_time_utc=t,
        c2_time_utc=t,
        c3_time_utc=t,
        size=abs(proximal - distal),
        size_atr_ratio=1.0,
    )


def _stub_ob(direction: str, proximal: float, distal: float) -> OrderBlock:
    return OrderBlock(
        direction=direction,  # type: ignore[arg-type]
        proximal=proximal,
        distal=distal,
        candle_time_utc=datetime(2025, 7, 14, 9, 0, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# _select_take_profit
# ---------------------------------------------------------------------------


def test_tp_selects_nearest_level_meeting_min_rr() -> None:
    sweep = _stub_sweep(extreme=99.0, level_price=99.5, direction="bullish")
    levels = [
        MarkedLevel(price=99.5, type="low", label="asian_low", strength="structural"),
        MarkedLevel(price=102.0, type="high", label="A", strength="structural"),  # RR 0.5
        MarkedLevel(price=107.5, type="high", label="B", strength="major"),  # RR 3.25 ← winner
        MarkedLevel(price=115.0, type="high", label="C", strength="structural"),  # RR 7.0
    ]
    # Entry 101.0, SL 99.0 ⇒ risk = 2.
    out = _select_take_profit(
        direction="long",
        entry=101.0,
        risk=2.0,
        levels=levels,
        sweep=sweep,
        min_rr=3.0,
    )
    assert out is not None
    tp, label, rr = out
    assert label == "B"
    assert rr == pytest.approx(3.25)
    assert tp == pytest.approx(107.5)


def test_tp_returns_none_when_no_level_meets_min_rr() -> None:
    sweep = _stub_sweep(99.0, 99.5, "bullish")
    levels = [MarkedLevel(price=101.5, type="high", label="A", strength="major")]  # RR 0.25
    out = _select_take_profit(
        direction="long",
        entry=101.0,
        risk=2.0,
        levels=levels,
        sweep=sweep,
        min_rr=3.0,
    )
    assert out is None


def test_tp_excludes_level_we_just_swept() -> None:
    """The level we swept is not eligible as TP; the next-furthest one is."""
    # Short setup: entry 99.0, risk 0.5, sweep of high 100.
    sweep = _stub_sweep(100.5, 100.0, "bearish")
    levels = [
        MarkedLevel(price=100.0, type="high", label="asian_high", strength="structural"),  # swept
        MarkedLevel(price=97.0, type="low", label="pdl", strength="structural"),  # RR 4.0
    ]
    out = _select_take_profit(
        direction="short",
        entry=99.0,
        risk=0.5,
        levels=levels,
        sweep=sweep,
        min_rr=3.0,
    )
    assert out is not None
    _, label, _ = out
    assert label == "pdl"


# ---------------------------------------------------------------------------
# _ote_overlaps_poi
# ---------------------------------------------------------------------------


def test_ote_overlap_bullish() -> None:
    # Leg from 100 (sweep low) to 110 (broken swing high). Range 10.
    # OTE zone = [110 - 0.79*10, 110 - 0.62*10] = [102.1, 103.8].
    # POI [102.5, 103.0] ⇒ overlaps.
    poi = _stub_fvg("bullish", proximal=103.0, distal=102.5)
    assert _ote_overlaps_poi(poi=poi, sweep_extreme=100.0, broken_swing=110.0) is True


def test_ote_no_overlap_bullish() -> None:
    # POI well above OTE zone.
    poi = _stub_fvg("bullish", proximal=109.0, distal=108.5)
    assert _ote_overlaps_poi(poi=poi, sweep_extreme=100.0, broken_swing=110.0) is False


def test_ote_overlap_bearish() -> None:
    # Leg from 110 (sweep high) to 100 (broken swing low). Range 10.
    # OTE zone = [100 + 0.62*10, 100 + 0.79*10] = [106.2, 107.9].
    poi = _stub_ob("bearish", proximal=107.0, distal=107.5)
    assert _ote_overlaps_poi(poi=poi, sweep_extreme=110.0, broken_swing=100.0) is True


def test_ote_zero_leg_returns_false() -> None:
    poi = _stub_fvg("bullish", 102.0, 101.0)
    assert _ote_overlaps_poi(poi=poi, sweep_extreme=100.0, broken_swing=100.0) is False


# ---------------------------------------------------------------------------
# build_setup_candidates — high-level smoke / no-bias short-circuit
# ---------------------------------------------------------------------------


def _empty_df_for_tf(timeframe: str) -> pd.DataFrame:
    return pd.DataFrame({"time": [], "open": [], "high": [], "low": [], "close": []})


def test_orchestrator_no_bias_returns_empty() -> None:
    """With empty H4/H1 the bias is 'no_trade' ⇒ orchestrator returns []."""
    out = build_setup_candidates(
        df_h4=_empty_df_for_tf("H4"),
        df_h1=_empty_df_for_tf("H1"),
        df_m5=_empty_df_for_tf("M5"),
        df_d1=_empty_df_for_tf("D1"),
        target_date=date(2025, 7, 14),
        symbol="TEST",
        settings=_settings(),
    )
    assert out == []


# ---------------------------------------------------------------------------
# Killzone gating (Sprint 4)
# ---------------------------------------------------------------------------


def _make_setup(timestamp_utc: datetime) -> Setup:
    """Minimal Setup with a configurable ``timestamp_utc``. Other fields are
    placeholders — the gating filter only inspects ``timestamp_utc`` and
    ``killzone``."""
    sweep = _stub_sweep(99.0, 99.5, "bullish")
    fvg = _stub_fvg("bullish", 102.0, 101.0)
    from src.detection.mss import MSS

    mss = MSS(
        direction="bullish",
        sweep=sweep,
        broken_swing_time_utc=timestamp_utc,
        broken_swing_price=110.0,
        mss_confirm_candle_time_utc=timestamp_utc,
        mss_confirm_candle_close=110.5,
        displacement_body_ratio=2.0,
        displacement_candle_time_utc=timestamp_utc,
    )
    return Setup(
        timestamp_utc=timestamp_utc,
        symbol="TEST",
        direction="long",
        daily_bias="bullish",
        killzone="ny",
        swept_level_price=99.5,
        swept_level_type="asian_low",
        swept_level_strength="structural",
        sweep=sweep,
        mss=mss,
        poi=fvg,
        poi_type="FVG",
        entry_price=102.0,
        stop_loss=99.0,
        target_level_type="asian_high",
        tp_runner_price=120.0,
        tp_runner_rr=6.0,
        tp1_price=117.0,
        tp1_rr=5.0,
        quality="A",
        confluences=[],
    )


def test_killzone_gating_drops_setups_with_late_mss_confirm(monkeypatch) -> None:
    """A setup whose MSS confirms after the killzone end is filtered out.

    Per docs/01 §6, notifications must not fire outside London/NY killzones
    even if the detection pipeline produces them (the MSS lookforward window
    extends ~120 minutes past the killzone end).
    """
    import src.detection.setup as setup_module

    settings = _settings()

    # Force daily bias = bullish so the orchestrator enters the killzone
    # loop. We don't need real H4/H1 data — bypass the bias detector.
    monkeypatch.setattr(setup_module, "compute_daily_bias", lambda **_: "bullish")
    # Bypass liquidity marking — the dummy levels list is unused by our stub.
    monkeypatch.setattr(
        setup_module,
        "_build_marked_levels",
        lambda **_: (None, None, [], [], []),
    )
    # Bypass sweep detection — emit a single dummy sweep so the inner loop
    # executes once per killzone.
    monkeypatch.setattr(
        setup_module, "detect_sweeps", lambda *a, **kw: [_stub_sweep(99.0, 99.5, "bullish")]
    )

    # NY killzone for 2025-07-14 in summer time:
    #   Paris 15:30–18:00 → UTC 13:30–16:00.
    # Build one setup at exactly kz_end (kept) and one one minute after (dropped).
    target_date = date(2025, 7, 14)
    kz_end_utc = datetime(2025, 7, 14, 16, 0, tzinfo=UTC)

    # London killzone returns no setup, NY returns the late-confirm one.
    calls = {"n": 0}

    def fake_try_build_setup(*, killzone, **_):
        calls["n"] += 1
        if killzone == "london":
            return None
        # NY: produce a setup 1 minute past kz end → must be dropped.
        return _make_setup(kz_end_utc + pd.Timedelta(minutes=1).to_pytimedelta())

    monkeypatch.setattr(setup_module, "_try_build_setup", fake_try_build_setup)

    out = build_setup_candidates(
        df_h4=_empty_df_for_tf("H4"),
        df_h1=_empty_df_for_tf("H1"),
        df_m5=_empty_df_for_tf("M5"),
        df_d1=_empty_df_for_tf("D1"),
        target_date=target_date,
        symbol="TEST",
        settings=settings,
    )
    assert out == []

    # Now retest with timestamp == kz_end_utc — must be kept.
    monkeypatch.setattr(
        setup_module,
        "_try_build_setup",
        lambda *, killzone, **_: _make_setup(kz_end_utc) if killzone == "ny" else None,
    )
    out = build_setup_candidates(
        df_h4=_empty_df_for_tf("H4"),
        df_h1=_empty_df_for_tf("H1"),
        df_m5=_empty_df_for_tf("M5"),
        df_d1=_empty_df_for_tf("D1"),
        target_date=target_date,
        symbol="TEST",
        settings=settings,
    )
    assert len(out) == 1
    assert out[0].timestamp_utc == kz_end_utc


# ---------------------------------------------------------------------------
# _compute_tp1 — partial-exit cap
# ---------------------------------------------------------------------------


def test_high_rr_runner_capped_tp1() -> None:
    """RR_runner=12 ⇒ TP1 capped at 5R, runner unchanged."""
    tp1_price, tp1_rr = _compute_tp1(
        direction="long",
        entry=100.0,
        risk=2.0,  # ⇒ runner reward = 12 × 2 = 24, runner_price = 124
        tp_runner_price=124.0,
        tp_runner_rr=12.0,
        partial_target=5.0,
    )
    assert tp1_rr == pytest.approx(5.0)
    assert tp1_price == pytest.approx(110.0)  # entry + 5 × risk
    assert tp1_price != pytest.approx(124.0)


def test_high_rr_runner_capped_tp1_short() -> None:
    """Symmetric for short setups."""
    tp1_price, tp1_rr = _compute_tp1(
        direction="short",
        entry=100.0,
        risk=2.0,
        tp_runner_price=76.0,  # 12R below entry
        tp_runner_rr=12.0,
        partial_target=5.0,
    )
    assert tp1_rr == pytest.approx(5.0)
    assert tp1_price == pytest.approx(90.0)  # entry - 5 × risk


def test_low_rr_no_capping() -> None:
    """RR_runner=3.5 ⇒ TP1 collapses onto runner (no partial benefit)."""
    tp1_price, tp1_rr = _compute_tp1(
        direction="long",
        entry=100.0,
        risk=2.0,
        tp_runner_price=107.0,  # 3.5R
        tp_runner_rr=3.5,
        partial_target=5.0,
    )
    assert tp1_rr == pytest.approx(3.5)
    assert tp1_price == pytest.approx(107.0)


def test_setup_backwards_compat_aliases() -> None:
    """``take_profit`` and ``risk_reward`` properties alias the runner."""
    from src.detection.fvg import FVG
    from src.detection.mss import MSS

    t = datetime(2025, 7, 14, 9, 0, tzinfo=UTC)
    sweep = _stub_sweep(99.0, 99.5, "bullish")
    fvg = _stub_fvg("bullish", 102.0, 101.0)
    mss = MSS(
        direction="bullish",
        sweep=sweep,
        broken_swing_time_utc=t,
        broken_swing_price=110.0,
        mss_confirm_candle_time_utc=t,
        mss_confirm_candle_close=110.5,
        displacement_body_ratio=2.0,
        displacement_candle_time_utc=t,
    )
    setup = Setup(
        timestamp_utc=t,
        symbol="TEST",
        direction="long",
        daily_bias="bullish",
        killzone="ny",
        swept_level_price=99.5,
        swept_level_type="asian_low",
        swept_level_strength="structural",
        sweep=sweep,
        mss=mss,
        poi=fvg,
        poi_type="FVG",
        entry_price=102.0,
        stop_loss=99.0,
        target_level_type="asian_high",
        tp_runner_price=120.0,
        tp_runner_rr=6.0,
        tp1_price=117.0,
        tp1_rr=5.0,
        quality="A",
        confluences=[],
    )
    assert isinstance(setup, FVG.__class__) is False  # silence unused import
    assert setup.take_profit == 120.0
    assert setup.risk_reward == 6.0
    assert setup.tp1_price == 117.0
    assert setup.tp1_rr == 5.0
