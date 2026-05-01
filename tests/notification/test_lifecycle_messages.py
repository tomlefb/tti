"""Sprint 7 — formatter tests for lifecycle event messages.

These are the templates Telegram emits at each transition managed by
the auto-execution layer. The formatter functions are pure (no I/O)
so the bot can compose / send / log them without coupling.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.detection.fvg import FVG
from src.detection.mss import MSS
from src.detection.setup import Setup
from src.detection.sweep import Sweep
from src.notification.message_formatter import (
    format_order_cancelled_message,
    format_order_filled_message,
    format_order_placed_message,
    format_orphan_alert_message,
    format_setup_skipped_message,
    format_sl_hit_message,
    format_tp1_hit_message,
    format_tp_runner_hit_message,
)


def _setup() -> Setup:
    ts = datetime(2026, 5, 1, 15, 35, tzinfo=UTC)
    sweep = Sweep(
        direction="bearish",
        swept_level_price=4380.0,
        swept_level_type="asian_high",
        swept_level_strength="structural",
        sweep_candle_time_utc=ts,
        sweep_extreme_price=4382.5,
        return_candle_time_utc=ts,
        excursion=2.5,
    )
    mss = MSS(
        direction="bearish",
        sweep=sweep,
        broken_swing_time_utc=ts,
        broken_swing_price=4365.0,
        mss_confirm_candle_time_utc=ts,
        mss_confirm_candle_close=4364.0,
        displacement_body_ratio=2.1,
        displacement_candle_time_utc=ts,
    )
    fvg = FVG(
        direction="bearish",
        proximal=4360.0,
        distal=4366.0,
        c1_time_utc=ts,
        c2_time_utc=ts,
        c3_time_utc=ts,
        size=6.0,
        size_atr_ratio=1.0,
    )
    return Setup(
        timestamp_utc=ts,
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
        tp_runner_price=4080.5,
        tp_runner_rr=18.7,
        tp1_price=4285.0,
        tp1_rr=5.0,
        quality="A",
        confluences=["FVG+OB"],
    )


# -----------------------------------------------------------------------------
# format_order_placed_message
# -----------------------------------------------------------------------------


def test_format_order_placed_includes_ticket_volume_and_risk():
    text = format_order_placed_message(
        setup=_setup(), ticket=12345678, volume=0.05, risk_usd=50.0
    )
    assert "ORDER PLACED" in text
    assert "12345678" in text
    assert "0.05" in text
    # Risk in USD shown.
    assert "$50" in text or "50.00" in text


# -----------------------------------------------------------------------------
# format_order_filled_message
# -----------------------------------------------------------------------------


def test_format_order_filled_includes_ticket_and_symbol():
    text = format_order_filled_message(
        symbol="XAUUSD",
        direction="short",
        ticket=12345678,
        entry_price=4360.0,
    )
    assert "FILLED" in text or "Filled" in text
    assert "12345678" in text
    assert "XAUUSD" in text


# -----------------------------------------------------------------------------
# format_tp1_hit_message
# -----------------------------------------------------------------------------


def test_format_tp1_hit_includes_partial_volume_and_be_move():
    text = format_tp1_hit_message(
        symbol="XAUUSD",
        ticket=12345678,
        partial_volume=0.025,
        tp1_price=4285.0,
        entry_price=4360.0,
    )
    assert "TP1" in text
    assert "12345678" in text
    assert "0.025" in text
    # SL → BE referenced.
    assert "BE" in text or "break-even" in text.lower()


# -----------------------------------------------------------------------------
# format_tp_runner_hit_message
# -----------------------------------------------------------------------------


def test_format_tp_runner_hit_includes_realized_r():
    text = format_tp_runner_hit_message(
        symbol="XAUUSD",
        ticket=12345678,
        exit_price=4080.0,
        realized_r=2.5,
    )
    assert "TP RUNNER" in text or "TP_RUNNER" in text or "Runner" in text
    assert "12345678" in text
    assert "4080" in text
    assert "2.5" in text or "2.50" in text


# -----------------------------------------------------------------------------
# format_sl_hit_message
# -----------------------------------------------------------------------------


def test_format_sl_hit_includes_realized_loss():
    text = format_sl_hit_message(
        symbol="XAUUSD",
        ticket=12345678,
        exit_price=4375.0,
        realized_r=-1.0,
    )
    assert "STOP" in text.upper() or "SL" in text.upper()
    assert "12345678" in text
    assert "4375" in text
    assert "-1.0" in text or "-1.00" in text


# -----------------------------------------------------------------------------
# format_order_cancelled_message
# -----------------------------------------------------------------------------


def test_format_order_cancelled_includes_reason():
    text = format_order_cancelled_message(
        ticket=12345678, reason="end_of_killzone"
    )
    assert "CANCEL" in text.upper()
    assert "12345678" in text
    assert "killzone" in text.lower()


# -----------------------------------------------------------------------------
# format_setup_skipped_message
# -----------------------------------------------------------------------------


def test_format_setup_skipped_includes_reason():
    text = format_setup_skipped_message(setup=_setup(), reason="kill_switch")
    assert "SKIP" in text.upper() or "Skipped" in text
    assert "XAUUSD" in text
    assert "kill_switch" in text


# -----------------------------------------------------------------------------
# format_orphan_alert_message — critical
# -----------------------------------------------------------------------------


def test_format_orphan_alert_includes_ticket_and_critical_marker():
    text = format_orphan_alert_message(
        ticket=99, symbol="NDX100", volume=1.0
    )
    assert "ORPHAN" in text.upper() or "CRITICAL" in text.upper()
    assert "99" in text
    assert "NDX100" in text
