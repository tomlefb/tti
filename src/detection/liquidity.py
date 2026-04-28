"""Liquidity marking — Asian range, PDH/PDL, swing levels, equal H/L.

Public API:

- ``mark_asian_range`` — pure logic; high/low of the Asia session.
- ``mark_pdh_pdl`` — pure logic; previous-trading-day high/low (walks back
  through gaps for weekends / holidays).
- ``mark_swing_levels`` — multi-TF confluence promotion (H4 ∩ H1) per
  ``calibration/runs/FINAL_swing_calibration.md``. Calibrated heuristic.
- ``find_equal_highs_lows`` — single-link clustering of same-type swings
  within a price tolerance.

Plus a small set of converters that turn the structured outputs of the four
markers into a unified ``MarkedLevel`` list which is what
``src.detection.sweep.detect_sweeps`` consumes.

Pure functions: take data + parameters, return data. No I/O. The caller
(CLI script, integration test) loads ``config.settings`` and passes
parameter values explicitly.

Heuristic choices documented inline per docs/07 §1.3.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import pandas as pd

from .swings import find_swings

logger = logging.getLogger(__name__)

_TZ_PARIS = ZoneInfo("Europe/Paris")
_TZ_UTC = ZoneInfo("UTC")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AsianRange:
    """High and low of one trading day's Asia session.

    ``date`` is the calendar date of the morning that hosts the session
    (Asia 02:00–06:00 Paris on the same day, converted to UTC).
    """

    date: date
    asian_high: float
    asian_low: float
    asian_high_time_utc: datetime
    asian_low_time_utc: datetime


@dataclass(frozen=True)
class DailyLevels:
    """Previous-trading-day high and low.

    ``source_date`` is the actual D1 candle used; differs from
    ``target_date - 1`` when yesterday's candle is missing (weekend,
    public holiday).
    """

    target_date: date
    pdh: float
    pdl: float
    source_date: date


@dataclass(frozen=True)
class SwingLevel:
    """One swing level returned by ``mark_swing_levels``.

    ``strength`` taxonomy:
        - ``"major"``         — H4 swing with a confirming H1 swing nearby
                                  (multi-TF confluence). High order density.
        - ``"major_h4_only"`` — H4 swing without an H1 confirmation.
        - ``"minor"``         — H1 swing without an H4 confirmation; only
                                  useful as fallback context.
    """

    type: Literal["high", "low"]
    price: float
    time_utc: datetime
    timeframe: Literal["H4", "H1"]
    strength: Literal["major", "major_h4_only", "minor"]
    touches: int


@dataclass(frozen=True)
class EqualLevel:
    """A cluster of two or more same-type swing levels at near-equal prices."""

    type: Literal["high", "low"]
    cluster_avg_price: float
    member_levels: list[SwingLevel] = field(default_factory=list)
    cluster_min_price: float = 0.0
    cluster_max_price: float = 0.0


@dataclass(frozen=True)
class MarkedLevel:
    """Unified abstraction consumed by ``detect_sweeps``.

    Each AsianRange / DailyLevels / SwingLevel / EqualLevel converts into
    one or two MarkedLevels via the helpers in this module.

    ``strength`` extends the SwingLevel taxonomy with ``"structural"`` for
    Asian range and PDH/PDL — these are not swings but always-strong
    liquidity anchors per docs/01 §4.
    """

    price: float
    type: Literal["high", "low"]
    label: str
    strength: Literal["major", "major_h4_only", "minor", "structural"]


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def paris_session_to_utc(
    target_date: date,
    session: tuple[int, int, int, int],
) -> tuple[datetime, datetime]:
    """Convert ``(target_date, (sh, sm, eh, em))`` Paris-local → UTC range.

    ``zoneinfo`` handles DST automatically; do NOT subtract a fixed offset.

    Args:
        target_date: calendar date of the session in Paris.
        session: ``(start_hour, start_min, end_hour, end_min)`` tuple.

    Returns:
        ``(start_utc, end_utc)`` as timezone-aware ``datetime`` objects.
    """
    sh, sm, eh, em = session
    start_paris = datetime(
        target_date.year, target_date.month, target_date.day, sh, sm, tzinfo=_TZ_PARIS
    )
    end_paris = datetime(
        target_date.year, target_date.month, target_date.day, eh, em, tzinfo=_TZ_PARIS
    )
    return start_paris.astimezone(_TZ_UTC), end_paris.astimezone(_TZ_UTC)


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------


def mark_asian_range(
    df_m5: pd.DataFrame,
    target_date: date,
    session_asia: tuple[int, int, int, int] = (2, 0, 6, 0),
) -> AsianRange | None:
    """High/low of the Asia session for ``target_date``.

    Asia window is the half-open interval ``[start_utc, end_utc)`` derived
    from ``session_asia`` (Paris) on ``target_date``. The detector looks at
    every M5 candle whose ``time`` falls in this window.

    Args:
        df_m5: M5 OHLC frame with UTC ``time`` column.
        target_date: calendar date in Paris of the morning hosting the session.
        session_asia: Paris-local session tuple. Default ``(2, 0, 6, 0)`` —
            matches ``config.settings.SESSION_ASIA``.

    Returns:
        ``AsianRange`` or ``None`` if no M5 candle exists in the window
        (weekend Sunday night, public holiday). A warning is logged.
    """
    start_utc, end_utc = paris_session_to_utc(target_date, session_asia)
    times = pd.to_datetime(df_m5["time"], utc=True)
    mask = (times >= start_utc) & (times < end_utc)
    window = df_m5.loc[mask]
    if len(window) == 0:
        logger.warning("no M5 data for Asia session of %s", target_date.isoformat())
        return None
    high_pos = window["high"].idxmax()
    low_pos = window["low"].idxmin()
    return AsianRange(
        date=target_date,
        asian_high=float(window.at[high_pos, "high"]),
        asian_low=float(window.at[low_pos, "low"]),
        asian_high_time_utc=pd.Timestamp(window.at[high_pos, "time"]).to_pydatetime(),
        asian_low_time_utc=pd.Timestamp(window.at[low_pos, "time"]).to_pydatetime(),
    )


def mark_pdh_pdl(
    df_d1: pd.DataFrame,
    target_date: date,
    *,
    max_walkback_days: int = 7,
) -> DailyLevels | None:
    """Previous-trading-day high and low, walking back through gaps.

    For ``target_date``, look first at the D1 candle dated ``target_date - 1``.
    If missing (weekend, public holiday), step back one day at a time up
    to ``max_walkback_days``. The actual date used is reported in
    ``DailyLevels.source_date``.

    A D1 candle is "for date X" iff its ``time.date() == X`` in UTC. MT5
    typically dates D1 candles at the broker's session open which is close
    enough to UTC midnight for our purposes; if a broker idiosyncrasy
    requires otherwise, revisit here.

    Args:
        df_d1: D1 OHLC frame.
        target_date: today's date.
        max_walkback_days: cap on the gap to tolerate. Default ``7``.

    Returns:
        ``DailyLevels`` with ``source_date`` set, or ``None`` if no D1
        candle exists within the walkback window.
    """
    times = pd.to_datetime(df_d1["time"], utc=True).dt.date
    candidate = target_date - timedelta(days=1)
    for _ in range(max_walkback_days):
        matches = df_d1.loc[times == candidate]
        if len(matches) > 0:
            row = matches.iloc[0]
            return DailyLevels(
                target_date=target_date,
                pdh=float(row["high"]),
                pdl=float(row["low"]),
                source_date=candidate,
            )
        candidate -= timedelta(days=1)
    logger.warning(
        "no D1 candle within %d days before %s",
        max_walkback_days,
        target_date.isoformat(),
    )
    return None


def _significant_swings_with_time(
    swings_df: pd.DataFrame,
    df_source: pd.DataFrame,
    as_of_utc: datetime,
) -> list[dict]:
    """Materialize the (type, price, time) tuples filtered by ``as_of_utc``."""
    sig = swings_df[swings_df["swing_type"].notna()]
    if sig.empty:
        return []
    times = pd.to_datetime(df_source.loc[sig.index, "time"], utc=True)
    out: list[dict] = []
    for t, swing_type, swing_price in zip(
        times, sig["swing_type"], sig["swing_price"], strict=False
    ):
        py_t = t.to_pydatetime()
        if py_t > as_of_utc:
            continue
        out.append({"type": swing_type, "price": float(swing_price), "time": py_t})
    return out


def mark_swing_levels(
    df_h4: pd.DataFrame,
    df_h1: pd.DataFrame,
    as_of_utc: datetime,
    *,
    lookback_h4: int,
    lookback_h1: int,
    min_amplitude_atr_mult_h4: float,
    min_amplitude_atr_mult_h1: float,
    n_swings: int = 5,
    h4_h1_time_tolerance_h4_candles: int = 2,
    h4_h1_price_tolerance_fraction: float = 0.001,
    atr_period: int = 14,
) -> list[SwingLevel]:
    """Multi-TF confluence promotion. See FINAL_swing_calibration.md §H1.

    Algorithm:
        a) Compute significant swings on H4 and H1 using
           ``src.detection.swings.find_swings``.
        b) Drop any swing whose timestamp exceeds ``as_of_utc`` — this
           prevents future-data leakage in live mode.
        c) For each of the last ``n_swings`` H4 swings: look for a same-type
           H1 swing within ±``h4_h1_time_tolerance_h4_candles`` H4 candles
           AND within ``h4_h1_price_tolerance_fraction`` price tolerance.
           If a match exists → emit ``"major"`` (touches=2). If not →
           ``"major_h4_only"`` (touches=1).
        d) For each of the last ``n_swings * 2`` H1 swings that did NOT
           match an H4 swing in (c), emit ``"minor"`` (touches=1).

    Heuristic per docs/07 §1.3 — alternative confluence rules (e.g. include
    Asian range / PDH/PDL in the matching) are valid; revisit on Sprint 2
    empirical review.

    Args:
        df_h4: H4 OHLC frame.
        df_h1: H1 OHLC frame.
        as_of_utc: cutoff; future swings are dropped.
        lookback_h4 / lookback_h1: fractal lookback per timeframe.
        min_amplitude_atr_mult_h4: ATR-amplitude filter multiplier on H4.
        min_amplitude_atr_mult_h1: ATR-amplitude filter multiplier on H1.
        n_swings: how many trailing H4 swings to consider.
        h4_h1_time_tolerance_h4_candles: matching window in H4 candles.
        h4_h1_price_tolerance_fraction: matching window in price (fraction).
        atr_period: ATR window for the amplitude filter.

    Returns:
        ``list[SwingLevel]`` sorted ``time_utc`` descending.
    """
    swings_h4 = find_swings(
        df_h4,
        lookback=lookback_h4,
        min_amplitude_atr_mult=min_amplitude_atr_mult_h4,
        atr_period=atr_period,
    )
    swings_h1 = find_swings(
        df_h1,
        lookback=lookback_h1,
        min_amplitude_atr_mult=min_amplitude_atr_mult_h1,
        atr_period=atr_period,
    )

    h4_sigs = _significant_swings_with_time(swings_h4, df_h4, as_of_utc)
    h1_sigs = _significant_swings_with_time(swings_h1, df_h1, as_of_utc)

    h4_recent = h4_sigs[-n_swings:]
    h1_minor_start = max(0, len(h1_sigs) - n_swings * 2)

    time_tol = timedelta(hours=4 * h4_h1_time_tolerance_h4_candles)
    matched_h1: set[int] = set()
    out: list[SwingLevel] = []

    for h4_sw in h4_recent:
        best_idx: int | None = None
        best_dt: timedelta | None = None
        price_tol = h4_h1_price_tolerance_fraction * abs(h4_sw["price"])
        for j, h1_sw in enumerate(h1_sigs):
            if j in matched_h1:
                continue
            if h1_sw["type"] != h4_sw["type"]:
                continue
            dt = abs(h1_sw["time"] - h4_sw["time"])
            if dt > time_tol:
                continue
            if abs(h1_sw["price"] - h4_sw["price"]) > price_tol:
                continue
            if best_idx is None or dt < best_dt:
                best_idx = j
                best_dt = dt
        if best_idx is not None:
            matched_h1.add(best_idx)
            out.append(
                SwingLevel(
                    type=h4_sw["type"],
                    price=h4_sw["price"],
                    time_utc=h4_sw["time"],
                    timeframe="H4",
                    strength="major",
                    touches=2,
                )
            )
        else:
            out.append(
                SwingLevel(
                    type=h4_sw["type"],
                    price=h4_sw["price"],
                    time_utc=h4_sw["time"],
                    timeframe="H4",
                    strength="major_h4_only",
                    touches=1,
                )
            )

    for j in range(h1_minor_start, len(h1_sigs)):
        if j in matched_h1:
            continue
        h1_sw = h1_sigs[j]
        out.append(
            SwingLevel(
                type=h1_sw["type"],
                price=h1_sw["price"],
                time_utc=h1_sw["time"],
                timeframe="H1",
                strength="minor",
                touches=1,
            )
        )

    out.sort(key=lambda s: s.time_utc, reverse=True)
    return out


def find_equal_highs_lows(
    swing_levels: list[SwingLevel],
    equal_hl_tolerance: float,
) -> list[EqualLevel]:
    """Cluster same-type swing levels whose prices are within ``tolerance``.

    Single-link clustering on sorted prices: a new cluster is started
    whenever the gap between adjacent prices exceeds ``equal_hl_tolerance``.
    Singletons are dropped — a cluster must have ≥ 2 members.

    Note: ``equal_hl_tolerance`` lives in
    ``config.settings.INSTRUMENT_CONFIG[symbol]["equal_hl_tolerance"]``;
    it is the caller's job to look it up. Keeping the function pure
    (numeric tolerance, not symbol) preserves the Sprint 1 detector
    pattern — see ``CLAUDE.md`` and ``docs/04`` "Pure functions".

    Args:
        swing_levels: list of ``SwingLevel`` (mixed types are OK; this
            function partitions by ``type`` internally).
        equal_hl_tolerance: max distance between adjacent same-type prices
            to keep them in one cluster.

    Returns:
        ``list[EqualLevel]`` — one per cluster of ≥ 2 members.
    """
    if equal_hl_tolerance < 0:
        raise ValueError(f"equal_hl_tolerance must be >= 0, got {equal_hl_tolerance}")

    out: list[EqualLevel] = []
    for swing_type in ("high", "low"):
        same_type = sorted([s for s in swing_levels if s.type == swing_type], key=lambda s: s.price)
        if len(same_type) < 2:
            continue
        cluster: list[SwingLevel] = [same_type[0]]
        for s in same_type[1:]:
            if s.price - cluster[-1].price <= equal_hl_tolerance:
                cluster.append(s)
            else:
                if len(cluster) >= 2:
                    out.append(_build_equal_level(swing_type, cluster))
                cluster = [s]
        if len(cluster) >= 2:
            out.append(_build_equal_level(swing_type, cluster))
    return out


def _build_equal_level(swing_type: Literal["high", "low"], members: list[SwingLevel]) -> EqualLevel:
    prices = [m.price for m in members]
    return EqualLevel(
        type=swing_type,
        cluster_avg_price=sum(prices) / len(prices),
        member_levels=list(members),
        cluster_min_price=min(prices),
        cluster_max_price=max(prices),
    )


# ---------------------------------------------------------------------------
# Converters to MarkedLevel (consumed by detect_sweeps)
# ---------------------------------------------------------------------------


def asian_range_to_marked_levels(asian_range: AsianRange | None) -> list[MarkedLevel]:
    if asian_range is None:
        return []
    return [
        MarkedLevel(
            price=asian_range.asian_high,
            type="high",
            label="asian_high",
            strength="structural",
        ),
        MarkedLevel(
            price=asian_range.asian_low,
            type="low",
            label="asian_low",
            strength="structural",
        ),
    ]


def daily_levels_to_marked_levels(daily_levels: DailyLevels | None) -> list[MarkedLevel]:
    if daily_levels is None:
        return []
    return [
        MarkedLevel(price=daily_levels.pdh, type="high", label="pdh", strength="structural"),
        MarkedLevel(price=daily_levels.pdl, type="low", label="pdl", strength="structural"),
    ]


def swing_level_to_marked_level(level: SwingLevel) -> MarkedLevel:
    return MarkedLevel(
        price=level.price,
        type=level.type,
        label=f"swing_{level.timeframe.lower()}_{level.type}",
        strength=level.strength,
    )


def equal_level_to_marked_level(level: EqualLevel) -> MarkedLevel:
    # Equal H/L = high-priority structural cluster per docs/01 §4 ("These
    # are high-priority liquidity zones"). Reported as "major" so downstream
    # treats them on par with multi-TF confluent swings.
    return MarkedLevel(
        price=level.cluster_avg_price,
        type=level.type,
        label=f"equal_{level.type}",
        strength="major",
    )
