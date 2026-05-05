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


# ---------------------------------------------------------------------------
# Rotation strategy — adaptive risk + per-rebalance pre-checks
# ---------------------------------------------------------------------------


def adaptive_risk_per_trade_pct(
    *,
    current_capital_usd: float,
    capital_floor_for_full_risk_usd: float,
    risk_full_pct: float,
    risk_reduced_pct: float,
) -> float:
    """Pick the per-trade risk fraction based on the live capital level.

    Implements the operator's $4,950 / 1.0 % vs <$4,950 / 0.5 % rule
    parametrically — the breakpoint and both rates come from settings so
    tests and config changes don't need to touch this code.

    Returns a fraction (e.g. ``0.005`` = 0.5 %) suitable for ``risk_usd =
    capital × fraction`` calls downstream. Both inputs are clipped to
    >= 0; a zero or negative capital returns the reduced rate.
    """
    capital = max(0.0, float(current_capital_usd))
    floor = float(capital_floor_for_full_risk_usd)
    full = float(risk_full_pct)
    reduced = float(risk_reduced_pct)
    return full if capital >= floor else reduced


def check_rotation_pre_rebalance(
    journal_session: Session,
    *,
    settings: Any,
    now_utc: datetime,
    current_capital_usd: float,
    daily_pnl_usd: float,
) -> tuple[bool, str | None]:
    """Pre-flight gate run once at the START of a rotation rebalance cycle.

    Runs the rotation-specific safety checks in order:

    1. Kill switch file present → block.
    2. Day-disabled flag (set by hard-stops on a critical fault) → block.
    3. Live capital below safe floor (``settings.ROTATION_CAPITAL_FLOOR_USD``)
       → block. Existing positions are left alone; only *new* opens are
       blocked.
    4. Daily P&L breached the hard daily-loss limit
       (``settings.DAILY_LOSS_LIMIT_USD``) → block. The 75 % soft warning
       is fired separately by the cycle, not here.

    Returns ``(allowed, reason_if_blocked)``. The first failing gate
    wins. Compatible with :func:`check_pre_trade` patterns — same
    return shape, distinct reason codes for cleaner Telegram routing.
    """
    kill_path = getattr(settings, "KILL_SWITCH_PATH", None)
    if kill_switch_active(kill_path):
        return False, "kill_switch"

    today_paris = now_utc.astimezone(_TZ_PARIS).date()
    if is_auto_trading_disabled(journal_session, day=today_paris):
        return False, "auto_trading_disabled"

    floor = float(getattr(settings, "ROTATION_CAPITAL_FLOOR_USD", 0.0))
    if floor > 0 and float(current_capital_usd) < floor:
        return False, "capital_below_safe_threshold"

    hard_limit = float(getattr(settings, "DAILY_LOSS_LIMIT_USD", 0.0))
    if hard_limit > 0 and float(daily_pnl_usd) <= -hard_limit:
        return False, "daily_loss_limit_reached"

    return True, None


def check_rotation_per_trade(
    *,
    symbol: str,
    volume: float,
    symbol_info: Any,
    margin_required_usd: float | None,
    margin_free_usd: float | None,
    spread: float,
    typical_spread: float | None,
    spread_anomaly_multiplier: float = 3.0,
) -> tuple[bool, str | None]:
    """Per-trade pre-flight for a single rotation order.

    Distinct from :func:`check_pre_trade` because rotation entries have
    no ``Setup`` (no SL/TP/RR) — the only inputs are the symbol, the
    computed volume, and the live broker info. Checks:

    1. Volume is between the broker's volume_min and volume_max.
    2. If margin info is supplied, ``margin_required_usd <= margin_free_usd``.
    3. Spread anomaly is logged BUT does not block (matches the TJR
       convention; the boolean returned reflects whether the anomaly
       fired so the caller can journal it).

    Returns ``(allowed, reason_if_blocked)``. ``allowed=True`` always
    when only spread is anomalous.
    """
    vmin = float(getattr(symbol_info, "volume_min", 0.0))
    vmax = float(getattr(symbol_info, "volume_max", 0.0))
    v = float(volume)
    if v < vmin:
        return False, f"volume_below_minimum:{symbol}:{v}<{vmin}"
    if vmax > 0 and v > vmax:
        return False, f"volume_above_maximum:{symbol}:{v}>{vmax}"

    if margin_required_usd is not None and margin_free_usd is not None:
        if float(margin_required_usd) > float(margin_free_usd):
            return False, (
                f"insufficient_margin:{symbol}:"
                f"req={margin_required_usd:.2f}>free={margin_free_usd:.2f}"
            )

    # Spread anomaly is logged-only (caller decides whether to journal);
    # this function does not block on it. Return value documents whether
    # it fired so the caller can act.
    _ = should_log_spread_anomaly(
        current=spread, typical=typical_spread, multiplier=spread_anomaly_multiplier
    )
    return True, None
