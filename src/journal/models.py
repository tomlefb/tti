"""SQLAlchemy ORM models for the journal (Sprint 5).

Four tables:

- ``setups``        — every setup produced by ``build_setup_candidates``,
                      including those rejected post-detection (killzone
                      gating, RR threshold, …). ``was_notified`` distinguishes.
- ``decisions``     — operator's Telegram callback (Taken / Skipped). 0 or 1
                      per setup.
- ``outcomes``      — MT5 trade reconciliation, populated by the outcome
                      tracker. 0 or 1 per setup.
- ``daily_state``   — aggregate state per UTC date for hard stops (mostly
                      Sprint 6).

All datetimes are UTC. SQLite has no native datetime type — SQLAlchemy
maps ``DateTime`` to ISO-8601 text. Foreign keys are enforced at runtime
via the ``PRAGMA foreign_keys=ON`` event hook in ``db.py``.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base shared by every model in this module."""


class SetupRow(Base):
    """One row per detected setup candidate.

    ``setup_uid`` is the stable identity used across tables. Format:
    ``f"{symbol}_{timestamp_utc.isoformat()}"`` — same shape as the Sprint 4
    Telegram ``callback_data`` prefix produced by
    ``src.notification.telegram_bot._setup_id``.
    """

    __tablename__ = "setups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    setup_uid: Mapped[str] = mapped_column(String, unique=True, nullable=False)

    detected_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    timestamp_utc: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    symbol: Mapped[str] = mapped_column(String, nullable=False)
    killzone: Mapped[str] = mapped_column(String, nullable=False)
    direction: Mapped[str] = mapped_column(String, nullable=False)
    daily_bias: Mapped[str] = mapped_column(String, nullable=False)

    swept_level_type: Mapped[str] = mapped_column(String, nullable=False)
    swept_level_strength: Mapped[str] = mapped_column(String, nullable=False)
    swept_level_price: Mapped[float] = mapped_column(Float, nullable=False)

    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    stop_loss: Mapped[float] = mapped_column(Float, nullable=False)
    tp1_price: Mapped[float] = mapped_column(Float, nullable=False)
    tp1_rr: Mapped[float] = mapped_column(Float, nullable=False)
    tp_runner_price: Mapped[float] = mapped_column(Float, nullable=False)
    tp_runner_rr: Mapped[float] = mapped_column(Float, nullable=False)
    target_level_type: Mapped[str] = mapped_column(String, nullable=False)

    poi_type: Mapped[str] = mapped_column(String, nullable=False)
    quality: Mapped[str] = mapped_column(String, nullable=False)
    confluences: Mapped[str] = mapped_column(String, nullable=False)

    was_notified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rejection_reason: Mapped[str | None] = mapped_column(String, nullable=True)

    decision: Mapped[DecisionRow | None] = relationship(
        back_populates="setup", uselist=False, cascade="all, delete-orphan"
    )
    outcome: Mapped[OutcomeRow | None] = relationship(
        back_populates="setup", uselist=False, cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_setups_timestamp_utc", "timestamp_utc"),
        Index("ix_setups_symbol", "symbol"),
    )


class DecisionRow(Base):
    """Operator's Taken / Skipped click captured from Telegram."""

    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    setup_uid: Mapped[str] = mapped_column(
        String, ForeignKey("setups.setup_uid", ondelete="CASCADE"), unique=True, nullable=False
    )
    decision: Mapped[str] = mapped_column(String, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    note: Mapped[str | None] = mapped_column(String, nullable=True)

    setup: Mapped[SetupRow] = relationship(back_populates="decision")


class OutcomeRow(Base):
    """Realised MT5 outcome attached to a ``taken`` setup.

    Populated by the outcome tracker. ``mt5_ticket`` is NULL when no
    matching MT5 trade has been found yet (``exit_reason='unmatched'``)
    or when the trade is still open (``exit_reason='open'``).
    """

    __tablename__ = "outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    setup_uid: Mapped[str] = mapped_column(
        String, ForeignKey("setups.setup_uid", ondelete="CASCADE"), unique=True, nullable=False
    )
    mt5_ticket: Mapped[int | None] = mapped_column(Integer, nullable=True)
    entry_time_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    exit_time_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    entry_price_filled: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    realized_pnl_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_r: Mapped[float | None] = mapped_column(Float, nullable=True)
    matched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    setup: Mapped[SetupRow] = relationship(back_populates="outcome")

    __table_args__ = (
        Index("ix_outcomes_setup_uid", "setup_uid"),
        Index("ix_outcomes_exit_reason", "exit_reason"),
    )


class DailyStateRow(Base):
    """Aggregate state per UTC date — drives Sprint 6 hard stops.

    Sprint 5 only creates the schema and basic upsert. The detection
    pipeline can optionally cache its bias decisions here, but the
    scheduler in Sprint 6 owns full population.
    """

    __tablename__ = "daily_state"

    date: Mapped[date] = mapped_column(Date, primary_key=True)

    bias_xauusd_london: Mapped[str | None] = mapped_column(String, nullable=True)
    bias_xauusd_ny: Mapped[str | None] = mapped_column(String, nullable=True)
    bias_ndx100_london: Mapped[str | None] = mapped_column(String, nullable=True)
    bias_ndx100_ny: Mapped[str | None] = mapped_column(String, nullable=True)
    bias_eurusd_london: Mapped[str | None] = mapped_column(String, nullable=True)
    bias_eurusd_ny: Mapped[str | None] = mapped_column(String, nullable=True)
    bias_gbpusd_london: Mapped[str | None] = mapped_column(String, nullable=True)
    bias_gbpusd_ny: Mapped[str | None] = mapped_column(String, nullable=True)

    trades_taken_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consecutive_sl_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    daily_loss_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    daily_stop_triggered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
