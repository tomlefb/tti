"""Unit tests for ``detect_return`` — spec §2.5."""

from __future__ import annotations

from datetime import datetime, time

import pandas as pd

from src.strategies.mean_reversion_bb_h4.bollinger import compute_bollinger
from src.strategies.mean_reversion_bb_h4.return_detection import detect_return
from src.strategies.mean_reversion_bb_h4.types import ExcessEvent


def _killzone() -> dict:
    return {
        "killzone_london_start_utc": time(8, 0),
        "killzone_london_end_utc": time(12, 0),
        "killzone_ny_start_utc": time(12, 0),
        "killzone_ny_end_utc": time(16, 0),
    }


def _build(
    closes: list[float],
    *,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    start: str = "2026-01-01 00:00",
) -> pd.DataFrame:
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


def _excess_at(idx: int, df: pd.DataFrame, direction: str = "upper") -> ExcessEvent:
    return ExcessEvent(
        timestamp_utc=df["time"].iloc[idx].to_pydatetime(),
        bar_index=idx,
        direction=direction,  # type: ignore[arg-type]
        close=float(df["close"].iloc[idx]),
        high=float(df["high"].iloc[idx]),
        low=float(df["low"].iloc[idx]),
        bb_level=0.0,
        penetration_atr=float("nan"),
    )


def test_return_within_max_bars_returns_event() -> None:
    """Excess at idx 20 (08:00) lifts above upper. idx 21 (12:00, NY)
    closes back inside the bands → return event fires."""
    # Build closes such that BB at idx 20+ is well-defined and the
    # bands sit around 100±2.
    closes = [100.0 + (i % 2) for i in range(20)] + [120.0, 100.5, 100.5]
    df = _build(closes)
    bb = compute_bollinger(df["close"], period=20, multiplier=2.0)
    excess = _excess_at(20, df, direction="upper")

    ev = detect_return(
        df,
        bb,
        excess,
        max_return_bars=3,
        now_bar_index=22,  # everything observable
        **_killzone(),
    )

    assert ev is not None
    assert ev.return_bar_index == 21
    assert ev.return_bar_close == 100.5
    assert ev.sma_at_return == bb.sma.iloc[21]


def test_return_outside_max_bars_returns_none() -> None:
    """If the price stays above upper for the full window, no return."""
    closes = [100.0 + (i % 2) for i in range(20)] + [120.0, 121.0, 122.0, 123.0]
    df = _build(closes)
    bb = compute_bollinger(df["close"], period=20, multiplier=2.0)
    excess = _excess_at(20, df, direction="upper")

    ev = detect_return(
        df,
        bb,
        excess,
        max_return_bars=3,
        now_bar_index=23,
        **_killzone(),
    )
    assert ev is None


def test_return_must_close_inside_bands() -> None:
    """An overshoot return-candidate (close < lower) is skipped.

    Idx 20 = 08:00 London (excess upper). Idx 21 = 12:00 NY (only
    in-killzone candidate inside max_return_bars=3, since idx 22/23
    are 16:00/20:00 OUT). At idx 21 we engineer close = 80 — far
    below the (wide) lower band built on a window containing the
    120 outlier. No further in-killzone bar in the window →
    ``ev is None``.
    """
    closes = [100.0 + (i % 2) for i in range(20)] + [120.0, 80.0, 100.5, 100.5]
    df = _build(closes)
    bb = compute_bollinger(df["close"], period=20, multiplier=2.0)
    excess = _excess_at(20, df, direction="upper")

    ev = detect_return(
        df,
        bb,
        excess,
        max_return_bars=3,
        now_bar_index=23,
        **_killzone(),
    )
    # idx 21 closes at 80 — below the (wide) lower band, NOT inside.
    # idx 22, 23 are OUT-of-killzone (16:00, 20:00) → window expires.
    assert ev is None


def test_return_respects_killzone() -> None:
    """A would-be return bar at 16:00 UTC (OUT) must be skipped."""
    # Excess at idx 20 (08:00). idx 21=12:00 (in), idx 22=16:00 (out),
    # idx 23=20:00 (out). For a return that requires idx 22 to fire,
    # we need closes[21] OUTSIDE bands and closes[22] inside. Then
    # the killzone filter drops idx 22 → no return.
    closes = [100.0 + (i % 2) for i in range(20)] + [120.0, 121.0, 100.5, 100.5]
    df = _build(closes)
    bb = compute_bollinger(df["close"], period=20, multiplier=2.0)
    excess = _excess_at(20, df, direction="upper")

    ev = detect_return(
        df,
        bb,
        excess,
        max_return_bars=3,
        now_bar_index=23,
        **_killzone(),
    )
    # idx 21 (NY): close=121 — still above upper, not a return.
    # idx 22 (16:00 OUT): close=100.5 (inside) but OFF-killzone → skip.
    # idx 23 (20:00 OUT): close=100.5 (inside) but OFF-killzone → skip.
    # Window expires → no return.
    assert ev is None


def test_return_skips_when_close_above_upper_at_return_bar() -> None:
    """If the return candidate still closes above upper (continuation),
    ``detect_return`` skips it. Symmetric to the overshoot test."""
    closes = [100.0 + (i % 2) for i in range(20)] + [120.0, 130.0, 140.0, 150.0]
    df = _build(closes)
    bb = compute_bollinger(df["close"], period=20, multiplier=2.0)
    excess = _excess_at(20, df, direction="upper")

    ev = detect_return(
        df,
        bb,
        excess,
        max_return_bars=3,
        now_bar_index=23,
        **_killzone(),
    )
    # idx 21 (NY IN): close=130, above upper → skip.
    # idx 22, 23 OUT-of-killzone → window expires.
    assert ev is None


def test_return_now_bar_index_truncates_search() -> None:
    """``now_bar_index`` caps the visible window — anti-look-ahead."""
    closes = [100.0 + (i % 2) for i in range(20)] + [120.0, 100.5, 100.5]
    df = _build(closes)
    bb = compute_bollinger(df["close"], period=20, multiplier=2.0)
    excess = _excess_at(20, df, direction="upper")

    # With now_bar_index=20, the only visible bar after the excess is
    # nothing (we're at the excess bar itself). No return possible.
    ev = detect_return(
        df,
        bb,
        excess,
        max_return_bars=3,
        now_bar_index=20,
        **_killzone(),
    )
    assert ev is None
