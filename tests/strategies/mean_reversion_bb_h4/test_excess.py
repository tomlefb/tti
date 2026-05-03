"""Unit tests for ``detect_excess`` — spec §2.2.

Helper convention: every fixture starts at ``2026-01-01 00:00 UTC``,
so the bar at idx ``i`` **opens** at hour ``(i * 4) mod 24`` UTC and
**closes** at hour ``((i + 1) * 4) mod 24`` UTC. The killzone gate
filters by **close timestamp** in ``[start, end]`` both-ends-inclusive
(spec §2.2 Option A). On the H4 grid + spec defaults
``London = [08:00, 12:00]``, ``NY = [13:00, 18:00]``, the
in-killzone close set is ``{08:00, 12:00, 16:00}``.

Index → bar close hour (the values these tests probe):

- idx 19 → close 08:00 (London IN, first index where BB(20) is defined)
- idx 20 → close 12:00 (London IN)
- idx 21 → close 16:00 (NY IN)
- idx 22 → close 20:00 (OUT)
- idx 23 → close 00:00 (OUT)
- idx 24 → close 04:00 (OUT)
- idx 25 → close 08:00 (London IN)
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
        "killzone_ny_start_utc": time(13, 0),
        "killzone_ny_end_utc": time(18, 0),
    }


def test_excess_upper_when_close_above_upper_band() -> None:
    """idx 20: bar opens 08:00, closes 12:00 (London IN).
    Close pushed far above any band."""
    closes = [100.0] * 20 + [120.0]
    df = _build(closes)
    assert df["time"].iloc[20].hour == 8  # bar OPEN hour

    bb = compute_bollinger(df["close"], period=20, multiplier=2.0)
    ev = detect_excess(df, bb, bar_index=20, **_killzone())

    assert ev is not None
    assert ev.direction == "upper"
    assert ev.bar_index == 20
    assert ev.close == 120.0


def test_excess_lower_when_close_below_lower_band() -> None:
    """idx 20 closes at 12:00 UTC (London IN). Close far below any band."""
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
    """idx 22 closes at 20:00 UTC → OUT (post-NY-end)."""
    closes = [100.0] * 20 + [105.0, 110.0, 120.0]
    df = _build(closes)  # idx 22: open 16:00, close 20:00
    assert df["time"].iloc[22].hour == 16  # bar OPEN hour
    bb = compute_bollinger(df["close"], period=20, multiplier=2.0)
    ev = detect_excess(df, bb, bar_index=22, **_killzone())
    assert ev is None, f"close 20:00 UTC must be OUT of killzone, got {ev}"


def test_killzone_includes_london_open_bar() -> None:
    """idx 20 closes at 12:00 UTC → London end inclusive → IN."""
    closes = [100.0] * 20 + [120.0]
    df = _build(closes)
    bb = compute_bollinger(df["close"], period=20, multiplier=2.0)
    ev = detect_excess(df, bb, bar_index=20, **_killzone())
    assert ev is not None
    assert ev.direction == "upper"


def test_killzone_includes_ny_h4_bar() -> None:
    """idx 21 closes at 16:00 UTC → 16:00 ∈ [13:00, 18:00] → NY IN."""
    closes = [100.0] * 20 + [110.0, 130.0]
    df = _build(closes)
    assert df["time"].iloc[21].hour == 12  # bar OPEN
    bb = compute_bollinger(df["close"], period=20, multiplier=2.0)
    ev = detect_excess(df, bb, bar_index=21, **_killzone())
    assert ev is not None
    assert ev.direction == "upper"


def test_killzone_excludes_late_ny_h4_bar() -> None:
    """idx 22 closes at 20:00 UTC → 20:00 > 18:00 (NY end) → OUT."""
    closes = [100.0] * 20 + [100.0, 100.0, 130.0]
    df = _build(closes)
    assert df["time"].iloc[22].hour == 16  # bar OPEN
    bb = compute_bollinger(df["close"], period=20, multiplier=2.0)
    ev = detect_excess(df, bb, bar_index=22, **_killzone())
    assert ev is None


def test_killzone_excludes_post_ny_overnight_bar() -> None:
    """idx 23 closes at 00:00 UTC → outside both windows → OUT.

    Replaces the legacy ``test_killzone_excludes_early_morning_bar``
    which assumed OPEN-time gating (idx 19 open=04:00 OUT). Under the
    spec §2.2 close-time convention, idx 19 closes at 08:00 (London
    end inclusive) → IN. The genuinely OUT closes on the H4 grid are
    20:00, 00:00, 04:00; this test probes 00:00.
    """
    # Need BB(20) defined → idx ≥ 19. idx 23 is well beyond. Vary
    # earlier closes so the std is non-zero, then force a huge close
    # at idx 23 — it should not register because the close-time gate
    # rejects 00:00.
    closes = [100.0 + (i % 2) for i in range(23)] + [200.0]
    df = _build(closes)
    assert df["time"].iloc[23].hour == 20  # bar OPEN — close lands at 00:00
    bb = compute_bollinger(df["close"], period=20, multiplier=2.0)
    ev = detect_excess(df, bb, bar_index=23, **_killzone())
    assert ev is None


def test_killzone_includes_london_start_close() -> None:
    """idx 19 closes at 08:00 UTC → 08:00 ∈ [08:00, 12:00] → London IN.

    Edge case: the London start bound (08:00) is inclusive — the bar
    that closes EXACTLY at 08:00 is in-killzone. Spec §2.2 Option A
    explicit: ``[start, end]`` both-ends-inclusive.
    """
    closes = [100.0 + (i % 2) for i in range(19)] + [200.0]
    df = _build(closes)
    assert df["time"].iloc[19].hour == 4  # bar OPEN
    bb = compute_bollinger(df["close"], period=20, multiplier=2.0)
    ev = detect_excess(df, bb, bar_index=19, **_killzone())
    assert ev is not None
    assert ev.direction == "upper"


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
