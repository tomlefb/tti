"""Telegram connectivity smoke test (Sprint 0).

Run this on the Windows host (or anywhere with internet, really) after
filling in TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in config/secrets.py.

What it does:
    1. Sends a test message tagged with a UTC timestamp to the configured
       chat.
    2. Attaches an inline keyboard with two dummy buttons (Taken / Skipped)
       so the full bot setup is validated end-to-end.
    3. Polls for callback_queries for ~30 seconds and prints whichever
       button (if any) the operator presses, then exits cleanly.

On failure, prints the exception with a hint about the most likely cause.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime

from _bootstrap import load_settings

_POLL_DURATION_SECONDS = 30


async def _run(settings) -> int:
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        from telegram.error import InvalidToken, TelegramError
        from telegram.ext import Application, CallbackQueryHandler, ContextTypes
    except ImportError:
        print(
            "ERROR: python-telegram-bot not installed — " "see requirements.txt.",
            file=sys.stderr,
        )
        return 2

    token = str(settings.TELEGRAM_BOT_TOKEN)
    chat_id = int(settings.TELEGRAM_CHAT_ID)

    if not token or token.startswith("PASTE_"):
        print(
            "ERROR: TELEGRAM_BOT_TOKEN is not set in config/secrets.py. "
            "Create a bot via @BotFather and paste the token there.",
            file=sys.stderr,
        )
        return 2
    if chat_id == 0:
        print(
            "ERROR: TELEGRAM_CHAT_ID is not set in config/secrets.py. "
            "See secrets.py.example for how to obtain it.",
            file=sys.stderr,
        )
        return 2

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Taken", callback_data="taken"),
                InlineKeyboardButton("Skipped", callback_data="skipped"),
            ]
        ]
    )

    text = (
        "✅ TJR system — Telegram connectivity OK from Windows host at "
        f"{datetime.now(UTC).isoformat()}"
    )

    application = Application.builder().token(token).build()

    received: dict[str, str] = {}

    async def on_callback(update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        received["data"] = query.data or "<empty>"
        print(f"Callback received: data={received['data']!r}")

    application.add_handler(CallbackQueryHandler(on_callback))

    try:
        async with application:
            try:
                await application.bot.send_message(
                    chat_id=chat_id, text=text, reply_markup=keyboard
                )
            except InvalidToken:
                print(
                    "ERROR: Telegram rejected the bot token. Check "
                    "TELEGRAM_BOT_TOKEN in config/secrets.py — re-copy it "
                    "from @BotFather.",
                    file=sys.stderr,
                )
                return 1
            except TelegramError as exc:
                print(
                    "ERROR: Telegram send_message failed. Likely causes: "
                    "(1) chat_id is wrong, (2) the user has not started a "
                    "conversation with the bot yet (open the bot in "
                    "Telegram and press Start). "
                    f"\n  cause: {exc!r}",
                    file=sys.stderr,
                )
                return 1

            print(
                f"Message sent. Polling for button callbacks for "
                f"{_POLL_DURATION_SECONDS}s — press a button in Telegram "
                "to validate end-to-end."
            )

            await application.start()
            try:
                await application.updater.start_polling()
                try:
                    await asyncio.sleep(_POLL_DURATION_SECONDS)
                finally:
                    await application.updater.stop()
            finally:
                await application.stop()

    except TelegramError as exc:
        print(f"ERROR: Telegram error during polling: {exc!r}", file=sys.stderr)
        return 1

    if "data" in received:
        print("\nTelegram smoke test OK (button pressed).")
    else:
        print(
            "\nTelegram smoke test OK (message sent). No button was pressed "
            "during the polling window — that part is optional, but pressing "
            "one would have confirmed the callback round-trip too."
        )
    return 0


def main() -> int:
    settings = load_settings()
    return asyncio.run(_run(settings))


if __name__ == "__main__":
    raise SystemExit(main())
