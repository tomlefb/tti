"""Telegram-compatible HTML formatter for ``Setup`` notifications.

Pure function — no I/O, no globals. Output is the caption attached to the
chart PNG sent by ``telegram_bot.send_setup``. Consumers of the bot may
also use it standalone (e.g. ``scripts/test_notification.py`` prints the
caption to stdout for visual review).

Format conventions (per Sprint 4 spec):

- Quality emoji: ``A → 🅰️``, ``A+ → 🅰️➕``, ``B → 🅱️``.
- Direction (LONG/SHORT) upper-case, in bold, on the title line.
- Time block: ``YYYY-MM-DD HH:MM TZ`` (UTC) where TZ is ``LON`` (London
  killzone) or ``NY`` (NY killzone). A ``(Paris: HH:MM)`` parenthetical
  follows for operator convenience — DST-correct via zoneinfo.
- Price precision is per-symbol — XAUUSD 2 dp, NDX100 1 dp, FX 5 dp.
- TP_R line is emitted only when the runner RR differs from TP1 RR
  (i.e. the runner extends beyond the partial-exit cap). When emitted
  AND the setup carries the ``high_rr_runner`` confluence flag, the
  runner line ends with 🚀 to signal the operator to scale at TP1.
- Confluences are listed verbatim (snake_case) — Sprint 5+ may prettify.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

from src.detection.setup import Setup

_TZ_PARIS = ZoneInfo("Europe/Paris")

_QUALITY_EMOJI: dict[str, str] = {
    "A": "🅰️",
    "A+": "🅰️➕",
    "B": "🅱️",
}

_KILLZONE_LABEL: dict[str, str] = {
    "london": "LON",
    "ny": "NY",
}

# Per-symbol decimal precision. Falls back to ``_DEFAULT_PRECISION`` for
# unknown symbols so that operator-added pairs render reasonably without a
# code change. Matches docs/01 §7's per-instrument note implicitly:
# XAUUSD trades in USD/cent (2 dp), NDX100 in points (1 dp), FX in pip+sub-pip
# (5 dp).
_PRICE_PRECISION: dict[str, int] = {
    "XAUUSD": 2,
    "NDX100": 1,
    "EURUSD": 5,
    "GBPUSD": 5,
}
_DEFAULT_PRECISION = 2


def _format_price(symbol: str, price: float) -> str:
    digits = _PRICE_PRECISION.get(symbol, _DEFAULT_PRECISION)
    return f"{price:.{digits}f}"


def format_setup_message(setup: Setup) -> str:
    """Build the HTML caption for ``setup``.

    Args:
        setup: A fully-built ``Setup`` produced by the orchestrator.

    Returns:
        HTML-formatted string suitable for Telegram ``parse_mode="HTML"``.
    """
    emoji = _QUALITY_EMOJI.get(setup.quality, setup.quality)
    direction = setup.direction.upper()
    kz_label = _KILLZONE_LABEL.get(setup.killzone, setup.killzone.upper())

    ts_utc = setup.timestamp_utc
    ts_paris = ts_utc.astimezone(_TZ_PARIS)
    time_line = (
        f"<code>{ts_utc.strftime('%Y-%m-%d %H:%M')} {kz_label}</code>"
        f" (Paris: {ts_paris.strftime('%H:%M')})"
    )

    fmt = lambda p: _format_price(setup.symbol, p)  # noqa: E731

    risk = abs(setup.entry_price - setup.stop_loss)

    lines: list[str] = [
        f"{emoji} <b>{setup.symbol} {direction}</b>",
        time_line,
        f"<b>Entry:</b> {fmt(setup.entry_price)}",
        f"<b>SL:</b>    {fmt(setup.stop_loss)} (risk {fmt(risk)})",
        f"<b>TP1:</b>   {fmt(setup.tp1_price)} (RR {setup.tp1_rr:.2f})",
    ]

    # TP_R line only when the runner extends past the partial cap (TP1 was
    # capped). 🚀 only when the operator-facing ``high_rr_runner`` flag is
    # set — kept distinct from "RR happens to be high" because that flag
    # is the grader's signal that the runner is tradable, not just a
    # by-product of an extended leg.
    if setup.tp_runner_rr != setup.tp1_rr:
        runner_line = f"<b>TP_R:</b>  {fmt(setup.tp_runner_price)} (RR {setup.tp_runner_rr:.2f})"
        if "high_rr_runner" in setup.confluences:
            runner_line += " 🚀"
        lines.append(runner_line)

    lines.append(f"<b>Bias:</b> {setup.daily_bias}")
    lines.append(f"<b>Sweep:</b> {setup.swept_level_type} ({setup.swept_level_strength})")
    lines.append(f"<b>POI:</b> {setup.poi_type}")
    lines.append(
        "<b>Confluences:</b> " + (", ".join(setup.confluences) if setup.confluences else "—")
    )

    return "\n".join(lines)
