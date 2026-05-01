"""Pure scheduler jobs — detection cycle, pre-killzone bias, heartbeats,
outcome reconciliation.

Each function takes its dependencies (``mt5_client``, ``journal_session_factory``,
``notifier``, ``settings``) explicitly so unit tests can drive them with
in-memory SQLite + mock MT5 + a no-network notifier.

The runner (``src/scheduler/runner.py``) wires APScheduler triggers to
these functions.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.detection.bias import compute_daily_bias
from src.detection.liquidity import paris_session_to_utc
from src.detection.setup import RejectedCandidate, Setup, build_setup_candidates
from src.journal.models import SetupRow
from src.journal.outcome_tracker import reconcile_outcomes
from src.journal.repository import (
    get_daily_state,
    insert_setup,
    upsert_daily_state,
)
from src.mt5_client.client import MT5Client
from src.mt5_client.exceptions import MT5Error
from src.notification.chart_renderer import render_setup_chart
from src.notification.telegram_bot import TelegramNotifier
from src.scheduler.hard_stops import BlockReason, is_blocked

logger = logging.getLogger(__name__)

_TZ_PARIS = ZoneInfo("Europe/Paris")
_TZ_UTC = ZoneInfo("UTC")


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class CycleReport:
    """Aggregate of one detection cycle for the operator's logs."""

    pairs_processed: int = 0
    setups_detected: int = 0
    setups_notifiable: int = 0  # subset of detected that pass the quality filter
    setups_notified: int = 0
    setups_rejected: int = 0
    blocks: dict[str, str] = field(default_factory=dict)  # pair → BlockReason.code
    errors: dict[str, str] = field(default_factory=dict)  # pair → repr(exc)


# ---------------------------------------------------------------------------
# Settings protocol
# ---------------------------------------------------------------------------
# A lightweight Protocol would duplicate three other surfaces; jobs.py
# accepts ``Any`` and reads the keys it needs at runtime. The runner
# documents the full settings.py contract.
SchedulerSettings = Any


# ---------------------------------------------------------------------------
# Killzone helpers
# ---------------------------------------------------------------------------


def current_killzone(
    now_utc: datetime, settings: SchedulerSettings
) -> Literal["london", "ny", "none"]:
    """Return the killzone enclosing ``now_utc``, or ``'none'``.

    Uses Paris-local boundaries via :func:`paris_session_to_utc` so DST
    is handled automatically. The interval is half-open
    ``[start, end)`` — boundary policy matches Sprint 4's killzone gating
    in the orchestrator.
    """
    today_paris = now_utc.astimezone(_TZ_PARIS).date()
    london_start, london_end = paris_session_to_utc(today_paris, settings.KILLZONE_LONDON)
    ny_start, ny_end = paris_session_to_utc(today_paris, settings.KILLZONE_NY)
    if london_start <= now_utc < london_end:
        return "london"
    if ny_start <= now_utc < ny_end:
        return "ny"
    return "none"


# ---------------------------------------------------------------------------
# Detection cycle
# ---------------------------------------------------------------------------


