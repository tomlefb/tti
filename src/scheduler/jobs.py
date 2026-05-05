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
from datetime import UTC, datetime
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


# ---------------------------------------------------------------------------
# Rotation strategy (trend_rotation_d1 v1.1) — D1 cycle
# ---------------------------------------------------------------------------
#
# Distinct from ``run_detection_cycle`` (TJR-shaped, killzone-driven, M5):
# rotation fires once per D1 close, scores the universe over a momentum
# lookback, and rotates the top-K basket. No setups, no SL/TP — the basket
# is the unit of decision.
#
# State persistence: the journal owns the source of truth. The cycle reads
# open rotation positions from `rotation_positions` (status='open') to
# reconstruct the current basket; it writes a new `rebalance_transitions`
# row + per-asset `rotation_positions` rows on each rebalance.


@dataclass
class RotationCycleReport:
    """Aggregate of one rotation rebalance for the operator's logs."""

    fired: bool = False
    skipped_reason: str | None = None  # 'not_due', 'pre_check_blocked', etc.
    basket_before: list[str] = field(default_factory=list)
    basket_after: list[str] = field(default_factory=list)
    closed_assets: list[str] = field(default_factory=list)
    opened_assets: list[str] = field(default_factory=list)
    closes_succeeded: int = 0
    closes_failed: int = 0
    opens_succeeded: int = 0
    opens_failed: int = 0
    capital_usd: float = 0.0
    risk_pct: float = 0.0


