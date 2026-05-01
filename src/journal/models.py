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
    """Aggregate state per UTC date — drives Sprint 6 hard stops + Sprint 7
    auto-trading kill flag.

    Sprint 5 only creates the schema and basic upsert. Sprint 6 wires the
    scheduler to populate trades_taken / loss / stop_triggered fields.
    Sprint 7 extends with ``auto_trading_disabled`` (set by the safe-guards
    layer when the daily-loss circuit breaker fires) and ``disabled_reason``
    (free-form tag, e.g. ``"daily_loss_circuit_breaker"``).
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
    bias_ethusd_london: Mapped[str | None] = mapped_column(String, nullable=True)
    bias_ethusd_ny: Mapped[str | None] = mapped_column(String, nullable=True)

    trades_taken_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consecutive_sl_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    daily_loss_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    daily_stop_triggered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Sprint 7 — auto-execution kill flag.
    auto_trading_disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    disabled_reason: Mapped[str | None] = mapped_column(String, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class OrderRow(Base):
    """One row per limit order placed by the auto-execution module (Sprint 7).

    ``mt5_ticket`` is the broker order/position identifier returned by
    ``mt5.order_send`` and is unique across the table. ``status`` walks
    the lifecycle ``pending → filled → (tp1_hit | tp_runner_hit | sl_hit |
    cancelled)`` — see ``src.execution.position_lifecycle`` for the
    transitions.

    ``setup_uid`` is the FK back to the originating setup so a journal
    query can join order → setup → outcome end-to-end.
    """

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    setup_uid: Mapped[str] = mapped_column(
        String, ForeignKey("setups.setup_uid", ondelete="CASCADE"), nullable=False
    )
    mt5_ticket: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)

    symbol: Mapped[str] = mapped_column(String, nullable=False)
    direction: Mapped[str] = mapped_column(String, nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False)

    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    stop_loss: Mapped[float] = mapped_column(Float, nullable=False)
    tp1: Mapped[float] = mapped_column(Float, nullable=False)
    tp_runner: Mapped[float] = mapped_column(Float, nullable=False)

    placed_at_utc: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    filled_at_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_at_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    realized_r: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        Index("ix_orders_setup_uid", "setup_uid"),
        Index("ix_orders_status", "status"),
        Index("ix_orders_placed_at_utc", "placed_at_utc"),
    )


class SpreadAnomalyRow(Base):
    """One row per spread anomaly observed at place_order time (Sprint 7).

    The system does NOT block on wide spreads (operator's call — see
    docs/04 §"Auto-execution rules"). Anomalies are journaled for
    post-mortem analysis instead. ``setup_uid`` is nullable so a periodic
    health-check observation outside any setup context can still be
    logged.
    """

    __tablename__ = "spread_anomalies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    detected_at_utc: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    spread: Mapped[float] = mapped_column(Float, nullable=False)
    typical_spread: Mapped[float | None] = mapped_column(Float, nullable=True)
    setup_uid: Mapped[str | None] = mapped_column(
        String, ForeignKey("setups.setup_uid", ondelete="SET NULL"), nullable=True
    )
    action_taken: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        Index("ix_spread_anomalies_symbol", "symbol"),
        Index("ix_spread_anomalies_detected_at_utc", "detected_at_utc"),
    )
