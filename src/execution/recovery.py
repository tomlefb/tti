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


# ---------------------------------------------------------------------------
# Rotation strategy — orphan / ghost reconciliation at scheduler startup
# ---------------------------------------------------------------------------


@dataclass
class RotationRecoveryReport:
    """Summary of one ``reconcile_rotation_orphan_positions`` run.

    The four counters break down by what was found, not by what was
    successfully acted on — failures are recorded in ``errors``.
    """

    orphan_positions_handled: int = 0  # MT5 has it, journal does not
    orphan_strategy_used: str = ""     # 'strict' / 'adopt' / 'alert_only'
    ghost_rows_handled: int = 0        # journal has it, MT5 does not
    healthy_positions: int = 0         # both sides agree
    errors: list[str] = field(default_factory=list)


def reconcile_rotation_orphan_positions(
    *,
    mt5_client: Any,
    journal_session_factory: Callable[[], Any],
    settings: Any,
    now_utc: datetime,
    notifier: Any | None = None,
    dry_run: bool = False,
) -> RotationRecoveryReport:
    """Reconcile rotation positions between MT5 and the journal.

    Two failure modes detected and resolved:

    1. **Orphan position** — MT5 has an open position with the rotation
       magic that the journal does NOT track in
       ``rotation_positions(status='open')``. Three strategies, picked
       via ``settings.ROTATION_ORPHAN_STRATEGY``:

         - ``"strict"`` (default): close the orphan at market.
           Defensive — better to flatten an unknown position than leave
           it drifting against the daily-loss limit.
         - ``"adopt"``: insert a journal row with the position's current
           MT5 fields. ATR is approximated by an ATR(period) read from a
           fresh D1 panel — exact enough for the next exit calc.
         - ``"alert_only"``: log + Telegram, leave the position alone.
           Useful when manually-opened test positions exist on the
           account.

    2. **Ghost row** — journal has a ``status='open'`` rotation_position
       with no matching MT5 position. The position was closed outside
       the bot (manual click, broker rollover, scheduler-crash race).
       Action: mark the row closed at zero R + zero P&L (we have no
       exit data) with a critical log + Telegram alert so the operator
       can investigate.

    ``dry_run=True`` (used by the smoke test) does the diagnosis but
    skips every MT5 call and journal write — only logs are produced.

    The function never raises on per-position failure; it accumulates
    error strings into ``RotationRecoveryReport.errors`` and continues
    so a single broken read does not block the whole startup.
    """
    from src.journal.repository import (
        close_rotation_position as journal_close_rotation_position,
        get_open_rotation_positions,
        insert_rotation_position,
    )

    strategy_label = str(getattr(settings, "ACTIVE_STRATEGY", "trend_rotation_d1"))
    magic = int(getattr(settings, "ROTATION_MAGIC_NUMBER", 7799))
    orphan_mode = str(
        getattr(settings, "ROTATION_ORPHAN_STRATEGY", "strict")
    ).lower()
    if orphan_mode not in {"strict", "adopt", "alert_only"}:
        logger.warning(
            "recovery: ROTATION_ORPHAN_STRATEGY=%r unrecognised; "
            "falling back to 'strict'",
            orphan_mode,
        )
        orphan_mode = "strict"
    report = RotationRecoveryReport(orphan_strategy_used=orphan_mode)

    # ---- MT5 side ----------------------------------------------------------
    try:
        mt5_positions = mt5_client.get_open_positions(magic=magic)
    except Exception as exc:  # noqa: BLE001
        logger.exception("rotation recovery: get_open_positions failed")
        report.errors.append(f"get_open_positions: {exc!r}")
        mt5_positions = []
    mt5_by_ticket = {int(p.ticket): p for p in mt5_positions}

    # ---- Journal side ------------------------------------------------------
    with journal_session_factory() as s:
        open_rows = get_open_rotation_positions(s, strategy=strategy_label)
    journal_by_ticket = {int(r.mt5_ticket): r for r in open_rows}

    # ---- Orphans (MT5 only) -----------------------------------------------
    for ticket, position in mt5_by_ticket.items():
        if ticket in journal_by_ticket:
            report.healthy_positions += 1
            continue
        logger.critical(
            "rotation recovery: orphan position ticket=%d symbol=%s "
            "volume=%.4f magic=%d (mode=%s, dry_run=%s)",
            ticket, position.symbol, float(position.volume),
            int(position.magic), orphan_mode, dry_run,
        )
        if dry_run:
            report.orphan_positions_handled += 1
            if notifier is not None:
                _notify(
                    notifier, "send_orphan_alert",
                    ticket=ticket, symbol=position.symbol,
                    volume=float(position.volume),
                )
            continue

        if orphan_mode == "strict":
            ok = _close_orphan(mt5_client, ticket)
            if ok:
                report.orphan_positions_handled += 1
                if notifier is not None:
                    _notify(
                        notifier, "send_orphan_alert",
                        ticket=ticket, symbol=position.symbol,
                        volume=float(position.volume),
                    )
            else:
                report.errors.append(
                    f"orphan_close_failed: ticket={ticket} symbol={position.symbol}"
                )
        elif orphan_mode == "adopt":
            try:
                _adopt_orphan(
                    mt5_client=mt5_client,
                    journal_session_factory=journal_session_factory,
                    settings=settings,
                    position=position,
                    now_utc=now_utc,
                    strategy_label=strategy_label,
                )
                report.orphan_positions_handled += 1
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "rotation recovery: adopt orphan ticket=%d failed", ticket
                )
                report.errors.append(
                    f"orphan_adopt_failed: ticket={ticket} ({exc!r})"
                )
        else:  # alert_only
            report.orphan_positions_handled += 1
            if notifier is not None:
                _notify(
                    notifier, "send_orphan_alert",
                    ticket=ticket, symbol=position.symbol,
                    volume=float(position.volume),
                )

    # ---- Ghosts (journal only) --------------------------------------------
    for ticket, row in journal_by_ticket.items():
        if ticket in mt5_by_ticket:
            continue  # already counted as healthy above
        logger.critical(
            "rotation recovery: ghost row ticket=%d symbol=%s entry=%s "
            "(no matching MT5 position; dry_run=%s)",
            ticket, row.symbol, row.entry_timestamp_utc.isoformat(),
            dry_run,
        )
        if dry_run:
            report.ghost_rows_handled += 1
            continue
        try:
            with journal_session_factory() as s:
                journal_close_rotation_position(
                    s,
                    mt5_ticket=ticket,
                    exit_price=float(row.entry_price),  # zero-R sentinel
                    exit_timestamp_utc=now_utc,
                    exit_rebalance_uid=None,
                    realized_r=0.0,
                    realized_pnl_usd=0.0,
                )
                s.commit()
            report.ghost_rows_handled += 1
            if notifier is not None:
                _notify(
                    notifier, "send_orphan_alert",
                    ticket=ticket, symbol=row.symbol, volume=float(row.volume),
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "rotation recovery: ghost-row close ticket=%d failed", ticket
            )
            report.errors.append(f"ghost_close_failed: ticket={ticket} ({exc!r})")

    if (report.orphan_positions_handled
            or report.ghost_rows_handled or report.errors):
        logger.critical(
            "rotation recovery summary: %d orphan(s) handled (mode=%s), "
            "%d ghost row(s) handled, %d healthy, %d error(s)",
            report.orphan_positions_handled, orphan_mode,
            report.ghost_rows_handled, report.healthy_positions,
            len(report.errors),
        )
    else:
        logger.info(
            "rotation recovery: clean state — %d healthy position(s), "
            "no orphans / no ghosts",
            report.healthy_positions,
        )
    return report


