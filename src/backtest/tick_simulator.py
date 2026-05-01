"""Tick-by-tick simulation of the production APScheduler.

Phase B of the look-ahead remediation: before this module, calibration
backtests called ``build_setup_candidates`` once per killzone with
``now_utc=None``, which exercised the (pre-fix) legacy code path and
let the detector read post-MSS data. With the Phase A detector fix in
place, the leak-free path requires a ``now_utc`` argument that bounds
every forward-looking sub-search; ``simulate_killzone_ticks`` provides
that by iterating 5-minute scheduler firings across the killzone and
calling the detector at each tick.

The simulator's contract is identity-locked, first-emission-wins:

- A setup is identified by the same tuple the look-ahead audit uses,
  ``(symbol, killzone, direction, mss_confirm_candle_time_utc,
  sweep.sweep_candle_time_utc, swept_level_price)``. Two distinct
  sweeps that produce MSSs at the same candle yield independent
  identities.
- The first tick at which a given identity surfaces is the version
  emitted. Later ticks may produce a "different" copy of the same
  identity (e.g. with a tighter FVG that becomes detectable as more
  M5 candles close); those copies are dropped. This mirrors the
  production scheduler, which notifies the operator at the moment a
  setup becomes detectable and never re-notifies.

Quality filtering (``NOTIFY_QUALITIES``) is the caller's
responsibility — the simulator emits every setup the detector
produces. This keeps the simulator agnostic to the operator's
notify-gate policy and lets B-grade rejections be journaled or
analysed independently.

The first usable scheduler tick within a killzone whose start is
``T0`` is ``T0 + tick_interval_minutes`` (the bar that opened at
``T0`` has just closed). The last usable tick is
``T_end + tick_interval_minutes`` — that bar opens at ``T_end`` and
closes one interval later, which is the latest moment a setup whose
``mss_confirm`` lands at exactly ``T_end`` becomes observable.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from src.detection.setup import Setup, SetupSettings, build_setup_candidates


def _identity(s: Setup) -> tuple:
    """Stable identity tuple — matches ``calibration/audit_lookahead.py``.

    POI / entry / SL / TP are deliberately NOT in the key: a setup can
    surface with a "weaker" POI at tick T (e.g. an OrderBlock fallback
    when no FVG is yet detectable) and a "stronger" POI at T+5min once
    a c3 candle has closed. Both versions share the same identity, and
    we lock on the first.
    """
    return (
        s.symbol,
        s.killzone,
        s.direction,
        s.mss.mss_confirm_candle_time_utc,
        s.sweep.sweep_candle_time_utc,
        round(float(s.swept_level_price), 6),
    )


def simulate_killzone_ticks(
    df_h4: pd.DataFrame,
    df_h1: pd.DataFrame,
    df_m5: pd.DataFrame,
    df_d1: pd.DataFrame,
    killzone_start_utc: datetime,
    killzone_end_utc: datetime,
    symbol: str,
    settings: SetupSettings,
    *,
    tick_interval_minutes: int = 5,
) -> list[Setup]:
    """Simulate the production scheduler across one killzone window.

    For every ``tick_interval_minutes`` instant in
    ``(killzone_start_utc, killzone_end_utc + tick_interval_minutes]``
    inclusive, calls
    ``build_setup_candidates(..., now_utc=tick)``. Setups whose
    ``mss.mss_confirm_candle_time_utc`` falls inside the killzone
    window are accumulated, deduped by :func:`_identity`, and the
    earliest-tick copy of each identity is kept.

    Setups whose ``mss_confirm`` lands outside ``[killzone_start_utc,
    killzone_end_utc]`` are ignored — those belong to the OTHER
    killzone of the same target date, which the caller simulates
    independently. Filtering here keeps each
    ``simulate_killzone_ticks`` call self-contained and lets two
    parallel runs (London and NY) compose without dedup conflicts.

    Args:
        df_h4 / df_h1 / df_m5 / df_d1: OHLC frames the detector
            consumes. They must already be wide enough to cover the
            killzone window plus the detector's own internal lookback
            (60 days is safe; ATR uses 14 bars max).
        killzone_start_utc: inclusive start of the killzone.
        killzone_end_utc: inclusive end of the killzone.
        symbol: instrument label, key in ``settings.INSTRUMENT_CONFIG``.
        settings: any ``SetupSettings``-shaped object.
        tick_interval_minutes: scheduler firing cadence. Default 5
            matches the production APScheduler cron.

    Returns:
        ``list[Setup]`` sorted by ``mss_confirm_candle_time_utc``
        ascending. Each entry is the first-emission copy of its
        identity. Empty if no setup confirms in the killzone.
    """
    target_date = (
        pd.Timestamp(killzone_start_utc)
        .tz_convert("Europe/Paris")
        .date()
    )
    interval = timedelta(minutes=tick_interval_minutes)
    # First tick where the bar that opened at killzone_start has closed.
    tick = killzone_start_utc + interval
    # Last tick where a bar that opened at killzone_end has closed.
    last_tick = killzone_end_utc + interval

    seen: set[tuple] = set()
    out: list[Setup] = []

    while tick <= last_tick:
        setups = build_setup_candidates(
            df_h4=df_h4,
            df_h1=df_h1,
            df_m5=df_m5,
            df_d1=df_d1,
            target_date=target_date,
            symbol=symbol,
            settings=settings,
            now_utc=tick,
        )
        for s in setups:
            if not (
                killzone_start_utc
                <= s.mss.mss_confirm_candle_time_utc
                <= killzone_end_utc
            ):
                continue
            key = _identity(s)
            if key in seen:
                continue
            seen.add(key)
            out.append(s)
        tick += interval

    out.sort(key=lambda s: s.mss.mss_confirm_candle_time_utc)
    return out


def simulate_target_date(
    df_h4: pd.DataFrame,
    df_h1: pd.DataFrame,
    df_m5: pd.DataFrame,
    df_d1: pd.DataFrame,
    target_date,
    symbol: str,
    settings: SetupSettings,
    *,
    tick_interval_minutes: int = 5,
) -> list[Setup]:
    """Convenience: run :func:`simulate_killzone_ticks` for both London
    and NY killzones of ``target_date`` and concatenate the result
    (London first, then NY). The two killzones produce disjoint
    identity sets by construction (different ``killzone`` field), so
    no cross-killzone dedup is needed.
    """
    from src.detection.liquidity import paris_session_to_utc

    out: list[Setup] = []
    for kz_session in (settings.KILLZONE_LONDON, settings.KILLZONE_NY):
        kz_start_utc, kz_end_utc = paris_session_to_utc(target_date, kz_session)
        out.extend(
            simulate_killzone_ticks(
                df_h4=df_h4,
                df_h1=df_h1,
                df_m5=df_m5,
                df_d1=df_d1,
                killzone_start_utc=kz_start_utc,
                killzone_end_utc=kz_end_utc,
                symbol=symbol,
                settings=settings,
                tick_interval_minutes=tick_interval_minutes,
            )
        )
    return out
