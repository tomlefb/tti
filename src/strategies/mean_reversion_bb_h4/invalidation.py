"""Hard invalidation rules — spec §2.7.

Three rules implemented:

1. ``rr < min_rr`` → skip (computed RR too tight to be worth it).
2. ``|entry - sl| > max_risk_distance`` → skip (anti-degenerate).
3. ``daily_count >= max_trades_per_day`` → skip (anti-overtrading).

The §2.4 exhaustion candle and §2.3 ATR penetration are filters
applied earlier in the pipeline (in ``filters.py``); they are NOT
invalidations and do not appear here.
"""

from __future__ import annotations

from datetime import date

from .types import Setup


def is_invalid(
    setup: Setup,
    *,
    min_rr: float,
    max_risk_distance: float,
    daily_count: int,
    max_trades_per_day: int,
) -> bool:
    """Return ``True`` if the setup must be skipped (spec §2.7).

    Args:
        setup: candidate setup.
        min_rr: hard floor on ``setup.risk_reward`` (spec §3.1: 1.0).
        max_risk_distance: instrument-priced cap on ``|entry - sl|``.
        daily_count: number of setups already produced today on this
            instrument (read from ``StrategyState.trades_today``).
        max_trades_per_day: cap from ``StrategyParams``; spec §3.1
            anchors at 2.

    Returns:
        ``True`` when *any* rule fires; ``False`` only when every
        rule passes.
    """
    if setup.risk_reward < min_rr:
        return True
    risk = abs(setup.entry_price - setup.stop_loss)
    if risk > max_risk_distance:
        return True
    if daily_count >= max_trades_per_day:
        return True
    return False


def daily_key(setup: Setup) -> tuple[str, date]:
    """Return the ``(instrument, calendar-date-UTC)`` key for the per-day cap."""
    return setup.instrument, setup.timestamp_utc.date()
