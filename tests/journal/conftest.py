"""Shared fixtures for the journal test suite.

In-memory SQLite engine + a ``make_setup`` factory that mirrors the
stub patterns used by ``tests/notification`` so journal tests can build
a realistic ``Setup`` without dragging in the full detection pipeline.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from src.detection.fvg import FVG
from src.detection.mss import MSS
from src.detection.setup import Setup
from src.detection.sweep import Sweep
from src.journal.db import get_engine, init_db


@pytest.fixture
def engine():
    """Throwaway in-memory SQLite engine with the journal schema applied."""
    eng = get_engine(":memory:")
    init_db(eng)
    return eng


def _stub_sweep(direction: str = "bearish") -> Sweep:
    return Sweep(
        direction=direction,  # type: ignore[arg-type]
        swept_level_price=4380.0,
        swept_level_type="asian_high",
        swept_level_strength="structural",
        sweep_candle_time_utc=datetime(2026, 1, 2, 16, 30, tzinfo=UTC),
        sweep_extreme_price=4382.5,
        return_candle_time_utc=datetime(2026, 1, 2, 16, 30, tzinfo=UTC),
        excursion=2.5,
    )


def _stub_mss(direction: str = "bearish") -> MSS:
    t = datetime(2026, 1, 2, 16, 35, tzinfo=UTC)
    return MSS(
        direction=direction,  # type: ignore[arg-type]
        sweep=_stub_sweep(direction),
        broken_swing_time_utc=t,
        broken_swing_price=4365.0,
        mss_confirm_candle_time_utc=t,
        mss_confirm_candle_close=4364.0,
        displacement_body_ratio=2.1,
        displacement_candle_time_utc=t,
    )


def _stub_fvg(direction: str = "bearish") -> FVG:
    t = datetime(2026, 1, 2, 16, 35, tzinfo=UTC)
    return FVG(
        direction=direction,  # type: ignore[arg-type]
        proximal=4360.0,
        distal=4366.0,
        c1_time_utc=t,
        c2_time_utc=t,
        c3_time_utc=t,
        size=6.0,
        size_atr_ratio=1.0,
    )


@pytest.fixture
def make_setup() -> Callable[..., Setup]:
    """Factory producing realistic ``Setup`` instances with overrides.

    Defaults mirror the XAUUSD A-grade fixture used in
    ``tests/notification`` — short, bearish bias, NY killzone, FVG POI,
    runner clamped at 5R.
    """

    def _factory(**overrides: Any) -> Setup:
        defaults: dict[str, Any] = {
            "timestamp_utc": datetime(2026, 1, 2, 16, 35, tzinfo=UTC),
            "symbol": "XAUUSD",
            "direction": "short",
            "daily_bias": "bearish",
            "killzone": "ny",
            "swept_level_price": 4380.0,
            "swept_level_type": "asian_high",
            "swept_level_strength": "structural",
            "sweep": _stub_sweep("bearish"),
            "mss": _stub_mss("bearish"),
            "poi": _stub_fvg("bearish"),
            "poi_type": "FVG",
            "entry_price": 4360.0,
            "stop_loss": 4375.0,
            "target_level_type": "swing_h1_low",
            "tp_runner_price": 4080.5,
            "tp_runner_rr": 18.70,
            "tp1_price": 4285.0,
            "tp1_rr": 5.0,
            "quality": "A",
            "confluences": ["structural_sweep", "FVG+OB", "high_rr_runner"],
        }
        defaults.update(overrides)
        return Setup(**defaults)

    return _factory