def run_detection_cycle(
    mt5_client: MT5Client,
    journal_session_factory: Callable[[], Session],
    notifier: TelegramNotifier,
    settings: SchedulerSettings,
    *,
    now_utc: datetime,
    chart_send_callback: Callable | None = None,
    place_order_callback: Callable | None = None,
) -> CycleReport:
    """Run one detection cycle for every watched pair.

    Per pair:

    1. Determine the current killzone. If none, log+skip (the scheduler
       should not have triggered the cycle outside a killzone — log
       a warning).
    2. Hard-stop check via :func:`hard_stops.is_blocked`. If blocked,
       record the reason and skip.
    3. Fetch OHLC for D1/H4/H1/M5 with sensible default windows.
    4. Run :func:`build_setup_candidates` with ``return_rejected=True``.
    5. Persist accepted setups (``was_notified=True``) and rejected
       candidates (``was_notified=False``).
    6. Render chart + format caption + ``notifier.send_setup`` for each
       accepted setup.

    Per-pair errors are caught and logged; one failing pair does NOT
    abort the cycle.

    ``chart_send_callback`` is an optional async hook used by tests /
    dry-run to capture send_setup calls without hitting Telegram.
    """
    report = CycleReport()
    kz = current_killzone(now_utc, settings)
    if kz == "none":
        logger.warning(
            "run_detection_cycle invoked outside any killzone (now_utc=%s) — "
            "scheduler misconfigured? Returning empty report.",
            now_utc.isoformat(),
        )
        return report

    target_date = now_utc.astimezone(_TZ_PARIS).date()

    for pair in settings.WATCHED_PAIRS:
        report.pairs_processed += 1
        try:
            with journal_session_factory() as session:
                block = is_blocked(
                    session,
                    mt5_client,
                    settings,
                    pair=pair,
                    now_utc=now_utc,
                )
            if block is not None:
                logger.info("hard_stop blocked %s: %s — %s", pair, block.code, block.message)
                report.blocks[pair] = block.code
                _send_block_alert_if_needed(
                    block, mt5_client, journal_session_factory, notifier, now_utc
                )
                continue

            df_d1 = mt5_client.fetch_ohlc(pair, "D1", 100)
            df_h4 = mt5_client.fetch_ohlc(pair, "H4", 200)
            df_h1 = mt5_client.fetch_ohlc(pair, "H1", 200)
            df_m5 = mt5_client.fetch_ohlc(pair, "M5", 500)

            setups, rejected = build_setup_candidates(
                df_h4=df_h4,
                df_h1=df_h1,
                df_m5=df_m5,
                df_d1=df_d1,
                target_date=target_date,
                symbol=pair,
                settings=settings,
                return_rejected=True,
            )
            # Filter: only the killzone we're currently inside matters for
            # notification — setups from the other killzone today already
            # had their cycle.
            setups = [s for s in setups if s.killzone == kz]
            report.setups_detected += len(setups)
            report.setups_rejected += len(rejected)

            # Quality gating (Sprint 6.5): A+/A reach Telegram, B is
            # journaled-only. Default to A+/A if NOTIFY_QUALITIES absent
            # so older configs still behave conservatively.
            allowed_qualities = set(getattr(settings, "NOTIFY_QUALITIES", ("A+", "A")))
            notifiable_setups = [s for s in setups if s.quality in allowed_qualities]
            report.setups_notifiable += len(notifiable_setups)

            with journal_session_factory() as session:
                _persist_setups(
                    session,
                    setups,
                    rejected,
                    now_utc=now_utc,
                    notifiable_setups=notifiable_setups,
                )

            for setup in notifiable_setups:
                ok = _send_setup_notification(
                    setup, df_m5, settings, notifier, chart_send_callback=chart_send_callback
                )
                if ok:
                    report.setups_notified += 1
                # Sprint 7: auto-execution. The callback (wired in
                # ``runner.py``) handles its own pre-flight via
                # ``safe_guards.check_pre_trade``, so the cycle does
                # not pre-gate here. Failures are logged inside the
                # callback and never crash the cycle.
                if (
                    place_order_callback is not None
                    and getattr(settings, "AUTO_TRADING_ENABLED", False)
                ):
                    try:
                        place_order_callback(setup)
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "place_order_callback raised for %s — continuing",
                            setup.symbol,
                        )

        except MT5Error as exc:
            logger.error("MT5 error processing %s: %r", pair, exc, exc_info=True)
            report.errors[pair] = repr(exc)
        except Exception as exc:  # noqa: BLE001 — must not abort the cycle
            logger.exception("unexpected error processing %s", pair)
            report.errors[pair] = repr(exc)

    logger.info(
        "cycle complete: %d pairs, %d setups detected (%d notifiable, %d notified), "
        "%d rejected, %d blocked, %d errors",
        report.pairs_processed,
        report.setups_detected,
        report.setups_notifiable,
        report.setups_notified,
        report.setups_rejected,
        len(report.blocks),
        len(report.errors),
    )
    return report


