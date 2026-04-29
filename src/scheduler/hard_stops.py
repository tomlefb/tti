"""Hard-stop layer — see docs/05 §"Hard stops".

A pure-ish function ``is_blocked`` returns the first ``BlockReason`` that
applies, or ``None`` when the cycle may proceed. Order of checks (matches
docs/05): max-loss-critical → daily-loss-reached → news → daily trade
count → consecutive SL → per-pair count.

The function reads:

- live MT5 account info (for equity/balance — drawdown calculation),
- MT5 closed trades since today's broker rollover (today's PnL),
- the journal (operator-confirmed taken decisions today, consecutive SL
  count from outcomes).

The journal is the source of truth for *operator-confirmed* trades:
hard stops apply to trades the operator opted in to, not every MT5
order on the account. ``MAX_TRADES_PER_DAY`` and
``MAX_CONSECUTIVE_SL_PER_DAY`` therefore count ``DecisionRow`` rows of
``decision='taken'`` for today, **not** raw MT5 deals.

``MAX_LOSS_OVERRIDE``: a per-settings boolean. The operator sets it
``True`` after manually reviewing a max-loss-critical breach so the
scheduler can resume. See docs/05.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.journal.models import DecisionRow, OutcomeRow, SetupRow

if TYPE_CHECKING:
    from src.mt5_client.client import AccountInfo, MT5Client

logger = logging.getLogger(__name__)

_TZ_PARIS = ZoneInfo("Europe/Paris")

# Broker rollover for FundedNext (per docs/05 §"Hard stops"). Closed
# trades exiting after this Paris-local hour roll over to the next
# trading day. Used to slice "today's" PnL.
_DAILY_ROLLOVER_HOUR_PARIS = 23


@dataclass(frozen=True)
class BlockReason:
    """A short, human-readable reason for suspending notifications."""

    code: str
    message: str


class HardStopSettings(Protocol):
    """Subset of ``config.settings`` consumed by the hard-stops layer."""

    ACCOUNT_BALANCE_BASE: float
    DAILY_LOSS_LIMIT: float
    MAX_LOSS_LIMIT: float
    DAILY_LOSS_STOP_FRACTION: float
    MAX_LOSS_STOP_FRACTION: float
    MAX_TRADES_PER_DAY: int
    MAX_TRADES_PER_PAIR_PER_DAY: int
    MAX_CONSECUTIVE_SL_PER_DAY: int
    NEWS_BLACKOUT_TODAY: bool
    MAX_LOSS_OVERRIDE: bool


def is_blocked(
    journal_session: Session,
    mt5_client: MT5Client,
    settings: HardStopSettings,
    *,
    pair: str,
    now_utc: datetime,
) -> BlockReason | None:
    """Run every hard-stop check; return the first reason that applies.

    Args:
        journal_session: open SQLAlchemy session (the caller manages the
            transaction).
        mt5_client: live MT5 client (already connected). Account info and
            trade history are read at most once per call.
        settings: anything satisfying ``HardStopSettings``.
        pair: the symbol about to be evaluated (used for per-pair count).
        now_utc: aware UTC ``datetime`` — single source of "now" so tests
            can pin it.

    Returns:
        The first matching :class:`BlockReason`, or ``None`` to proceed.
    """
    # Pull account info up-front. Both max-loss and daily-loss need it.
    try:
        account = mt5_client.get_account_info()
    except Exception as exc:  # noqa: BLE001 — surface as block, not crash
        logger.error("hard_stops: account_info failed (%r) — blocking cycle", exc)
        return BlockReason(
            code="account_info_unavailable",
            message=f"MT5 account info unreachable: {exc!r}",
        )

    # 1. Max loss critical — single static drawdown threshold per docs/05.
    max_loss_block = _check_max_loss_critical(account, settings)
    if max_loss_block is not None:
        return max_loss_block

    # 2. Daily loss reached.
    daily_loss_block = _check_daily_loss(account, mt5_client, settings, now_utc=now_utc)
    if daily_loss_block is not None:
        return daily_loss_block

    # 3. News blackout (manual switch).
    if settings.NEWS_BLACKOUT_TODAY:
        return BlockReason(
            code="news_blackout",
            message="NEWS_BLACKOUT_TODAY=True — notifications suppressed.",
        )

    today_paris = _paris_local_date(now_utc)

    # 4. Daily trade count.
    taken_today = _count_taken_today(journal_session, today_paris)
    if taken_today >= settings.MAX_TRADES_PER_DAY:
        return BlockReason(
            code="daily_trade_count",
            message=(
                f"daily trade cap reached: {taken_today}/{settings.MAX_TRADES_PER_DAY} "
                "taken today (Paris)."
            ),
        )

    # 5. Consecutive SL — last N outcomes among today's taken setups.
    if _consecutive_sl_today(journal_session, today_paris) >= settings.MAX_CONSECUTIVE_SL_PER_DAY:
        return BlockReason(
            code="consecutive_sl",
            message=(
                f"{settings.MAX_CONSECUTIVE_SL_PER_DAY}× consecutive stop-loss today — "
                "cooling off until tomorrow."
            ),
        )

    # 6. Per-pair count.
    taken_pair_today = _count_taken_today(journal_session, today_paris, symbol=pair)
    if taken_pair_today >= settings.MAX_TRADES_PER_PAIR_PER_DAY:
        return BlockReason(
            code="pair_count",
            message=(
                f"{pair}: {taken_pair_today}/{settings.MAX_TRADES_PER_PAIR_PER_DAY} "
                "trades already taken today."
            ),
        )

    return None


def _check_max_loss_critical(
    account: AccountInfo,
    settings: HardStopSettings,
) -> BlockReason | None:
    """Evaluate the max-loss-critical hard stop (static drawdown)."""
    drawdown = max(0.0, settings.ACCOUNT_BALANCE_BASE - account.equity)
    threshold = settings.MAX_LOSS_LIMIT * settings.MAX_LOSS_STOP_FRACTION
    if drawdown < threshold:
        return None
    if settings.MAX_LOSS_OVERRIDE:
        logger.warning(
            "hard_stops: max-loss threshold reached (drawdown=$%.2f) but "
            "MAX_LOSS_OVERRIDE=True — proceeding.",
            drawdown,
        )
        return None
    return BlockReason(
        code="max_loss_critical",
        message=(
            f"Max loss threshold reached: drawdown $-{drawdown:.2f} ≥ "
            f"${threshold:.2f} ({settings.MAX_LOSS_STOP_FRACTION:.0%} of "
            f"${settings.MAX_LOSS_LIMIT:.0f}). Manual reset required."
        ),
    )


def _check_daily_loss(
    account: AccountInfo,
    mt5_client: MT5Client,
    settings: HardStopSettings,
    *,
    now_utc: datetime,
) -> BlockReason | None:
    """Evaluate the daily-loss-reached hard stop.

    "Today" is the Paris-local trading day, opening at 23:00 Paris on the
    previous calendar day per FundedNext rollover.
    """
    threshold = settings.DAILY_LOSS_LIMIT * settings.DAILY_LOSS_STOP_FRACTION
    rollover_utc = _today_rollover_utc(now_utc)

    try:
        trades = mt5_client.get_recent_trades(rollover_utc)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "hard_stops: get_recent_trades failed (%r) — blocking cycle defensively",
            exc,
        )
        return BlockReason(
            code="trade_history_unavailable",
            message=f"MT5 trade history unreachable: {exc!r}",
        )

    realized_loss = sum(
        -float(t.profit_usd)
        for t in trades
        if t.profit_usd is not None and t.profit_usd < 0 and t.exit_time_utc is not None
    )
    realized_pnl = sum(
        float(t.profit_usd)
        for t in trades
        if t.profit_usd is not None and t.exit_time_utc is not None
    )
    # Account equity vs balance — float P&L on open positions counts too.
    floating = account.equity - account.balance
    today_loss = max(0.0, -(realized_pnl + floating))

    if today_loss >= threshold:
        return BlockReason(
            code="daily_loss_reached",
            message=(
                f"Daily loss limit reached: ${today_loss:.2f} ≥ ${threshold:.2f} "
                f"(realised ${realized_loss:.2f} + floating ${-floating:.2f})."
            ),
        )
    return None


def _count_taken_today(
    session: Session,
    today_paris: date,
    *,
    symbol: str | None = None,
) -> int:
    """Count operator-confirmed taken decisions whose setup falls on ``today_paris``."""
    start_utc, end_utc = _paris_day_to_utc_range(today_paris)
    stmt = (
        select(DecisionRow)
        .join(SetupRow, SetupRow.setup_uid == DecisionRow.setup_uid)
        .where(DecisionRow.decision == "taken")
        .where(SetupRow.timestamp_utc >= start_utc)
        .where(SetupRow.timestamp_utc < end_utc)
    )
    if symbol is not None:
        stmt = stmt.where(SetupRow.symbol == symbol)
    rows = list(session.execute(stmt).scalars().all())
    return len(rows)


def _consecutive_sl_today(session: Session, today_paris: date) -> int:
    """Trailing-from-most-recent count of consecutive SL outcomes today."""
    start_utc, end_utc = _paris_day_to_utc_range(today_paris)
    stmt = (
        select(SetupRow, OutcomeRow)
        .join(DecisionRow, DecisionRow.setup_uid == SetupRow.setup_uid)
        .join(OutcomeRow, OutcomeRow.setup_uid == SetupRow.setup_uid)
        .where(DecisionRow.decision == "taken")
        .where(SetupRow.timestamp_utc >= start_utc)
        .where(SetupRow.timestamp_utc < end_utc)
        .order_by(SetupRow.timestamp_utc.desc())
    )
    rows = list(session.execute(stmt).all())
    streak = 0
    for _, outcome in rows:
        if outcome is None or outcome.exit_reason != "sl_hit":
            break
        streak += 1
    return streak


def _paris_local_date(now_utc: datetime) -> date:
    """Return the Paris-local *trading* date for ``now_utc``.

    The trading day rolls over at 23:00 Paris. Anything between 23:00 and
    midnight Paris is counted as the next day.
    """
    paris_now = now_utc.astimezone(_TZ_PARIS)
    if paris_now.hour >= _DAILY_ROLLOVER_HOUR_PARIS:
        return (paris_now + timedelta(days=1)).date()
    return paris_now.date()


def _today_rollover_utc(now_utc: datetime) -> datetime:
    """UTC datetime corresponding to today's 23:00-previous-day Paris rollover."""
    today_paris = _paris_local_date(now_utc)
    rollover_paris = datetime.combine(
        today_paris - timedelta(days=1),
        time(_DAILY_ROLLOVER_HOUR_PARIS, 0),
        tzinfo=_TZ_PARIS,
    )
    return rollover_paris.astimezone(UTC)


def _paris_day_to_utc_range(day: date) -> tuple[datetime, datetime]:
    """Inclusive-start / exclusive-end UTC range for one Paris-local day.

    The day starts at the 23:00-previous-day rollover and ends at 23:00 of
    the day itself — matching the broker convention used everywhere else
    in the hard-stop layer.
    """
    start = datetime.combine(
        day - timedelta(days=1),
        time(_DAILY_ROLLOVER_HOUR_PARIS, 0),
        tzinfo=_TZ_PARIS,
    ).astimezone(UTC)
    end = datetime.combine(
        day,
        time(_DAILY_ROLLOVER_HOUR_PARIS, 0),
        tzinfo=_TZ_PARIS,
    ).astimezone(UTC)
    return start, end
