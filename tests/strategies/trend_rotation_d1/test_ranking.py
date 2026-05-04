"""Unit tests for ``select_top_k`` — spec §2.3."""

from __future__ import annotations

import pytest

from src.strategies.trend_rotation_d1.ranking import select_top_k


def test_select_top_k_returns_k_assets_by_score_desc() -> None:
    """K=3 → top-3 by score, descending."""
    scores = {"A": 0.10, "B": 0.20, "C": 0.05, "D": 0.30, "E": -0.10}
    top = select_top_k(scores, K=3)
    assert top == ["D", "B", "A"]  # 0.30, 0.20, 0.10


def test_select_top_k_handles_none_scores() -> None:
    """Assets with score=None (insufficient history / vol filter) are
    excluded from the ranking entirely."""
    scores = {"A": 0.10, "B": None, "C": 0.30, "D": None, "E": 0.20}
    top = select_top_k(scores, K=3)
    assert top == ["C", "E", "A"]  # B and D excluded


def test_select_top_k_with_k_larger_than_available_assets() -> None:
    """K > valid-asset-count → return all valid assets, no padding."""
    scores = {"A": 0.10, "B": None, "C": 0.30, "D": None}
    top = select_top_k(scores, K=5)
    assert top == ["C", "A"]  # only 2 valid assets


def test_select_top_k_deterministic_tiebreak() -> None:
    """Equal scores → tie-break by alphabetical asset name (stable
    determinism for audit reproducibility)."""
    scores = {"D": 0.20, "B": 0.20, "A": 0.20, "C": 0.20}
    top = select_top_k(scores, K=2)
    assert top == ["A", "B"]  # alpha order on equal scores


def test_select_top_k_zero_k_returns_empty() -> None:
    """K=0 → empty list (degenerate but not an error)."""
    scores = {"A": 0.10, "B": 0.20}
    assert select_top_k(scores, K=0) == []


def test_select_top_k_all_none() -> None:
    """All assets gated out → empty basket."""
    scores = {"A": None, "B": None, "C": None}
    assert select_top_k(scores, K=3) == []


def test_select_top_k_negative_k_raises() -> None:
    """Negative K is a programming error, not a runtime case."""
    with pytest.raises(ValueError, match="K"):
        select_top_k({"A": 0.1}, K=-1)