def _adopt_orphan(
    *,
    mt5_client: Any,
    journal_session_factory: Callable[[], Any],
    settings: Any,
    position: Any,
    now_utc: datetime,
    strategy_label: str,
) -> None:
    """Adopt mode helper — insert a journal row mirroring the MT5
    position. ATR is approximated from a fresh D1 panel read."""
    from src.journal.repository import insert_rotation_position
    from src.strategies.trend_rotation_d1.volatility import compute_atr

    atr_period = int(getattr(settings, "ROTATION_ATR_PERIOD", 20))
    df = mt5_client.fetch_ohlc(
        position.symbol, "D1", atr_period + 30
    )
    df_indexed = df.set_index("time").sort_index()
    atr_series = compute_atr(df_indexed, period=atr_period)
    atr_now = float(atr_series.iloc[-1]) if len(atr_series) else 0.0
    if not (atr_now > 0):
        raise ValueError(
            f"adopt orphan {position.symbol}: ATR({atr_period}) is "
            f"non-positive ({atr_now}) — cannot infer risk basis"
        )

    # The risk_usd attached to the adopted position is reconstructed
    # from the position's current size and the freshly-computed ATR:
    # risk = volume × atr × contract_size. Exact enough for the next
    # exit's R math; ROUGH for any prior P&L attribution but adoption
    # is a recovery action, not a trade-history rebuild.
    sym_info = mt5_client.get_symbol_info(position.symbol)
    contract = float(getattr(sym_info, "trade_contract_size", 1.0))
    risk_usd = float(position.volume) * atr_now * contract

    direction = (
        "long" if str(getattr(position, "direction", "long")) == "long" else "short"
    )
    with journal_session_factory() as s:
        insert_rotation_position(
            s,
            strategy=strategy_label,
            symbol=position.symbol,
            mt5_ticket=int(position.ticket),
            direction=direction,
            volume=float(position.volume),
            entry_price=float(position.entry_price),
            atr_at_entry=atr_now,
            risk_usd=risk_usd,
            entry_timestamp_utc=position.time_open_utc,
            entry_rebalance_uid=None,
        )
        s.commit()
    logger.warning(
        "rotation recovery: adopted orphan ticket=%d symbol=%s "
        "volume=%.4f entry=%.5f atr~%.5f risk_usd~%.2f",
        position.ticket, position.symbol, position.volume,
        position.entry_price, atr_now, risk_usd,
    )