def _persist_setups(
    session: Session,
    setups: list[Setup],
    rejected: list[RejectedCandidate],
    *,
    now_utc: datetime,
    notifiable_setups: list[Setup] | None = None,
) -> None:
    """Insert accepted + rejected candidates idempotently.

    ``was_notified`` is set per the quality gate: setups present in
    ``notifiable_setups`` are flagged ``True``; B-grade detections that
    were filtered out at notification time are flagged ``False`` so the
    operator can audit them later via the journal.
    """
    notifiable_ids = {id(s) for s in notifiable_setups} if notifiable_setups is not None else None
    for setup in setups:
        was_notified = True if notifiable_ids is None else id(setup) in notifiable_ids
        insert_setup(session, setup, was_notified=was_notified, detected_at=now_utc)
    for r in rejected:
        # Build a synthetic Setup-shaped row for journaling rejected candidates.
        # The journal schema requires several setup-only fields (entry, SL, TPs,
        # quality, …) — for rejected candidates these are not meaningful, so
        # we record a minimal RejectedSetupRow directly via the Sprint 5 surface
        # by routing through a tiny adapter (see ``_insert_rejected``).
        _insert_rejected(session, r, now_utc=now_utc)


def _insert_rejected(session: Session, candidate: RejectedCandidate, *, now_utc: datetime) -> None:
    """Insert a sweep-stage rejection into the ``setups`` table.

    The schema requires every setup-only column to be non-null. Since a
    rejected candidate has no entry/SL/TPs, we synthesize sentinels:
    prices = 0.0, RR = 0.0, quality = "B". The row is flagged
    ``was_notified=False`` and carries the rejection reason.
    """
    uid = f"rejected:{candidate.symbol}_{candidate.timestamp_utc.isoformat()}"
    existing = session.execute(
        select(SetupRow).where(SetupRow.setup_uid == uid)
    ).scalar_one_or_none()
    if existing is not None:
        return
    sweep_info = candidate.sweep_info or {}
    row = SetupRow(
        setup_uid=uid,
        detected_at=now_utc,
        timestamp_utc=candidate.timestamp_utc,
        symbol=candidate.symbol,
        killzone=current_killzone_of(candidate.timestamp_utc) or "none",
        direction=("long" if sweep_info.get("direction") == "bullish" else "short"),
        daily_bias="unknown",
        swept_level_type=str(sweep_info.get("swept_level_type", "unknown")),
        swept_level_strength="unknown",
        swept_level_price=float(sweep_info.get("swept_level_price", 0.0)),
        entry_price=0.0,
        stop_loss=0.0,
        tp1_price=0.0,
        tp1_rr=0.0,
        tp_runner_price=0.0,
        tp_runner_rr=0.0,
        target_level_type="unknown",
        poi_type="unknown",
        quality="B",
        confluences="[]",
        was_notified=False,
        rejection_reason=candidate.rejection_reason,
    )
    session.add(row)
    session.flush()


def current_killzone_of(ts_utc: datetime) -> str | None:
    """Best-effort label for which killzone (if any) a timestamp falls into.

    Used purely as a journal field for rejected candidates — when the
    settings object isn't readily available, falls back to ``None``.
    """
    return None


# ---------------------------------------------------------------------------
# Notification helper
# ---------------------------------------------------------------------------


def _send_setup_notification(
    setup: Setup,
    df_m5,
    settings: SchedulerSettings,
    notifier: TelegramNotifier,
    *,
    chart_send_callback: Callable | None = None,
) -> bool:
    """Render the chart and dispatch a Telegram notification.

    ``chart_send_callback`` (if provided) replaces the actual Telegram
    send — used by tests / dry-run mode.
    """
    chart_dir = Path(getattr(settings, "CHART_OUTPUT_DIR", "runtime_charts"))
    chart_dir.mkdir(parents=True, exist_ok=True)
    safe_ts = setup.timestamp_utc.strftime("%Y%m%dT%H%M%SZ")
    chart_path = chart_dir / f"{setup.symbol}_{safe_ts}_{setup.quality.replace('+', 'plus')}.png"

    levels = []  # Live cycle re-uses the same level set already used by
    # build_setup_candidates internally; the chart renderer accepts an
    # empty list and simply omits HTF overlays beyond swept/target.
    try:
        render_setup_chart(
            setup=setup,
            df_m5=df_m5,
            marked_levels=levels,
            output_path=chart_path,
            lookback_candles=getattr(settings, "CHART_LOOKBACK_CANDLES_M5", 80),
            lookforward_candles=getattr(settings, "CHART_LOOKFORWARD_CANDLES_M5", 10),
        )
    except Exception:  # noqa: BLE001 — never crash the cycle on chart issues
        logger.exception("chart render failed for %s — sending text-only fallback", setup.symbol)
        chart_path = None

    if chart_send_callback is not None:
        # Test / dry-run hook — synchronous capture, no Telegram.
        try:
            chart_send_callback(setup, chart_path)
        except Exception:  # noqa: BLE001
            logger.exception("chart_send_callback raised")
        return True

    if chart_path is None:
        _run_async(notifier.send_text(f"⚠️ chart render failed for {setup.symbol} {setup.quality}"))
    else:
        _run_async(notifier.send_setup(setup, chart_path))
    return True


