"""Unit tests for ``reconcile_rotation_orphan_positions``.

Recovery runs once at scheduler startup when ``ACTIVE_STRATEGY ==
"trend_rotation_d1"`` and reconciles two failure modes:

- **Orphan position** — MT5 has an open position with the rotation
  magic that the journal does NOT track in
  ``rotation_positions(status='open')``. Three handling strategies:
  ``strict`` (close at market), ``adopt`` (insert journal row),
  ``alert_only`` (log + Telegram, leave the position).

- **Ghost row** — journal has a ``status='open'`` rotation_position
  with no matching MT5 position. The position was closed outside the
  bot. Action: mark the row closed at zero R + critical log + alert.

The fake MT5 client implements only the methods the recovery touches:
``get_open_positions`` (filtered on magic), ``close_position_at_market``,
``fetch_ohlc`` + ``get_symbol_info`` (for adopt mode).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

from src.execution.recovery import (
    RotationRecoveryReport,
    reconcile_rotation_orphan_positions,
)
from src.journal.db import get_engine, init_db, session_scope
from src.journal.models import RotationPositionRow
from src.journal.repository import insert_rotation_position


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    eng = get_engine(":memory:")
    init_db(eng)
    return eng


@pytest.fixture
def session_factory(engine):
    def factory():
        return session_scope(engine)

    return factory


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        ACTIVE_STRATEGY="trend_rotation_d1",
        ROTATION_MAGIC_NUMBER=7799,
        ROTATION_ATR_PERIOD=20,
        ROTATION_ORPHAN_STRATEGY="strict",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@dataclass
class _PositionSnapshot:
    ticket: int
    symbol: str
    direction: str
    volume: float
    entry_price: float
    magic: int = 7799
    time_open_utc: datetime = field(
        default_factory=lambda: datetime(2026, 5, 1, 21, 0, tzinfo=UTC)
    )
    sl: float = 0.0
    tp: float = 0.0
    profit: float = 0.0


@dataclass
class _FakeMT5Client:
    positions: list[_PositionSnapshot] = field(default_factory=list)
    next_close_ok: bool = True
    panel: dict[str, pd.DataFrame] = field(default_factory=dict)
    calls: list[tuple[str, dict]] = field(default_factory=list)

    def get_open_positions(self, magic=None):
        self.calls.append(("get_open_positions", {"magic": magic}))
        if magic is None:
            return list(self.positions)
        return [p for p in self.positions if p.magic == int(magic)]

    def close_position_at_market(self, ticket: int) -> bool:
        self.calls.append(("close_position_at_market", {"ticket": ticket}))
        if not self.next_close_ok:
            return False
        # Simulate the close: drop the position from the local state.
        self.positions = [p for p in self.positions if p.ticket != ticket]
        return True

    def fetch_ohlc(self, symbol: str, timeframe: str, n_candles: int):
        self.calls.append((
            "fetch_ohlc",
            {"symbol": symbol, "timeframe": timeframe, "n_candles": n_candles},
        ))
        if symbol not in self.panel:
            raise RuntimeError(f"no panel for {symbol}")
        return self.panel[symbol].tail(n_candles).reset_index(drop=True)

    def get_symbol_info(self, symbol: str):
        self.calls.append(("get_symbol_info", {"symbol": symbol}))
        return SimpleNamespace(trade_contract_size=100.0)


def _ohlc_panel(symbol: str, n_bars: int = 60) -> pd.DataFrame:
    """Synthesize a D1 OHLC frame with non-zero ATR for adopt-mode tests."""
    end = pd.Timestamp("2026-05-01", tz=UTC)
    rng = np.random.default_rng(seed=hash(symbol) & 0xFFFFFFFF)
    base = 100.0
    closes = base + rng.normal(0, 1.0, n_bars).cumsum() * 0.1
    highs = closes + 0.5
    lows = closes - 0.5
    opens = closes
    dates = pd.date_range(end=end, periods=n_bars, freq="D", tz=UTC)
    return pd.DataFrame({
        "time": dates, "open": opens, "high": highs,
        "low": lows, "close": closes, "volume": 1000,
    })


class _Notifier:
    """Sync notifier double — recovery uses sync `_notify` helper."""

    def __init__(self):
        self.alerts: list[dict] = []

    def send_orphan_alert(self, **kwargs):
        self.alerts.append(kwargs)


# ---------------------------------------------------------------------------
# Clean state
# ---------------------------------------------------------------------------


def test_clean_state_returns_zero_counters(session_factory, engine):
    mt5 = _FakeMT5Client(positions=[])
    notifier = _Notifier()
    report = reconcile_rotation_orphan_positions(
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=_settings(),
        now_utc=datetime(2026, 5, 5, 21, 0, tzinfo=UTC),
        notifier=notifier,
    )
    assert isinstance(report, RotationRecoveryReport)
    assert report.orphan_positions_handled == 0
    assert report.ghost_rows_handled == 0
    assert report.healthy_positions == 0
    assert report.errors == []
    assert notifier.alerts == []


def test_healthy_state_counts_paired_positions(session_factory, engine):
    """MT5 has positions matching open journal rows — both sides agree."""
    with session_scope(engine) as s:
        insert_rotation_position(
            s, strategy="trend_rotation_d1", symbol="XAUUSD",
            mt5_ticket=1001, direction="long", volume=0.05,
            entry_price=2400.0, atr_at_entry=12.5, risk_usd=24.25,
            entry_timestamp_utc=datetime(2026, 5, 1, 21, 0, tzinfo=UTC),
            entry_rebalance_uid=None,
        )
    mt5 = _FakeMT5Client(positions=[
        _PositionSnapshot(
            ticket=1001, symbol="XAUUSD", direction="long",
            volume=0.05, entry_price=2400.0,
        ),
    ])
    report = reconcile_rotation_orphan_positions(
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=_settings(),
        now_utc=datetime(2026, 5, 5, 21, 0, tzinfo=UTC),
    )
    assert report.healthy_positions == 1
    assert report.orphan_positions_handled == 0
    assert report.ghost_rows_handled == 0


# ---------------------------------------------------------------------------
# Orphan — strict mode
# ---------------------------------------------------------------------------


def test_orphan_strict_closes_at_market_and_alerts(session_factory, engine):
    mt5 = _FakeMT5Client(positions=[
        _PositionSnapshot(
            ticket=2002, symbol="BTCUSD", direction="long",
            volume=0.01, entry_price=70000.0,
        ),
    ])
    notifier = _Notifier()
    report = reconcile_rotation_orphan_positions(
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=_settings(ROTATION_ORPHAN_STRATEGY="strict"),
        now_utc=datetime(2026, 5, 5, 21, 0, tzinfo=UTC),
        notifier=notifier,
    )
    assert report.orphan_positions_handled == 1
    assert report.orphan_strategy_used == "strict"
    assert mt5.positions == []  # closed
    assert len(notifier.alerts) == 1
    assert notifier.alerts[0]["ticket"] == 2002


def test_orphan_strict_close_failure_records_error(session_factory):
    mt5 = _FakeMT5Client(
        positions=[
            _PositionSnapshot(
                ticket=2003, symbol="ETHUSD", direction="long",
                volume=0.5, entry_price=3000.0,
            ),
        ],
        next_close_ok=False,
    )
    notifier = _Notifier()
    report = reconcile_rotation_orphan_positions(
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=_settings(ROTATION_ORPHAN_STRATEGY="strict"),
        now_utc=datetime(2026, 5, 5, 21, 0, tzinfo=UTC),
        notifier=notifier,
    )
    assert report.orphan_positions_handled == 0
    assert len(report.errors) == 1
    assert "orphan_close_failed" in report.errors[0]
    # Position still on MT5 since close was rejected.
    assert mt5.positions[0].ticket == 2003


# ---------------------------------------------------------------------------
# Orphan — adopt mode
# ---------------------------------------------------------------------------


def test_orphan_adopt_inserts_journal_row(session_factory, engine):
    mt5 = _FakeMT5Client(
        positions=[
            _PositionSnapshot(
                ticket=2004, symbol="XAUUSD", direction="long",
                volume=0.05, entry_price=2410.5,
                time_open_utc=datetime(2026, 5, 4, 21, 0, tzinfo=UTC),
            ),
        ],
        panel={"XAUUSD": _ohlc_panel("XAUUSD", n_bars=60)},
    )
    notifier = _Notifier()
    report = reconcile_rotation_orphan_positions(
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=_settings(ROTATION_ORPHAN_STRATEGY="adopt"),
        now_utc=datetime(2026, 5, 5, 21, 0, tzinfo=UTC),
        notifier=notifier,
    )
    assert report.orphan_positions_handled == 1
    assert report.orphan_strategy_used == "adopt"
    # Position NOT closed under adopt — left on MT5.
    assert len(mt5.positions) == 1
    # Journal row inserted.
    with session_scope(engine) as s:
        rows = list(s.execute(select(RotationPositionRow)).scalars())
        assert len(rows) == 1
        adopted = rows[0]
        assert adopted.symbol == "XAUUSD"
        assert adopted.mt5_ticket == 2004
        assert adopted.entry_price == pytest.approx(2410.5)
        assert adopted.atr_at_entry > 0
        assert adopted.status == "open"


# ---------------------------------------------------------------------------
# Orphan — alert_only mode
# ---------------------------------------------------------------------------


def test_orphan_alert_only_does_not_close_or_insert(session_factory, engine):
    mt5 = _FakeMT5Client(positions=[
        _PositionSnapshot(
            ticket=2005, symbol="EURUSD", direction="long",
            volume=0.02, entry_price=1.08,
        ),
    ])
    notifier = _Notifier()
    report = reconcile_rotation_orphan_positions(
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=_settings(ROTATION_ORPHAN_STRATEGY="alert_only"),
        now_utc=datetime(2026, 5, 5, 21, 0, tzinfo=UTC),
        notifier=notifier,
    )
    assert report.orphan_positions_handled == 1
    assert report.orphan_strategy_used == "alert_only"
    # Position untouched.
    assert mt5.positions[0].ticket == 2005
    # Telegram alert fired.
    assert len(notifier.alerts) == 1
    # Journal still empty.
    with session_scope(engine) as s:
        rows = list(s.execute(select(RotationPositionRow)).scalars())
        assert rows == []


# ---------------------------------------------------------------------------
# Ghost rows
# ---------------------------------------------------------------------------


def test_ghost_row_closed_with_zero_r_when_no_mt5_position(session_factory, engine):
    with session_scope(engine) as s:
        insert_rotation_position(
            s, strategy="trend_rotation_d1", symbol="XAUUSD",
            mt5_ticket=3001, direction="long", volume=0.05,
            entry_price=2400.0, atr_at_entry=12.5, risk_usd=24.25,
            entry_timestamp_utc=datetime(2026, 5, 1, 21, 0, tzinfo=UTC),
            entry_rebalance_uid=None,
        )
    mt5 = _FakeMT5Client(positions=[])  # no MT5 match
    notifier = _Notifier()
    report = reconcile_rotation_orphan_positions(
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=_settings(),
        now_utc=datetime(2026, 5, 5, 21, 0, tzinfo=UTC),
        notifier=notifier,
    )
    assert report.ghost_rows_handled == 1
    assert report.healthy_positions == 0
    assert len(notifier.alerts) == 1
    # Journal row marked closed at zero R.
    with session_scope(engine) as s:
        row = s.execute(
            select(RotationPositionRow).where(
                RotationPositionRow.mt5_ticket == 3001
            )
        ).scalar_one()
        assert row.status == "closed"
        assert row.realized_r == pytest.approx(0.0)
        assert row.realized_pnl_usd == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Mixed state
# ---------------------------------------------------------------------------


def test_mixed_orphan_ghost_healthy(session_factory, engine):
    """MT5 has 1 healthy + 1 orphan; journal has 1 healthy + 1 ghost."""
    with session_scope(engine) as s:
        insert_rotation_position(
            s, strategy="trend_rotation_d1", symbol="XAUUSD",
            mt5_ticket=4001, direction="long", volume=0.05,
            entry_price=2400.0, atr_at_entry=12.5, risk_usd=24.25,
            entry_timestamp_utc=datetime(2026, 5, 1, 21, 0, tzinfo=UTC),
            entry_rebalance_uid=None,
        )
        insert_rotation_position(
            s, strategy="trend_rotation_d1", symbol="GHOST",
            mt5_ticket=4002, direction="long", volume=0.01,
            entry_price=999.0, atr_at_entry=10.0, risk_usd=24.25,
            entry_timestamp_utc=datetime(2026, 5, 1, 21, 0, tzinfo=UTC),
            entry_rebalance_uid=None,
        )
    mt5 = _FakeMT5Client(positions=[
        _PositionSnapshot(
            ticket=4001, symbol="XAUUSD", direction="long",
            volume=0.05, entry_price=2400.0,
        ),
        _PositionSnapshot(
            ticket=4099, symbol="ORPHAN", direction="long",
            volume=0.10, entry_price=500.0,
        ),
    ])
    notifier = _Notifier()
    report = reconcile_rotation_orphan_positions(
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=_settings(ROTATION_ORPHAN_STRATEGY="strict"),
        now_utc=datetime(2026, 5, 5, 21, 0, tzinfo=UTC),
        notifier=notifier,
    )
    assert report.healthy_positions == 1
    assert report.orphan_positions_handled == 1  # 4099 closed
    assert report.ghost_rows_handled == 1        # GHOST row marked closed
    # Two alerts: one orphan + one ghost.
    assert len(notifier.alerts) == 2


# ---------------------------------------------------------------------------
# dry_run mode (smoke-test pathway)
# ---------------------------------------------------------------------------


def test_dry_run_does_not_close_or_journal_changes(session_factory, engine):
    with session_scope(engine) as s:
        insert_rotation_position(
            s, strategy="trend_rotation_d1", symbol="GHOST",
            mt5_ticket=5001, direction="long", volume=0.01,
            entry_price=100.0, atr_at_entry=5.0, risk_usd=10.0,
            entry_timestamp_utc=datetime(2026, 5, 1, 21, 0, tzinfo=UTC),
            entry_rebalance_uid=None,
        )
    mt5 = _FakeMT5Client(positions=[
        _PositionSnapshot(
            ticket=5099, symbol="ORPHAN", direction="long",
            volume=0.1, entry_price=200.0,
        ),
    ])
    notifier = _Notifier()
    report = reconcile_rotation_orphan_positions(
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=_settings(ROTATION_ORPHAN_STRATEGY="strict"),
        now_utc=datetime(2026, 5, 5, 21, 0, tzinfo=UTC),
        notifier=notifier,
        dry_run=True,
    )
    assert report.orphan_positions_handled == 1
    assert report.ghost_rows_handled == 1
    # MT5 not touched.
    assert len(mt5.positions) == 1
    methods = [c[0] for c in mt5.calls]
    assert "close_position_at_market" not in methods
    # Journal not touched.
    with session_scope(engine) as s:
        row = s.execute(
            select(RotationPositionRow).where(
                RotationPositionRow.mt5_ticket == 5001
            )
        ).scalar_one()
        assert row.status == "open"


# ---------------------------------------------------------------------------
# Unknown ROTATION_ORPHAN_STRATEGY value
# ---------------------------------------------------------------------------


def test_unknown_orphan_strategy_falls_back_to_strict(session_factory):
    mt5 = _FakeMT5Client(positions=[
        _PositionSnapshot(
            ticket=6001, symbol="XAUUSD", direction="long",
            volume=0.05, entry_price=2400.0,
        ),
    ])
    report = reconcile_rotation_orphan_positions(
        mt5_client=mt5,
        journal_session_factory=session_factory,
        settings=_settings(ROTATION_ORPHAN_STRATEGY="bogus"),
        now_utc=datetime(2026, 5, 5, 21, 0, tzinfo=UTC),
    )
    assert report.orphan_strategy_used == "strict"
    assert report.orphan_positions_handled == 1
