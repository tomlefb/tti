"""Broker-time ↔ UTC conversion helpers.

Many FX brokers run their MT5 server in Athens time (UTC+2 winter,
UTC+3 summer) so that the daily candle aligns with the New York close.
The MT5 Python API returns POSIX timestamps in **broker** time, not UTC.
This module owns the offset detection and the conversion.

Two layers:

- :func:`detect_broker_offset_hours` — observed at connect time by
  comparing ``mt5.symbol_info_tick(...).time`` (broker) to local UTC.
- :func:`broker_naive_to_utc` — converts a naive broker-time
  ``datetime`` (or POSIX seconds) to an aware UTC ``datetime`` using a
  cached offset.

Falls back to the ``UTC+2 winter / UTC+3 summer`` Athens convention when
the runtime probe is unavailable (tests, dry runs).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta, timezone

logger = logging.getLogger(__name__)


# Athens (EET / EEST) DST switches occur on the last Sunday of March and
# the last Sunday of October. We approximate "summer" as Apr 1 – Oct 31 to
# avoid pulling in a calendar lookup; the worst-case error is one hour
# during the few days bracketing each switch, which only matters for the
# fallback path. The runtime-detected offset (preferred) sidesteps this.
def _athens_default_offset_hours(now_utc: datetime) -> int:
    """Return ``+2`` (winter) or ``+3`` (summer) using a coarse Apr–Oct window."""
    month = now_utc.month
    return 3 if 4 <= month <= 10 else 2


def detect_broker_offset_hours(
    broker_now_seconds: float | int | None,
    *,
    now_utc: datetime | None = None,
    tolerance_minutes: int = 5,
) -> int:
    """Estimate the broker timezone offset in whole hours.

    Compares a tick-derived ``broker_now_seconds`` (POSIX seconds, but
    interpreted as if the wallclock were UTC) against ``now_utc``.
    Snaps the difference to the nearest whole hour. Returns the Athens
    fallback when ``broker_now_seconds`` is ``None`` or the difference
    is wildly off (suggests broker_now refers to a stale tick).

    Args:
        broker_now_seconds: ``mt5.symbol_info_tick(symbol).time`` value.
            ``None`` if probing failed.
        now_utc: aware UTC ``datetime`` for comparison. Defaults to
            ``datetime.now(UTC)``.
        tolerance_minutes: max permissible mod-1h residual after rounding;
            larger residuals trigger the Athens fallback (defensive — most
            FX brokers offset by a whole number of hours).

    Returns:
        Integer offset in hours (positive = broker ahead of UTC).
    """
    now_utc = now_utc if now_utc is not None else datetime.now(tz=UTC)

    if broker_now_seconds is None:
        fallback = _athens_default_offset_hours(now_utc)
        logger.warning(
            "MT5 broker offset probe unavailable — using Athens fallback %+d h",
            fallback,
        )
        return fallback

    broker_naive = datetime.fromtimestamp(float(broker_now_seconds), tz=UTC)
    delta_seconds = (broker_naive - now_utc).total_seconds()
    hours_float = delta_seconds / 3600.0
    rounded = round(hours_float)
    residual_minutes = abs(hours_float - rounded) * 60.0

    if residual_minutes > tolerance_minutes:
        fallback = _athens_default_offset_hours(now_utc)
        logger.warning(
            "MT5 broker offset probe inconsistent (delta=%.2fh, residual=%.1fmin) "
            "— using Athens fallback %+d h",
            hours_float,
            residual_minutes,
            fallback,
        )
        return fallback

    logger.info("MT5 broker offset detected: UTC%+d", rounded)
    return rounded


def broker_naive_seconds_to_utc(seconds: float | int, offset_hours: int) -> datetime:
    """Convert a broker POSIX timestamp to an aware UTC ``datetime``.

    The MT5 Python API returns ``time`` as POSIX seconds, but the value
    encodes the **broker wallclock** (e.g. 13:00 broker = +3 → the API
    returns ``datetime(...,13,0).timestamp()``). To recover the true UTC
    instant we extract the wallclock components and subtract the offset.
    """
    # Treat seconds as if naive UTC to get the wallclock components.
    wallclock = datetime.fromtimestamp(float(seconds), tz=UTC).replace(tzinfo=None)
    aware_broker = wallclock.replace(tzinfo=timezone(timedelta(hours=offset_hours)))
    return aware_broker.astimezone(UTC)


def broker_naive_to_utc(naive_dt: datetime, offset_hours: int) -> datetime:
    """Convert a naive broker-local ``datetime`` to an aware UTC ``datetime``.

    Raises:
        ValueError: ``naive_dt`` already carries tzinfo.
    """
    if naive_dt.tzinfo is not None:
        raise ValueError(f"expected naive datetime, got {naive_dt!r} with tzinfo")
    aware_broker = naive_dt.replace(tzinfo=timezone(timedelta(hours=offset_hours)))
    return aware_broker.astimezone(UTC)
