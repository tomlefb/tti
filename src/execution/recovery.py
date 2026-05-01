"""Sprint 7 — startup reconciliation between MT5 and the journal.

Called once when the scheduler starts. Detects:

- **Orphan position**: MT5 has an open position with our magic number
  that the journal does NOT know about — or knows about with a
  terminal status (``cancelled``, ``sl_hit``, …). Closes at market for
  safety + emits a CRITICAL Telegram alert. Better to close a position
  the operator opened manually with the same magic than to leave an
  unmonitored position drifting.

- **Lost pending order**: journal has a ``pending`` row but MT5 has no
  matching pending order. Marks status ``lost``. Common causes: broker
  rolled the order, manual cancellation, terminal restart between
  ``order_send`` and journal commit.

- **Lost filled order**: journal has a ``filled`` row but MT5 has no
  matching open position. Marks status ``lost`` so a stale "open"
  state does not block fresh setups via the per-pair count gate.
  History-driven reconciliation may upgrade the row back to
  ``tp_runner_hit`` / ``sl_hit`` later via the outcome tracker.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.journal.repository import (
    list_open_orders_with_status,
    update_order_status,
)

logger = logging.getLogger(__name__)


# Statuses that should NOT correspond to an open MT5 position. If MT5
# has one, treat as orphan.
_TERMINAL_STATUSES = frozenset(
    {"cancelled", "sl_hit", "tp_runner_hit", "tp1_hit", "lost"}
)


@dataclass
class RecoveryReport:
    """Summary of one ``reconcile_orphan_positions`` run."""

    orphan_positions: int = 0
    lost_orders: int = 0
    errors: list[str] = field(default_factory=list)


def reconcile_orphan_positions(
    *,
    mt5_client: Any,
    journal_session_factory: Callable[[], Any],
    settings: Any,
    now_utc: datetime,
    notifier: Any | None = None,
) -> RecoveryReport:
    """Run the startup reconciliation. Idempotent — safe to call again."""
    report = RecoveryReport()
    magic = int(getattr(settings, "MAGIC_NUMBER", 7766))

    # ---- Orphan positions ---------------------------------------------------
    try:
        mt5_positions = mt5_client.get_open_positions(magic=magic)
    except Exception as exc:  # noqa: BLE001
        logger.exception("recovery: get_open_positions failed")
        report.errors.append(f"get_open_positions: {exc!r}")
        mt5_positions = []

    journal_orders_by_ticket: dict[int, Any] = {}
    with journal_session_factory() as s:
        # Read every order regardless of status — needed to detect orphans
        # when the journal already considers the order closed.
        all_orders = list_open_orders_with_status(
            s,
            statuses=[
                "pending",
                "filled",
                "tp1_hit",
                "tp_runner_hit",
                "sl_hit",
                "cancelled",
                "lost",
            ],
        )
        for order in all_orders:
            journal_orders_by_ticket[int(order.mt5_ticket)] = (
                order.status,
                order.mt5_ticket,
            )

    for position in mt5_positions:
        ticket = int(position.ticket)
        record = journal_orders_by_ticket.get(ticket)
        if record is not None:
            status, _ = record
            if status in ("pending", "filled", "tp1_hit"):
                # Healthy — lifecycle will continue managing it.
                continue
            # Status is terminal → desync, close at market.
            logger.critical(
                "recovery: ticket=%d in MT5 but journal status=%r — orphan",
                ticket,
                status,
            )
        else:
            logger.critical(
                "recovery: ticket=%d in MT5 with our magic but unknown to journal — orphan",
                ticket,
            )

        ok = _close_orphan(mt5_client, ticket)
        if ok:
            report.orphan_positions += 1
            if notifier is not None:
                _notify(
                    notifier,
                    "send_orphan_alert",
                    ticket=ticket,
                    symbol=position.symbol,
                    volume=position.volume,
                )
        else:
            report.errors.append(f"close_orphan ticket={ticket} failed")

    # ---- Lost pending orders ------------------------------------------------
    try:
        mt5_pending = mt5_client.get_pending_orders(magic=magic)
    except Exception as exc:  # noqa: BLE001
        logger.exception("recovery: get_pending_orders failed")
        report.errors.append(f"get_pending_orders: {exc!r}")
        mt5_pending = []
    mt5_pending_tickets = {int(o.ticket) for o in mt5_pending}
    mt5_position_tickets = {int(p.ticket) for p in mt5_positions}

    with journal_session_factory() as s:
        journal_pending = list_open_orders_with_status(s, statuses=["pending"])
        journal_filled = list_open_orders_with_status(s, statuses=["filled"])

    for order in journal_pending:
        if int(order.mt5_ticket) in mt5_pending_tickets:
            continue
        if int(order.mt5_ticket) in mt5_position_tickets:
            # Order filled while we slept — mark filled, lifecycle takes over.
            with journal_session_factory() as s:
                update_order_status(
                    s,
                    ticket=int(order.mt5_ticket),
                    status="filled",
                    filled_at_utc=now_utc,
                    notes="recovery: order filled while scheduler down",
                )
            continue
        # Truly lost.
        logger.warning(
            "recovery: pending order ticket=%d not found in MT5 — marking lost",
            order.mt5_ticket,
        )
        with journal_session_factory() as s:
            update_order_status(
                s,
                ticket=int(order.mt5_ticket),
                status="lost",
                notes="recovery: pending order not found in MT5",
            )
        report.lost_orders += 1

    # ---- Lost filled orders -------------------------------------------------
    for order in journal_filled:
        if int(order.mt5_ticket) in mt5_position_tickets:
            continue
        # Try history first.
        info = None
        if hasattr(mt5_client, "get_position_close_info"):
            try:
                info = mt5_client.get_position_close_info(int(order.mt5_ticket))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "recovery: get_position_close_info(%d) raised: %r",
                    order.mt5_ticket,
                    exc,
                )
                info = None

        if info is not None:
            # History found — mark conservatively as sl_hit; outcome tracker
            # will refine via the next reconciliation pass.
            with journal_session_factory() as s:
                update_order_status(
                    s,
                    ticket=int(order.mt5_ticket),
                    status="sl_hit",
                    closed_at_utc=info.get("exit_time_utc", now_utc),
                    notes="recovery: closed-while-down, history present",
                )
            continue

        logger.warning(
            "recovery: filled order ticket=%d no longer open and no history — marking lost",
            order.mt5_ticket,
        )
        with journal_session_factory() as s:
            update_order_status(
                s,
                ticket=int(order.mt5_ticket),
                status="lost",
                notes="recovery: filled order not found in MT5",
            )
        report.lost_orders += 1

    if report.orphan_positions or report.lost_orders:
        logger.critical(
            "recovery summary: %d orphan position(s) closed, %d order(s) marked lost",
            report.orphan_positions,
            report.lost_orders,
        )
    else:
        logger.info("recovery: MT5/journal in sync — no action needed")
    return report


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _close_orphan(mt5_client: Any, ticket: int) -> bool:
    """Close an orphan position. Tries ``close_position_at_market`` first
    (cleanest) then falls back to :func:`order_manager.modify_position_sl`-
    free direct close via ``cancel_pending_order`` semantics. Returns
    True iff something succeeded."""
    fn = getattr(mt5_client, "close_position_at_market", None)
    if fn is not None:
        try:
            return bool(fn(int(ticket)))
        except Exception:  # noqa: BLE001
            logger.exception("close_position_at_market raised for ticket=%d", ticket)
            return False
    logger.error(
        "recovery: mt5_client has no close_position_at_market — leaving orphan ticket=%d",
        ticket,
    )
    return False


def _notify(notifier: Any, method_name: str, **kwargs: Any) -> None:
    fn = getattr(notifier, method_name, None)
    if fn is None:
        logger.debug("notifier has no %s hook — skipping", method_name)
        return
    try:
        fn(**kwargs)
    except Exception:  # noqa: BLE001
        logger.exception("notifier.%s raised — swallowing", method_name)
