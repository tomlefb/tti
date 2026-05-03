"""Unit tests for ``detect_excess`` — spec §2.2.

Helper convention: every fixture starts at ``2026-01-01 00:00 UTC``,
so the bar at idx ``i`` opens at hour ``(i * 4) mod 24`` UTC. The
in-killzone bars over the 21 first indices are therefore:

- idx 2  →  08:00 (London)
- idx 3  →  12:00 (NY)
- idx 8  →  08:00 (London)
- idx 20 →  08:00 (London)  — first index where BB(20) is defined
- idx 21 →  12:00 (NY)
- idx 26 →  08:00 (London)
"""

from __future__ import annotations

from datetime import time

import pandas as pd

from src.strategies.mean_reversion_bb_h4.bollinger import compute_bollinger
from src.strategies.mean_reversion_bb_h4.excess import detect_excess


def _build(
    closes: list[float],
    *,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    start: str = "2026-01-01 00:00",
) -> pd.DataFrame:
    """H4 frame anchored at UTC midnight by default."""
    times = pd.date_range(start, periods=len(closes), freq="4h", tz="UTC")
    return pd.DataFrame(
        {
            "time": times,
            "open": closes,
            "high": highs if highs is not None else closes,
            "low": lows if lows is not None else closes,
            "close": closes,
        }
    )


def _killzone() -> dict:
    """Default killzone kwargs (matching ``StrategyParams`` defaults)."""
    return {
        "killzone_london_start_utc": time(8, 0),
        "killzone_london_end_utc": time(12, 0),
        "killzone_ny_start_utc": time(12, 0),
        "killzone_ny_end_utc": time(16, 0),
    }


def test_excess_upper_when_close_above_upper_band() -> None:
    """idx 20 = 08:00 UTC (London). Close pushed far above any band."""
    closes = [100.0] * 20 + [120.0]
    df = _build(closes)
    assert df["time"].iloc[20].hour == 8

    bb = compute_bollinger(df["close"], period=20, multiplier=2.0)
    ev = detect_excess(df, bb, bar_index=20, **_killzone())

    assert ev is not None
    assert ev.direction == "upper"
    assert ev.bar_index == 20
    assert ev.close == 120.0


def test_excess_lower_when_close_below_lower_band() -> None:
    """idx 20 = 08:00 UTC. Close pushed far below any band."""
    closes = [100.0] * 20 + [80.0]
    df = _build(closes)
    bb = compute_bollinger(df["close"], period=20, multiplier=2.0)
    ev = detect_excess(df, bb, bar_index=20, **_killzone())
    assert ev is not None
    assert ev.direction == "lower"
    assert ev.close == 80.0


def test_no_excess_when_close_within_bands() -> None:
    """Constant series → bands collapse on close → no strict piercing."""
    closes = [100.0] * 21
    df = _build(closes)
    bb = compute_bollinger(df["close"], period=20, multiplier=2.0)
    ev = detect_excess(df, bb, bar_index=20, **_killzone())
    assert ev is None


def test_killzone_filter_excludes_off_session_bars() -> None:
    """idx 22 = 16:00 UTC → OUT per spec §2.2 narrative."""
    closes = [100.0] * 20 + [105.0, 110.0, 120.0]
    df = _build(closes)  # idx 22 → 22*4 mod 24 = 88 mod 24 = 16 → OUT
    assert df["time"].iloc[22].hour == 16
    bb = compute_bollinger(df["close"], period=20, multiplier=2.0)
    ev = detect_excess(df, bb, bar_index=22, **_killzone())
    assert ev is None, f"16:00 UTC must be OUT of killzone, got {ev}"


def test_killzone_includes_london_open_bar() -> None:
    """idx 20 = 08:00 UTC → London → IN."""
    closes = [100.0] * 20 + [120.0]
    df = _build(closes)
    bb = compute_bollinger(df["close"], period=20, multiplier=2.0)
    ev = detect_excess(df, bb, bar_index=20, **_killzone())
    assert ev is not None
    assert ev.direction == "upper"


def test_killzone_includes_ny_first_h4_bar() -> None:
    """idx 21 = 12:00 UTC → first NY → IN."""
    closes = [100.0] * 20 + [110.0, 130.0]  # idx 20 also pierces, but
    # we test idx 21 specifically.
    df = _build(closes)
    assert df["time"].iloc[21].hour == 12
    bb = compute_bollinger(df["close"], period=20, multiplier=2.0)
    ev = detect_excess(df, bb, bar_index=21, **_killzone())
    assert ev is not None
    assert ev.direction == "upper"


def test_killzone_excludes_late_ny_h4_bar() -> None:
    """idx 22 = 16:00 UTC → OUT."""
    # Same as test_killzone_filter_excludes — kept as a labelled
    # variant so the spec §2.2 narrative coverage is explicit.
    closes = [100.0] * 20 + [100.0, 100.0, 130.0]
    df = _build(closes)
    assert df["time"].iloc[22].hour == 16
    bb = compute_bollinger(df["close"], period=20, multiplier=2.0)
    ev = detect_excess(df, bb, bar_index=22, **_killzone())
    assert ev is None


def test_killzone_excludes_early_morning_bar() -> None:
    """idx 19 = 04:00 UTC → OUT."""
    # We need BB defined at idx 19 — but BB(20) is first defined at
    # idx 19. Engineer close[19] above bands by varying earlier closes.
    closes = [100.0 + (i % 2) for i in range(19)] + [200.0]  # huge close
    df = _build(closes)
    assert df["time"].iloc[19].hour == 4
    bb = compute_bollinger(df["close"], period=20, multiplier=2.0)
    ev = detect_excess(df, bb, bar_index=19, **_killzone())
    assert ev is None


def test_excess_uses_close_only_not_wick() -> None:
    """High pierces upper band, but close is inside → no excess."""
    # Make the closes vary enough to keep std > 0, then engineer
    # close[20] strictly inside [lower, upper] and high[20] above upper.
    base = [100.0 + (i % 2) for i in range(20)]
    closes = base + [100.5]  # close inside the bands
    highs = base + [110.0]   # wick well above any band
    lows = base + [100.0]
    df = _build(closes, highs=highs, lows=lows)
    bb = compute_bollinger(df["close"], period=20, multiplier=2.0)

    ev = detect_excess(df, bb, bar_index=20, **_killzone())
    assert ev is None
