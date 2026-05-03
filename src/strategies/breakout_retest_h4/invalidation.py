"""Hard invalidation rules — spec §2.6."""

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
    """Return True if the setup must be skipped — see spec §2.6. Stub."""
    raise NotImplementedError


def daily_key(setup: Setup) -> tuple[str, date]:
    """Return the ``(instrument, calendar-date-UTC)`` key for the per-day cap."""
    return setup.instrument, setup.timestamp_utc.date()
