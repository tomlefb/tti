"""Unit tests for ``src.detection.sweep.deduplicate_sweeps``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.detection.sweep import Sweep, deduplicate_sweeps


def _sweep(
    minutes_offset: int,
    price: float,
    excursion: float,
    direction: str = "bullish",
    label: str = "asian_low",
    strength: str = "structural",
) -> Sweep:
    base = datetime(2025, 7, 14, 9, 0, tzinfo=UTC)
    t = base + timedelta(minutes=minutes_offset)
    return Sweep(
        direction=direction,  # type: ignore[arg-type]
        swept_level_price=price,
        swept_level_type=label,
        swept_level_strength=strength,  # type: ignore[arg-type]
        sweep_candle_time_utc=t,
        sweep_extreme_price=price - excursion if direction == "bullish" else price + excursion,
        return_candle_time_utc=t + timedelta(minutes=5),
        excursion=excursion,
    )


def test_dedup_collapses_close_in_time_same_level() -> None:
    sweeps = [
        _sweep(0, 100.0, 1.5),
        _sweep(5, 100.0, 2.5),  # deeper — should win
        _sweep(10, 100.0, 1.0),
        _sweep(15, 100.0, 2.0),
        _sweep(20, 100.0, 1.2),
    ]
    out = deduplicate_sweeps(sweeps, time_window_minutes=30, price_tolerance_fraction=0.001)
    assert len(out) == 1
    assert out[0].excursion == 2.5


def test_dedup_keeps_different_levels() -> None:
    sweeps = [
        _sweep(0, 100.0, 1.5),
        _sweep(5, 200.0, 1.5),  # different price — different level
    ]
    out = deduplicate_sweeps(sweeps)
    assert len(out) == 2


def test_dedup_keeps_different_directions() -> None:
    sweeps = [
        _sweep(0, 100.0, 1.5, direction="bullish"),
        _sweep(5, 100.0, 1.5, direction="bearish"),
    ]
    out = deduplicate_sweeps(sweeps)
    assert len(out) == 2


def test_dedup_keeps_far_in_time() -> None:
    sweeps = [
        _sweep(0, 100.0, 1.5),
        _sweep(60, 100.0, 1.5),  # 60 min later — outside default 30-min window
    ]
    out = deduplicate_sweeps(sweeps, time_window_minutes=30)
    assert len(out) == 2


def test_dedup_transitive_clustering() -> None:
    # A and C are 40 min apart (outside window) but B bridges them
    # ⇒ all three should collapse to one cluster via union-find.
    sweeps = [
        _sweep(0, 100.0, 1.0),
        _sweep(20, 100.0, 5.0),  # bridge, deepest → wins
        _sweep(40, 100.0, 2.0),
    ]
    out = deduplicate_sweeps(sweeps, time_window_minutes=30)
    assert len(out) == 1
    assert out[0].excursion == 5.0


def test_dedup_price_tolerance_symmetric() -> None:
    # Two levels at 100.00 and 100.05, tol 0.001 ⇒ allowed diff = 0.001 × 100.025 ≈ 0.1.
    # 0.05 < 0.1 ⇒ same cluster.
    sweeps = [
        _sweep(0, 100.00, 1.0),
        _sweep(5, 100.05, 2.0),
    ]
    out = deduplicate_sweeps(sweeps, price_tolerance_fraction=0.001)
    assert len(out) == 1


def test_dedup_price_tolerance_too_strict() -> None:
    sweeps = [
        _sweep(0, 100.00, 1.0),
        _sweep(5, 105.00, 2.0),
    ]
    out = deduplicate_sweeps(sweeps, price_tolerance_fraction=0.001)
    assert len(out) == 2


def test_dedup_empty_and_singleton() -> None:
    assert deduplicate_sweeps([]) == []
    s = _sweep(0, 100.0, 1.0)
    assert deduplicate_sweeps([s]) == [s]


def test_dedup_returns_sorted_by_time() -> None:
    sweeps = [
        _sweep(60, 200.0, 1.0),
        _sweep(0, 100.0, 1.0),
        _sweep(120, 300.0, 1.0),
    ]
    out = deduplicate_sweeps(sweeps)
    assert [s.swept_level_price for s in out] == [100.0, 200.0, 300.0]


def test_dedup_negative_params_raise() -> None:
    with pytest.raises(ValueError):
        deduplicate_sweeps([], time_window_minutes=-1)
    with pytest.raises(ValueError):
        deduplicate_sweeps([], price_tolerance_fraction=-0.001)


def test_detect_sweeps_dedupe_flag_off_by_request() -> None:
    """Sanity: integration via ``detect_sweeps`` honours the `dedupe` flag."""
    import pandas as pd

    from src.detection.liquidity import MarkedLevel
    from src.detection.sweep import detect_sweeps

    start = datetime(2025, 7, 14, 9, 0, tzinfo=UTC)
    times = [start + timedelta(minutes=5 * i) for i in range(4)]
    df = pd.DataFrame(
        {
            "time": times,
            "open": [101, 101, 101, 101],
            "high": [102, 102, 102, 102],
            "low": [98, 98, 98, 98],
            "close": [101, 101, 101, 101],
        }
    )
    levels = [MarkedLevel(price=100.0, type="low", label="asian_low", strength="structural")]
    deduped = detect_sweeps(
        df,
        levels,
        (start, start + timedelta(minutes=20)),
        sweep_buffer=1.0,
        return_window_candles=2,
        dedupe=True,
    )
    raw = detect_sweeps(
        df,
        levels,
        (start, start + timedelta(minutes=20)),
        sweep_buffer=1.0,
        return_window_candles=2,
        dedupe=False,
    )
    assert len(deduped) == 1
    assert len(raw) == 4