def _send_block_alert_if_needed(
    block: BlockReason,
    mt5_client: MT5Client,
    journal_session_factory: Callable[[], Session],
    notifier: TelegramNotifier,
    now_utc: datetime,
) -> None:
    """Send a Telegram alert the first time a daily/max-loss block fires today.

    Tracks "already alerted" via ``daily_state.daily_stop_triggered`` so
    the operator does not get the same warning every 5 minutes.
    """
    if block.code not in ("daily_loss_reached", "max_loss_critical"):
        return

    today_paris = now_utc.astimezone(_TZ_PARIS).date()
    with journal_session_factory() as session:
        ds = get_daily_state(session, today_paris)
        if ds is not None and ds.daily_stop_triggered:
            return
        upsert_daily_state(session, today_paris, daily_stop_triggered=True)

    if block.code == "daily_loss_reached":
        text = f"🛑 Daily loss limit reached.\nSuspending notifications until tomorrow.\n{block.message}"
    else:
        text = (
            "🚨 CRITICAL: Max loss limit threshold reached.\n"
            "System suspended permanently.\n"
            "Manual reset required: edit config/settings.py, set "
            "MAX_LOSS_OVERRIDE = True, restart scheduler.\n"
            f"{block.message}"
        )
    _run_async(notifier.send_error(text))


def _run_async(coro) -> None:
    """Schedule a coroutine on the running loop, or run inline if none."""
    import asyncio

    try:
        asyncio.get_running_loop()
        asyncio.ensure_future(coro)
        return
    except RuntimeError:
        # No running loop — drive the coroutine to completion inline.
        asyncio.run(coro)


# ---------------------------------------------------------------------------
# Pre-killzone bias job
# ---------------------------------------------------------------------------


