"""Sweep detection on M5 vs marked liquidity levels (docs/01 §5 step 1).

A sweep is a wick that pierces a known liquidity level by at least the
per-instrument ``sweep_buffer`` and whose close (this candle or one of the
next ``return_window_candles`` candles) returns back across the level.

Pure logic given the parameters. The selection of "the" sweep that triggers
a setup is downstream — see docs/07 §1.3: "most recent qualifying sweep
wins" is one of several valid heuristics. This module returns ALL sweeps
in the killzone window; downstream code (Sprint 3+) picks one.

This module also does NOT filter by daily bias direction. Per docs/01 §5,
bullish bias only takes sweeps of lows and bearish only of highs, but that
filtering belongs in ``setup.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import pandas as pd

from .liquidity import MarkedLevel


@dataclass(frozen=True)
class Sweep:
    """One detected liquidity sweep on M5.

    ``direction`` semantics:
        - ``"bullish"`` — a low was swept (wick down then close back up).
          Aligns with a bullish daily bias.
        - ``"bearish"`` — a high was swept (wick up then close back down).
    """

    direction: Literal["bullish", "bearish"]
    swept_level_price: float
    swept_level_type: str
    swept_level_strength: Literal["major", "major_h4_only", "minor", "structural"]
    sweep_candle_time_utc: datetime
    sweep_extreme_price: float
    return_candle_time_utc: datetime
    excursion: float


def detect_sweeps(
    df_m5: pd.DataFrame,
    levels: list[MarkedLevel],
    killzone_window_utc: tuple[datetime, datetime],
    *,
    sweep_buffer: float,
    return_window_candles: int,
) -> list[Sweep]:
    """Detect every sweep that occurs inside ``killzone_window_utc``.

    For each M5 candle in ``[start_utc, end_utc]`` and each level:

    - **Bullish sweep** (sweep of a low):
        - ``candle.low <= level.price - sweep_buffer``, AND
        - ``close`` of the same candle OR one of the next
          ``return_window_candles`` candles is ``>= level.price``.
    - **Bearish sweep** (symmetric on highs).

    The 1-2-candle return window is itself a docs/07 §1.3 heuristic;
    alternatives include 0 (same-candle only) or larger windows.

    A single candle can sweep multiple levels and emit multiple Sweeps.
    The same level can be swept multiple times in one killzone — all are
    returned. Selection is downstream.

    Args:
        df_m5: M5 OHLC frame with ``time, open, high, low, close`` columns.
            ``time`` must be tz-aware UTC.
        levels: union of ``MarkedLevel``s — see liquidity.py converters.
        killzone_window_utc: ``(start_utc, end_utc)``, half-open
            ``[start, end]`` (inclusive on both ends).
        sweep_buffer: ``INSTRUMENT_CONFIG[symbol]["sweep_buffer"]``.
        return_window_candles: ``SWEEP_RETURN_WINDOW_CANDLES``. Total
            candles checked = ``return_window_candles + 1`` (current
            candle plus this many lookahead candles).

    Returns:
        ``list[Sweep]`` ordered by ``sweep_candle_time_utc`` ascending.
    """
    if sweep_buffer < 0:
        raise ValueError(f"sweep_buffer must be >= 0, got {sweep_buffer}")
    if return_window_candles < 0:
        raise ValueError(f"return_window_candles must be >= 0, got {return_window_candles}")

    if len(df_m5) == 0 or not levels:
        return []

    start_utc, end_utc = killzone_window_utc
    times = pd.to_datetime(df_m5["time"], utc=True)
    in_kz = (times >= start_utc) & (times <= end_utc)
    if not in_kz.any():
        return []

    # Numpy-position iteration to avoid index/loc gymnastics.
    n = len(df_m5)
    highs = df_m5["high"].to_numpy(dtype="float64")
    lows = df_m5["low"].to_numpy(dtype="float64")
    closes = df_m5["close"].to_numpy(dtype="float64")
    times_py = [pd.Timestamp(t).to_pydatetime() for t in times]
    in_kz_arr = in_kz.to_numpy()

    sweeps: list[Sweep] = []

    for i in range(n):
        if not in_kz_arr[i]:
            continue
        candle_low = lows[i]
        candle_high = highs[i]
        candle_time = times_py[i]

        for level in levels:
            if level.type == "low":
                # Bullish sweep candidate.
                if candle_low <= level.price - sweep_buffer:
                    return_pos = _find_return(
                        closes, n, i, return_window_candles, level.price, above=True
                    )
                    if return_pos is not None:
                        sweeps.append(
                            Sweep(
                                direction="bullish",
                                swept_level_price=level.price,
                                swept_level_type=level.label,
                                swept_level_strength=level.strength,
                                sweep_candle_time_utc=candle_time,
                                sweep_extreme_price=float(candle_low),
                                return_candle_time_utc=times_py[return_pos],
                                excursion=float(level.price - candle_low),
                            )
                        )
            elif level.type == "high":
                # Bearish sweep candidate.
                if candle_high >= level.price + sweep_buffer:
                    return_pos = _find_return(
                        closes, n, i, return_window_candles, level.price, above=False
                    )
                    if return_pos is not None:
                        sweeps.append(
                            Sweep(
                                direction="bearish",
                                swept_level_price=level.price,
                                swept_level_type=level.label,
                                swept_level_strength=level.strength,
                                sweep_candle_time_utc=candle_time,
                                sweep_extreme_price=float(candle_high),
                                return_candle_time_utc=times_py[return_pos],
                                excursion=float(candle_high - level.price),
                            )
                        )

    sweeps.sort(key=lambda s: s.sweep_candle_time_utc)
    return sweeps


def _find_return(
    closes,
    n: int,
    sweep_pos: int,
    window: int,
    level_price: float,
    *,
    above: bool,
) -> int | None:
    """Return the position of the first candle whose close returns across the level.

    Checks positions ``sweep_pos, sweep_pos+1, ..., sweep_pos+window``
    (inclusive on both ends — the candle that did the sweeping itself is a
    valid return candidate per docs/01 §5: "within the same candle, OR…").

    Returns ``None`` if no return is observed within the window.
    """
    last = min(sweep_pos + window + 1, n)
    for k in range(sweep_pos, last):
        c = closes[k]
        if above and c >= level_price:
            return k
        if not above and c <= level_price:
            return k
    return None
