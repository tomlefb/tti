"""Basket transition detection — spec §2.4.

Pure set-difference operation: at each rebalance, compare the
prior basket with the new (post-ranking) basket; assets dropping
out are closed, assets entering are opened.
"""

from __future__ import annotations


def detect_rebalance_trades(
    previous_basket: set[str],
    new_basket: set[str],
) -> tuple[set[str], set[str]]:
    """Return ``(closed, opened)`` per spec §2.4.

    Args:
        previous_basket: assets in the basket before this rebalance
            (empty set on the first rebalance of a run).
        new_basket: assets in the basket after this rebalance.

    Returns:
        ``(closed, opened)``:
        - ``closed`` = ``previous_basket \\ new_basket``: positions
          to close at this rebalance close.
        - ``opened`` = ``new_basket \\ previous_basket``: new
          positions to open at this rebalance close.

        Both as fresh ``set`` instances; the inputs are not mutated.
    """
    closed = previous_basket - new_basket
    opened = new_basket - previous_basket
    return closed, opened
