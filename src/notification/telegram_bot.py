"""Telegram bot wrapper around python-telegram-bot v21+.

Sprint 4 deliverable. Public surface is a single class, ``TelegramNotifier``,
that:

- Sends a setup notification = chart PNG + HTML caption + Taken/Skipped
  inline keyboard, with up to 3 retry attempts (Sprint 6).
- Sends a plain text error/heartbeat message via ``send_error`` and
  ``send_text`` (Sprint 6).
- Polls Telegram for button callbacks.
- Routes callbacks to an injected ``on_callback`` so Sprint 5 can plug in
  the SQLite journal without changing this module.

All real network calls are async. CI never hits Telegram — the unit tests
mock ``Application``/``bot`` instead.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from src.detection.setup import Setup
from src.notification.message_formatter import format_setup_message

logger = logging.getLogger(__name__)

# Callback signature: (decision, setup_id, timestamp_utc) → None.
# Sprint 4 ships a logging stub; Sprint 5 will inject the SQLite repo.
OnCallback = Callable[[str, str, datetime], None]


def _setup_id(setup: Setup) -> str:
    """Stable identifier for a Setup, used as Telegram callback_data prefix.

    Format: ``"<symbol>_<isoformat-timestamp>"``. The orchestrator
    guarantees ``timestamp_utc`` uniqueness within a (symbol, day) so this
    is collision-free in practice. Note: Telegram limits callback_data to
    64 bytes — well within range for our 4 symbols and ISO timestamps.
    """
    return f"{setup.symbol}_{setup.timestamp_utc.isoformat()}"


class TelegramNotifier:
    """Async wrapper around python-telegram-bot for the TJR notification flow.

    Lifecycle:

        notifier = TelegramNotifier(token, chat_id, on_callback=...)
        await notifier.send_setup(setup, chart_path)
        await notifier.start_polling()        # blocks
        await notifier.stop()                 # graceful shutdown

    The notifier never raises into the caller for Telegram-level errors —
    they are logged and swallowed so the detection pipeline keeps running
    (per docs/04 Error Handling: Telegram failures must not crash the system).
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: int,
        on_callback: OnCallback | None = None,
    ) -> None:
        self._chat_id = chat_id
        self._on_callback = on_callback
        # Mapping setup_id → original Telegram message_id, captured at send
        # time so the callback handler can edit the right message later.
        self._message_ids: dict[str, int] = {}
        # Mapping setup_id → its UTC timestamp, so the callback only needs
        # the parsed ``decision:setup_id`` string from Telegram.
        self._timestamps: dict[str, datetime] = {}
        self._application: Application = Application.builder().token(bot_token).build()
        self._application.add_handler(CallbackQueryHandler(self._handle_callback_query))
        self._started = False

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------

    async def send_setup(
        self,
        setup: Setup,
        chart_path: Path,
        *,
        max_attempts: int = 3,
        retry_delay_seconds: float = 1.0,
    ) -> bool:
        """Send the setup notification with up to 3 retries on failure.

        Caption is built from ``format_setup_message``. Telegram caption
        size limit is 1024 chars; our captions stay well below ~600.

        Sprint 6: per docs/04 §"Error handling", a Telegram failure must
        NOT crash the scheduler. We retry ``max_attempts`` times and then
        give up — the setup remains in the journal so it's not lost.

        Returns:
            ``True`` if the message was sent, ``False`` if every attempt
            failed.
        """
        caption = format_setup_message(setup)
        sid = _setup_id(setup)
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Taken", callback_data=f"taken:{sid}"),
                    InlineKeyboardButton("Skipped", callback_data=f"skipped:{sid}"),
                ]
            ]
        )

        for attempt in range(1, max_attempts + 1):
            try:
                with Path(chart_path).open("rb") as f:
                    message = await self._application.bot.send_photo(
                        chat_id=self._chat_id,
                        photo=f,
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=keyboard,
                    )
            except Exception as exc:  # noqa: BLE001 — Telegram errors swallowed
                logger.warning(
                    "send_setup attempt %d/%d failed for %s: %r",
                    attempt,
                    max_attempts,
                    sid,
                    exc,
                )
                if attempt == max_attempts:
                    logger.error(
                        "send_setup giving up for %s after %d attempts — setup "
                        "remains in journal but no Telegram notification fired",
                        sid,
                        max_attempts,
                    )
                    return False
                await asyncio.sleep(retry_delay_seconds * attempt)
                continue

            self._message_ids[sid] = message.message_id
            self._timestamps[sid] = setup.timestamp_utc
            logger.info(
                "Sent setup notification for %s (message_id=%d, attempt=%d)",
                sid,
                message.message_id,
                attempt,
            )
            return True
        return False  # unreachable; appeases the type-checker

    async def send_text(self, text: str, *, parse_mode: str | None = None) -> bool:
        """Send a plain text message with no chart, no buttons, no retries.

        Used by the scheduler for heartbeats and error alerts. A failure
        is logged but not raised — the scheduler can survive a
        Telegram-down moment.
        """
        try:
            await self._application.bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode=parse_mode,
            )
        except Exception as exc:  # noqa: BLE001 — never crash the scheduler
            logger.error("send_text failed: %r — text=%r", exc, text)
            return False
        return True

    async def send_error(self, text: str) -> bool:
        """Send an error/critical alert. Thin wrapper around ``send_text``."""
        return await self.send_text(text)

    async def start_polling(self) -> None:
        """Start the bot's update polling loop. Blocks until ``stop()``."""
        await self._application.initialize()
        await self._application.start()
        await self._application.updater.start_polling()
        self._started = True
        logger.info("Telegram polling started")

    async def stop(self) -> None:
        """Stop the polling loop and release resources."""
        if not self._started:
            return
        await self._application.updater.stop()
        await self._application.stop()
        await self._application.shutdown()
        self._started = False
        logger.info("Telegram polling stopped")

    # -----------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------

    async def _handle_callback_query(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        if query is None or not query.data:
            return

        decision, _, sid = query.data.partition(":")
        if decision not in ("taken", "skipped") or not sid:
            logger.warning("Unparseable callback_data: %r", query.data)
            await query.answer()
            return

        # Update message UI to show the chosen action — leaves a clear
        # "decided" state in Telegram. Done before invoking the callback
        # so a user-callback failure doesn't leave the button spinning.
        chosen_label = "✅ Taken" if decision == "taken" else "✗ Skipped"
        try:
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton(chosen_label, callback_data="noop")]]
                )
            )
        except Exception:  # noqa: BLE001 — Telegram errors are non-fatal here
            logger.exception("Failed to edit message reply markup for sid=%s", sid)

        # Invoke user callback. Errors are logged but swallowed; we always
        # ack the callback so the user doesn't see a spinning button.
        if self._on_callback is not None:
            ts = self._timestamps.get(sid)
            if ts is None:
                # The notifier was restarted between send and click;
                # we don't have the original timestamp in memory anymore.
                # Parse it from the sid as a best-effort recovery.
                try:
                    ts = datetime.fromisoformat(sid.split("_", 1)[1])
                except (ValueError, IndexError):
                    logger.warning("Cannot recover timestamp for sid=%s — passing now()", sid)
                    ts = datetime.utcnow()
            try:
                self._on_callback(decision, sid, ts)
            except Exception:  # noqa: BLE001 — must not break the bot
                logger.exception("on_callback raised for decision=%s sid=%s", decision, sid)

        await query.answer()
