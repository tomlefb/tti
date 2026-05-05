"""CRUD primitives for the journal tables.

Functions, not classes. Each takes an open ``Session`` as its first
argument so the caller controls transaction lifetime via
``session_scope``.

The functions speak the ORM-row vocabulary defined in ``models.py`` and
the ``Setup`` dataclass from ``src.detection.setup`` (insert-only — the
journal never reconstructs ``Setup`` instances since ``Sweep`` / ``MSS``
sub-objects are not persisted).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.detection.setup import Setup
from src.journal.models import (
    DailyPnlRow,
    DailyStateRow,
    DecisionRow,
    OrderRow,
    OutcomeRow,
    RebalanceTransitionRow,
    RotationPositionRow,
    SetupRow,
    SpreadAnomalyRow,
)

logger = logging.getLogger(__name__)


def setup_uid_for(setup: Setup) -> str:
    """Stable identity for a ``Setup``.

    Mirrors the Sprint 4 ``_setup_id`` used as Telegram callback_data so
    the journal can be looked up directly from a callback payload.
    """
    return f"{setup.symbol}_{setup.timestamp_utc.isoformat()}"


def _now_utc() -> datetime:
    """Aware UTC ``datetime`` — single helper so tests can monkeypatch."""
    return datetime.now(tz=UTC)


def insert_setup(
    session: Session,
    setup: Setup,
    *,
    was_notified: bool,
    rejection_reason: str | None = None,
    detected_at: datetime | None = None,
) -> str:
    """Persist a ``Setup`` row. Idempotent on ``setup_uid``.

    If a row with the same ``setup_uid`` already exists, this is a no-op
    and the existing UID is returned — useful when the Sprint 6 scheduler
    re-runs the same minute or when scripts replay a fixture.

    Args:
        session: open SQLAlchemy session.
        setup: detection-pipeline output to persist.
        was_notified: ``True`` if a Telegram notification was sent for
            this setup, ``False`` if it was rejected by post-detection
            filters (killzone gating, RR, …).
        rejection_reason: free-form tag explaining why the setup was not
            notified. Must be ``None`` when ``was_notified`` is True.
        detected_at: when the detection cycle ran (UTC). Defaults to
            ``datetime.now(UTC)`` if not provided.

    Returns:
        The ``setup_uid`` of the inserted (or pre-existing) row.
    """
    if was_notified and rejection_reason is not None:
        raise ValueError("rejection_reason must be None when was_notified=True")

    uid = setup_uid_for(setup)
    existing = session.execute(
        select(SetupRow).where(SetupRow.setup_uid == uid)
    ).scalar_one_or_none()
    if existing is not None:
        return uid

    row = SetupRow(
        setup_uid=uid,
        detected_at=detected_at if detected_at is not None else _now_utc(),
        timestamp_utc=setup.timestamp_utc,
        symbol=setup.symbol,
        killzone=setup.killzone,
        direction=setup.direction,
        daily_bias=setup.daily_bias,
        swept_level_type=setup.swept_level_type,
        swept_level_strength=setup.swept_level_strength,
        swept_level_price=float(setup.swept_level_price),
        entry_price=float(setup.entry_price),
        stop_loss=float(setup.stop_loss),
        tp1_price=float(setup.tp1_price),
        tp1_rr=float(setup.tp1_rr),
        tp_runner_price=float(setup.tp_runner_price),
        tp_runner_rr=float(setup.tp_runner_rr),
        target_level_type=setup.target_level_type,
        poi_type=setup.poi_type,
        quality=setup.quality,
        confluences=json.dumps(list(setup.confluences)),
        was_notified=was_notified,
        rejection_reason=rejection_reason,
    )
    session.add(row)
    session.flush()
    return uid


def get_setup(session: Session, setup_uid: str) -> SetupRow | None:
    """Fetch a setup row by UID. ``None`` if not found."""
    return session.execute(
        select(SetupRow).where(SetupRow.setup_uid == setup_uid)
    ).scalar_one_or_none()


def list_setups(
    session: Session,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    symbol: str | None = None,
    quality: str | None = None,
    was_notified: bool | None = None,
) -> list[SetupRow]:
    """Filtered query for the dashboard.

    All filters are AND-combined. ``since`` / ``until`` apply to
    ``timestamp_utc`` (the MSS-confirm time the operator actually
    cares about), not ``detected_at``.
    """
    stmt = select(SetupRow)
    if since is not None:
        stmt = stmt.where(SetupRow.timestamp_utc >= since)
    if until is not None:
        stmt = stmt.where(SetupRow.timestamp_utc <= until)
    if symbol is not None:
        stmt = stmt.where(SetupRow.symbol == symbol)
    if quality is not None:
        stmt = stmt.where(SetupRow.quality == quality)
    if was_notified is not None:
        stmt = stmt.where(SetupRow.was_notified == was_notified)
    stmt = stmt.order_by(SetupRow.timestamp_utc.asc())
    return list(session.execute(stmt).scalars().all())


def insert_decision(
    session: Session,
    setup_uid: str,
    decision: str,
    decided_at: datetime,
    note: str | None = None,
) -> None:
    """Insert a Taken / Skipped decision. Raises if no setup or duplicate.

    Raises:
        ValueError: ``decision`` is not one of ``{"taken", "skipped"}``,
            no setup with ``setup_uid`` exists, or a decision already
            exists for that setup_uid.
    """
    if decision not in ("taken", "skipped"):
        raise ValueError(f"decision must be 'taken' or 'skipped', got {decision!r}")
    if get_setup(session, setup_uid) is None:
        raise ValueError(f"no setup with uid={setup_uid!r}")
    existing = session.execute(
        select(DecisionRow).where(DecisionRow.setup_uid == setup_uid)
    ).scalar_one_or_none()
    if existing is not None:
        raise ValueError(f"decision already recorded for setup_uid={setup_uid!r}")

    session.add(
        DecisionRow(
            setup_uid=setup_uid,
            decision=decision,
            decided_at=decided_at,
            note=note,
        )
    )
    session.flush()


def get_decision(session: Session, setup_uid: str) -> DecisionRow | None:
    """Fetch the (single) decision for a setup, ``None`` if no click yet."""
    return session.execute(
        select(DecisionRow).where(DecisionRow.setup_uid == setup_uid)
    ).scalar_one_or_none()


def upsert_outcome(session: Session, setup_uid: str, **fields: Any) -> None:
    """Insert or update an outcome row keyed by ``setup_uid``.

    Used by the outcome tracker which may re-run as a trade evolves
    (open → tp1_hit → tp_runner_hit). Unknown fields raise
    ``AttributeError`` so typos surface fast.
    """
    if get_setup(session, setup_uid) is None:
        raise ValueError(f"no setup with uid={setup_uid!r}")

    row = session.execute(
        select(OutcomeRow).where(OutcomeRow.setup_uid == setup_uid)
    ).scalar_one_or_none()
    if row is None:
        row = OutcomeRow(setup_uid=setup_uid)
        session.add(row)

    for key, value in fields.items():
        if not hasattr(OutcomeRow, key):
            raise AttributeError(f"OutcomeRow has no attribute {key!r}")
        setattr(row, key, value)
    if "matched_at" not in fields:
        row.matched_at = _now_utc()
    session.flush()


def get_outcome(session: Session, setup_uid: str) -> OutcomeRow | None:
    """Fetch the outcome row for a setup, or ``None``."""
    return session.execute(
        select(OutcomeRow).where(OutcomeRow.setup_uid == setup_uid)
    ).scalar_one_or_none()


def get_outcomes_to_match(session: Session) -> list[SetupRow]:
    """Return setups awaiting outcome reconciliation.

    A setup needs reconciliation when the operator clicked Taken AND
    either no outcome row exists, or the existing outcome is still
    flagged ``'open'``. Setups with terminal exit reasons
    (tp1_hit / tp_runner_hit / sl_hit / manual_close / unmatched) are
    skipped — reconciliation already settled them.
    """
    stmt = (
        select(SetupRow)
        .join(DecisionRow, DecisionRow.setup_uid == SetupRow.setup_uid)
        .where(DecisionRow.decision == "taken")
        .order_by(SetupRow.timestamp_utc.asc())
    )
    rows = list(session.execute(stmt).scalars().all())
    pending: list[SetupRow] = []
    for r in rows:
        outcome = r.outcome
        if outcome is None or outcome.exit_reason == "open":
            pending.append(r)
    return pending


def upsert_daily_state(session: Session, day: date, **fields: Any) -> None:
    """Insert or update a daily_state row keyed by ``day``."""
    row = session.get(DailyStateRow, day)
    if row is None:
        row = DailyStateRow(date=day, updated_at=_now_utc())
        session.add(row)
    for key, value in fields.items():
        if not hasattr(DailyStateRow, key) or key == "date":
            raise AttributeError(f"DailyStateRow has no settable attribute {key!r}")
        setattr(row, key, value)
    row.updated_at = _now_utc()
    session.flush()


def get_daily_state(session: Session, day: date) -> DailyStateRow | None:
    """Fetch the daily_state for ``day``, or ``None``."""
    return session.get(DailyStateRow, day)


# ---------------------------------------------------------------------------
# Sprint 7 — auto-execution surface
# ---------------------------------------------------------------------------


def insert_order(
    session: Session,
    *,
    setup_uid: str,
    mt5_ticket: int,
    symbol: str,
    direction: str,
    volume: float,
    entry_price: float,
    stop_loss: float,
    tp1: float,
    tp_runner: float,
    placed_at_utc: datetime,
    status: str,
    notes: str | None = None,
) -> OrderRow:
    """Persist an order row. Raises ``ValueError`` on duplicate ticket.

    Caller responsibilities:

    - ``setup_uid`` must reference an existing ``setups`` row (FK enforced
      by the schema; AttributeError surfaces fast if violated).
    - ``status`` is one of ``{"pending", "filled", "cancelled", "sl_hit",
      "tp1_hit", "tp_runner_hit"}``. The repository does not enumerate to
      keep this layer thin — :mod:`src.execution.position_lifecycle`
      drives the transitions.
    """
    existing = session.execute(
        select(OrderRow).where(OrderRow.mt5_ticket == mt5_ticket)
    ).scalar_one_or_none()
    if existing is not None:
        raise ValueError(f"order with ticket {mt5_ticket!r} already exists")

    row = OrderRow(
        setup_uid=setup_uid,
        mt5_ticket=int(mt5_ticket),
        symbol=symbol,
        direction=direction,
        volume=float(volume),
        entry_price=float(entry_price),
        stop_loss=float(stop_loss),
        tp1=float(tp1),
        tp_runner=float(tp_runner),
        placed_at_utc=placed_at_utc,
        status=status,
        notes=notes,
    )
    session.add(row)
    session.flush()
    return row


def get_order_by_ticket(session: Session, ticket: int) -> OrderRow | None:
    """Fetch one order by MT5 ticket. Returns ``None`` if not found."""
    return session.execute(
        select(OrderRow).where(OrderRow.mt5_ticket == int(ticket))
    ).scalar_one_or_none()


def get_order_by_setup_uid(session: Session, setup_uid: str) -> OrderRow | None:
    """Fetch the (single) order placed for a given setup. ``None`` if absent.

    A setup spawns at most one order under the standard pipeline — only
    the first ``place_order`` per detected setup runs (re-running a
    cycle on the same minute is idempotent at the journal layer via
    ``insert_setup``'s setup_uid uniqueness).
    """
    return session.execute(
        select(OrderRow).where(OrderRow.setup_uid == setup_uid)
    ).scalar_one_or_none()


def update_order_status(
    session: Session,
    *,
    ticket: int,
    status: str,
    **fields: Any,
) -> None:
    """Update an order's ``status`` and any of: ``filled_at_utc``,
    ``closed_at_utc``, ``realized_r``, ``notes``.

    Raises:
        ValueError: no order with that ticket.
        AttributeError: an unknown field name was passed (typo guard).
    """
    row = session.execute(
        select(OrderRow).where(OrderRow.mt5_ticket == int(ticket))
    ).scalar_one_or_none()
    if row is None:
        raise ValueError(f"no order with ticket={ticket!r}")

    row.status = status
    allowed = {"filled_at_utc", "closed_at_utc", "realized_r", "notes"}
    for key, value in fields.items():
        if key not in allowed:
            raise AttributeError(
                f"OrderRow has no settable field {key!r} (allowed: {sorted(allowed)})"
            )
        setattr(row, key, value)
    session.flush()


def list_open_orders_with_status(
    session: Session, *, statuses: list[str]
) -> list[OrderRow]:
    """List orders matching any of the given statuses, oldest first.

    Used by:

    - position lifecycle: ``["filled"]`` for active position monitoring.
    - end-of-killzone cleanup: ``["pending"]`` to cancel unfilled limits.
    - recovery on startup: ``["pending", "filled"]`` to reconcile against
      MT5 state.
    """
    stmt = (
        select(OrderRow)
        .where(OrderRow.status.in_(list(statuses)))
        .order_by(OrderRow.placed_at_utc.asc())
    )
    return list(session.execute(stmt).scalars().all())


def insert_spread_anomaly(
    session: Session,
    *,
    detected_at_utc: datetime,
    symbol: str,
    spread: float,
    typical_spread: float | None = None,
    setup_uid: str | None = None,
    action_taken: str | None = None,
) -> SpreadAnomalyRow:
    """Persist a spread anomaly row (no duplicate check — anomalies are
    timestamped events, not unique on any natural key)."""
    row = SpreadAnomalyRow(
        detected_at_utc=detected_at_utc,
        symbol=symbol,
        spread=float(spread),
        typical_spread=float(typical_spread) if typical_spread is not None else None,
        setup_uid=setup_uid,
        action_taken=action_taken,
    )
    session.add(row)
    session.flush()
    return row


def disable_auto_trading_for_day(
    session: Session, *, day: date, reason: str
) -> None:
    """Flip ``auto_trading_disabled=True`` and set ``disabled_reason`` for
    the given trading day. Creates the daily_state row if absent.

    Other columns on an existing row (bias_*, trades_taken_count,
    daily_loss_usd, …) are left untouched.
    """
    row = session.get(DailyStateRow, day)
    if row is None:
        row = DailyStateRow(date=day, updated_at=_now_utc())
        session.add(row)
    row.auto_trading_disabled = True
    row.disabled_reason = reason
    row.updated_at = _now_utc()
    session.flush()


def is_auto_trading_disabled(session: Session, *, day: date) -> bool:
    """Return ``True`` iff the daily_state row for ``day`` flags trading off."""
    row = session.get(DailyStateRow, day)
    if row is None:
        return False
    return bool(row.auto_trading_disabled)


# ---------------------------------------------------------------------------
# Rotation strategy — rebalance transitions, open positions, daily P&L
# ---------------------------------------------------------------------------


def rebalance_uid_for(strategy: str, timestamp_utc: datetime) -> str:
    """Stable identity for a rebalance: ``"{strategy}_{ts.isoformat()}"``."""
    return f"{strategy}_{timestamp_utc.isoformat()}"


def insert_rebalance_transition(
    session: Session,
    *,
    strategy: str,
    timestamp_utc: datetime,
    basket_before: list[str],
    basket_after: list[str],
    closed_assets: list[str],
    opened_assets: list[str],
    capital_at_rebalance_usd: float,
    risk_per_trade_pct: float,
    notes: str | None = None,
) -> str:
    """Persist a rebalance row. Idempotent on the (strategy, timestamp_utc) key.

    Returns the ``rebalance_uid`` so the caller can attach
    :class:`RotationPositionRow` rows to it via the FK.
    """
    uid = rebalance_uid_for(strategy, timestamp_utc)
    existing = session.execute(
        select(RebalanceTransitionRow).where(RebalanceTransitionRow.rebalance_uid == uid)
    ).scalar_one_or_none()
    if existing is not None:
        return uid
    row = RebalanceTransitionRow(
        rebalance_uid=uid,
        timestamp_utc=timestamp_utc,
        strategy=strategy,
        basket_before=json.dumps(sorted(basket_before)),
        basket_after=json.dumps(sorted(basket_after)),
        closed_assets=json.dumps(sorted(closed_assets)),
        opened_assets=json.dumps(sorted(opened_assets)),
        capital_at_rebalance_usd=float(capital_at_rebalance_usd),
        risk_per_trade_pct=float(risk_per_trade_pct),
        notes=notes,
    )
    session.add(row)
    session.flush()
    return uid


def insert_rotation_position(
    session: Session,
    *,
    strategy: str,
    symbol: str,
    mt5_ticket: int,
    direction: str,
    volume: float,
    entry_price: float,
    atr_at_entry: float,
    risk_usd: float,
    entry_timestamp_utc: datetime,
    entry_rebalance_uid: str | None,
) -> RotationPositionRow:
    """Insert a new ``status='open'`` rotation position row."""
    row = RotationPositionRow(
        strategy=strategy,
        symbol=symbol,
        mt5_ticket=int(mt5_ticket),
        direction=direction,
        volume=float(volume),
        entry_price=float(entry_price),
        atr_at_entry=float(atr_at_entry),
        risk_usd=float(risk_usd),
        entry_timestamp_utc=entry_timestamp_utc,
        entry_rebalance_uid=entry_rebalance_uid,
        status="open",
    )
    session.add(row)
    session.flush()
    return row


def get_open_rotation_positions(
    session: Session, *, strategy: str
) -> list[RotationPositionRow]:
    """Return every ``status='open'`` rotation position for ``strategy``."""
    stmt = (
        select(RotationPositionRow)
        .where(RotationPositionRow.strategy == strategy)
        .where(RotationPositionRow.status == "open")
        .order_by(RotationPositionRow.entry_timestamp_utc)
    )
    return list(session.execute(stmt).scalars())


def get_open_rotation_position(
    session: Session, *, strategy: str, symbol: str
) -> RotationPositionRow | None:
    """Return the single open rotation position for (strategy, symbol), or None."""
    stmt = (
        select(RotationPositionRow)
        .where(RotationPositionRow.strategy == strategy)
        .where(RotationPositionRow.symbol == symbol)
        .where(RotationPositionRow.status == "open")
    )
    return session.execute(stmt).scalar_one_or_none()


def close_rotation_position(
    session: Session,
    *,
    mt5_ticket: int,
    exit_price: float,
    exit_timestamp_utc: datetime,
    exit_rebalance_uid: str | None,
    realized_r: float,
    realized_pnl_usd: float,
) -> RotationPositionRow | None:
    """Flip a rotation position from ``'open'`` to ``'closed'``.

    Returns the updated row, or ``None`` if no open row matched the
    ticket. Missing-row case is logged-only — production may have
    closed the MT5 position outside the journal (manual intervention)
    and the rotation cycle should still proceed.
    """
    stmt = select(RotationPositionRow).where(
        RotationPositionRow.mt5_ticket == int(mt5_ticket)
    )
    row = session.execute(stmt).scalar_one_or_none()
    if row is None:
        logger.warning(
            "close_rotation_position: ticket=%d not found in journal", mt5_ticket
        )
        return None
    if row.status != "open":
        logger.warning(
            "close_rotation_position: ticket=%d already in status=%r",
            mt5_ticket,
            row.status,
        )
        return row
    row.status = "closed"
    row.exit_price = float(exit_price)
    row.exit_timestamp_utc = exit_timestamp_utc
    row.exit_rebalance_uid = exit_rebalance_uid
    row.realized_r = float(realized_r)
    row.realized_pnl_usd = float(realized_pnl_usd)
    session.flush()
    return row


def upsert_rotation_daily_pnl(
    session: Session,
    *,
    day: date,
    current_balance_usd: float,
    daily_loss_limit_remaining_usd: float,
    opening_balance_usd: float | None = None,
) -> DailyPnlRow:
    """Insert the day's row if absent (capturing ``opening_balance``); otherwise
    refresh ``current_balance``, ``daily_pnl``, and the remaining-limit field.

    ``opening_balance_usd`` is read on first call of the day from the live
    MT5 account; subsequent calls leave it untouched. ``daily_pnl_usd`` is
    derived as ``current_balance - opening_balance``.
    """
    row = session.get(DailyPnlRow, day)
    if row is None:
        if opening_balance_usd is None:
            opening_balance_usd = current_balance_usd
        row = DailyPnlRow(
            date=day,
            opening_balance_usd=float(opening_balance_usd),
            current_balance_usd=float(current_balance_usd),
            daily_pnl_usd=float(current_balance_usd) - float(opening_balance_usd),
            daily_loss_limit_remaining_usd=float(daily_loss_limit_remaining_usd),
            updated_at=_now_utc(),
        )
        session.add(row)
    else:
        row.current_balance_usd = float(current_balance_usd)
        row.daily_pnl_usd = float(current_balance_usd) - row.opening_balance_usd
        row.daily_loss_limit_remaining_usd = float(daily_loss_limit_remaining_usd)
        row.updated_at = _now_utc()
    session.flush()
    return row


def get_rotation_daily_pnl(session: Session, *, day: date) -> DailyPnlRow | None:
    return session.get(DailyPnlRow, day)
