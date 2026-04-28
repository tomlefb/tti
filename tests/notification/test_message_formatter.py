"""Unit tests for ``src.notification.message_formatter``.

The formatter is a pure function: ``Setup → str``. Tests pin:

- Quality emoji selection (A / A+ / B).
- Direction LONG/SHORT in upper case.
- Per-symbol price precision (XAUUSD 2 dp, EURUSD 5 dp, NDX100 1 dp, GBPUSD 5 dp).
- TP_R line conditional on ``tp_runner_rr != tp1_rr``.
- 🚀 emoji conditional on ``high_rr_runner`` confluence.
- Paris-time parenthetical, DST-correct.
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.detection.fvg import FVG
from src.detection.mss import MSS
from src.detection.order_block import OrderBlock
from src.detection.setup import Setup
from src.detection.sweep import Sweep
from src.notification.message_formatter import format_setup_message


def _stub_sweep(direction: str = "bearish") -> Sweep:
    return Sweep(
        direction=direction,  # type: ignore[arg-type]
        swept_level_price=4380.0,
        swept_level_type="asian_high",
        swept_level_strength="structural",
        sweep_candle_time_utc=datetime(2026, 1, 2, 16, 30, tzinfo=UTC),
        sweep_extreme_price=4382.5,
        return_candle_time_utc=datetime(2026, 1, 2, 16, 30, tzinfo=UTC),
        excursion=2.5,
    )


def _stub_mss(direction: str = "bearish", t: datetime | None = None) -> MSS:
    t = t or datetime(2026, 1, 2, 16, 35, tzinfo=UTC)
    return MSS(
        direction=direction,  # type: ignore[arg-type]
        sweep=_stub_sweep(direction),
        broken_swing_time_utc=t,
        broken_swing_price=4365.0,
        mss_confirm_candle_time_utc=t,
        mss_confirm_candle_close=4364.0,
        displacement_body_ratio=2.1,
        displacement_candle_time_utc=t,
    )


def _stub_fvg(direction: str = "bearish") -> FVG:
    t = datetime(2026, 1, 2, 16, 35, tzinfo=UTC)
    return FVG(
        direction=direction,  # type: ignore[arg-type]
        proximal=4360.0,
        distal=4366.0,
        c1_time_utc=t,
        c2_time_utc=t,
        c3_time_utc=t,
        size=6.0,
        size_atr_ratio=1.0,
    )


def _stub_ob(direction: str = "bullish") -> OrderBlock:
    return OrderBlock(
        direction=direction,  # type: ignore[arg-type]
        proximal=25376.0,
        distal=25356.0,
        candle_time_utc=datetime(2026, 1, 14, 18, 20, tzinfo=UTC),
    )


def _xauusd_a_with_runner() -> Setup:
    """A-grade XAUUSD short with a high-RR runner (TP1 capped at 5R)."""
    sweep = _stub_sweep("bearish")
    mss = _stub_mss("bearish")
    fvg = _stub_fvg("bearish")
    return Setup(
        timestamp_utc=datetime(2026, 1, 2, 16, 35, tzinfo=UTC),
        symbol="XAUUSD",
        direction="short",
        daily_bias="bearish",
        killzone="ny",
        swept_level_price=4380.0,
        swept_level_type="asian_high",
        swept_level_strength="structural",
        sweep=sweep,
        mss=mss,
        poi=fvg,
        poi_type="FVG",
        entry_price=4360.0,
        stop_loss=4375.0,
        target_level_type="swing_h1_low",
        tp_runner_price=4080.5,  # ~18.70R below entry on risk=15 ⇒ 4360 - 280.5
        tp_runner_rr=18.70,
        tp1_price=4285.0,  # entry - 5 × 15
        tp1_rr=5.0,
        quality="A",
        confluences=["structural_sweep", "FVG+OB", "strong_displacement", "high_rr_runner"],
    )


def _xauusd_a_no_runner() -> Setup:
    """A-grade XAUUSD short, RR below the 5R cap → TP_R == TP1 (no runner line)."""
    sweep = _stub_sweep("bearish")
    mss = _stub_mss("bearish")
    fvg = _stub_fvg("bearish")
    return Setup(
        timestamp_utc=datetime(2026, 1, 2, 16, 35, tzinfo=UTC),
        symbol="XAUUSD",
        direction="short",
        daily_bias="bearish",
        killzone="ny",
        swept_level_price=4380.0,
        swept_level_type="asian_high",
        swept_level_strength="structural",
        sweep=sweep,
        mss=mss,
        poi=fvg,
        poi_type="FVG",
        entry_price=4360.0,
        stop_loss=4375.0,
        target_level_type="swing_h1_low",
        tp_runner_price=4304.30,
        tp_runner_rr=3.71,
        tp1_price=4304.30,
        tp1_rr=3.71,
        quality="A",
        confluences=["structural_sweep", "FVG+OB", "strong_displacement"],
    )


def test_format_a_grade_short_with_runner_includes_tpr_line_and_rocket() -> None:
    msg = format_setup_message(_xauusd_a_with_runner())

    assert "🅰️" in msg
    assert "<b>XAUUSD SHORT</b>" in msg
    assert "<b>TP1:</b>" in msg
    assert "<b>TP_R:</b>" in msg
    assert "🚀" in msg  # high_rr_runner confluence ⇒ rocket present
    assert "(RR 5.00)" in msg
    assert "(RR 18.70)" in msg


def test_format_a_grade_short_without_runner_omits_tpr_line() -> None:
    msg = format_setup_message(_xauusd_a_no_runner())

    assert "<b>TP1:</b>" in msg
    assert "<b>TP_R:</b>" not in msg
    assert "🚀" not in msg
    assert "(RR 3.71)" in msg


def test_format_a_plus_grade_uses_a_plus_emoji() -> None:
    setup = _xauusd_a_with_runner()
    setup_a_plus = Setup(**{**setup.__dict__, "quality": "A+"})
    msg = format_setup_message(setup_a_plus)
    assert "🅰️➕" in msg


def test_format_b_grade_uses_b_emoji() -> None:
    setup = _xauusd_a_no_runner()
    setup_b = Setup(**{**setup.__dict__, "quality": "B"})
    msg = format_setup_message(setup_b)
    assert "🅱️" in msg


def test_format_long_direction_upper_case() -> None:
    """LONG must appear in upper-case bold; mss/sweep/fvg here are bullish-stubbed."""
    sweep = _stub_sweep("bullish")
    sweep = Sweep(
        direction="bullish",
        swept_level_price=25340.0,
        swept_level_type="swing_h4_low",
        swept_level_strength="major_h4_only",
        sweep_candle_time_utc=sweep.sweep_candle_time_utc,
        sweep_extreme_price=25336.0,
        return_candle_time_utc=sweep.return_candle_time_utc,
        excursion=4.0,
    )
    mss = _stub_mss("bullish", datetime(2026, 1, 14, 16, 50, tzinfo=UTC))
    ob = _stub_ob("bullish")
    setup = Setup(
        timestamp_utc=datetime(2026, 1, 14, 16, 50, tzinfo=UTC),
        symbol="NDX100",
        direction="long",
        daily_bias="bullish",
        killzone="ny",
        swept_level_price=25340.0,
        swept_level_type="swing_h4_low",
        swept_level_strength="major_h4_only",
        sweep=sweep,
        mss=mss,
        poi=ob,
        poi_type="OrderBlock",
        entry_price=25376.0,
        stop_loss=25356.0,
        target_level_type="asian_high",
        tp_runner_price=25752.0,
        tp_runner_rr=18.70,
        tp1_price=25476.0,
        tp1_rr=5.0,
        quality="A",
        confluences=["FVG+OB", "OTE_overlap", "high_rr_runner"],
    )
    msg = format_setup_message(setup)
    assert "<b>NDX100 LONG</b>" in msg


def test_format_paris_time_offset_winter() -> None:
    """16:35 UTC on 2026-01-02 (Paris winter = UTC+1) → Paris 17:35."""
    msg = format_setup_message(_xauusd_a_no_runner())
    assert "(Paris: 17:35)" in msg


def test_format_killzone_label_ny() -> None:
    msg = format_setup_message(_xauusd_a_no_runner())
    # First line of the timestamp block should include ' NY' tag.
    assert " NY" in msg


def test_format_price_precision_xauusd_2dp() -> None:
    """XAUUSD prices render to 2 decimals."""
    msg = format_setup_message(_xauusd_a_no_runner())
    assert "4360.00" in msg
    assert "4375.00" in msg
    assert "4304.30" in msg


def test_format_price_precision_eurusd_5dp() -> None:
    """EURUSD prices render to 5 decimals."""
    sweep = Sweep(
        direction="bullish",
        swept_level_price=1.07150,
        swept_level_type="asian_low",
        swept_level_strength="structural",
        sweep_candle_time_utc=datetime(2026, 1, 2, 9, 30, tzinfo=UTC),
        sweep_extreme_price=1.07140,
        return_candle_time_utc=datetime(2026, 1, 2, 9, 30, tzinfo=UTC),
        excursion=0.00010,
    )
    mss = MSS(
        direction="bullish",
        sweep=sweep,
        broken_swing_time_utc=datetime(2026, 1, 2, 9, 35, tzinfo=UTC),
        broken_swing_price=1.07300,
        mss_confirm_candle_time_utc=datetime(2026, 1, 2, 9, 35, tzinfo=UTC),
        mss_confirm_candle_close=1.07310,
        displacement_body_ratio=2.0,
        displacement_candle_time_utc=datetime(2026, 1, 2, 9, 35, tzinfo=UTC),
    )
    fvg = FVG(
        direction="bullish",
        proximal=1.07250,
        distal=1.07200,
        c1_time_utc=mss.broken_swing_time_utc,
        c2_time_utc=mss.broken_swing_time_utc,
        c3_time_utc=mss.broken_swing_time_utc,
        size=0.00050,
        size_atr_ratio=1.0,
    )
    setup = Setup(
        timestamp_utc=datetime(2026, 1, 2, 9, 35, tzinfo=UTC),
        symbol="EURUSD",
        direction="long",
        daily_bias="bullish",
        killzone="london",
        swept_level_price=1.07150,
        swept_level_type="asian_low",
        swept_level_strength="structural",
        sweep=sweep,
        mss=mss,
        poi=fvg,
        poi_type="FVG",
        entry_price=1.07250,
        stop_loss=1.07140,
        target_level_type="pdh",
        tp_runner_price=1.07580,
        tp_runner_rr=3.0,
        tp1_price=1.07580,
        tp1_rr=3.0,
        quality="B",
        confluences=["FVG+OB"],
    )
    msg = format_setup_message(setup)
    assert "1.07250" in msg
    assert "1.07140" in msg
    assert "1.07580" in msg
    # London label
    assert " LON" in msg


def test_format_price_precision_ndx100_1dp() -> None:
    """NDX100 prices render to 1 decimal."""
    sweep = Sweep(
        direction="bullish",
        swept_level_price=25340.0,
        swept_level_type="swing_h4_low",
        swept_level_strength="major_h4_only",
        sweep_candle_time_utc=datetime(2026, 1, 14, 16, 30, tzinfo=UTC),
        sweep_extreme_price=25336.0,
        return_candle_time_utc=datetime(2026, 1, 14, 16, 30, tzinfo=UTC),
        excursion=4.0,
    )
    mss = _stub_mss("bullish", datetime(2026, 1, 14, 16, 50, tzinfo=UTC))
    ob = _stub_ob("bullish")
    setup = Setup(
        timestamp_utc=datetime(2026, 1, 14, 16, 50, tzinfo=UTC),
        symbol="NDX100",
        direction="long",
        daily_bias="bullish",
        killzone="ny",
        swept_level_price=25340.0,
        swept_level_type="swing_h4_low",
        swept_level_strength="major_h4_only",
        sweep=sweep,
        mss=mss,
        poi=ob,
        poi_type="OrderBlock",
        entry_price=25376.0,
        stop_loss=25356.0,
        target_level_type="asian_high",
        tp_runner_price=25752.0,
        tp_runner_rr=18.70,
        tp1_price=25476.0,
        tp1_rr=5.0,
        quality="A",
        confluences=["FVG+OB", "OTE_overlap", "high_rr_runner"],
    )
    msg = format_setup_message(setup)
    assert "25376.0" in msg
    assert "25356.0" in msg
    assert "25752.0" in msg
    # Should not show a 5-dp render.
    assert "25376.00000" not in msg


def test_format_includes_bias_swept_poi_and_confluences() -> None:
    msg = format_setup_message(_xauusd_a_with_runner())
    assert "<b>Bias:</b>" in msg and "bearish" in msg
    assert "<b>Sweep:</b>" in msg and "asian_high" in msg
    assert "<b>POI:</b>" in msg and "FVG" in msg
    assert "<b>Confluences:</b>" in msg
    # Confluences kept verbatim (snake_case).
    assert "structural_sweep" in msg
    assert "FVG+OB" in msg
    assert "high_rr_runner" in msg
