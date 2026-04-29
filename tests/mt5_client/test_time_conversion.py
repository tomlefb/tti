"""Unit tests for src.mt5_client.time_conversion."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.mt5_client.time_conversion import (
    broker_naive_seconds_to_utc,
    broker_naive_to_utc,
    detect_broker_offset_hours,
)


def test_detect_offset_athens_summer_clean():
    """Broker tick is 3h ahead of UTC in summer → +3."""
    now_utc = datetime(2026, 7, 15, 10, 0, 0, tzinfo=UTC)
    broker_seconds = now_utc.timestamp() + 3 * 3600  # broker shows 13:00
    assert detect_broker_offset_hours(broker_seconds, now_utc=now_utc) == 3


def test_detect_offset_athens_winter_clean():
    """Broker tick is 2h ahead of UTC in winter → +2."""
    now_utc = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)
    broker_seconds = now_utc.timestamp() + 2 * 3600
    assert detect_broker_offset_hours(broker_seconds, now_utc=now_utc) == 2


def test_detect_offset_rounds_to_nearest_hour():
    """Small clock skew (≤ 5 min) gets rounded to the closest hour."""
    now_utc = datetime(2026, 7, 15, 10, 0, 0, tzinfo=UTC)
    broker_seconds = now_utc.timestamp() + 3 * 3600 + 90  # 90s skew
    assert detect_broker_offset_hours(broker_seconds, now_utc=now_utc) == 3


def test_detect_offset_falls_back_when_residual_too_large():
    """Half-hour offset triggers Athens fallback (logged as warning)."""
    now_utc = datetime(2026, 7, 15, 10, 0, 0, tzinfo=UTC)
    broker_seconds = now_utc.timestamp() + 2 * 3600 + 30 * 60  # +2:30
    # Residual is 30 min > tolerance → falls back to Athens summer (+3).
    assert detect_broker_offset_hours(broker_seconds, now_utc=now_utc) == 3


def test_detect_offset_uses_athens_fallback_when_probe_none_summer():
    now_utc = datetime(2026, 7, 15, 10, 0, 0, tzinfo=UTC)
    assert detect_broker_offset_hours(None, now_utc=now_utc) == 3


def test_detect_offset_uses_athens_fallback_when_probe_none_winter():
    now_utc = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)
    assert detect_broker_offset_hours(None, now_utc=now_utc) == 2


def test_broker_naive_seconds_to_utc_roundtrip():
    """A POSIX seconds value of `2026-07-15 13:00 broker` → 10:00 UTC at +3."""
    broker_seconds = datetime(2026, 7, 15, 13, 0, 0, tzinfo=UTC).timestamp()
    out = broker_naive_seconds_to_utc(broker_seconds, offset_hours=3)
    assert out == datetime(2026, 7, 15, 10, 0, 0, tzinfo=UTC)


def test_broker_naive_to_utc_roundtrip():
    naive_broker = datetime(2026, 7, 15, 13, 0, 0)
    out = broker_naive_to_utc(naive_broker, offset_hours=3)
    assert out == datetime(2026, 7, 15, 10, 0, 0, tzinfo=UTC)


def test_broker_naive_to_utc_rejects_aware_input():
    aware = datetime(2026, 7, 15, 13, 0, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="expected naive datetime"):
        broker_naive_to_utc(aware, offset_hours=3)
