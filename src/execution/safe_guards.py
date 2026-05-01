"""Sprint 7 — pre-trade safe-guards layer.

Stacks on top of Sprint 6's :mod:`src.scheduler.hard_stops`:

- :func:`check_pre_trade` evaluates two NEW gates before delegating to
  :func:`hard_stops.is_blocked` for the financial / daily-stop / consecutive-SL
  checks. The two new gates are:

    1. The ``KILL_SWITCH`` file at the project root — manual hard-disable.
    2. The ``daily_state.auto_trading_disabled`` flag — set by
       :func:`disable_for_day` when a critical fault is observed mid-cycle.

- :func:`log_spread_anomaly` writes to the ``spread_anomalies`` table when
  the live spread exceeds ``typical × multiplier``. The system NEVER blocks
  on wide spreads (operator's design call — see docs/04 §"Auto-execution
  rules"); anomalies are journaled for post-mortem only.

- :func:`disable_for_day` is the kill-flag setter — used by
  :mod:`src.execution.order_manager` when a critical fault is observed
  mid-cycle.

Why two layers (hard_stops + safe_guards) rather than one: hard_stops
predates Sprint 7 and gates Telegram notifications. Auto-execution adds
gates on top (kill switch + day-disabled flag) that should NOT also
block notifications — the operator wants to be alerted to setups even
when auto-execution is paused, so they can decide manually.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from src.journal.repository import (
    insert_spread_anomaly,
    is_auto_trading_disabled,
)
from src.scheduler.hard_stops import BlockReason, is_blocked

if TYPE_CHECKING:
    from src.detection.setup import Setup
    from src.journal.models import SpreadAnomalyRow
    from src.mt5_client.client import MT5Client

logger = logging.getLogger(__name__)

_TZ_PARIS = ZoneInfo("Europe/Paris")

# Default project-root path for the KILL_SWITCH file. Tests pass an
# explicit path; the runtime invocation reads ``settings.KILL_SWITCH_PATH``
# (or falls back to this constant).
_DEFAULT_KILL_SWITCH_PATH = Path("KILL_SWITCH")


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


def kill_switch_active(path: Path | None = None) -> bool:
    """Return ``True`` iff the kill-switch file exists at ``path``.

    The file content is irrelevant — its mere presence trips the gate.
    The operator drops a free-form note (e.g. "manual stop, ops chat
    2026-05-01") for the post-mortem trail.
    """
    target = Path(path) if path is not None else _DEFAULT_KILL_SWITCH_PATH
    return target.exists()


# ---------------------------------------------------------------------------
# check_pre_trade
# ---------------------------------------------------------------------------


def check_pre_trade(
    journal_session: Session,
    mt5_client: "MT5Client",
    settings: Any,
    *,
    setup: "Setup",
    now_utc: datetime,
) -> tuple[bool, str | None]:
    """Run every pre-trade gate; return ``(allowed, reason_if_blocked)``.

    Order of checks:

    1. **Kill switch** — short-circuits before any I/O (hard-disable).
    2. **auto_trading_disabled flag** — short-circuits before MT5 I/O.
    3. **hard_stops.is_blocked** — full delegation: account info,
       max-loss, daily-loss, news blackout, daily trade count,
       consecutive SL, per-pair count.

    The first gate to fire wins. ``reason`` is the gate's stable code
    (e.g. ``"kill_switch"``, ``"daily_loss_reached"``) — used for
    structured logging and Telegram skip notifications.
    """
    kill_path = getattr(settings, "KILL_SWITCH_PATH", None)
    if kill_switch_active(kill_path):
        return False, "kill_switch"

    today_paris = now_utc.astimezone(_TZ_PARIS).date()
    if is_auto_trading_disabled(journal_session, day=today_paris):
        return False, "auto_trading_disabled"

    block: BlockReason | None = is_blocked(
        journal_session,
        mt5_client,
        settings,
        pair=setup.symbol,
        now_utc=now_utc,
    )
    if block is not None:
        return False, block.code

    return True, None


# ---------------------------------------------------------------------------
# Spread anomaly
# ---------------------------------------------------------------------------


def should_log_spread_anomaly(
    *,
    current: float,
    typical: float | None,
    multiplier: float = 3.0,
) -> bool:
    """Return ``True`` iff ``current > typical × multiplier``.

    Returns ``False`` when ``typical`` is ``None`` — without a baseline,
    no anomaly judgment is possible. The default multiplier (3.0)
    matches docs/04 §"Auto-execution rules".
    """
    if typical is None or typical <= 0:
        return False
    return float(current) > float(typical) * float(multiplier)


def log_spread_anomaly(
    journal_session: Session,
    *,
    symbol: str,
    current_spread: float,
    typical_spread: float | None,
    setup_uid: str | None,
    detected_at_utc: datetime,
    action_taken: str | None = "executed_anyway",
) -> "SpreadAnomalyRow":
    """Persist a spread anomaly row.

    The system does NOT block on wide spreads (operator design call).
    ``action_taken`` defaults to ``"executed_anyway"`` for that reason;
    pass ``"logged_no_setup"`` (or any free-form tag) for context-less
    observations from a periodic health check.
    """
    logger.warning(
        "spread anomaly: %s spread=%.4f typical=%s — %s",
        symbol,
        current_spread,
        f"{typical_spread:.4f}" if typical_spread is not None else "n/a",
        action_taken,
    )
    return insert_spread_anomaly(
        journal_session,
        detected_at_utc=detected_at_utc,
        symbol=symbol,
        spread=current_spread,
        typical_spread=typical_spread,
        setup_uid=setup_uid,
        action_taken=action_taken,
    )


# ---------------------------------------------------------------------------
# disable_for_day
# ---------------------------------------------------------------------------


def disable_for_day(
    journal_session: Session, *, day, reason: str
) -> None:
    """Flip ``auto_trading_disabled=True`` on ``day``'s daily_state row.

    Thin wrapper over :func:`src.journal.repository.disable_auto_trading_for_day`
    so the execution layer doesn't import the journal repository directly
    (cleaner module boundary; the rest of ``src/execution/`` only knows
    about ``safe_guards``).
    """
    from src.journal.repository import disable_auto_trading_for_day

    disable_auto_trading_for_day(journal_session, day=day, reason=reason)
    logger.critical("auto-trading disabled for %s — reason=%s", day, reason)
