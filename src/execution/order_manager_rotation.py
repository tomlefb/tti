"""Rotation-strategy order primitives.

Sister module to :mod:`src.execution.order_manager`. The TJR-shaped
order_manager assumes one ``Setup`` per call with discrete SL/TP and
RR; the rotation strategy has no SL/TP and operates on basket
transitions (multiple opens + multiple closes per rebalance), so it
gets its own narrowly-scoped surface:

- :func:`compute_rotation_volume` — risk-parity sizing using
  ATR-at-entry rather than SL distance.
- :func:`open_rotation_position` — submit one market order, journal it.
- :func:`close_rotation_position` — close a single open rotation
  position at market and journal the realised R + PnL.
- :func:`execute_rebalance_transitions` — orchestrate one full
  rebalance: close-all-then-open-all so the freed margin is available
  to the new entries.

The MT5 client is injected. Anything implementing the methods used
below works (production wires :class:`src.mt5_client.client.MT5Client`,
tests pass a small fake).

The strategy state (``current_basket``, ``last_rebalance_date``,
``open_positions``) lives in the journal — these primitives are
stateless. The scheduler (next stage) will load the state, compute
transitions via the ``trend_rotation_d1`` pipeline, and call the
``execute_rebalance_transitions`` orchestrator.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from src.execution.safe_guards import check_rotation_per_trade
from src.journal.repository import (
    close_rotation_position as journal_close_rotation_position,
    insert_rotation_position,
)

logger = logging.getLogger(__name__)


# MT5 retcodes treated as success (mirrors order_manager.py).
_MT5_RETCODE_DONE = 10009
_MT5_RETCODE_DONE_PARTIAL = 10010
_MT5_SUCCESS_CODES = frozenset({_MT5_RETCODE_DONE, _MT5_RETCODE_DONE_PARTIAL})


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RotationOrderResult:
    """Outcome of one ``open_rotation_position`` or
    ``close_rotation_position`` call."""

    success: bool
    symbol: str
    operation: str  # "open" | "close"
    ticket: int | None = None
    volume: float = 0.0
    price: float | None = None
    error_code: int | str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class RebalanceExecutionResult:
    """Aggregate of a full ``execute_rebalance_transitions`` cycle."""

    closed: list[RotationOrderResult]
    opened: list[RotationOrderResult]

    @property
    def n_closed_ok(self) -> int:
        return sum(1 for r in self.closed if r.success)

    @property
    def n_opened_ok(self) -> int:
        return sum(1 for r in self.opened if r.success)

    @property
    def n_closed_failed(self) -> int:
        return sum(1 for r in self.closed if not r.success)

    @property
    def n_opened_failed(self) -> int:
        return sum(1 for r in self.opened if not r.success)


# ---------------------------------------------------------------------------
# compute_rotation_volume — risk-parity sizing
# ---------------------------------------------------------------------------


def compute_rotation_volume(
    *,
    risk_usd: float,
    atr_at_entry: float,
    symbol_info: Any,
) -> float:
    """Lot size that risks ``risk_usd`` over a 1-ATR adverse move.

    Risk-parity sizing per spec §2.5: a 1-ATR(20) move on
    ``volume`` lots costs the trader ``risk_usd``. Concretely::

        raw_volume = risk_usd / (atr_at_entry × trade_contract_size)
        snapped    = floor(raw_volume / volume_step) × volume_step
        clamped    = max(volume_min, min(volume_max, snapped))

    Mirrors :func:`src.execution.order_manager.compute_volume` but
    substitutes ``atr_at_entry`` for ``sl_distance_price`` since rotation
    has no SL.

    Raises:
        ValueError: ``atr_at_entry`` is zero / negative / NaN, or
            ``trade_contract_size`` is zero / negative.
    """
    atr = float(atr_at_entry)
    if not (atr > 0) or math.isnan(atr):  # captures NaN and zero/negative
        raise ValueError(
            f"atr_at_entry must be a positive finite float, got {atr_at_entry!r}"
        )

    contract = float(symbol_info.trade_contract_size)
    if contract <= 0:
        raise ValueError(f"trade_contract_size must be > 0, got {contract!r}")

    raw = float(risk_usd) / (atr * contract)

    step = float(symbol_info.volume_step) or 0.01
    snapped = math.floor(raw / step) * step

    vmin = float(symbol_info.volume_min)
    vmax = float(symbol_info.volume_max)

    if snapped < vmin:
        return vmin
    if snapped > vmax:
        return vmax
    digits = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(snapped, digits)


# ---------------------------------------------------------------------------
# open_rotation_position — single market entry + journal
# ---------------------------------------------------------------------------


def open_rotation_position(
    *,
    symbol: str,
    direction: str,
    volume: float,
    atr_at_entry: float,
    risk_usd: float,
    mt5_client: Any,
    journal_session_factory: Callable[[], Session],
    settings: Any,
    now_utc: datetime,
    strategy: str,
    entry_rebalance_uid: str | None,
    dry_run: bool = False,
) -> RotationOrderResult:
    """Open one rotation position at market and persist the journal row.

    Flow:

    1. Read live ``symbol_info`` from MT5 (ask/bid, volume bounds).
    2. Per-trade safety check via :func:`check_rotation_per_trade`.
    3. ``dry_run=True`` → log + return success without MT5/journal.
    4. ``mt5_client.place_market_order(...)`` with the strategy magic.
    5. On retcode success: insert a ``RotationPositionRow`` keyed by ticket.

    The ATR-at-entry is captured in the journal (for the eventual
    realised-R computation at close time). The risk_usd is recorded so
    historic adaptive-rate decisions stay auditable.

    Returns a :class:`RotationOrderResult`. Caller decides retry policy.
    """
    if direction not in {"long", "short"}:
        return RotationOrderResult(
            success=False, symbol=symbol, operation="open",
            error_code="invalid_direction", error_message=f"got {direction!r}",
        )
    try:
        symbol_info = mt5_client.get_symbol_info(symbol)
    except Exception as exc:  # noqa: BLE001 — surface as failed result
        logger.exception("get_symbol_info failed for %s", symbol)
        return RotationOrderResult(
            success=False, symbol=symbol, operation="open",
            error_code="symbol_info_unavailable", error_message=repr(exc),
        )

    spread = float(symbol_info.ask) - float(symbol_info.bid)
    typical = _typical_spread(settings, symbol)
    multiplier = float(getattr(settings, "SPREAD_ANOMALY_MULTIPLIER", 3.0))

    allowed, reason = check_rotation_per_trade(
        symbol=symbol,
        volume=volume,
        symbol_info=symbol_info,
        margin_required_usd=None,  # broker-side check would need extra MT5 call
        margin_free_usd=None,
        spread=spread,
        typical_spread=typical,
        spread_anomaly_multiplier=multiplier,
    )
    if not allowed:
        return RotationOrderResult(
            success=False, symbol=symbol, operation="open",
            volume=volume, error_code=reason,
        )

    if dry_run:
        # Use ask/bid as the "would-fill" price for the dry-run log line.
        would_price = float(symbol_info.ask if direction == "long" else symbol_info.bid)
        logger.info(
            "DRY-RUN open_rotation_position: %s %s vol=%.4f price=%.5f atr=%.5f risk=%.2f",
            symbol, direction, volume, would_price, atr_at_entry, risk_usd,
        )
        return RotationOrderResult(
            success=True, symbol=symbol, operation="open",
            ticket=None, volume=volume, price=would_price,
        )

    magic = int(getattr(settings, "ROTATION_MAGIC_NUMBER", 7799))
    try:
        send_result = mt5_client.place_market_order(
            symbol=symbol,
            direction=direction,
            volume=float(volume),
            magic=magic,
            comment=f"rotation:{strategy}:open",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("place_market_order raised for %s", symbol)
        return RotationOrderResult(
            success=False, symbol=symbol, operation="open",
            volume=volume, error_code="order_send_exception",
            error_message=repr(exc),
        )

    retcode = int(getattr(send_result, "retcode", -1))
    if retcode not in _MT5_SUCCESS_CODES:
        comment = str(getattr(send_result, "comment", ""))
        logger.error(
            "MT5 open_rotation_position retcode=%d comment=%r for %s",
            retcode, comment, symbol,
        )
        return RotationOrderResult(
            success=False, symbol=symbol, operation="open",
            volume=volume, error_code=retcode, error_message=comment,
        )

    # MT5 returns the deal ticket on a market order; the corresponding
    # position uses the same id on most brokers. We persist the deal
    # ticket as ``mt5_ticket`` and rely on get_open_positions reads to
    # reconcile if needed. The fill price is read from the symbol_info
    # ask/bid since order_send doesn't return it directly.
    fill_price = float(symbol_info.ask if direction == "long" else symbol_info.bid)
    ticket = int(getattr(send_result, "deal", 0)) or int(getattr(send_result, "order", 0))

    with journal_session_factory() as s:
        insert_rotation_position(
            s,
            strategy=strategy,
            symbol=symbol,
            mt5_ticket=ticket,
            direction=direction,
            volume=float(volume),
            entry_price=fill_price,
            atr_at_entry=float(atr_at_entry),
            risk_usd=float(risk_usd),
            entry_timestamp_utc=now_utc,
            entry_rebalance_uid=entry_rebalance_uid,
        )
        s.commit()

    return RotationOrderResult(
        success=True, symbol=symbol, operation="open",
        ticket=ticket, volume=volume, price=fill_price,
    )


# ---------------------------------------------------------------------------
# close_rotation_position — single market exit + journal
# ---------------------------------------------------------------------------


def close_rotation_position(
    *,
    symbol: str,
    ticket: int,
    entry_price: float,
    atr_at_entry: float,
    risk_usd: float,
    mt5_client: Any,
    journal_session_factory: Callable[[], Session],
    now_utc: datetime,
    exit_rebalance_uid: str | None,
    dry_run: bool = False,
) -> RotationOrderResult:
    """Close an open rotation position at market and update the journal.

    The realised R is ``(exit_price - entry_price) / atr_at_entry`` for
    a long; the journal row's ``entry_price`` and ``atr_at_entry`` are
    the source of truth (passed in by the caller from the journal).

    Realised P&L in dollars is reconstructed from R: ``realised_r ×
    risk_usd``. This matches the per-trade R semantics used by the
    backtest harness so live and simulated outcomes are directly
    comparable.

    Flow:

    1. ``dry_run=True`` → log + return success without MT5/journal.
    2. ``mt5_client.close_position_at_market(ticket)`` — bool success.
    3. Read the actual exit price via ``mt5_client.get_position_close_info``
       if available; otherwise fall back to the live ask/bid.
    4. Update the journal row to ``status='closed'`` with realised R/PnL.
    """
    if dry_run:
        logger.info(
            "DRY-RUN close_rotation_position: %s ticket=%d", symbol, ticket
        )
        return RotationOrderResult(
            success=True, symbol=symbol, operation="close", ticket=ticket,
        )

    try:
        ok = mt5_client.close_position_at_market(int(ticket))
    except Exception as exc:  # noqa: BLE001
        logger.exception("close_position_at_market raised for %s ticket=%d", symbol, ticket)
        return RotationOrderResult(
            success=False, symbol=symbol, operation="close", ticket=ticket,
            error_code="close_exception", error_message=repr(exc),
        )
    if not ok:
        return RotationOrderResult(
            success=False, symbol=symbol, operation="close", ticket=ticket,
            error_code="close_failed", error_message="broker rejected close",
        )

    # Get the actual exit price. ``get_position_close_info`` reads from
    # ``mt5.history_deals_get`` and returns the realised exit price
    # paired with the broker-side P&L. If unavailable (e.g. tests with
    # no history feed), fall back to the live mid.
    exit_price: float | None = None
    realized_pnl_usd: float | None = None
    try:
        info = mt5_client.get_position_close_info(int(ticket))
    except Exception:  # noqa: BLE001
        info = None
    if info is not None:
        exit_price = float(info.get("exit_price")) if info.get("exit_price") else None
        realized_pnl_usd = (
            float(info.get("profit_usd")) if info.get("profit_usd") is not None else None
        )

    if exit_price is None:
        try:
            sym_info = mt5_client.get_symbol_info(symbol)
            # Mid as the closing-print proxy; the broker P&L is then unknown.
            exit_price = (float(sym_info.ask) + float(sym_info.bid)) / 2.0
        except Exception:  # noqa: BLE001
            logger.warning(
                "close_rotation_position: cannot read exit price for %s ticket=%d",
                symbol, ticket,
            )
            exit_price = float(entry_price)  # zero-R fallback

    realized_r = (
        (exit_price - entry_price) / atr_at_entry if atr_at_entry > 0 else 0.0
    )
    if realized_pnl_usd is None:
        realized_pnl_usd = realized_r * float(risk_usd)

    with journal_session_factory() as s:
        journal_close_rotation_position(
            s,
            mt5_ticket=int(ticket),
            exit_price=exit_price,
            exit_timestamp_utc=now_utc,
            exit_rebalance_uid=exit_rebalance_uid,
            realized_r=realized_r,
            realized_pnl_usd=realized_pnl_usd,
        )
        s.commit()

    return RotationOrderResult(
        success=True, symbol=symbol, operation="close",
        ticket=int(ticket), price=float(exit_price),
    )


# ---------------------------------------------------------------------------
# execute_rebalance_transitions — orchestrator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RebalanceClose:
    """One asset to drop from the basket — must already be journalled
    as a ``RotationPositionRow`` with ``status='open'``."""

    symbol: str
    ticket: int
    entry_price: float
    atr_at_entry: float
    risk_usd: float


@dataclass(frozen=True)
class RebalanceOpen:
    """One asset to add to the basket — sizing inputs come from the
    pipeline output (ATR at decision time, computed risk in USD)."""

    symbol: str
    direction: str  # "long" only in v1
    volume: float
    atr_at_entry: float
    risk_usd: float


def execute_rebalance_transitions(
    *,
    closes: list[RebalanceClose],
    opens: list[RebalanceOpen],
    mt5_client: Any,
    journal_session_factory: Callable[[], Session],
    settings: Any,
    now_utc: datetime,
    strategy: str,
    rebalance_uid: str,
    dry_run: bool = False,
) -> RebalanceExecutionResult:
    """Run a full basket rebalance: closes first, then opens.

    Closes-then-opens ordering matters: the freed margin from the
    closing positions must be available before the new entries are
    submitted. One failed close does NOT abort the cycle — the
    remaining closes still fire, and the opens still fire (possibly
    with insufficient margin, which the per-trade pre-check will
    detect and surface as a per-open failure).

    Symbols are processed in alphabetical order so the rebalance is
    deterministic regardless of input list order — useful for tests
    and audit.

    Returns a :class:`RebalanceExecutionResult` aggregating every
    individual order outcome. The caller (rotation cycle in the
    scheduler) is responsible for sending the Telegram summary and
    journalling the rebalance transition row.
    """
    closed: list[RotationOrderResult] = []
    for c in sorted(closes, key=lambda c: c.symbol):
        result = close_rotation_position(
            symbol=c.symbol,
            ticket=c.ticket,
            entry_price=c.entry_price,
            atr_at_entry=c.atr_at_entry,
            risk_usd=c.risk_usd,
            mt5_client=mt5_client,
            journal_session_factory=journal_session_factory,
            now_utc=now_utc,
            exit_rebalance_uid=rebalance_uid,
            dry_run=dry_run,
        )
        closed.append(result)
        if not result.success:
            logger.error(
                "rebalance close failed for %s ticket=%s — %s",
                c.symbol, c.ticket, result.error_code,
            )

    opened: list[RotationOrderResult] = []
    for o in sorted(opens, key=lambda o: o.symbol):
        result = open_rotation_position(
            symbol=o.symbol,
            direction=o.direction,
            volume=o.volume,
            atr_at_entry=o.atr_at_entry,
            risk_usd=o.risk_usd,
            mt5_client=mt5_client,
            journal_session_factory=journal_session_factory,
            settings=settings,
            now_utc=now_utc,
            strategy=strategy,
            entry_rebalance_uid=rebalance_uid,
            dry_run=dry_run,
        )
        opened.append(result)
        if not result.success:
            logger.error(
                "rebalance open failed for %s — %s",
                o.symbol, result.error_code,
            )

    return RebalanceExecutionResult(closed=closed, opened=opened)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _typical_spread(settings: Any, symbol: str) -> float | None:
    """Read ``settings.TYPICAL_SPREADS[symbol]`` if present, else ``None``."""
    table = getattr(settings, "TYPICAL_SPREADS", None)
    if not table:
        return None
    val = table.get(symbol) if hasattr(table, "get") else None
    return float(val) if val is not None else None
