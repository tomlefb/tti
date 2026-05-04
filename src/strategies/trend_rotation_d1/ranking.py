"""Cross-sectional top-K selection — spec §2.3.

Pure function over a per-asset score map. Assets whose score is
``None`` (insufficient history, volatility-regime filter, etc.) are
removed from the ranking entirely — they are not "ranked low",
they are absent.

Tie-break: alphabetical asset name on equal scores. Determinism
matters for the gate-3 streaming-vs-full-history audit and for
report reproducibility.
"""

from __future__ import annotations


def select_top_k(scores: dict[str, float | None], K: int) -> list[str]:
    """Return the ``K`` highest-scoring assets, descending.

    Args:
        scores: per-asset momentum score. ``None`` values are
            excluded.
        K: basket size (spec §3.2 grid: 3 or 4). 0 returns the
            empty list; negative is an error.

    Returns:
        Up to ``K`` asset names, in score-descending order. Ties
        broken alphabetically by asset name. Fewer than ``K``
        names returned if there are fewer than ``K`` non-``None``
        scores.
    """
    if K < 0:
        raise ValueError(f"K must be >= 0, got {K}")
    if K == 0:
        return []
    valid = [(asset, score) for asset, score in scores.items() if score is not None]
    # Sort by (-score, asset) so descending-score, then ascending-name on ties.
    valid.sort(key=lambda x: (-x[1], x[0]))
    return [asset for asset, _ in valid[:K]]
