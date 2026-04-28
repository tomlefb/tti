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
from src.journal.models import DailyStateRow, DecisionRow, OutcomeRow, SetupRow

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
