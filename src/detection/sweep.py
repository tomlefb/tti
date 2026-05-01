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
from datetime import datetime, timedelta
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
    dedupe: bool = True,
    dedupe_time_window_minutes: int = 30,
    dedupe_price_tolerance_fraction: float = 0.001,
    now_utc: datetime | None = None,
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
        dedupe: when ``True`` (default), collapse near-duplicate sweeps via
            ``deduplicate_sweeps`` before returning. Sprint 2 closing note
            recommended this — without it, downstream MSS/FVG detection
            re-runs on dozens of essentially identical sweep events per
            killzone.
        dedupe_time_window_minutes: ``SWEEP_DEDUP_TIME_WINDOW_MINUTES``;
            forwarded to ``deduplicate_sweeps``.
        dedupe_price_tolerance_fraction: ``SWEEP_DEDUP_PRICE_TOLERANCE_FRACTION``;
            forwarded to ``deduplicate_sweeps``.
        now_utc: optional production scheduler tick. When set, only
            sweeps whose **return candle has closed by** ``now_utc``
            (i.e. ``return_candle.time + M5 timeframe <= now_utc``) are
            emitted. The dedupe pool is therefore restricted to those.
            ``None`` (default) is the legacy unconstrained mode used by
            tests and the pre-fix backtest harness. Without this bound
            the dedupe pool spans the entire killzone, which leaks
            future data into historical detections (see the look-ahead
            audit at calibration/runs/FINAL_lookahead_audit_2026-05-01.md).

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

    # Real-time bound: a sweep is only observable once its return candle
    # has closed. Infer the M5 timeframe from candle spacing rather than
    # hard-coding 5min so the function stays usable on any timeframe.
    m5_timeframe: pd.Timedelta | None = None
    if now_utc is not None and n >= 2:
        diffs = pd.Series(times_py[1:]) - pd.Series(times_py[:-1])
        m5_timeframe = pd.Timedelta(diffs.median())

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
                        return_time = times_py[return_pos]
                        if (
                            now_utc is not None
                            and m5_timeframe is not None
                            and return_time + m5_timeframe > now_utc
                        ):
                            continue
                        sweeps.append(
                            Sweep(
                                direction="bullish",
                                swept_level_price=level.price,
                                swept_level_type=level.label,
                                swept_level_strength=level.strength,
                                sweep_candle_time_utc=candle_time,
                                sweep_extreme_price=float(candle_low),
                                return_candle_time_utc=return_time,
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
                        return_time = times_py[return_pos]
                        if (
                            now_utc is not None
                            and m5_timeframe is not None
                            and return_time + m5_timeframe > now_utc
                        ):
                            continue
                        sweeps.append(
                            Sweep(
                                direction="bearish",
                                swept_level_price=level.price,
                                swept_level_type=level.label,
                                swept_level_strength=level.strength,
                                sweep_candle_time_utc=candle_time,
                                sweep_extreme_price=float(candle_high),
                                return_candle_time_utc=return_time,
                                excursion=float(candle_high - level.price),
                            )
                        )

    sweeps.sort(key=lambda s: s.sweep_candle_time_utc)
    if dedupe:
        sweeps = deduplicate_sweeps(
            sweeps,
            time_window_minutes=dedupe_time_window_minutes,
            price_tolerance_fraction=dedupe_price_tolerance_fraction,
        )
    return sweeps


def deduplicate_sweeps(
    sweeps: list[Sweep],
    time_window_minutes: int = 30,
    price_tolerance_fraction: float = 0.001,
) -> list[Sweep]:
    """Collapse multiple ``Sweep`` objects describing the same structural event.

    Two sweeps are considered duplicates iff:

    - same ``direction`` (bullish/bullish or bearish/bearish), AND
    - their ``swept_level_price`` differ by no more than
      ``price_tolerance_fraction × (|p1| + |p2|) / 2`` — symmetric form,
      see note below, AND
    - their ``sweep_candle_time_utc`` differ by no more than
      ``time_window_minutes`` minutes.

    The relation is grouped via union-find: A~B and B~C ⇒ A, B, C all
    in the same cluster, even if A and C alone fall outside the time
    window. Within each cluster the **largest-excursion** sweep is kept
    (the deepest sweep — most likely to have grabbed the most liquidity);
    ties are broken by earliest ``sweep_candle_time_utc``.

    Returned list is sorted by ``sweep_candle_time_utc`` ascending.

    Heuristic per docs/07 §1.3. Defaults rationale:

    - 30-minute window: covers most multi-touch sweep events on M5 within
      a single killzone phase. Alternatives: per-killzone-phase grouping
      (cleaner cut but loses fine-grained intra-phase events), or
      excursion-weighted clustering (more complex, marginal gain).
    - 0.1% price tolerance: matches the ``H4_H1_PRICE_TOLERANCE_FRACTION``
      used by the Sprint 2 multi-TF confluence promotion and the calibration
      harness — keeping the same relative tolerance across the codebase
      avoids cross-tuning surprises.

    Note on symmetry: this uses ``|p1 - p2| <= tol * (|p1| + |p2|) / 2``,
    which is symmetric in (p1, p2). ``mark_swing_levels`` uses the
    asymmetric ``tol * abs(annotated_price)`` because it matches a
    detected swing against an annotated reference price (the annotation
    is canonical). Here we compare two detector outputs where neither
    is canonical, so symmetry is the right default. Do NOT change
    ``mark_swing_levels``.

    Args:
        sweeps: any list of ``Sweep`` objects. Empty list is allowed.
        time_window_minutes: pairwise time window for cluster membership.
            Must be ``>= 0``.
        price_tolerance_fraction: pairwise relative price tolerance.
            Must be ``>= 0``.

    Returns:
        Deduplicated ``list[Sweep]`` sorted by time ascending.
    """
    if time_window_minutes < 0:
        raise ValueError(f"time_window_minutes must be >= 0, got {time_window_minutes}")
    if price_tolerance_fraction < 0:
        raise ValueError(f"price_tolerance_fraction must be >= 0, got {price_tolerance_fraction}")
    if len(sweeps) <= 1:
        return list(sweeps)

    time_tol = timedelta(minutes=time_window_minutes)
    n = len(sweeps)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        for j in range(i + 1, n):
            si, sj = sweeps[i], sweeps[j]
            if si.direction != sj.direction:
                continue
            dt = abs(si.sweep_candle_time_utc - sj.sweep_candle_time_utc)
            if dt > time_tol:
                continue
            avg_abs_price = (abs(si.swept_level_price) + abs(sj.swept_level_price)) / 2.0
            price_tol = price_tolerance_fraction * avg_abs_price
            if abs(si.swept_level_price - sj.swept_level_price) > price_tol:
                continue
            union(i, j)

    clusters: dict[int, list[int]] = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)

    kept: list[Sweep] = []
    for members in clusters.values():
        # Largest excursion wins; tie-break on earliest time.
        best_idx = max(
            members,
            key=lambda k: (sweeps[k].excursion, -sweeps[k].sweep_candle_time_utc.timestamp()),
        )
        kept.append(sweeps[best_idx])

    kept.sort(key=lambda s: s.sweep_candle_time_utc)
    return kept


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
