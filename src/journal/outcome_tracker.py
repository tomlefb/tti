"""Reconcile MT5 trade history with journaled ``Taken`` setups.

The tracker is **CLI-only** in Sprint 5 (no auto-trigger; Sprint 6
scheduler will wire the daily reconciliation cron). It runs ad hoc via
``scripts/run_outcome_tracker.py`` to attach realised outcomes to
operator-confirmed setups.

The MT5 dependency is duck-typed: any object exposing
``get_recent_trades(since: datetime) -> list[Mt5Trade]`` works. Tests
inject a fake; the production scheduler will inject the real
``src.mt5_client`` wrapper once it lands (Sprint 6).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy.orm import Session

from src.journal.models import SetupRow
from src.journal.repository import get_outcomes_to_match, upsert_outcome

logger = logging.getLogger(__name__)


# Default match window — operator may take ~5–20 min to click Taken and
# place the order. ±30 min covers the realistic spread.
DEFAULT_MATCH_WINDOW_MINUTES = 30

# Relative tolerance for classifying an exit as TP1 / TP_runner / SL.
# 0.1% of entry price — same scale as the cross-codebase 0.1% tolerance
# used for swing-level matching and sweep dedup.
_EXIT_PRICE_TOLERANCE_FRACTION = 0.001


@dataclass(frozen=True)
class Mt5Trade:
    """Closed-trade dict returned by an ``Mt5Client`` implementation.

    Times are aware UTC ``datetime`` instances. ``exit_time_utc`` and
    ``exit_price`` are ``None`` when the position is still open.
    """

    ticket: int
    symbol: str
    direction: str  # "long" or "short"
    entry_time_utc: datetime
    entry_price: float
    exit_time_utc: datetime | None
    exit_price: float | None
    profit_usd: float | None


class Mt5Client(Protocol):
    """Minimal MT5 surface the outcome tracker needs."""

    def get_recent_trades(self, since: datetime) -> list[Mt5Trade]: ...


def reconcile_outcomes(
    session: Session,
    mt5_client: Mt5Client,
    since: datetime,
    *,
    match_window_minutes: int = DEFAULT_MATCH_WINDOW_MINUTES,
) -> int:
    """Match MT5 trades to journaled ``Taken`` setups awaiting outcomes.

    Strategy:

    1. Pull every ``Taken`` setup with no outcome (or outcome still
       ``'open'``) from the journal.
    2. Fetch MT5 trade history since ``since``.
    3. For each pending setup, find MT5 trades on the same symbol whose
       ``entry_time_utc`` falls within ±``match_window_minutes`` of the
       setup's ``timestamp_utc`` AND whose direction matches the setup.
    4. Multiple matches: log a warning and pick the trade closest in time
       to the setup's MSS confirm.
    5. No match: upsert outcome with ``exit_reason='unmatched'`` so the
       dashboard can flag it.
    6. For closed trades, classify the exit by comparing ``exit_price``
       to ``tp1_price`` / ``tp_runner_price`` / ``stop_loss``. Anything
       outside the tolerance is recorded as ``'manual_close'``.

    Returns:
        Number of outcome rows upserted (matched + unmatched).
    """
    pending = get_outcomes_to_match(session)
    if not pending:
        logger.info("outcome tracker: no pending setups to reconcile")
        return 0

    trades = mt5_client.get_recent_trades(since)
    logger.info(
        "outcome tracker: %d pending setups, %d MT5 trades fetched since %s",
        len(pending),
        len(trades),
        since.isoformat(),
    )

    upserted = 0
    window = timedelta(minutes=match_window_minutes)

    for setup in pending:
        match = _select_trade_match(setup, trades, window=window)
        if match is None:
            upsert_outcome(session, setup.setup_uid, exit_reason="unmatched")
            upserted += 1
            continue

        if match.exit_time_utc is None or match.exit_price is None:
            # Trade still open — record what we know, leave exit blank.
            upsert_outcome(
                session,
                setup.setup_uid,
                mt5_ticket=match.ticket,
                entry_time_utc=match.entry_time_utc,
                entry_price_filled=match.entry_price,
                exit_reason="open",
            )
            upserted += 1
            continue

        exit_reason = _classify_exit(setup, match.exit_price)
        realized_r = _realized_r(setup, match.exit_price)
        upsert_outcome(
            session,
            setup.setup_uid,
            mt5_ticket=match.ticket,
            entry_time_utc=match.entry_time_utc,
            exit_time_utc=match.exit_time_utc,
            entry_price_filled=match.entry_price,
            exit_price=match.exit_price,
            exit_reason=exit_reason,
            realized_pnl_usd=match.profit_usd,
            realized_r=realized_r,
        )
        upserted += 1

    logger.info("outcome tracker: %d outcomes upserted", upserted)
    return upserted


def _select_trade_match(
    setup: SetupRow,
    trades: list[Mt5Trade],
    *,
    window: timedelta,
) -> Mt5Trade | None:
    """Pick the best MT5 trade for ``setup`` or ``None``.

    Filter: same symbol, same direction, ``entry_time_utc`` within
    ``±window`` of ``setup.timestamp_utc``. Tie-break on multiple matches
    by absolute time distance to the MSS confirm — closest wins.
    """
    setup_ts = _ensure_aware(setup.timestamp_utc)
    candidates = []
    for t in trades:
        if t.symbol != setup.symbol:
            continue
        if t.direction != setup.direction:
            continue
        delta = abs(_ensure_aware(t.entry_time_utc) - setup_ts)
        if delta <= window:
            candidates.append((delta, t))

    if not candidates:
        return None
    if len(candidates) > 1:
        logger.warning(
            "outcome tracker: %d MT5 trades match setup_uid=%s — picking closest in time",
            len(candidates),
            setup.setup_uid,
        )
    candidates.sort(key=lambda dt: dt[0])
    return candidates[0][1]


def _classify_exit(setup: SetupRow, exit_price: float) -> str:
    """Tag a closed trade as tp1_hit / tp_runner_hit / sl_hit / manual_close.

    Tolerance is ``_EXIT_PRICE_TOLERANCE_FRACTION × setup.entry_price``
    (relative — same scale across instruments). The closest target wins
    when multiple are within tolerance.
    """
    tolerance = abs(setup.entry_price) * _EXIT_PRICE_TOLERANCE_FRACTION
    targets = (
        ("tp1_hit", setup.tp1_price),
        ("tp_runner_hit", setup.tp_runner_price),
        ("sl_hit", setup.stop_loss),
    )

    best_label: str | None = None
    best_distance = float("inf")
    for label, price in targets:
        distance = abs(exit_price - price)
        if distance <= tolerance and distance < best_distance:
            best_label = label
            best_distance = distance

    return best_label if best_label is not None else "manual_close"


def _realized_r(setup: SetupRow, exit_price: float) -> float:
    """Compute the realised R multiple for the trade.

    Risk = ``|entry - stop_loss|`` (always positive). Reward sign matches
    setup direction: long ⇒ ``exit - entry``; short ⇒ ``entry - exit``.
    """
    risk = abs(setup.entry_price - setup.stop_loss)
    if risk <= 0:
        return 0.0
    reward = exit_price - setup.entry_price
    if setup.direction == "short":
        reward = -reward
    return reward / risk


def _ensure_aware(ts: datetime) -> datetime:
    """Tolerate naive datetimes coming back from SQLite (assume UTC)."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts
