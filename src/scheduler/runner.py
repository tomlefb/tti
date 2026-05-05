"""Process entry point — wires APScheduler triggers to the jobs module.

Boots the MT5 client, journal engine, Telegram notifier, and an
:class:`AsyncIOScheduler` that runs detection cycles, pre-killzone bias
computation, killzone heartbeats, and the daily outcome reconciliation.

Why ``AsyncIOScheduler`` rather than ``BlockingScheduler``: the
``python-telegram-bot`` package is asyncio-native (every send is a
coroutine). Co-locating the bot's polling loop and the scheduler in a
single asyncio loop avoids a thread-bridge dance for sending messages
from inside detection-cycle callbacks.

This module is invoked via ``scripts/run_scheduler.py`` on the Windows
host. Pytest does **not** test the main loop — it would block forever.
``tests/scheduler/test_jobs.py`` exercises every job function the
scheduler triggers.
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import signal
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.execution.order_manager import place_order
from src.execution.position_lifecycle import (
    check_open_positions,
    end_of_killzone_cleanup,
)
from src.execution.recovery import reconcile_orphan_positions
from src.journal.db import get_engine, init_db, session_scope
from src.journal.repository import get_decision, get_setup, insert_decision
from src.mt5_client.client import MT5Client
from src.mt5_client.exceptions import MT5ConnectionError
from src.notification.telegram_bot import TelegramNotifier
from src.scheduler.jobs import (
    run_detection_cycle,
    run_outcome_reconciliation,
    run_pre_killzone_bias,
    run_rotation_cycle,
    send_killzone_close_heartbeat,
    send_killzone_open_heartbeat,
)

logger = logging.getLogger(__name__)

_TZ_PARIS = ZoneInfo("Europe/Paris")


def _configure_logging(settings: ModuleType) -> None:
    """Set up rotating-file + console logging once per process.

    Mirrors the scheme described in docs/04 §Logging.
    """
    log_path = Path(getattr(settings, "LOG_FILE", "logs/system.log"))
    log_path.parent.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=int(getattr(settings, "LOG_MAX_BYTES", 10 * 1024 * 1024)),
        backupCount=int(getattr(settings, "LOG_BACKUP_COUNT", 5)),
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.handlers = [file_handler, console_handler]
    root.setLevel(getattr(settings, "LOG_LEVEL", "INFO"))


def _build_journal_callback(engine, settings):
    """Build the Telegram on_callback closure that persists Taken/Skipped."""

    def on_callback(decision: str, sid: str, ts: datetime) -> None:
        try:
            with session_scope(engine) as s:
                if get_setup(s, sid) is None:
                    logger.warning("callback for unknown setup_uid=%s — decision dropped", sid)
                    return
                if get_decision(s, sid) is None:
                    insert_decision(s, sid, decision, ts)
                    logger.info("journaled decision: uid=%s decision=%s", sid, decision)
        except Exception:  # noqa: BLE001
            logger.exception("on_callback persistence failed for sid=%s", sid)

    return on_callback


async def _amain(settings: ModuleType) -> None:
    """Async entry point — runs forever until SIGTERM/SIGINT."""
    _configure_logging(settings)
    logger.info("scheduler: starting")

    # Journal
    engine = get_engine(getattr(settings, "DB_PATH", "data/journal.db"))
    init_db(engine)

    def session_factory():
        return session_scope(engine)

    # MT5
    mt5 = MT5Client(
        login=int(settings.MT5_LOGIN),
        password=str(settings.MT5_PASSWORD),
        server=str(settings.MT5_SERVER),
    )
    try:
        mt5.connect()
    except MT5ConnectionError as exc:
        logger.critical("scheduler: MT5 connect failed (%r) — aborting", exc)
        raise

    # Telegram
    notifier = TelegramNotifier(
        bot_token=str(settings.TELEGRAM_BOT_TOKEN),
        chat_id=int(settings.TELEGRAM_CHAT_ID),
        on_callback=_build_journal_callback(engine, settings),
    )
    await notifier.start_polling()

    auto_trading = bool(getattr(settings, "AUTO_TRADING_ENABLED", False))
    active_strategy = str(getattr(settings, "ACTIVE_STRATEGY", "tjr")).lower()
    mode_label = "auto-execution (live)" if auto_trading else "notifications-only"
    await notifier.send_text(
        f"✅ Scheduler started — strategy={active_strategy}, mode={mode_label}."
    )

    # Sprint 7: orphan / lost-order reconciliation on startup. Idempotent
    # so a crash-restart loop does not amplify side effects.
    try:
        reconcile_orphan_positions(
            mt5_client=mt5,
            journal_session_factory=session_factory,
            settings=settings,
            now_utc=datetime.now(UTC),
            notifier=notifier,
        )
    except Exception:  # noqa: BLE001
        logger.exception("startup reconciliation raised — proceeding anyway")

    # Scheduler
    scheduler = AsyncIOScheduler(timezone=_TZ_PARIS)

    # ------------------------------------------------------------------
    # Rotation strategy registration (trend_rotation_d1).
    # Runs ONE job per weekday at the configured Paris-local D1 close.
    # No killzones, no per-trade lifecycle (rotation has no SL/TP),
    # no end-of-killzone cleanup. Outcome reconciliation is also
    # skipped — rotation positions journal themselves on close.
    # ------------------------------------------------------------------
    if active_strategy == "trend_rotation_d1":
        rotation_hour = int(getattr(settings, "ROTATION_CRON_HOUR_PARIS", 23))
        rotation_minute = int(getattr(settings, "ROTATION_CRON_MINUTE_PARIS", 0))

        def rotation_job():
            try:
                run_rotation_cycle(
                    mt5,
                    session_factory,
                    notifier,
                    settings,
                    now_utc=datetime.now(UTC),
                    dry_run=bool(getattr(settings, "AUTO_TRADING_DRY_RUN", False))
                    or not auto_trading,
                )
            except Exception as exc:  # noqa: BLE001 — survive bad cycles
                logger.exception("rotation_job uncaught error")
                asyncio.ensure_future(
                    notifier.send_error(f"⚠️ rotation scheduler error: {exc!r}")
                )

        scheduler.add_job(
            rotation_job,
            CronTrigger(
                day_of_week="mon-fri",
                hour=rotation_hour,
                minute=rotation_minute,
                timezone=_TZ_PARIS,
            ),
            id="rotation_cycle",
            replace_existing=True,
        )
        scheduler.start()
        logger.info(
            "scheduler: rotation strategy active, %d job(s) registered, "
            "trigger=%02d:%02d Paris weekdays",
            len(scheduler.get_jobs()), rotation_hour, rotation_minute,
        )
        # Block on shutdown signal — same pattern as the TJR branch.
        await _await_shutdown(notifier, scheduler, mt5)
        return

    # ------------------------------------------------------------------
    # TJR strategy registration (default — preserved unchanged).
    # ------------------------------------------------------------------
    # Detection cycle: every N min during each killzone (Paris-local cron).
    interval = int(getattr(settings, "DETECTION_INTERVAL_MINUTES", 5))
    london_kz = settings.KILLZONE_LONDON
    ny_kz = settings.KILLZONE_NY

    def _place_order_callback(setup):
        """Sprint 7 — invoke order_manager.place_order with proper deps.

        Wrapped here (rather than passed as a partial) so the inline
        try/except keeps execution failures from killing the cycle.
        """
        try:
            place_order(
                setup=setup,
                mt5_client=mt5,
                journal_session_factory=session_factory,
                settings=settings,
                now_utc=datetime.now(UTC),
                notifier=notifier,
                dry_run=bool(getattr(settings, "AUTO_TRADING_DRY_RUN", False)),
            )
        except Exception:  # noqa: BLE001
            logger.exception("place_order failed for %s — continuing cycle", setup.symbol)

    def cycle_job():
        try:
            run_detection_cycle(
                mt5,
                session_factory,
                notifier,
                settings,
                now_utc=datetime.now(UTC),
                place_order_callback=_place_order_callback if auto_trading else None,
            )
        except Exception as exc:  # noqa: BLE001 — survive bad cycles
            logger.exception("cycle_job uncaught error")
            asyncio.ensure_future(notifier.send_error(f"⚠️ scheduler error: {exc!r}"))

    # Cron expression covering both killzones; APScheduler runs the job at
    # every ``interval`` minute mark inside each window.
    minute_expr = f"*/{interval}"
    scheduler.add_job(
        cycle_job,
        CronTrigger(
            day_of_week="mon-fri",
            hour=f"{london_kz[0]}-{london_kz[2]-1}",
            minute=minute_expr,
            timezone=_TZ_PARIS,
        ),
        id="detection_cycle_london",
        replace_existing=True,
    )
    scheduler.add_job(
        cycle_job,
        CronTrigger(
            day_of_week="mon-fri",
            hour=f"{ny_kz[0]}-{ny_kz[2]-1}",
            minute=minute_expr,
            timezone=_TZ_PARIS,
        ),
        id="detection_cycle_ny",
        replace_existing=True,
    )

    # Pre-killzone bias.
    lead = int(getattr(settings, "PRE_KILLZONE_BIAS_LEAD_MINUTES", 5))

    def _pre_minutes(start_h: int, start_m: int, lead_min: int) -> tuple[int, int]:
        total = start_h * 60 + start_m - lead_min
        return total // 60, total % 60

    london_pre_h, london_pre_m = _pre_minutes(london_kz[0], london_kz[1], lead)
    ny_pre_h, ny_pre_m = _pre_minutes(ny_kz[0], ny_kz[1], lead)

    scheduler.add_job(
        lambda: run_pre_killzone_bias(
            mt5, session_factory, settings, "london", now_utc=datetime.now(UTC)
        ),
        CronTrigger(
            day_of_week="mon-fri", hour=london_pre_h, minute=london_pre_m, timezone=_TZ_PARIS
        ),
        id="bias_pre_london",
        replace_existing=True,
    )
    scheduler.add_job(
        lambda: run_pre_killzone_bias(
            mt5, session_factory, settings, "ny", now_utc=datetime.now(UTC)
        ),
        CronTrigger(day_of_week="mon-fri", hour=ny_pre_h, minute=ny_pre_m, timezone=_TZ_PARIS),
        id="bias_pre_ny",
        replace_existing=True,
    )

    # Killzone open / close heartbeats.
    if getattr(settings, "HEARTBEAT_AT_KILLZONE_START", True):
        scheduler.add_job(
            lambda: send_killzone_open_heartbeat(
                notifier, session_factory, settings, "london", now_utc=datetime.now(UTC)
            ),
            CronTrigger(
                day_of_week="mon-fri", hour=london_kz[0], minute=london_kz[1], timezone=_TZ_PARIS
            ),
            id="hb_open_london",
            replace_existing=True,
        )
        scheduler.add_job(
            lambda: send_killzone_open_heartbeat(
                notifier, session_factory, settings, "ny", now_utc=datetime.now(UTC)
            ),
            CronTrigger(day_of_week="mon-fri", hour=ny_kz[0], minute=ny_kz[1], timezone=_TZ_PARIS),
            id="hb_open_ny",
            replace_existing=True,
        )
    if getattr(settings, "HEARTBEAT_AT_KILLZONE_CLOSE_IF_EMPTY", True):
        scheduler.add_job(
            lambda: send_killzone_close_heartbeat(
                notifier, session_factory, settings, "london", now_utc=datetime.now(UTC)
            ),
            CronTrigger(
                day_of_week="mon-fri", hour=london_kz[2], minute=london_kz[3], timezone=_TZ_PARIS
            ),
            id="hb_close_london",
            replace_existing=True,
        )
        scheduler.add_job(
            lambda: send_killzone_close_heartbeat(
                notifier, session_factory, settings, "ny", now_utc=datetime.now(UTC)
            ),
            CronTrigger(day_of_week="mon-fri", hour=ny_kz[2], minute=ny_kz[3], timezone=_TZ_PARIS),
            id="hb_close_ny",
            replace_existing=True,
        )

    # Outcome reconciliation — once per day at 23:00 Paris.
    rec_hour = int(getattr(settings, "OUTCOME_RECONCILIATION_HOUR_PARIS", 23))

    def reconciliation_job():
        try:
            run_outcome_reconciliation(
                mt5,
                session_factory,
                since=datetime.now(UTC) - timedelta(days=2),
            )
        except Exception:  # noqa: BLE001
            logger.exception("reconciliation_job error")

    scheduler.add_job(
        reconciliation_job,
        CronTrigger(hour=rec_hour, minute=0, timezone=_TZ_PARIS),
        id="outcome_reconciliation",
        replace_existing=True,
    )

    # Sprint 7: position lifecycle polling (every N seconds, all hours).
    # Cheap operation — at most a handful of MT5 reads per call.
    if auto_trading:
        from apscheduler.triggers.interval import IntervalTrigger

        lifecycle_interval = int(
            getattr(settings, "LIFECYCLE_CHECK_INTERVAL_SEC", 30)
        )

        def lifecycle_job():
            try:
                check_open_positions(
                    mt5_client=mt5,
                    journal_session_factory=session_factory,
                    settings=settings,
                    now_utc=datetime.now(UTC),
                    notifier=notifier,
                )
            except Exception:  # noqa: BLE001
                logger.exception("lifecycle_job uncaught error")

        scheduler.add_job(
            lifecycle_job,
            IntervalTrigger(seconds=lifecycle_interval),
            id="position_lifecycle",
            replace_existing=True,
        )

        # End-of-killzone cleanup — cancel pending limits at killzone close.
        def london_cleanup():
            try:
                end_of_killzone_cleanup(
                    mt5_client=mt5,
                    journal_session_factory=session_factory,
                    settings=settings,
                    killzone="london",
                    now_utc=datetime.now(UTC),
                    notifier=notifier,
                )
            except Exception:  # noqa: BLE001
                logger.exception("london_cleanup uncaught error")

        def ny_cleanup():
            try:
                end_of_killzone_cleanup(
                    mt5_client=mt5,
                    journal_session_factory=session_factory,
                    settings=settings,
                    killzone="ny",
                    now_utc=datetime.now(UTC),
                    notifier=notifier,
                )
            except Exception:  # noqa: BLE001
                logger.exception("ny_cleanup uncaught error")

        scheduler.add_job(
            london_cleanup,
            CronTrigger(
                day_of_week="mon-fri",
                hour=london_kz[2],
                minute=london_kz[3],
                timezone=_TZ_PARIS,
            ),
            id="london_killzone_end_cleanup",
            replace_existing=True,
        )
        scheduler.add_job(
            ny_cleanup,
            CronTrigger(
                day_of_week="mon-fri",
                hour=ny_kz[2],
                minute=ny_kz[3],
                timezone=_TZ_PARIS,
            ),
            id="ny_killzone_end_cleanup",
            replace_existing=True,
        )

    scheduler.start()
    logger.info(
        "scheduler: %d jobs registered, killzones London=%s NY=%s",
        len(scheduler.get_jobs()),
        london_kz,
        ny_kz,
    )

    await _await_shutdown(notifier, scheduler, mt5)


async def _await_shutdown(notifier, scheduler, mt5_client) -> None:
    """Block on SIGTERM/SIGINT, then shut down scheduler / notifier / MT5.

    Extracted so the TJR and rotation registration branches share the
    same shutdown path without duplicating signal-handler boilerplate.
    """
    stop = asyncio.Event()

    def _on_signal():
        logger.info("scheduler: shutdown signal received")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:  # pragma: no cover — Windows
            signal.signal(sig, lambda *_: _on_signal())

    try:
        await stop.wait()
    finally:
        logger.info("scheduler: shutting down")
        scheduler.shutdown(wait=False)
        try:
            await notifier.send_text("⏹️ Scheduler stopped.")
        except Exception:  # noqa: BLE001
            pass
        await notifier.stop()
        mt5_client.shutdown()
        logger.info("scheduler: clean exit")


def main(settings: ModuleType | None = None) -> None:
    """CLI entry point — used by ``scripts/run_scheduler.py``."""
    if settings is None:
        from config import settings as settings_module

        settings = settings_module
    asyncio.run(_amain(settings))