def run_rotation_cycle(
    mt5_client: MT5Client,
    journal_session_factory: Callable[[], Session],
    notifier: TelegramNotifier,
    settings: SchedulerSettings,
    *,
    now_utc: datetime,
    dry_run: bool = False,
) -> RotationCycleReport:
    """Run one rebalance cycle for the trend_rotation_d1 strategy.

    Triggered by APScheduler at the configured Paris-local time on
    weekdays (D1 close convention — the default is 23:00 Paris which
    sits comfortably after every covered market closes for the day).

    Steps:

    1. Read live account snapshot. Compute live capital + daily P&L
       and refresh the ``rotation_daily_pnl`` row.
    2. Pre-flight: :func:`safe_guards.check_rotation_pre_rebalance`
       (kill switch, day-disabled, capital floor, daily limit).
    3. Read persisted state from the journal: open rotation positions
       form the ``current_basket``; the most recent
       ``entry_timestamp_utc`` defines ``last_rebalance_date``.
    4. Cadence gate: skip if (now - last_rebalance) <
       ``ROTATION_REBALANCE_DAYS``.
    5. Build the per-asset panel from MT5 D1 OHLC.
    6. Score every asset in the universe (momentum + ATR(20) +
       volatility regime filter), pick top-K via the rotation
       pipeline's ranking helper.
    7. Compute transitions: ``closed = current - new``,
       ``opened = new - current``. If both are empty, journal the
       no-op rebalance (idempotency anchor) and return without
       touching MT5.
    8. Compute risk-per-trade via the adaptive schedule and size
       every new entry via :func:`compute_rotation_volume`.
    9. Insert the ``rebalance_transitions`` row to anchor every
       per-position FK.
    10. Execute transitions (closes-then-opens) via
        :func:`execute_rebalance_transitions`.
    11. Send a Telegram summary (one message for "scheduled", one
        for "executed" so the operator sees both phases).

    The TJR cycle (``run_detection_cycle``) is unchanged; the runner
    decides which one fires based on ``ACTIVE_STRATEGY``.

    Pure data path: every dependency is injected (``mt5_client``,
    ``journal_session_factory``, ``notifier``, ``settings``). Tests
    drive the function with a fake MT5 + in-memory SQLite.
    """
    # Late imports keep the top-of-module import graph small for the
    # legacy TJR-only entry points; rotation pulls a deeper subgraph
    # (pipeline helpers + execution primitives + new repository CRUD).
    from src.execution.order_manager_rotation import (
        RebalanceClose,
        RebalanceOpen,
        compute_rotation_volume,
        execute_rebalance_transitions,
    )
    from src.execution.safe_guards import (
        adaptive_risk_per_trade_pct,
        check_rotation_pre_rebalance,
    )
    from src.journal.repository import (
        get_open_rotation_positions,
        insert_rebalance_transition,
        upsert_rotation_daily_pnl,
    )
    from src.notification.message_formatter import (
        format_capital_below_threshold_message,
        format_killswitch_triggered_message,
        format_rebalance_error_message,
        format_rebalance_executed_message,
        format_rebalance_scheduled_message,
    )
    from src.strategies.trend_rotation_d1 import StrategyParams
    from src.strategies.trend_rotation_d1.pipeline import _score_one_asset
    from src.strategies.trend_rotation_d1.ranking import select_top_k

    strategy = str(getattr(settings, "ACTIVE_STRATEGY", "trend_rotation_d1"))
    universe = tuple(getattr(settings, "ROTATION_UNIVERSE", ()))
    K = int(getattr(settings, "ROTATION_K", 5))
    momentum_lookback = int(getattr(settings, "ROTATION_MOMENTUM_LOOKBACK_DAYS", 126))
    rebalance_freq_days = int(getattr(settings, "ROTATION_REBALANCE_DAYS", 5))
    atr_period = int(getattr(settings, "ROTATION_ATR_PERIOD", 20))
    today_paris = now_utc.astimezone(_TZ_PARIS).date()

    report = RotationCycleReport()

    # ---- 1. Account snapshot + daily P&L refresh ----
    try:
        account = mt5_client.get_account_info()
    except Exception as exc:  # noqa: BLE001
        logger.exception("rotation cycle: get_account_info failed")
        report.skipped_reason = "account_info_unavailable"
        _run_async(notifier.send_error(
            format_rebalance_error_message(strategy=strategy, error=repr(exc))
        ))
        return report

    capital = float(account.balance)
    report.capital_usd = capital
    daily_limit = float(getattr(settings, "DAILY_LOSS_LIMIT_USD", 0.0))

    # Two-pass to avoid the chicken-and-egg: read the prior day's row to
    # know opening_balance, compute the new daily P&L, then upsert. On
    # the first call of the day the prior row is absent and the upsert
    # itself captures opening_balance == current_balance (P&L starts at
    # 0 and grows / drains from there).
    from src.journal.repository import get_rotation_daily_pnl as _get_dp
    with journal_session_factory() as s:
        prior_row = _get_dp(s, day=today_paris)
    prior_opening = (
        float(prior_row.opening_balance_usd) if prior_row is not None else capital
    )
    daily_pnl_usd = capital - prior_opening
    limit_remaining = (
        max(0.0, daily_limit + min(0.0, daily_pnl_usd)) if daily_limit > 0 else 0.0
    )
    with journal_session_factory() as s:
        upsert_rotation_daily_pnl(
            s, day=today_paris,
            current_balance_usd=capital,
            daily_loss_limit_remaining_usd=limit_remaining,
        )

    # ---- 2. Pre-flight ----
    with journal_session_factory() as s:
        allowed, reason = check_rotation_pre_rebalance(
            s, settings=settings, now_utc=now_utc,
            current_capital_usd=capital, daily_pnl_usd=daily_pnl_usd,
        )
    if not allowed:
        logger.warning("rotation cycle blocked: %s", reason)
        report.skipped_reason = reason
        if reason == "kill_switch":
            text = format_killswitch_triggered_message(reason=reason, capital_usd=capital)
        elif reason == "capital_below_safe_threshold":
            text = format_capital_below_threshold_message(
                capital_usd=capital,
                threshold_usd=float(getattr(settings, "ROTATION_CAPITAL_FLOOR_USD", 0.0)),
            )
        else:
            text = format_killswitch_triggered_message(reason=reason, capital_usd=capital)
        _run_async(notifier.send_error(text))
        return report

    # ---- 3. Read persisted state ----
    with journal_session_factory() as s:
        open_rows = get_open_rotation_positions(s, strategy=strategy)
    current_basket = {row.symbol for row in open_rows}
    last_rebalance: datetime | None = None
    if open_rows:
        last_rebalance = max(row.entry_timestamp_utc for row in open_rows)
        if last_rebalance.tzinfo is None:
            last_rebalance = last_rebalance.replace(tzinfo=UTC)

    # ---- 4. Cadence gate ----
    if last_rebalance is not None:
        age_days = (now_utc - last_rebalance).total_seconds() / 86400.0
        if age_days < rebalance_freq_days:
            logger.info(
                "rotation cycle: not due (last=%s, age=%.2f d < freq=%d d)",
                last_rebalance.isoformat(), age_days, rebalance_freq_days,
            )
            report.skipped_reason = "not_due"
            return report

    # ---- 5. Build panel ----
    panel: dict[str, "pd.DataFrame"] = {}
    n_bars_needed = momentum_lookback + atr_period + 30  # warmup buffer
    for asset in universe:
        try:
            df = mt5_client.fetch_ohlc(asset, "D1", n_bars_needed)
        except Exception as exc:  # noqa: BLE001 — surface but keep going
            logger.error("rotation cycle: fetch_ohlc(%s, D1) failed: %r", asset, exc)
            continue
        # The pipeline expects a time-indexed frame.
        df_indexed = df.set_index("time").sort_index()
        panel[asset] = df_indexed

    # ---- 6. Score + select top-K ----
    params = StrategyParams(
        universe=universe,
        momentum_lookback_days=momentum_lookback,
        K=K,
        rebalance_frequency_days=rebalance_freq_days,
        atr_period=atr_period,
    )
    scores: dict[str, float | None] = {}
    atrs: dict[str, float] = {}
    for asset in universe:
        df = panel.get(asset)
        if df is None:
            scores[asset] = None
            continue
        score, atr = _score_one_asset(df, now_utc, params)
        scores[asset] = score
        atrs[asset] = atr
    new_basket = set(select_top_k(scores, K))

    report.basket_before = sorted(current_basket)
    report.basket_after = sorted(new_basket)

    # ---- 7. Transitions ----
    closed_set = current_basket - new_basket
    opened_set = new_basket - current_basket
    report.closed_assets = sorted(closed_set)
    report.opened_assets = sorted(opened_set)

    if not closed_set and not opened_set and current_basket == new_basket:
        # No-op rebalance — basket unchanged. Journal the cadence anchor
        # so the next cycle's "not_due" gate updates correctly, but skip
        # MT5 calls entirely.
        with journal_session_factory() as s:
            insert_rebalance_transition(
                s, strategy=strategy, timestamp_utc=now_utc,
                basket_before=sorted(current_basket),
                basket_after=sorted(new_basket),
                closed_assets=[], opened_assets=[],
                capital_at_rebalance_usd=capital,
                risk_per_trade_pct=adaptive_risk_per_trade_pct(
                    current_capital_usd=capital,
                    capital_floor_for_full_risk_usd=float(
                        getattr(settings, "ROTATION_CAPITAL_FLOOR_FOR_FULL_RISK_USD", 4950.0)
                    ),
                    risk_full_pct=float(
                        getattr(settings, "ROTATION_RISK_PER_TRADE_FULL_PCT", 0.01)
                    ),
                    risk_reduced_pct=float(
                        getattr(settings, "ROTATION_RISK_PER_TRADE_REDUCED_PCT", 0.005)
                    ),
                ),
                notes="no-op rebalance (basket unchanged)",
            )
        report.fired = False
        report.skipped_reason = "basket_unchanged"
        logger.info(
            "rotation cycle: basket unchanged (%s) — journalled as no-op",
            sorted(current_basket),
        )
        return report

    # ---- 8. Adaptive risk + per-asset sizing ----
    risk_pct = adaptive_risk_per_trade_pct(
        current_capital_usd=capital,
        capital_floor_for_full_risk_usd=float(
            getattr(settings, "ROTATION_CAPITAL_FLOOR_FOR_FULL_RISK_USD", 4950.0)
        ),
        risk_full_pct=float(getattr(settings, "ROTATION_RISK_PER_TRADE_FULL_PCT", 0.01)),
        risk_reduced_pct=float(getattr(settings, "ROTATION_RISK_PER_TRADE_REDUCED_PCT", 0.005)),
    )
    risk_usd = capital * risk_pct
    report.risk_pct = risk_pct

    closes: list[RebalanceClose] = []
    for sym in sorted(closed_set):
        row = next((r for r in open_rows if r.symbol == sym), None)
        if row is None:
            logger.warning(
                "rotation cycle: asset %s in close-set but no open row in journal",
                sym,
            )
            continue
        closes.append(RebalanceClose(
            symbol=sym, ticket=int(row.mt5_ticket),
            entry_price=float(row.entry_price),
            atr_at_entry=float(row.atr_at_entry),
            risk_usd=float(row.risk_usd),
        ))

    opens: list[RebalanceOpen] = []
    for sym in sorted(opened_set):
        atr = atrs.get(sym, float("nan"))
        if not (atr > 0):
            logger.warning(
                "rotation cycle: asset %s has invalid ATR (%s) — skipping open",
                sym, atr,
            )
            continue
        try:
            sym_info = mt5_client.get_symbol_info(sym)
        except Exception:  # noqa: BLE001
            logger.exception(
                "rotation cycle: get_symbol_info(%s) failed — skipping open", sym
            )
            continue
        try:
            volume = compute_rotation_volume(
                risk_usd=risk_usd, atr_at_entry=atr, symbol_info=sym_info,
            )
        except ValueError:
            logger.exception(
                "rotation cycle: compute_rotation_volume(%s) failed — skipping open",
                sym,
            )
            continue
        opens.append(RebalanceOpen(
            symbol=sym, direction="long", volume=volume,
            atr_at_entry=atr, risk_usd=risk_usd,
        ))

    # ---- 9. Anchor rebalance transition row ----
    with journal_session_factory() as s:
        rebal_uid = insert_rebalance_transition(
            s, strategy=strategy, timestamp_utc=now_utc,
            basket_before=sorted(current_basket),
            basket_after=sorted(new_basket),
            closed_assets=sorted(closed_set),
            opened_assets=sorted(opened_set),
            capital_at_rebalance_usd=capital,
            risk_per_trade_pct=risk_pct,
            notes=("dry-run" if dry_run else None),
        )

    # ---- 10. Notify "scheduled" ----
    _run_async(notifier.send_text(
        format_rebalance_scheduled_message(timestamp_utc=now_utc, strategy=strategy)
    ))

    # ---- 11. Execute and notify "executed" ----
    try:
        result = execute_rebalance_transitions(
            closes=closes, opens=opens,
            mt5_client=mt5_client,
            journal_session_factory=journal_session_factory,
            settings=settings,
            now_utc=now_utc,
            strategy=strategy,
            rebalance_uid=rebal_uid,
            dry_run=dry_run,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("rotation cycle: execute_rebalance_transitions raised")
        _run_async(notifier.send_error(
            format_rebalance_error_message(strategy=strategy, error=repr(exc))
        ))
        report.skipped_reason = "execute_exception"
        return report

    report.fired = True
    report.closes_succeeded = result.n_closed_ok
    report.closes_failed = result.n_closed_failed
    report.opens_succeeded = result.n_opened_ok
    report.opens_failed = result.n_opened_failed

    _run_async(notifier.send_text(
        format_rebalance_executed_message(
            timestamp_utc=now_utc, strategy=strategy,
            closed_assets=sorted(closed_set),
            opened_assets=sorted(opened_set),
            basket_after=sorted(new_basket),
            capital_usd=capital, risk_pct=risk_pct,
        )
    ))
    logger.info(
        "rotation cycle fired: closed=%s opened=%s (closes %d/%d ok, opens %d/%d ok)",
        sorted(closed_set), sorted(opened_set),
        result.n_closed_ok, result.n_closed_ok + result.n_closed_failed,
        result.n_opened_ok, result.n_opened_ok + result.n_opened_failed,
    )
    return report


