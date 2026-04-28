"""Unit tests for ``src.detection.grading``."""

from __future__ import annotations

from datetime import UTC, datetime

from src.detection.fvg import FVG
from src.detection.grading import SetupComponents, grade_setup
from src.detection.order_block import OrderBlock


def _fvg(size_atr_ratio: float = 1.0) -> FVG:
    t = datetime(2025, 7, 14, 9, 0, tzinfo=UTC)
    return FVG(
        direction="bullish",
        proximal=102.0,
        distal=101.0,
        c1_time_utc=t,
        c2_time_utc=t,
        c3_time_utc=t,
        size=1.0,
        size_atr_ratio=size_atr_ratio,
    )


def _ob() -> OrderBlock:
    return OrderBlock(
        direction="bullish",
        proximal=102.0,
        distal=101.0,
        candle_time_utc=datetime(2025, 7, 14, 9, 0, tzinfo=UTC),
    )


def _components(**overrides):
    base = dict(
        swept_level_strength="structural",
        poi=_fvg(),
        poi_type="FVG",
        risk_reward=4.5,
        displacement_body_ratio=2.0,
        ote_overlap=True,
        has_alternative_ob_when_fvg=True,
        fvg_min_size_atr_multiplier=0.3,
        mss_displacement_multiplier=1.5,
        min_rr=3.0,
        a_plus_rr_threshold=4.0,
    )
    base.update(overrides)
    return SetupComponents(**base)


def test_grade_a_plus() -> None:
    grade, conf = grade_setup(_components())
    assert grade == "A+"
    assert "structural_sweep" in conf
    assert "OTE_overlap" in conf
    assert "FVG+OB" in conf
    assert "strong_displacement" in conf  # ratio 2.0


def test_grade_a_when_no_ote() -> None:
    grade, _ = grade_setup(_components(ote_overlap=False))
    assert grade == "A"


def test_grade_a_with_h4_only() -> None:
    grade, _ = grade_setup(_components(swept_level_strength="major_h4_only", ote_overlap=False))
    assert grade == "A"


def test_grade_b_when_poi_is_ob() -> None:
    grade, _ = grade_setup(
        _components(
            poi=_ob(), poi_type="OrderBlock", ote_overlap=False, has_alternative_ob_when_fvg=False
        )
    )
    assert grade == "B"


def test_grade_b_when_displacement_weak() -> None:
    """A-grade only requires (structural/major/major_h4_only) + FVG + RR.
    Weak displacement blocks A+ only. Force B by using 'minor' so A is
    also blocked, then weak displacement becomes the qualifying weakness."""
    grade, _ = grade_setup(
        _components(
            swept_level_strength="minor",
            displacement_body_ratio=1.2,  # in [1.0, 1.5) ⇒ weak
            ote_overlap=False,
        )
    )
    assert grade == "B"


def test_grade_reject_when_below_min_rr() -> None:
    grade, _ = grade_setup(_components(risk_reward=2.5))
    assert grade is None


def test_grade_reject_when_minor_with_no_weakness_path() -> None:
    """Minor sweep with FVG passes B (no weaknesses bar 'minor')... actually
    the grader's count_b_weaknesses doesn't penalise 'minor' specifically.
    Verify behaviour: minor + RR>=MIN_RR + clean FVG + strong disp + no OTE
    must collapse to None because no weakness is registered."""
    grade, _ = grade_setup(
        _components(
            swept_level_strength="minor",
            ote_overlap=False,
            risk_reward=4.5,  # well above MIN_RR
            displacement_body_ratio=2.0,
            poi=_fvg(size_atr_ratio=1.0),  # not 'small' (small = [0.3, 0.45))
        )
    )
    # Not A (strength minor); not A+ (strength minor); B requires a weakness:
    # - poi is FVG with ratio 1.0 ⇒ NOT in [0.3, 0.45) ⇒ not small
    # - displacement_body_ratio 2.0 >= 1.5 ⇒ not weak
    # - rr 4.5 ⇒ not at MIN_RR
    # - poi_type FVG ⇒ no OB-weakness
    # ⇒ no weaknesses ⇒ reject.
    assert grade is None


def test_grade_b_with_small_fvg() -> None:
    grade, _ = grade_setup(
        _components(
            swept_level_strength="minor",
            poi=_fvg(size_atr_ratio=0.35),  # in [0.3, 0.45) ⇒ small
            ote_overlap=False,
        )
    )
    assert grade == "B"


def test_grade_b_with_rr_at_min() -> None:
    grade, _ = grade_setup(
        _components(
            swept_level_strength="minor",
            risk_reward=3.0,
            ote_overlap=False,
        )
    )
    assert grade == "B"
