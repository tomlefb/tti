"""Sprint 7 — order placement / modification / cancellation.

Public surface:

- :class:`OrderResult` — the outcome of a :func:`place_order` call.
- :func:`compute_volume` — pure position-size calc (testable without MT5).
- :func:`place_order` — full place-order pipeline (pre-flight, sizing,
  spread anomaly, MT5 ``order_send``, journal persistence, notifier).
- :func:`cancel_order` — cancel a pending limit order (used by lifecycle).
- :func:`modify_position_sl` — change SL on an open position (used for BE
  move after TP1).

The MT5 client is injected. Anything satisfying the order-operations
Protocol works — production wires :class:`src.mt5_client.client.MT5Client`,
tests pass a small dataclass double.

Lifecycle delegates the partial-close / BE-move / runner-exit
sequencing to :mod:`src.execution.position_lifecycle`. The order manager
only places / cancels / modifies — it does NOT poll positions.

MT5 retcode reference (subset used here):

- 10009 (TRADE_RETCODE_DONE) — request fulfilled.
- 10010 (TRADE_RETCODE_DONE_PARTIAL) — partial fill (treated as success
  for limit orders since MT5 fills partially over time anyway).

Anything else is a failure; the caller gets the retcode + comment in
:class:`OrderResult` and may decide to retry or escalate.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy.orm import Session

from src.execution.safe_guards import (
    check_pre_trade,
    log_spread_anomaly,
    should_log_spread_anomaly,
)
from src.journal.repository import (
    insert_order,
    update_order_status,
)

if TYPE_CHECKING:
    from src.detection.setup import Setup

logger = logging.getLogger(__name__)


# MT5 retcodes treated as success.
_MT5_RETCODE_DONE = 10009
_MT5_RETCODE_DONE_PARTIAL = 10010
_MT5_SUCCESS_CODES = frozenset({_MT5_RETCODE_DONE, _MT5_RETCODE_DONE_PARTIAL})


# ---------------------------------------------------------------------------
# Result + Protocol
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderResult:
    """Outcome of :func:`place_order`.

    ``error_code`` is either:

    - the safe-guard reason string (``"kill_switch"``,
      ``"daily_loss_reached"``, …) when the trade was blocked pre-flight,
    - the MT5 retcode (int) when ``mt5.order_send`` returned a non-success
      code,
    - ``None`` on success.

    ``error_message`` is a free-form human-readable explanation; ``None``
    when the call succeeds.
    """

    success: bool
    ticket: int | None = None
    error_code: int | str | None = None
    error_message: str | None = None


class _SymbolInfoLike(Protocol):
    """Subset of MT5 symbol_info used by the sizing logic."""

    trade_contract_size: float
    point: float
    volume_min: float
    volume_step: float
    volume_max: float
    ask: float
    bid: float


# ---------------------------------------------------------------------------
# compute_volume
# ---------------------------------------------------------------------------


def compute_volume(
    *,
    risk_usd: float,
    sl_distance_price: float,
    symbol_info: _SymbolInfoLike,
) -> float:
    """Compute the lot size that risks ``risk_usd`` over ``sl_distance_price``.

    Formula::

        raw_volume = risk_usd / (sl_distance_price × trade_contract_size)
        snapped    = floor(raw_volume / volume_step) × volume_step
        clamped    = max(volume_min, min(volume_max, snapped))

    The floor (rather than round) ensures the actual risk never exceeds
    the budget on snap-up. When ``raw_volume`` is below ``volume_min``,
    we clamp UP to the broker minimum — this means the operator's actual
    risk slightly exceeds RISK_PER_TRADE_FRACTION on tiny SL distances;
    the broker's lot-step floor wins (documented in docs/04 §"Auto-execution
    rules").

    Raises:
        ValueError: ``sl_distance_price`` is zero or negative — invariant
            violation, refuse to silently divide-by-zero.
    """
    if sl_distance_price <= 0:
        raise ValueError(
            f"sl_distance_price must be > 0, got {sl_distance_price!r}"
        )

    contract = float(symbol_info.trade_contract_size)
    if contract <= 0:
        raise ValueError(f"trade_contract_size must be > 0, got {contract!r}")

    raw = float(risk_usd) / (float(sl_distance_price) * contract)

    step = float(symbol_info.volume_step) or 0.01
    # floor to step resolution so we never overshoot risk by rounding up.
    snapped = math.floor(raw / step) * step

    vmin = float(symbol_info.volume_min)
    vmax = float(symbol_info.volume_max)

    if snapped < vmin:
        return vmin
    if snapped > vmax:
        return vmax
    # Round to step's decimal precision so floating-point noise doesn't
    # leak into MT5 (e.g. 0.05 vs 0.050000001).
    digits = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(snapped, digits)


# ---------------------------------------------------------------------------
# place_order
# ---------------------------------------------------------------------------


def place_order(
    *,
    setup: "Setup",
    mt5_client: Any,
    journal_session_factory: Callable[[], Any],
    settings: Any,
    now_utc: datetime,
    notifier: Any | None = None,
    dry_run: bool = False,
) -> OrderResult:
    """Place a limit order from a Setup. Full pipeline.

    Steps:

    1. Pre-flight via :func:`safe_guards.check_pre_trade` (kill switch,
       day-disabled, hard_stops). Blocked → notify-skip + return failure.
    2. Read account + symbol info from MT5.
    3. Compute risk in USD (account.balance × RISK_PER_TRADE_FRACTION),
       optionally capped by MAX_RISK_PER_TRADE_USD.
    4. Compute volume via :func:`compute_volume`.
    5. Read current spread. Log anomaly if > typical × multiplier — but
       proceed regardless (operator design call).
    6. ``dry_run=True`` → return success WITHOUT touching MT5 or journal.
    7. Call ``mt5_client.place_limit_order(...)``. On success retcode,
       persist the order row (status="pending") and notify.
    """
    # Step 1: pre-flight.
    with journal_session_factory() as s:
        allowed, reason = check_pre_trade(
            s, mt5_client, settings, setup=setup, now_utc=now_utc
        )
    if not allowed:
        logger.info(
            "place_order blocked for %s: %s", setup.symbol, reason
        )
        if notifier is not None:
            _notify(notifier, "send_setup_skipped", setup, reason)
        return OrderResult(success=False, error_code=reason)

    # Step 2: account + symbol info.
    try:
        account = mt5_client.get_account_info()
    except Exception as exc:  # noqa: BLE001
        logger.exception("get_account_info failed before order placement")
        return OrderResult(
            success=False,
            error_code="account_info_unavailable",
            error_message=repr(exc),
        )

    try:
        symbol_info = mt5_client.get_symbol_info(setup.symbol)
    except Exception as exc:  # noqa: BLE001
        logger.exception("get_symbol_info failed for %s", setup.symbol)
        return OrderResult(
            success=False,
            error_code="symbol_info_unavailable",
            error_message=repr(exc),
        )

    # Step 3: risk budget.
    risk_fraction = float(getattr(settings, "RISK_PER_TRADE_FRACTION", 0.01))
    risk_usd = float(account.balance) * risk_fraction
    cap = getattr(settings, "MAX_RISK_PER_TRADE_USD", None)
    if cap is not None:
        risk_usd = min(risk_usd, float(cap))

    sl_distance = abs(float(setup.entry_price) - float(setup.stop_loss))
    if sl_distance <= 0:
        return OrderResult(
            success=False,
            error_code="invalid_sl_distance",
            error_message=f"SL == entry for setup {setup.symbol} {setup.timestamp_utc}",
        )

    # Step 4: volume.
    try:
        volume = compute_volume(
            risk_usd=risk_usd,
            sl_distance_price=sl_distance,
            symbol_info=symbol_info,
        )
    except ValueError as exc:
        logger.error("compute_volume failed: %s", exc)
        return OrderResult(
            success=False, error_code="invalid_volume", error_message=str(exc)
        )

    # Step 5: spread anomaly (log-only).
    typical_spread = _typical_spread(settings, setup.symbol)
    spread = float(symbol_info.ask) - float(symbol_info.bid)
    multiplier = float(getattr(settings, "SPREAD_ANOMALY_MULTIPLIER", 3.0))
    if should_log_spread_anomaly(
        current=spread, typical=typical_spread, multiplier=multiplier
    ):
        from src.journal.repository import setup_uid_for

        with journal_session_factory() as s:
            log_spread_anomaly(
                s,
                symbol=setup.symbol,
                current_spread=spread,
                typical_spread=typical_spread,
                setup_uid=setup_uid_for(setup),
                detected_at_utc=now_utc,
                action_taken="executed_anyway",
            )

    # Step 6: dry_run short-circuit.
    if dry_run:
        logger.info(
            "DRY-RUN place_order: %s %s vol=%.2f price=%.5f sl=%.5f tp=%.5f",
            setup.symbol,
            setup.direction,
            volume,
            setup.entry_price,
            setup.stop_loss,
            setup.tp_runner_price,
        )
        return OrderResult(success=True, ticket=None)

    # Step 7: actual MT5 send + journal persistence + notify.
    magic = int(getattr(settings, "MAGIC_NUMBER", 7766))
    try:
        send_result = mt5_client.place_limit_order(
            symbol=setup.symbol,
            direction=setup.direction,
            volume=volume,
            price=float(setup.entry_price),
            sl=float(setup.stop_loss),
            tp=float(setup.tp_runner_price),
            magic=magic,
            comment=f"sprint7:{setup.quality}",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("mt5.place_limit_order raised for %s", setup.symbol)
        return OrderResult(
            success=False,
            error_code="order_send_exception",
            error_message=repr(exc),
        )

    retcode = int(getattr(send_result, "retcode", -1))
    if retcode not in _MT5_SUCCESS_CODES:
        comment = str(getattr(send_result, "comment", ""))
        logger.error(
            "MT5 order_send returned retcode=%d comment=%r for %s",
            retcode,
            comment,
            setup.symbol,
        )
        return OrderResult(
            success=False,
            error_code=retcode,
            error_message=comment,
        )

    ticket = int(getattr(send_result, "order", 0))

    # Persist.
    from src.journal.repository import setup_uid_for

    with journal_session_factory() as s:
        insert_order(
            s,
            setup_uid=setup_uid_for(setup),
            mt5_ticket=ticket,
            symbol=setup.symbol,
            direction=setup.direction,
            volume=volume,
            entry_price=float(setup.entry_price),
            stop_loss=float(setup.stop_loss),
            tp1=float(setup.tp1_price),
            tp_runner=float(setup.tp_runner_price),
            placed_at_utc=now_utc,
            status="pending",
        )

    # Notify.
    if notifier is not None:
        _notify(
            notifier,
            "send_order_placed",
            setup,
            ticket=ticket,
            volume=volume,
            risk_usd=risk_usd,
        )

    return OrderResult(success=True, ticket=ticket)


# ---------------------------------------------------------------------------
# cancel_order
# ---------------------------------------------------------------------------


def cancel_order(
    *,
    ticket: int,
    mt5_client: Any,
    journal_session_factory: Callable[[], Any],
    reason: str,
    now_utc: datetime,
) -> bool:
    """Cancel a pending limit order on MT5 and mark the journal.

    Used for end-of-killzone cleanup, manual cancel, and (later)
    operator-driven cancel via Telegram inline button.

    Returns ``True`` iff MT5 reported success. The journal update is
    best-effort — a missing ticket logs a warning but does not raise.
    """
    try:
        ok = bool(mt5_client.cancel_pending_order(int(ticket)))
    except Exception as exc:  # noqa: BLE001
        logger.exception("mt5.cancel_pending_order raised for ticket=%d", ticket)
        return False

    if not ok:
        logger.warning("MT5 cancel_pending_order returned False for ticket=%d", ticket)
        return False

    try:
        with journal_session_factory() as s:
            update_order_status(
                s,
                ticket=int(ticket),
                status="cancelled",
                closed_at_utc=now_utc,
                notes=f"cancelled: {reason}",
            )
    except ValueError:
        logger.warning(
            "cancel_order: ticket=%d not found in journal — MT5 cancelled anyway",
            ticket,
        )
    return True


# ---------------------------------------------------------------------------
# modify_position_sl
# ---------------------------------------------------------------------------


def modify_position_sl(
    *,
    ticket: int,
    new_sl: float,
    mt5_client: Any,
) -> bool:
    """Move SL on an open position. Idempotent at the MT5 level (broker
    short-circuits when new_sl ≈ current_sl).

    Used by :mod:`position_lifecycle` to move SL to break-even after
    TP1 partial close.
    """
    try:
        return bool(
            mt5_client.modify_position_sl(ticket=int(ticket), new_sl=float(new_sl))
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("mt5.modify_position_sl raised for ticket=%d", ticket)
        return False


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _typical_spread(settings: Any, symbol: str) -> float | None:
    """Look up ``settings.INSTRUMENT_CONFIG[symbol]['typical_spread']``.

    Returns ``None`` if the key is absent — callers treat that as
    "no anomaly judgment possible".
    """
    cfg = getattr(settings, "INSTRUMENT_CONFIG", None)
    if not isinstance(cfg, dict):
        return None
    inner = cfg.get(symbol)
    if not isinstance(inner, dict):
        return None
    val = inner.get("typical_spread")
    return float(val) if val is not None else None


def _notify(notifier: Any, method_name: str, *args: Any, **kwargs: Any) -> None:
    """Best-effort notification — never raise into the caller.

    The execution path must keep going even if Telegram is down.
    """
    fn = getattr(notifier, method_name, None)
    if fn is None:
        # Notifier doesn't implement this hook yet — log only.
        logger.debug("notifier has no %s hook — skipping", method_name)
        return
    try:
        fn(*args, **kwargs)
    except Exception:  # noqa: BLE001
        logger.exception("notifier.%s raised — swallowing", method_name)
