"""Sprint 7 — position-lifecycle polling and end-of-killzone cleanup.

The lifecycle owns the post-place-order trajectory:

- ``pending → filled``         when MT5 reports a position with our ticket.
- ``filled → tp1_hit``         when current price crosses TP1; the
                               lifecycle closes 50% of volume at market
                               and moves SL to break-even on the
                               remainder.
- ``filled → tp_runner_hit``   when MT5 closes the remainder at TP_runner.
- ``filled → sl_hit``          when MT5 closes the remainder at SL.
- ``pending → cancelled``      at end of killzone if not filled.

Convention: ``mt5_ticket`` stored in the journal is the order ticket
returned by ``order_send.order``. In hedging-mode accounts (FundedNext
default), the resulting position carries the same identifier so this
polling loop can match journal rows to MT5 positions one-to-one. If
the broker uses netting mode, the matching layer needs to be revised
(documented as a known limitation in docs/04 §"Auto-execution rules").

The lifecycle does NOT touch the detection pipeline. It reads the
``orders`` journal table + MT5 state and emits status updates +
Telegram notifications.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.execution.order_manager import (
    cancel_order as _cancel_order,
    modify_position_sl as _modify_position_sl,
)
from src.journal.models import OrderRow, SetupRow
from src.journal.repository import (
    list_open_orders_with_status,
    update_order_status,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass
class LifecycleReport:
    """Summary of a single ``check_open_positions`` cycle."""

    filled: int = 0
    tp1_hit: int = 0
    tp_runner_hit: int = 0
    sl_hit: int = 0
    errors: dict[int, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# check_open_positions
# ---------------------------------------------------------------------------


def check_open_positions(
    *,
    mt5_client: Any,
    journal_session_factory: Callable[[], Any],
    settings: Any,
    now_utc: datetime,
    notifier: Any | None = None,
) -> LifecycleReport:
    """Poll MT5 + journal, transition order statuses, fire actions.

    Triggered every ``LIFECYCLE_CHECK_INTERVAL_SEC`` seconds by the
    scheduler. Cheap operation — at most a few MT5 reads per call.
    """
    report = LifecycleReport()
    magic = int(getattr(settings, "MAGIC_NUMBER", 7766))
    tp1_partial_fraction = float(getattr(settings, "TP1_PARTIAL_FRACTION", 0.5))

    try:
        mt5_positions = mt5_client.get_open_positions(magic=magic)
    except Exception as exc:  # noqa: BLE001
        logger.exception("check_open_positions: get_open_positions failed")
        return report
    positions_by_ticket = {int(p.ticket): p for p in mt5_positions}

    with journal_session_factory() as s:
        # Include ``tp1_hit`` so the runner-exit / BE-stop reconciliation
        # fires when the remaining half of a partially-closed position
        # finally closes on MT5.
        journal_orders = list_open_orders_with_status(
            s, statuses=["pending", "filled", "tp1_hit"]
        )

    for order in journal_orders:
        try:
            if order.status == "pending":
                _handle_pending(
                    order,
                    positions_by_ticket,
                    mt5_client,
                    journal_session_factory,
                    now_utc,
                    notifier,
                    report,
                )
            elif order.status == "filled":
                _handle_filled(
                    order,
                    positions_by_ticket,
                    mt5_client,
                    journal_session_factory,
                    settings,
                    tp1_partial_fraction,
                    now_utc,
                    notifier,
                    report,
                )
            elif order.status == "tp1_hit":
                # After TP1 partial close, the remaining half rides until
                # TP_runner OR the BE-stop. Reconcile when the position
                # vanishes from MT5.
                if order.mt5_ticket not in positions_by_ticket:
                    _reconcile_closed_position(
                        order,
                        mt5_client,
                        journal_session_factory,
                        now_utc,
                        notifier,
                        report,
                    )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "lifecycle error for ticket=%d status=%s",
                order.mt5_ticket,
                order.status,
            )
            report.errors[order.mt5_ticket] = repr(exc)

    return report


# ---------------------------------------------------------------------------
# pending → filled
# ---------------------------------------------------------------------------


def _handle_pending(
    order: OrderRow,
    positions_by_ticket: dict[int, Any],
    mt5_client: Any,
    journal_session_factory: Callable[[], Any],
    now_utc: datetime,
    notifier: Any | None,
    report: LifecycleReport,
) -> None:
    if order.mt5_ticket not in positions_by_ticket:
        # Still pending in MT5 (or already cancelled — recovery handles).
        return
    # Order filled.
    with journal_session_factory() as s:
        update_order_status(
            s, ticket=order.mt5_ticket, status="filled", filled_at_utc=now_utc
        )
    report.filled += 1
    if notifier is not None:
        _notify(notifier, "send_order_filled", order=order, ticket=order.mt5_ticket)


# ---------------------------------------------------------------------------
# filled → (tp1_hit | tp_runner_hit | sl_hit)
# ---------------------------------------------------------------------------


def _handle_filled(
    order: OrderRow,
    positions_by_ticket: dict[int, Any],
    mt5_client: Any,
    journal_session_factory: Callable[[], Any],
    settings: Any,
    tp1_partial_fraction: float,
    now_utc: datetime,
    notifier: Any | None,
    report: LifecycleReport,
) -> None:
    position = positions_by_ticket.get(order.mt5_ticket)
    if position is None:
        # Position closed by MT5 (TP_runner or SL) — reconcile from history.
        _reconcile_closed_position(
            order, mt5_client, journal_session_factory, now_utc, notifier, report
        )
        return

    # Position still open. Decide whether to trigger TP1 partial close.
    # Skip if partial already executed (volume reduced).
    if float(position.volume) < float(order.volume) - 1e-9:
        return  # partial already done, nothing to do until close

    # Read current price.
    try:
        symbol_info = mt5_client.get_symbol_info(order.symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "lifecycle: get_symbol_info(%s) failed (%r) — skipping",
            order.symbol,
            exc,
        )
        return

    if not _tp1_hit(order, symbol_info):
        return

    # Trigger TP1 partial close + SL move to BE.
    volume_to_close = float(order.volume) * float(tp1_partial_fraction)
    try:
        ok = bool(
            mt5_client.close_partial_position(
                ticket=int(order.mt5_ticket), volume=volume_to_close
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("close_partial_position raised for ticket=%d", order.mt5_ticket)
        return
    if not ok:
        logger.warning(
            "close_partial_position returned False for ticket=%d", order.mt5_ticket
        )
        return

    # Move SL to BE (= entry).
    _modify_position_sl(
        ticket=int(order.mt5_ticket),
        new_sl=float(order.entry_price),
        mt5_client=mt5_client,
    )

    # Update journal.
    with journal_session_factory() as s:
        update_order_status(
            s,
            ticket=order.mt5_ticket,
            status="tp1_hit",
            notes=f"TP1 partial closed {volume_to_close:.4f}, SL → BE",
        )
    report.tp1_hit += 1
    if notifier is not None:
        _notify(
            notifier,
            "send_tp1_hit",
            order=order,
            ticket=order.mt5_ticket,
            partial_volume=volume_to_close,
        )


def _tp1_hit(order: OrderRow, symbol_info: Any) -> bool:
    """``True`` iff the current price has crossed the order's TP1.

    For long: hit when bid ≥ TP1 (book-side at which the operator could
    actually realise the partial).
    For short: hit when ask ≤ TP1 (same logic mirrored).
    """
    bid = float(getattr(symbol_info, "bid", 0.0))
    ask = float(getattr(symbol_info, "ask", 0.0))
    tp1 = float(order.tp1)
    if order.direction == "long":
        return bid >= tp1
    if order.direction == "short":
        return ask <= tp1
    raise ValueError(f"unknown direction {order.direction!r}")


# ---------------------------------------------------------------------------
# Position-closed reconciliation
# ---------------------------------------------------------------------------


def _reconcile_closed_position(
    order: OrderRow,
    mt5_client: Any,
    journal_session_factory: Callable[[], Any],
    now_utc: datetime,
    notifier: Any | None,
    report: LifecycleReport,
) -> None:
    """Determine whether a vanished position closed at TP_runner or SL.

    Reads ``mt5_client.get_position_close_info(ticket)`` (or the broader
    history surface) for the last exit price + profit.
    """
    info: dict[str, Any] | None = None
    if hasattr(mt5_client, "get_position_close_info"):
        try:
            info = mt5_client.get_position_close_info(int(order.mt5_ticket))
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "get_position_close_info raised for ticket=%d", order.mt5_ticket
            )
            return

    if info is None:
        # No history yet — try again next cycle. Don't mark as closed.
        logger.info(
            "lifecycle: ticket=%d no longer open but history not yet available — "
            "deferring reconciliation",
            order.mt5_ticket,
        )
        return

    exit_price = float(info.get("exit_price", 0.0))
    profit_usd = info.get("profit_usd")
    # Prefer profit-based R when available (handles blended TP1-partial +
    # runner outcomes correctly). Fall back to exit-price-based R when the
    # broker history doesn't report profit.
    realized_r = _realized_r(
        order, exit_price=exit_price, profit_usd=profit_usd, mt5_client=mt5_client
    )
    exit_reason = _classify_exit(order, exit_price)

    with journal_session_factory() as s:
        update_order_status(
            s,
            ticket=order.mt5_ticket,
            status=exit_reason,
            closed_at_utc=info.get("exit_time_utc", now_utc),
            realized_r=realized_r,
            notes=f"reconciled: exit_price={exit_price}",
        )

    if exit_reason == "tp_runner_hit":
        report.tp_runner_hit += 1
        if notifier is not None:
            _notify(
                notifier,
                "send_tp_runner_hit",
                order=order,
                ticket=order.mt5_ticket,
                exit_price=exit_price,
                realized_r=realized_r,
            )
    elif exit_reason == "sl_hit":
        report.sl_hit += 1
        if notifier is not None:
            _notify(
                notifier,
                "send_sl_hit",
                order=order,
                ticket=order.mt5_ticket,
                exit_price=exit_price,
                realized_r=realized_r,
            )


def _classify_exit(order: OrderRow, exit_price: float) -> str:
    """Return ``"tp_runner_hit"`` | ``"sl_hit"`` | ``"manual_close"``.

    Tolerance: 0.1% of entry price — same scale used elsewhere in the
    codebase (sweep dedup, swing-level matching).
    """
    tolerance = abs(float(order.entry_price)) * 0.001
    if abs(exit_price - float(order.tp_runner)) <= tolerance * 5:
        # 5× to absorb broker slippage on a runner-distance close.
        return "tp_runner_hit"
    if abs(exit_price - float(order.stop_loss)) <= tolerance * 5:
        return "sl_hit"
    # Some other exit — manual close, BE-stop after TP1, etc.
    # If post-TP1 (SL was moved to entry), an exit at entry classifies as BE.
    if abs(exit_price - float(order.entry_price)) <= tolerance * 5:
        return "sl_hit"  # BE-stop counts as the SL leg from the operator's POV
    return "sl_hit"  # default conservative — outcome tracker can refine


def _realized_r(
    order: OrderRow,
    *,
    exit_price: float,
    profit_usd: float | None = None,
    mt5_client: Any | None = None,
) -> float:
    """Compute realised R for a closed position.

    Prefer ``profit_usd / initial_risk_usd`` when both are available — this
    correctly captures BLENDED outcomes (TP1 partial + runner close).
    Fall back to ``(exit_price − entry_price) / sl_distance`` (sign-flipped
    for shorts) when profit is not reported, which only matches the
    full-close case.
    """
    risk_price = abs(float(order.entry_price) - float(order.stop_loss))
    if risk_price <= 0:
        return 0.0

    if profit_usd is not None and mt5_client is not None:
        try:
            symbol_info = mt5_client.get_symbol_info(order.symbol)
            contract_size = float(getattr(symbol_info, "trade_contract_size", 0.0))
        except Exception:  # noqa: BLE001
            contract_size = 0.0
        if contract_size > 0:
            initial_risk_usd = (
                risk_price * float(order.volume) * contract_size
            )
            if initial_risk_usd > 0:
                return float(profit_usd) / initial_risk_usd

    move = float(exit_price) - float(order.entry_price)
    if order.direction == "short":
        move = -move
    return move / risk_price


# ---------------------------------------------------------------------------
# end_of_killzone_cleanup
# ---------------------------------------------------------------------------


def end_of_killzone_cleanup(
    *,
    mt5_client: Any,
    journal_session_factory: Callable[[], Any],
    settings: Any,
    killzone: Literal["london", "ny"],
    now_utc: datetime,
    notifier: Any | None = None,
) -> int:
    """Cancel every pending limit order whose setup belongs to ``killzone``.

    Triggered by APScheduler at killzone close (12:00 / 18:00 Paris).
    Returns the number of orders cancelled.
    """
    cancelled = 0
    with journal_session_factory() as s:
        # Join orders → setups to filter by killzone.
        stmt = (
            select(OrderRow, SetupRow.killzone)
            .join(SetupRow, SetupRow.setup_uid == OrderRow.setup_uid)
            .where(OrderRow.status == "pending")
            .where(SetupRow.killzone == killzone)
        )
        targets = [
            (order, kz) for order, kz in s.execute(stmt).all()
        ]
        ticket_list = [int(o.mt5_ticket) for o, _ in targets]

    for ticket in ticket_list:
        ok = _cancel_order(
            ticket=ticket,
            mt5_client=mt5_client,
            journal_session_factory=journal_session_factory,
            reason=f"end_of_{killzone}_killzone",
            now_utc=now_utc,
        )
        if ok:
            cancelled += 1
            if notifier is not None:
                _notify(notifier, "send_order_cancelled", ticket=ticket, reason=killzone)

    if ticket_list:
        logger.info(
            "end_of_killzone_cleanup(%s): %d/%d orders cancelled",
            killzone,
            cancelled,
            len(ticket_list),
        )
    return cancelled


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _notify(notifier: Any, method_name: str, *args: Any, **kwargs: Any) -> None:
    fn = getattr(notifier, method_name, None)
    if fn is None:
        logger.debug("notifier has no %s hook — skipping", method_name)
        return
    try:
        fn(*args, **kwargs)
    except Exception:  # noqa: BLE001
        logger.exception("notifier.%s raised — swallowing", method_name)
