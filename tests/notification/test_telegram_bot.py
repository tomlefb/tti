"""Unit tests for ``src.notification.telegram_bot``.

No real Telegram traffic. We mock ``Application.builder`` to inject a
fake bot/application; tests verify the surface contract.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.detection.fvg import FVG
from src.detection.mss import MSS
from src.detection.setup import Setup
from src.detection.sweep import Sweep
from src.notification import telegram_bot as tg_module


def _stub_setup() -> Setup:
    t = datetime(2026, 1, 2, 16, 35, tzinfo=UTC)
    sweep = Sweep(
        direction="bearish",
        swept_level_price=4380.0,
        swept_level_type="asian_high",
        swept_level_strength="structural",
        sweep_candle_time_utc=t,
        sweep_extreme_price=4382.5,
        return_candle_time_utc=t,
        excursion=2.5,
    )
    mss = MSS(
        direction="bearish",
        sweep=sweep,
        broken_swing_time_utc=t,
        broken_swing_price=4365.0,
        mss_confirm_candle_time_utc=t,
        mss_confirm_candle_close=4364.0,
        displacement_body_ratio=2.0,
        displacement_candle_time_utc=t,
    )
    fvg = FVG(
        direction="bearish",
        proximal=4360.0,
        distal=4366.0,
        c1_time_utc=t,
        c2_time_utc=t,
        c3_time_utc=t,
        size=6.0,
        size_atr_ratio=1.0,
    )
    return Setup(
        timestamp_utc=t,
        symbol="XAUUSD",
        direction="short",
        daily_bias="bearish",
        killzone="ny",
        swept_level_price=4380.0,
        swept_level_type="asian_high",
        swept_level_strength="structural",
        sweep=sweep,
        mss=mss,
        poi=fvg,
        poi_type="FVG",
        entry_price=4360.0,
        stop_loss=4375.0,
        target_level_type="swing_h1_low",
        tp_runner_price=4304.30,
        tp_runner_rr=3.71,
        tp1_price=4304.30,
        tp1_rr=3.71,
        quality="A",
        confluences=["FVG+OB"],
    )


@pytest.fixture
def fake_application(monkeypatch):
    """Replace ``Application.builder()`` with a builder returning a mock app.

    The mock app exposes:
        - ``.bot.send_photo`` (AsyncMock returning an object with message_id)
        - ``.add_handler``
        - ``.initialize/.start/.stop/.shutdown`` (AsyncMocks)
        - ``.updater.start_polling/.stop`` (AsyncMocks)
    """
    app = MagicMock()
    app.bot = MagicMock()
    sent_message = SimpleNamespace(message_id=12345)
    app.bot.send_photo = AsyncMock(return_value=sent_message)
    app.add_handler = MagicMock()
    app.initialize = AsyncMock()
    app.start = AsyncMock()
    app.stop = AsyncMock()
    app.shutdown = AsyncMock()
    app.updater = MagicMock()
    app.updater.start_polling = AsyncMock()
    app.updater.stop = AsyncMock()

    builder = MagicMock()
    builder.token.return_value = builder
    builder.build.return_value = app

    monkeypatch.setattr(tg_module.Application, "builder", staticmethod(lambda: builder))
    return app


async def test_send_setup_calls_send_photo_with_caption(fake_application, tmp_path: Path) -> None:
    chart = tmp_path / "chart.png"
    chart.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)

    notifier = tg_module.TelegramNotifier(bot_token="t", chat_id=42)
    setup = _stub_setup()
    await notifier.send_setup(setup, chart)

    fake_application.bot.send_photo.assert_awaited_once()
    kwargs = fake_application.bot.send_photo.await_args.kwargs
    assert kwargs["chat_id"] == 42
    assert kwargs["parse_mode"] == "HTML"
    assert "<b>XAUUSD SHORT</b>" in kwargs["caption"]
    keyboard = kwargs["reply_markup"]
    # Two buttons in one row.
    assert len(keyboard.inline_keyboard) == 1
    row = keyboard.inline_keyboard[0]
    assert [b.text for b in row] == ["Taken", "Skipped"]
    sid_expected = f"{setup.symbol}_{setup.timestamp_utc.isoformat()}"
    assert row[0].callback_data == f"taken:{sid_expected}"
    assert row[1].callback_data == f"skipped:{sid_expected}"


async def test_callback_handler_invokes_on_callback(fake_application, tmp_path: Path) -> None:
    captured: list[tuple[str, str, datetime]] = []

    def on_cb(decision: str, sid: str, ts: datetime) -> None:
        captured.append((decision, sid, ts))

    notifier = tg_module.TelegramNotifier(bot_token="t", chat_id=42, on_callback=on_cb)
    setup = _stub_setup()

    chart = tmp_path / "chart.png"
    chart.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    await notifier.send_setup(setup, chart)

    sid = f"{setup.symbol}_{setup.timestamp_utc.isoformat()}"

    query = MagicMock()
    query.data = f"taken:{sid}"
    query.answer = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    update = SimpleNamespace(callback_query=query)

    await notifier._handle_callback_query(update, MagicMock())

    assert len(captured) == 1
    decision, captured_sid, ts = captured[0]
    assert decision == "taken"
    assert captured_sid == sid
    assert ts == setup.timestamp_utc
    query.answer.assert_awaited_once()
    query.edit_message_reply_markup.assert_awaited_once()


async def test_callback_handler_acknowledges_telegram_even_on_user_error(
    fake_application,
) -> None:
    """If the injected callback raises, we still ack Telegram."""

    def boom(decision: str, sid: str, ts: datetime) -> None:
        raise RuntimeError("downstream journal write blew up")

    notifier = tg_module.TelegramNotifier(bot_token="t", chat_id=42, on_callback=boom)

    sid = "XAUUSD_2026-01-02T16:35:00+00:00"
    notifier._timestamps[sid] = datetime(2026, 1, 2, 16, 35, tzinfo=UTC)

    query = MagicMock()
    query.data = f"skipped:{sid}"
    query.answer = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    update = SimpleNamespace(callback_query=query)

    # Must NOT raise — the bot's contract is that on_callback errors are
    # logged and swallowed so the user doesn't see a spinning button.
    await notifier._handle_callback_query(update, MagicMock())

    query.answer.assert_awaited_once()


async def test_callback_handler_ignores_unparseable_data(fake_application) -> None:
    captured: list = []

    def on_cb(decision: str, sid: str, ts: datetime) -> None:
        captured.append((decision, sid, ts))

    notifier = tg_module.TelegramNotifier(bot_token="t", chat_id=42, on_callback=on_cb)

    query = MagicMock()
    query.data = "noop"
    query.answer = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    update = SimpleNamespace(callback_query=query)

    await notifier._handle_callback_query(update, MagicMock())

    assert captured == []
    query.answer.assert_awaited_once()


async def test_stop_without_start_is_noop(fake_application) -> None:
    """Calling stop() before start_polling() must not raise."""
    notifier = tg_module.TelegramNotifier(bot_token="t", chat_id=42)
    await notifier.stop()  # Should silently no-op.
    fake_application.stop.assert_not_called()


async def test_send_setup_retries_on_failure(fake_application, tmp_path: Path) -> None:
    """send_setup retries up to 3 times on transient send_photo errors."""
    chart = tmp_path / "chart.png"
    chart.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)

    sent_message = SimpleNamespace(message_id=99)
    fake_application.bot.send_photo = AsyncMock(
        side_effect=[RuntimeError("flaky"), RuntimeError("flaky2"), sent_message]
    )

    notifier = tg_module.TelegramNotifier(bot_token="t", chat_id=42)
    setup = _stub_setup()
    ok = await notifier.send_setup(setup, chart, retry_delay_seconds=0.0)
    assert ok is True
    assert fake_application.bot.send_photo.await_count == 3


async def test_send_setup_returns_false_after_max_attempts(
    fake_application, tmp_path: Path
) -> None:
    """When every attempt fails, send_setup returns False without raising."""
    chart = tmp_path / "chart.png"
    chart.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)

    fake_application.bot.send_photo = AsyncMock(side_effect=RuntimeError("dead"))

    notifier = tg_module.TelegramNotifier(bot_token="t", chat_id=42)
    setup = _stub_setup()
    ok = await notifier.send_setup(setup, chart, retry_delay_seconds=0.0, max_attempts=3)
    assert ok is False
    assert fake_application.bot.send_photo.await_count == 3


async def test_send_error_uses_send_message_and_swallows_failures(fake_application) -> None:
    fake_application.bot.send_message = AsyncMock(return_value=None)

    notifier = tg_module.TelegramNotifier(bot_token="t", chat_id=42)
    ok = await notifier.send_error("⚠️ MT5 cycle skipped")
    assert ok is True
    fake_application.bot.send_message.assert_awaited_once()
    kwargs = fake_application.bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == 42
    assert kwargs["text"] == "⚠️ MT5 cycle skipped"

    # Now make it fail and ensure it returns False rather than raising.
    fake_application.bot.send_message = AsyncMock(side_effect=RuntimeError("nope"))
    ok = await notifier.send_error("again")
    assert ok is False
