"""Unit tests for ``detect_rebalance_trades`` — spec §2.4."""

from __future__ import annotations

from src.strategies.trend_rotation_d1.transitions import detect_rebalance_trades


def test_detect_no_transitions_when_basket_unchanged() -> None:
    """Identical baskets → no closed, no opened."""
    prev = {"A", "B", "C"}
    new = {"A", "B", "C"}
    closed, opened = detect_rebalance_trades(prev, new)
    assert closed == set()
    assert opened == set()


def test_detect_complete_rotation_when_basket_disjoint() -> None:
    """Fully disjoint baskets → all prior closed, all new opened."""
    prev = {"A", "B", "C"}
    new = {"X", "Y", "Z"}
    closed, opened = detect_rebalance_trades(prev, new)
    assert closed == {"A", "B", "C"}
    assert opened == {"X", "Y", "Z"}


def test_detect_partial_rotation() -> None:
    """Some assets stay (intersection), some swap."""
    prev = {"A", "B", "C"}
    new = {"B", "C", "D"}  # A out, D in; B & C stay
    closed, opened = detect_rebalance_trades(prev, new)
    assert closed == {"A"}
    assert opened == {"D"}


def test_first_rebalance_no_closed_assets() -> None:
    """Empty prior basket → no closed, all new opened."""
    prev: set[str] = set()
    new = {"A", "B"}
    closed, opened = detect_rebalance_trades(prev, new)
    assert closed == set()
    assert opened == {"A", "B"}


def test_empty_new_basket_closes_all_priors() -> None:
    """All prior closed, none opened (degenerate but well-defined)."""
    prev = {"A", "B"}
    new: set[str] = set()
    closed, opened = detect_rebalance_trades(prev, new)
    assert closed == {"A", "B"}
    assert opened == set()


def test_returns_pure_sets_no_input_mutation() -> None:
    """The function must not mutate its inputs."""
    prev = {"A", "B"}
    new = {"B", "C"}
    prev_copy = set(prev)
    new_copy = set(new)
    detect_rebalance_trades(prev, new)
    assert prev == prev_copy
    assert new == new_copy
