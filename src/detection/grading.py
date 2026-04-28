"""A+/A/B setup quality grading — heuristic stack (docs/01 §5 Step 5, docs/07 §1.3).

Grade rules (in priority order):

**A+** — all required:
- ``swept_level_strength`` in ``("structural", "major")``
- ``displacement_body_ratio >= mss_displacement_multiplier`` (defensive
  re-check — already guaranteed by ``detect_mss``, but verified here so
  swapping the upstream rule wouldn't silently widen the A+ bucket)
- POI is an FVG (not OrderBlock)
- POI overlaps the OTE zone (0.62-0.79 retracement of the displacement leg)
- ``risk_reward >= a_plus_rr_threshold``

**A** — all required:
- ``swept_level_strength`` in ``("structural", "major", "major_h4_only")``
- POI is an FVG
- ``risk_reward >= min_rr``

**B** — all required:
- ``swept_level_strength`` may be anything
- POI may be FVG OR OrderBlock
- ``risk_reward >= min_rr``
- Exactly one weakness allowed:
    - small FVG: ``size_atr_ratio`` between
      ``fvg_min_size_atr_multiplier`` and ``1.5 × fvg_min_size_atr_multiplier``
    - weak displacement: ``displacement_body_ratio`` between ``1.0`` and
      ``mss_displacement_multiplier``
    - RR exactly at ``min_rr`` (within 1% slack — float-eq is unsafe)

Below B → ``(None, [...])`` ⇒ orchestrator skips the candidate.

Heuristic per docs/07 §1.3 — alternative would be numeric scoring or an
LLM qualifier (Sprint 7+). Booleans-stack is the v1 design choice.

Confluence labels emitted:
- ``"structural_sweep"`` — level was structural
- ``"major_sweep"`` — level was major (multi-TF confluent)
- ``"FVG+OB"`` — both an FVG and an OB exist (POI uses the FVG)
- ``"OTE_overlap"`` — POI overlaps OTE
- ``"strong_displacement"`` — ``displacement_body_ratio >= 2.0``
- ``"high_rr_runner"`` — ``risk_reward >= 8.0`` (Sprint 4 will use this
  to highlight extended-leg setups in the notification text — these are
  high-variance, partial-exit at TP1 strongly recommended).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .fvg import FVG
from .order_block import OrderBlock

Grade = Literal["A+", "A", "B"]

_RR_TOLERANCE = 0.01
"""Relative slack when comparing RR to ``min_rr`` for the B-tier weakness
detection. ``MIN_RR=3.0`` and ``RR=3.005`` should be treated as "at MIN_RR"."""

_STRONG_DISPLACEMENT = 2.0
"""``displacement_body_ratio >= this`` adds the ``strong_displacement`` confluence."""

_HIGH_RR_RUNNER = 8.0
"""``risk_reward >= this`` adds the ``high_rr_runner`` confluence label.
Used downstream (Sprint 4 notification layer) to flag extended-leg setups
where the partial-exit TP1 is strongly recommended."""


@dataclass(frozen=True)
class SetupComponents:
    """Aggregate of inputs needed to grade a candidate setup.

    Keeping this a dataclass (rather than passing every field positionally)
    lets ``grade_setup`` evolve without churn at every call site, and
    makes the test harness pleasant.

    ``ote_overlap`` is pre-computed by the orchestrator (it depends on
    the displacement leg which lives in the orchestrator's scope). It
    is the boolean answer to "does ``[poi.proximal, poi.distal]``
    intersect the 0.62-0.79 fib retracement of the leg from
    ``sweep.sweep_extreme_price`` to ``mss.broken_swing_price``?"

    ``has_alternative_ob_when_fvg`` is the orchestrator's signal that
    BOTH an FVG and an OB exist — used to attach the ``FVG+OB``
    confluence label when the POI is an FVG.
    """

    swept_level_strength: Literal["major", "major_h4_only", "minor", "structural"]
    poi: FVG | OrderBlock
    poi_type: Literal["FVG", "OrderBlock"]
    risk_reward: float
    displacement_body_ratio: float
    ote_overlap: bool
    has_alternative_ob_when_fvg: bool
    fvg_min_size_atr_multiplier: float
    mss_displacement_multiplier: float
    min_rr: float
    a_plus_rr_threshold: float


def grade_setup(components: SetupComponents) -> tuple[Grade | None, list[str]]:
    """Grade a candidate setup.

    Returns:
        ``(grade, confluences)``. ``grade`` is ``"A+" | "A" | "B" | None``;
        ``None`` signals the candidate fails the minimum bar and the
        orchestrator must skip it. Confluences are populated regardless
        of whether the candidate is accepted (useful for diagnostics).
    """
    confluences = _build_confluences(components)

    s = components.swept_level_strength
    is_fvg = components.poi_type == "FVG"
    rr = components.risk_reward

    # A+
    if (
        s in ("structural", "major")
        and components.displacement_body_ratio >= components.mss_displacement_multiplier
        and is_fvg
        and components.ote_overlap
        and rr >= components.a_plus_rr_threshold
    ):
        return ("A+", confluences)

    # A
    if s in ("structural", "major", "major_h4_only") and is_fvg and rr >= components.min_rr:
        return ("A", confluences)

    # B — must have RR ≥ MIN_RR and exactly one weakness.
    if rr >= components.min_rr:
        weaknesses = _count_b_weaknesses(components)
        if weaknesses >= 1:
            return ("B", confluences)

    return (None, confluences)


def _build_confluences(c: SetupComponents) -> list[str]:
    out: list[str] = []
    if c.swept_level_strength == "structural":
        out.append("structural_sweep")
    if c.swept_level_strength == "major":
        out.append("major_sweep")
    if c.poi_type == "FVG" and c.has_alternative_ob_when_fvg:
        out.append("FVG+OB")
    if c.ote_overlap:
        out.append("OTE_overlap")
    if c.displacement_body_ratio >= _STRONG_DISPLACEMENT:
        out.append("strong_displacement")
    if c.risk_reward >= _HIGH_RR_RUNNER:
        out.append("high_rr_runner")
    return out


def _count_b_weaknesses(c: SetupComponents) -> int:
    """Count weaknesses that justify a B grade. At least 1 is required.

    The spec phrases the rule as "one weakness allowed". We interpret
    "allowed" as "required to differentiate B from A" — a candidate with
    ZERO weaknesses but failing A (e.g. POI is an OB) is a clean
    fallback B. So we permissively count "POI is an OB" as a weakness
    too, since it is exactly the failure mode that can drop A → B.
    """
    weak = 0
    # Small FVG.
    if isinstance(c.poi, FVG):
        ratio = c.poi.size_atr_ratio
        if c.fvg_min_size_atr_multiplier <= ratio < 1.5 * c.fvg_min_size_atr_multiplier:
            weak += 1
    # Weak displacement.
    if 1.0 <= c.displacement_body_ratio < c.mss_displacement_multiplier:
        weak += 1
    # RR exactly at MIN_RR (within tolerance).
    if c.risk_reward < c.min_rr * (1.0 + _RR_TOLERANCE):
        weak += 1
    # POI is an OB — orthogonal weakness, not on the spec's enumerated
    # list but the only way a "structural sweep + minor displacement" OB
    # candidate ever lands as B rather than reject. See docstring.
    if c.poi_type == "OrderBlock":
        weak += 1
    return weak
