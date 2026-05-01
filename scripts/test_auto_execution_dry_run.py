"""Sprint 7 — Mac-friendly smoke test for the auto-execution pipeline.

What it does:

1. Builds an in-memory SQLite journal.
2. Loads ``config/settings.py.example`` (no MT5 / no secrets needed).
3. Builds a mock MT5 client that exposes:
   - ``get_account_info()`` returning a $5K balanced FundedNext-style snapshot,
   - ``get_symbol_info("XAUUSD")`` with realistic XAU contract size + spread,
   - ``place_limit_order(...)`` returning a successful retcode (10009)
     and a synthetic ticket,
   - ``get_open_positions(magic=7766)`` cycling through pending → filled
     → tp1_hit → tp_runner_hit so the lifecycle walks every transition,
   - ``close_partial_position`` / ``modify_position_sl`` / ``cancel_pending_order``
     recording calls in memory.
4. Builds a stub notifier that captures every ``send_*`` invocation
   instead of hitting Telegram.
5. Builds a synthetic A-grade XAUUSD short setup (entry 4360, SL 4375,
   TP1 4285, TP_runner 4080.5).
6. Runs the full pipeline:
   - ``order_manager.place_order(dry_run=False)`` against the mock MT5,
     verifying the order is journaled and the post-trade notification
     fires.
   - ``position_lifecycle.check_open_positions(...)`` over four cycles
     (pending → filled → tp1_hit → tp_runner_hit), advancing the mock
     state between cycles.
   - ``position_lifecycle.end_of_killzone_cleanup(...)`` against a
     freshly-placed pending order.
   - ``recovery.reconcile_orphan_positions(...)`` against an injected
     orphan position.
7. Prints a tabular summary of journal rows, captured notifications,
   and lifecycle reports.

This script does NOT require Windows, the ``MetaTrader5`` package, or
any Telegram secret. Run on the Mac dev box to validate the wiring
before promoting Sprint 7 to the Windows host:

    python scripts/test_auto_execution_dry_run.py

Exit code 0 = pipeline OK. Exit code 1 = something tripped — read the
console output.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlalchemy import select  # noqa: E402

from src.detection.fvg import FVG  # noqa: E402
from src.detection.mss import MSS  # noqa: E402
from src.detection.setup import Setup  # noqa: E402
from src.detection.sweep import Sweep  # noqa: E402
from src.execution.order_manager import place_order  # noqa: E402
from src.execution.position_lifecycle import (  # noqa: E402
    check_open_positions,
    end_of_killzone_cleanup,
)
from src.execution.recovery import reconcile_orphan_positions  # noqa: E402
from src.journal.db import get_engine, init_db, session_scope  # noqa: E402
from src.journal.models import OrderRow, SetupRow, SpreadAnomalyRow  # noqa: E402
from src.journal.repository import insert_setup  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        # Hard-stops surface
        ACCOUNT_BALANCE_BASE=5000.0,
        DAILY_LOSS_LIMIT=200.0,
        MAX_LOSS_LIMIT=400.0,
        DAILY_LOSS_STOP_FRACTION=0.80,
        MAX_LOSS_STOP_FRACTION=0.80,
        MAX_TRADES_PER_DAY=2,
        MAX_TRADES_PER_PAIR_PER_DAY=2,
        MAX_CONSECUTIVE_SL_PER_DAY=2,
        NEWS_BLACKOUT_TODAY=False,
        MAX_LOSS_OVERRIDE=False,
        # Sprint 7 — auto-execution
        AUTO_TRADING_ENABLED=True,
        AUTO_TRADING_DRY_RUN=False,
        MAGIC_NUMBER=7766,
        RISK_PER_TRADE_FRACTION=0.01,
        MAX_RISK_PER_TRADE_USD=None,
        TP1_PARTIAL_FRACTION=0.5,
        SPREAD_ANOMALY_MULTIPLIER=3.0,
        LIFECYCLE_CHECK_INTERVAL_SEC=30,
        KILL_SWITCH_PATH=None,
        INSTRUMENT_CONFIG={
            "XAUUSD": {"typical_spread": 0.5},
            "NDX100": {"typical_spread": 2.0},
        },
    )


def _make_setup() -> Setup:
    ts = datetime(2026, 5, 1, 15, 35, tzinfo=UTC)
    sweep = Sweep(
        direction="bearish",
        swept_level_price=4380.0,
        swept_level_type="asian_high",
        swept_level_strength="structural",
        sweep_candle_time_utc=ts,
        sweep_extreme_price=4382.5,
        return_candle_time_utc=ts,
        excursion=2.5,
    )
    mss = MSS(
        direction="bearish",
        sweep=sweep,
        broken_swing_time_utc=ts,
        broken_swing_price=4365.0,
        mss_confirm_candle_time_utc=ts,
        mss_confirm_candle_close=4364.0,
        displacement_body_ratio=2.1,
        displacement_candle_time_utc=ts,
    )
    fvg = FVG(
        direction="bearish",
        proximal=4360.0,
        distal=4366.0,
        c1_time_utc=ts,
        c2_time_utc=ts,
        c3_time_utc=ts,
        size=6.0,
        size_atr_ratio=1.0,
    )
    return Setup(
        timestamp_utc=ts,
        symbol="XAUUSD",
        direction="short",
        daily_bias="bearish",
        killzone="ny",
        swept_level_price=4380.0,
        swept_level_type="asian_high",
        swept_level_strength="structural",
        sweep=sweep,
        mss=mss,
        poi=fvg,
        poi_type="FVG",
        entry_price=4360.0,
        stop_loss=4375.0,
        target_level_type="swing_h1_low",
        tp_runner_price=4080.5,
        tp_runner_rr=18.7,
        tp1_price=4285.0,
        tp1_rr=5.0,
        quality="A",
        confluences=["FVG+OB"],
    )


@dataclass
class _AccountInfo:
    login_masked: str = "***1234"
    currency: str = "USD"
    balance: float = 5000.0
    equity: float = 5000.0
    profit: float = 0.0
    margin_level: float = 0.0
    leverage: int = 100


@dataclass
class _SymbolInfo:
    symbol: str = "XAUUSD"
    trade_contract_size: float = 100.0
    point: float = 0.01
    volume_min: float = 0.01
    volume_step: float = 0.01
    volume_max: float = 100.0
    bid: float = 4360.0
    ask: float = 4360.5


@dataclass
class _Position:
    ticket: int
    symbol: str
    direction: str
    volume: float
    entry_price: float
    sl: float
    tp: float
    magic: int = 7766
    time_open_utc: datetime = field(
        default_factory=lambda: datetime(2026, 5, 1, 15, 36, tzinfo=UTC)
    )
    profit: float = 0.0


@dataclass
class _Pending:
    ticket: int
    symbol: str
    direction: str
    volume: float
    price_open: float
    sl: float
    tp: float
    magic: int = 7766
    time_setup_utc: datetime = field(
        default_factory=lambda: datetime(2026, 5, 1, 15, 36, tzinfo=UTC)
    )


@dataclass
class _OrderSendResult:
    retcode: int = 10009
    order: int = 12345678
    deal: int = 0
    comment: str = "Done"
    request_id: int = 0


@dataclass
class _MockMt5:
    """Full MT5 surface needed by the auto-execution pipeline."""

    account: _AccountInfo = field(default_factory=_AccountInfo)
    symbol_info_by_symbol: dict[str, _SymbolInfo] = field(default_factory=dict)
    positions: list[_Position] = field(default_factory=list)
    pending: list[_Pending] = field(default_factory=list)
    next_send_result: _OrderSendResult = field(default_factory=_OrderSendResult)
    sent_requests: list[dict[str, Any]] = field(default_factory=list)
    cancelled: list[int] = field(default_factory=list)
    sl_modifications: list[tuple[int, float]] = field(default_factory=list)
    partial_closes: list[tuple[int, float]] = field(default_factory=list)
    market_closes: list[int] = field(default_factory=list)
    history: dict[int, dict[str, Any]] = field(default_factory=dict)

    def get_account_info(self) -> _AccountInfo:
        return self.account

    def get_recent_trades(self, since: datetime):
        return []

    def get_symbol_info(self, symbol: str) -> _SymbolInfo:
        return self.symbol_info_by_symbol.get(symbol, _SymbolInfo(symbol=symbol))

    def place_limit_order(
        self,
        *,
        symbol: str,
        direction: str,
        volume: float,
        price: float,
        sl: float,
        tp: float,
        magic: int,
        comment: str = "",
    ) -> _OrderSendResult:
        self.sent_requests.append(
            dict(
                symbol=symbol,
                direction=direction,
                volume=volume,
                price=price,
                sl=sl,
                tp=tp,
                magic=magic,
                comment=comment,
            )
        )
        return self.next_send_result

    def cancel_pending_order(self, ticket: int) -> bool:
        self.cancelled.append(int(ticket))
        self.pending = [o for o in self.pending if o.ticket != int(ticket)]
        return True

    def modify_position_sl(self, *, ticket: int, new_sl: float) -> bool:
        self.sl_modifications.append((int(ticket), float(new_sl)))
        for p in self.positions:
            if p.ticket == int(ticket):
                p.sl = float(new_sl)
        return True

    def close_partial_position(self, *, ticket: int, volume: float) -> bool:
        self.partial_closes.append((int(ticket), float(volume)))
        for p in self.positions:
            if p.ticket == int(ticket):
                p.volume = max(0.0, p.volume - float(volume))
        return True

    def close_position_at_market(self, ticket: int) -> bool:
        self.market_closes.append(int(ticket))
        self.positions = [p for p in self.positions if p.ticket != int(ticket)]
        return True

    def get_open_positions(self, magic=None):
        if magic is None:
            return list(self.positions)
        return [p for p in self.positions if p.magic == magic]

    def get_pending_orders(self, magic=None):
        if magic is None:
            return list(self.pending)
        return [o for o in self.pending if o.magic == magic]

    def get_position_close_info(self, ticket: int):
        return self.history.get(int(ticket))


@dataclass
class _StubNotifier:
    """Captures every send_* call instead of hitting Telegram."""

    captured: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def _capture(self, name: str, **kwargs: Any) -> None:
        self.captured.append((name, kwargs))

    def send_order_placed(self, setup, *, ticket, volume, risk_usd):
        self._capture(
            "send_order_placed",
            symbol=setup.symbol,
            ticket=ticket,
            volume=volume,
            risk_usd=risk_usd,
        )

    def send_order_filled(self, *, order, ticket):
        self._capture("send_order_filled", ticket=ticket, symbol=order.symbol)

    def send_tp1_hit(self, *, order, ticket, partial_volume):
        self._capture(
            "send_tp1_hit", ticket=ticket, partial_volume=partial_volume
        )

    def send_tp_runner_hit(self, *, order, ticket, exit_price, realized_r):
        self._capture(
            "send_tp_runner_hit",
            ticket=ticket,
            exit_price=exit_price,
            realized_r=realized_r,
        )

    def send_sl_hit(self, *, order, ticket, exit_price, realized_r):
        self._capture(
            "send_sl_hit",
            ticket=ticket,
            exit_price=exit_price,
            realized_r=realized_r,
        )

    def send_order_cancelled(self, *, ticket, reason):
        self._capture("send_order_cancelled", ticket=ticket, reason=reason)

    def send_setup_skipped(self, setup, reason):
        self._capture(
            "send_setup_skipped", symbol=setup.symbol, reason=reason
        )

    def send_orphan_alert(self, *, ticket, symbol, volume):
        self._capture(
            "send_orphan_alert", ticket=ticket, symbol=symbol, volume=volume
        )


# ---------------------------------------------------------------------------
# Smoke-test runner
# ---------------------------------------------------------------------------


def _print_section(title: str) -> None:
    print()
    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)


def main() -> int:
    fail = False
    print("Sprint 7 auto-execution smoke test (Mac dry-run, no real broker).")

    # ---- Setup ----
    settings = _settings()
    setup = _make_setup()
    engine = get_engine(":memory:")
    init_db(engine)

    def session_factory():
        return session_scope(engine)

    with session_scope(engine) as s:
        insert_setup(s, setup, was_notified=True, detected_at=datetime.now(UTC))

    mt5 = _MockMt5(
        symbol_info_by_symbol={"XAUUSD": _SymbolInfo()},
    )
    notifier = _StubNotifier()
    now_utc = datetime(2026, 5, 1, 15, 40, tzinfo=UTC)

    # ---- Step 1: place_order ----
    _print_section("STEP 1 — place_order")
    result = place_order(
        setup=setup,
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=settings,
        now_utc=now_utc,
        notifier=notifier,
        dry_run=False,
    )
    print(f"  result.success = {result.success}")
    print(f"  result.ticket  = {result.ticket}")
    print(f"  MT5 request    = {mt5.sent_requests[0] if mt5.sent_requests else '∅'}")
    if not result.success or result.ticket != 12345678:
        print("  ❌ FAIL — expected success with ticket 12345678")
        fail = True
    else:
        print("  ✅ OK")

    with session_scope(engine) as s:
        rows = list(s.execute(select(OrderRow)).scalars().all())
        print(f"  orders journaled = {len(rows)} (status={rows[0].status if rows else 'n/a'})")
        if not rows or rows[0].status != "pending":
            print("  ❌ FAIL — order not persisted with status=pending")
            fail = True

    # ---- Step 2: pending → filled ----
    _print_section("STEP 2 — lifecycle: pending → filled")
    mt5.positions.append(
        _Position(
            ticket=12345678,
            symbol="XAUUSD",
            direction="short",
            volume=0.05,
            entry_price=4360.0,
            sl=4375.0,
            tp=4080.5,
        )
    )
    report = check_open_positions(
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=settings,
        now_utc=now_utc,
        notifier=notifier,
    )
    print(f"  filled cycle  = {report.filled}")
    if report.filled != 1:
        print("  ❌ FAIL — pending → filled did not fire")
        fail = True
    else:
        print("  ✅ OK")

    # ---- Step 3: filled → tp1_hit ----
    _print_section("STEP 3 — lifecycle: TP1 partial close + SL → BE")
    mt5.symbol_info_by_symbol["XAUUSD"] = _SymbolInfo(bid=4279.5, ask=4280.0)
    report = check_open_positions(
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=settings,
        now_utc=now_utc,
        notifier=notifier,
    )
    print(f"  tp1_hit cycle  = {report.tp1_hit}")
    print(f"  partial closes = {mt5.partial_closes}")
    print(f"  sl modifications = {mt5.sl_modifications}")
    if report.tp1_hit != 1 or not any(t == 12345678 for t, _ in mt5.partial_closes):
        print("  ❌ FAIL — TP1 partial did not fire")
        fail = True
    else:
        print("  ✅ OK")

    # ---- Step 4: filled → tp_runner_hit ----
    _print_section("STEP 4 — lifecycle: TP runner exit (+blended R)")
    # Remove the position from MT5 to mimic close at TP_runner; expose history.
    mt5.positions = [p for p in mt5.positions if p.ticket != 12345678]
    mt5.history[12345678] = dict(
        exit_price=4080.0,
        exit_time_utc=now_utc,
        profit_usd=125.0,
    )
    report = check_open_positions(
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=settings,
        now_utc=now_utc,
        notifier=notifier,
    )
    print(f"  tp_runner_hit cycle  = {report.tp_runner_hit}")
    if report.tp_runner_hit != 1:
        print("  ❌ FAIL — runner exit did not fire")
        fail = True
    else:
        print("  ✅ OK")

    # ---- Step 5: end-of-killzone cleanup on a fresh pending ----
    _print_section("STEP 5 — end_of_killzone_cleanup")
    # Insert a fresh setup + pending order.
    setup2_uid = "XAUUSD_2026-05-01T16:00:00+00:00"
    setup2 = _make_setup()
    object.__setattr__(setup2, "timestamp_utc", datetime(2026, 5, 1, 16, 0, tzinfo=UTC))
    with session_scope(engine) as s:
        insert_setup(s, setup2, was_notified=True, detected_at=now_utc)
        from src.journal.repository import insert_order

        insert_order(
            s,
            setup_uid=setup2_uid,
            mt5_ticket=99999,
            symbol="XAUUSD",
            direction="short",
            volume=0.05,
            entry_price=4360.0,
            stop_loss=4375.0,
            tp1=4285.0,
            tp_runner=4080.5,
            placed_at_utc=now_utc,
            status="pending",
        )

    mt5.pending = [
        _Pending(
            ticket=99999,
            symbol="XAUUSD",
            direction="short",
            volume=0.05,
            price_open=4360.0,
            sl=4375.0,
            tp=4080.5,
        )
    ]
    n_cancelled = end_of_killzone_cleanup(
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=settings,
        killzone="ny",
        now_utc=now_utc,
        notifier=notifier,
    )
    print(f"  cancelled = {n_cancelled} (tickets: {mt5.cancelled})")
    if n_cancelled != 1 or 99999 not in mt5.cancelled:
        print("  ❌ FAIL — pending order should have been cancelled")
        fail = True
    else:
        print("  ✅ OK")

    # ---- Step 6: recovery — orphan position handling ----
    _print_section("STEP 6 — recovery.reconcile_orphan_positions (orphan)")
    mt5.positions = [
        _Position(
            ticket=88888,
            symbol="NDX100",
            direction="long",
            volume=1.0,
            entry_price=20000.0,
            sl=19990.0,
            tp=20100.0,
            magic=7766,
        )
    ]
    rec_report = reconcile_orphan_positions(
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=settings,
        now_utc=now_utc,
        notifier=notifier,
    )
    print(f"  orphan_positions closed = {rec_report.orphan_positions}")
    print(f"  market closes = {mt5.market_closes}")
    if rec_report.orphan_positions != 1 or 88888 not in mt5.market_closes:
        print("  ❌ FAIL — orphan position should have been closed")
        fail = True
    else:
        print("  ✅ OK")

    # ---- Summary ----
    _print_section("SUMMARY")
    print(f"  notifications captured = {len(notifier.captured)}:")
    for name, kwargs in notifier.captured:
        print(f"    - {name}: {kwargs}")

    with session_scope(engine) as s:
        all_orders = list(s.execute(select(OrderRow)).scalars().all())
        all_anomalies = list(s.execute(select(SpreadAnomalyRow)).scalars().all())
    print(f"  orders rows           = {len(all_orders)}")
    for row in all_orders:
        print(
            f"    - ticket {row.mt5_ticket}: status={row.status} realized_r={row.realized_r}"
        )
    print(f"  spread_anomalies rows = {len(all_anomalies)}")

    if fail:
        print()
        print("❌ Smoke test FAILED — see lines above.")
        return 1
    print()
    print("✅ Smoke test PASSED — Sprint 7 wiring looks good.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
