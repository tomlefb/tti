"""Hard invalidation rules — spec §2.6.

Two rules implemented:

1. ``|entry - sl| > max_risk_distance`` — instrument-specific cap so a
   degenerate retest with a deep wick does not produce a giant-stop
   trade.
2. Per-day cap: ``daily_count >= max_trades_per_day`` on the same
   instrument and same calendar day (UTC) → skip.

The news-window filter (spec §2.6 third bullet) is **deliberately
omitted at gate 2**: the spec itself notes "if no clean source, ship
without this filter and document". Documenting here. If a calendar
source is added later, plug it into a separate ``is_news_window``
helper and OR it into ``is_invalid``.
"""

from __future__ import annotations

from datetime import date

from .setup import Setup


def is_invalid(
    setup: Setup,
    *,
    max_risk_distance: float,
    daily_count: int,
    max_trades_per_day: int,
) -> bool:
    """Return True if the setup must be skipped (spec §2.6).

    Args:
        setup: candidate setup.
        max_risk_distance: instrument-priced cap on ``|entry - sl|``.
        daily_count: number of setups already produced today on this
            instrument (read from ``StrategyState.trades_today``).
        max_trades_per_day: cap from ``StrategyParams``; spec §3.1
            anchors at 2.

    Returns:
        ``True`` when *any* invalidation rule fires; ``False`` only
        when every rule passes.
    """
    risk = abs(setup.entry_price - setup.stop_loss)
    if risk > max_risk_distance:
        return True
    if daily_count >= max_trades_per_day:
        return True
    return False


def daily_key(setup: Setup) -> tuple[str, date]:
    """Return the ``(instrument, calendar-date-UTC)`` key for the per-day cap."""
    return setup.instrument, setup.timestamp_utc.date()