def run_pre_killzone_bias(
    mt5_client: MT5Client,
    journal_session_factory: Callable[[], Session],
    settings: SchedulerSettings,
    killzone: Literal["london", "ny"],
    *,
    now_utc: datetime,
) -> dict[str, str]:
    """Compute and cache the daily bias per pair into ``daily_state``.

    Triggered at 08:55 Paris (London) and 15:25 Paris (NY). The cached
    bias is what :func:`send_killzone_open_heartbeat` reads.

    Returns a ``{symbol: bias}`` dict for caller convenience (e.g. dry-run
    inspection).
    """
    target_date = now_utc.astimezone(_TZ_PARIS).date()
    out: dict[str, str] = {}

    for pair in settings.WATCHED_PAIRS:
        try:
            df_h4 = mt5_client.fetch_ohlc(pair, "H4", 200)
            df_h1 = mt5_client.fetch_ohlc(pair, "H1", 200)
            bias = compute_daily_bias(
                df_h4=df_h4,
                df_h1=df_h1,
                swing_lookback_h4=settings.SWING_LOOKBACK_H4,
                swing_lookback_h1=settings.SWING_LOOKBACK_H1,
                min_amplitude_atr_mult_h4=settings.MIN_SWING_AMPLITUDE_ATR_MULT_H4,
                min_amplitude_atr_mult_h1=settings.MIN_SWING_AMPLITUDE_ATR_MULT_H1,
                bias_swing_count=settings.BIAS_SWING_COUNT,
                require_h1_confirmation=settings.BIAS_REQUIRE_H1_CONFIRMATION,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("pre_killzone_bias: %s failed (%r)", pair, exc)
            bias = "unknown"

        out[pair] = bias
        column = f"bias_{pair.lower()}_{killzone}"
        with journal_session_factory() as session:
            try:
                upsert_daily_state(session, target_date, **{column: bias})
            except AttributeError:
                # Pair not in the daily_state schema — log and continue.
                logger.warning("daily_state has no column for %s — skipping cache", column)

    logger.info("pre_killzone_bias %s: %s", killzone, out)
    return out


# ---------------------------------------------------------------------------
# Heartbeats
# ---------------------------------------------------------------------------


def send_killzone_open_heartbeat(
    notifier: TelegramNotifier,
    journal_session_factory: Callable[[], Session],
    settings: SchedulerSettings,
    killzone: Literal["london", "ny"],
    *,
    now_utc: datetime,
) -> str:
    """Format and send a "killzone open" heartbeat. Returns the text sent."""
    today_paris = now_utc.astimezone(_TZ_PARIS).date()
    with journal_session_factory() as session:
        ds = get_daily_state(session, today_paris)

    bias_parts = []
    for pair in settings.WATCHED_PAIRS:
        column = f"bias_{pair.lower()}_{killzone}"
        bias = getattr(ds, column, None) if ds is not None else None
        bias_parts.append(f"{_pair_short(pair)} {bias or 'unknown'}")

    label = "London" if killzone == "london" else "NY"
    text = (
        f"🔔 {label} killzone open\n"
        f"Watching: {', '.join(settings.WATCHED_PAIRS)}\n"
        f"Daily bias: {' | '.join(bias_parts)}"
    )
    _run_async(notifier.send_text(text))
    return text


def send_killzone_close_heartbeat(
    notifier: TelegramNotifier,
    journal_session_factory: Callable[[], Session],
    settings: SchedulerSettings,
    killzone: Literal["london", "ny"],
    *,
    now_utc: datetime,
) -> str | None:
    """Send a "killzone closed — no setup" heartbeat **only** when nothing fired.

    Returns the text that was sent, or ``None`` if the heartbeat was
    suppressed (because a setup notification already went out for this
    killzone today).
    """
    today_paris = now_utc.astimezone(_TZ_PARIS).date()
    start_utc, end_utc = paris_session_to_utc(today_paris, _killzone_session(settings, killzone))

    with journal_session_factory() as session:
        stmt = (
            select(SetupRow)
            .where(SetupRow.was_notified.is_(True))
            .where(SetupRow.killzone == killzone)
            .where(SetupRow.timestamp_utc >= start_utc)
            .where(SetupRow.timestamp_utc < end_utc)
        )
        notified = list(session.execute(stmt).scalars().all())
        ds = get_daily_state(session, today_paris)

    if notified:
        logger.info(
            "killzone_close_heartbeat suppressed for %s — %d setups already notified",
            killzone,
            len(notified),
        )
        return None

    bias_parts = []
    for pair in settings.WATCHED_PAIRS:
        column = f"bias_{pair.lower()}_{killzone}"
        bias = getattr(ds, column, None) if ds is not None else None
        bias_parts.append(f"{_pair_short(pair)} {bias or 'unknown'}")

    label = "London" if killzone == "london" else "NY"
    text = (
        f"✅ {label} killzone closed — no valid setup detected.\n"
        f"Bias was: {' | '.join(bias_parts)}"
    )
    _run_async(notifier.send_text(text))
    return text


# ---------------------------------------------------------------------------
# Outcome reconciliation
# ---------------------------------------------------------------------------


def run_outcome_reconciliation(
    mt5_client: MT5Client,
    journal_session_factory: Callable[[], Session],
    *,
    since: datetime,
) -> int:
    """Wraps :func:`reconcile_outcomes` for scheduler use."""
    with journal_session_factory() as session:
        return reconcile_outcomes(session, mt5_client, since)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _killzone_session(
    settings: SchedulerSettings, killzone: Literal["london", "ny"]
) -> tuple[int, int, int, int]:
    return settings.KILLZONE_LONDON if killzone == "london" else settings.KILLZONE_NY


def _pair_short(pair: str) -> str:
    """Three-letter shorthand for heartbeat formatting."""
    aliases = {
        "XAUUSD": "XAU",
        "NDX100": "NDX",
        "EURUSD": "EUR",
        "GBPUSD": "GBP",
        "ETHUSD": "ETH",
    }
    return aliases.get(pair, pair[:3])
