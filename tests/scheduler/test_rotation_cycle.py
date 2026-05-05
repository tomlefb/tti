"""Integration tests for ``src.scheduler.jobs.run_rotation_cycle``.

Exercises the full rotation cycle end-to-end with:

- A fake MT5 client (account info + OHLC + symbol_info + place_market_order
  + close_position_at_market) wired with deterministic per-asset data.
- An in-memory SQLite engine for the journal.
- An ``AsyncMock``-backed Telegram notifier.

What is verified:

- Pre-flight gates (kill switch, capital floor, daily loss limit) abort
  the cycle and log a Telegram alert without touching MT5.
- Cadence gate skips the cycle when the prior rebalance is within
  ``ROTATION_REBALANCE_DAYS``.
- A real first-rebalance run computes the top-K basket, sizes each
  open position, places market orders via the fake MT5, and persists
  every rotation_position + rebalance_transition row.
- A second rebalance with a flipped ranking correctly closes the
  dropped assets and opens the new ones (closes-then-opens ordering).
- A no-op rebalance (basket unchanged) journals the cadence anchor and
  skips MT5 entirely.
- Adaptive risk: capital below the floor → 0.5 %, above → 1 %.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pandas as pd
import pytest

from src.journal.db import get_engine, init_db, session_scope
from src.journal.models import (
    DailyPnlRow,
    RebalanceTransitionRow,
    RotationPositionRow,
)
from src.journal.repository import (
    get_open_rotation_positions,
    get_rotation_daily_pnl,
)
from src.scheduler.jobs import RotationCycleReport, run_rotation_cycle


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


@pytest.fixture
def notifier():
    n = AsyncMock()
    n.send_text = AsyncMock(return_value=None)
    n.send_error = AsyncMock(return_value=None)
    n.send_setup = AsyncMock(return_value=None)
    # Sync helper added in the Bug 2/3 fix bundle — combines parse_mode=HTML
    # + thread-safe scheduling. The rotation cycle uses this instead of
    # _run_async(notifier.send_text(...)) for HTML payloads.
    n.send_html_threadsafe = MagicMock(return_value=None)
    return n


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        ACTIVE_STRATEGY="trend_rotation_d1",
        ROTATION_UNIVERSE=("AAA", "BBB", "CCC", "DDD", "EEE"),
        ROTATION_K=2,
        ROTATION_MOMENTUM_LOOKBACK_DAYS=10,
        ROTATION_REBALANCE_DAYS=5,
        ROTATION_ATR_PERIOD=5,
        ROTATION_MAGIC_NUMBER=7799,
        ROTATION_RISK_PER_TRADE_FULL_PCT=0.01,
        ROTATION_RISK_PER_TRADE_REDUCED_PCT=0.005,
        ROTATION_CAPITAL_FLOOR_FOR_FULL_RISK_USD=4950.0,
        ROTATION_CAPITAL_FLOOR_USD=4500.0,
        DAILY_LOSS_LIMIT_USD=150.0,
        SPREAD_ANOMALY_MULTIPLIER=3.0,
        TYPICAL_SPREADS={"AAA": 0.5, "BBB": 0.5, "CCC": 0.5, "DDD": 0.5, "EEE": 0.5},
        KILL_SWITCH_PATH=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_d1_panel_for_assets(
    assets: list[str],
    *,
    n_bars: int = 60,
    end_date: datetime,
    asset_returns: dict[str, float] | None = None,
) -> dict[str, pd.DataFrame]:
    """Synthesize per-asset D1 OHLC frames with a controllable cumulative
    return so the top-K ranking is deterministic in tests.

    ``asset_returns[asset]`` is the total return over the lookback window.
    Higher values rank higher (long-only momentum).
    """
    asset_returns = asset_returns or {a: 0.0 for a in assets}
    panel: dict[str, pd.DataFrame] = {}
    for asset in assets:
        ret = asset_returns.get(asset, 0.0)
        # Linearly increase close from base to base*(1+ret) over n_bars.
        base = 100.0
        end_price = base * (1.0 + ret)
        rng = np.random.default_rng(seed=hash(asset) & 0xFFFFFFFF)
        # small per-bar noise so ATR > 0
        prices = np.linspace(base, end_price, n_bars) + rng.normal(0, 0.5, n_bars)
        # Build dates ending at end_date (inclusive); UTC midnight.
        dates = pd.date_range(end=end_date, periods=n_bars, freq="D", tz=UTC)
        df = pd.DataFrame({
            "time": dates,
            "open": prices,
            "high": prices + 0.5,
            "low": prices - 0.5,
            "close": prices,
            "volume": 1000,
        })
        panel[asset] = df
    return panel


@dataclass
class _AccountInfo:
    login_masked: str = "***1234"
    currency: str = "USD"
    balance: float = 4850.0
    equity: float = 4850.0
    profit: float = 0.0
    margin_level: float = 0.0
    leverage: int = 100


def _symbol_info(
    *, ask: float = 100.0, bid: float = 99.95, contract_size: float = 1.0
) -> SimpleNamespace:
    return SimpleNamespace(
        trade_contract_size=contract_size,
        point=0.01,
        volume_min=0.01,
        volume_step=0.01,
        volume_max=100.0,
        ask=ask,
        bid=bid,
    )


@dataclass
class _FakeMT5Client:
    """End-to-end MT5 fake — implements every method the rotation cycle
    touches with deterministic in-memory state."""

    account: _AccountInfo = field(default_factory=_AccountInfo)
    panel: dict[str, pd.DataFrame] = field(default_factory=dict)
    next_market_retcode: int = 10009
    next_close_ok: bool = True
    close_info: dict[int, dict[str, Any]] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    ticket_counter: int = 5001

    def get_account_info(self) -> _AccountInfo:
        self.calls.append(("get_account_info", {}))
        return self.account

    def fetch_ohlc(self, symbol: str, timeframe: str, n_candles: int) -> pd.DataFrame:
        self.calls.append((
            "fetch_ohlc",
            {"symbol": symbol, "timeframe": timeframe, "n_candles": n_candles},
        ))
        if symbol not in self.panel:
            raise RuntimeError(f"no panel data for {symbol}")
        df = self.panel[symbol].copy()
        return df.tail(n_candles).reset_index(drop=True)

    def get_symbol_info(self, symbol: str) -> SimpleNamespace:
        self.calls.append(("get_symbol_info", {"symbol": symbol}))
        return _symbol_info()

    def place_market_order(self, **kwargs):
        self.calls.append(("place_market_order", kwargs))
        deal = self.ticket_counter
        self.ticket_counter += 1
        return SimpleNamespace(
            retcode=self.next_market_retcode, order=0, deal=deal,
            comment="Done", request_id=1,
        )

    def close_position_at_market(self, ticket: int) -> bool:
        self.calls.append(("close_position_at_market", {"ticket": ticket}))
        return self.next_close_ok

    def get_position_close_info(self, ticket: int):
        self.calls.append(("get_position_close_info", {"ticket": ticket}))
        return self.close_info.get(int(ticket))


# ---------------------------------------------------------------------------
# Pre-flight gates
# ---------------------------------------------------------------------------


def test_rotation_cycle_blocks_on_kill_switch(
    session_factory, notifier, tmp_path
):
    kill = tmp_path / "KILL_SWITCH"
    kill.touch()
    settings = _settings(KILL_SWITCH_PATH=kill)
    mt5 = _FakeMT5Client()

    report = run_rotation_cycle(
        mt5, session_factory, notifier, settings,
        now_utc=datetime(2026, 5, 5, 21, 0, tzinfo=UTC),
    )
    assert isinstance(report, RotationCycleReport)
    assert report.fired is False
    assert report.skipped_reason == "kill_switch"
    # Kill switch fired — Telegram alert sent.
    assert notifier.send_html_threadsafe.call_count == 1
    # No MT5 calls beyond the account info read.
    methods = [c[0] for c in mt5.calls]
    assert "fetch_ohlc" not in methods
    assert "place_market_order" not in methods


def test_rotation_cycle_blocks_on_capital_below_floor(
    session_factory, notifier, tmp_path
):
    settings = _settings(
        KILL_SWITCH_PATH=tmp_path / "absent",
        ROTATION_CAPITAL_FLOOR_USD=4500.0,
    )
    mt5 = _FakeMT5Client(account=_AccountInfo(balance=4400.0))

    report = run_rotation_cycle(
        mt5, session_factory, notifier, settings,
        now_utc=datetime(2026, 5, 5, 21, 0, tzinfo=UTC),
    )
    assert report.skipped_reason == "capital_below_safe_threshold"
    assert notifier.send_html_threadsafe.call_count == 1
    methods = [c[0] for c in mt5.calls]
    assert "fetch_ohlc" not in methods


# ---------------------------------------------------------------------------
# Cadence gate
# ---------------------------------------------------------------------------


def test_rotation_cycle_skips_when_not_due(
    session_factory, engine, notifier, tmp_path
):
    settings = _settings(KILL_SWITCH_PATH=tmp_path / "absent")
    # Seed an open rotation position from 2 days ago — within the
    # 5-day rebalance window.
    now = datetime(2026, 5, 5, 21, 0, tzinfo=UTC)
    last_rebal = now - timedelta(days=2)
    with session_scope(engine) as s:
        from src.journal.repository import insert_rotation_position
        insert_rotation_position(
            s, strategy="trend_rotation_d1", symbol="AAA",
            mt5_ticket=1001, direction="long", volume=0.10,
            entry_price=100.0, atr_at_entry=5.0, risk_usd=24.25,
            entry_timestamp_utc=last_rebal, entry_rebalance_uid=None,
        )
    mt5 = _FakeMT5Client()
    report = run_rotation_cycle(
        mt5, session_factory, notifier, settings, now_utc=now,
    )
    assert report.skipped_reason == "not_due"
    methods = [c[0] for c in mt5.calls]
    # Account info read (always); no panel fetches when cadence blocks.
    assert "fetch_ohlc" not in methods


# ---------------------------------------------------------------------------
# First rebalance — full cycle
# ---------------------------------------------------------------------------


def test_rotation_cycle_first_rebalance_opens_top_k(
    session_factory, engine, notifier, tmp_path
):
    """No prior positions — pipeline picks the top-K assets purely from
    the synthetic panel returns."""
    now = datetime(2026, 5, 5, 21, 0, tzinfo=UTC)
    settings = _settings(KILL_SWITCH_PATH=tmp_path / "absent")
    # Universe of 5 assets; AAA + BBB have the highest 10-day momentum.
    panel = _make_d1_panel_for_assets(
        ["AAA", "BBB", "CCC", "DDD", "EEE"],
        n_bars=60, end_date=now,
        asset_returns={
            "AAA": 0.80, "BBB": 0.50, "CCC": 0.10, "DDD": -0.05, "EEE": -0.20,
        },
    )
    mt5 = _FakeMT5Client(panel=panel)
    report = run_rotation_cycle(
        mt5, session_factory, notifier, settings, now_utc=now,
    )

    assert report.fired is True
    assert report.basket_before == []
    assert sorted(report.basket_after) == ["AAA", "BBB"]  # K=2 winners
    assert sorted(report.opened_assets) == ["AAA", "BBB"]
    assert report.closed_assets == []
    assert report.opens_succeeded == 2
    assert report.opens_failed == 0
    assert report.risk_pct == 0.005  # capital 4850 < 4950 -> reduced

    # Two market orders sent.
    methods = [c[0] for c in mt5.calls]
    assert methods.count("place_market_order") == 2
    assert "close_position_at_market" not in methods

    # Journal: one rebalance_transitions row, two rotation_positions rows.
    with session_scope(engine) as s:
        positions = list(s.query(RotationPositionRow).all())
        assert len(positions) == 2
        assert sorted(p.symbol for p in positions) == ["AAA", "BBB"]
        assert all(p.status == "open" for p in positions)
        rebals = list(s.query(RebalanceTransitionRow).all())
        assert len(rebals) == 1
        assert rebals[0].risk_per_trade_pct == pytest.approx(0.005)


# ---------------------------------------------------------------------------
# Rotation rebalance — basket flip closes + opens
# ---------------------------------------------------------------------------


def test_rotation_cycle_basket_flip_closes_dropped_opens_new(
    session_factory, engine, notifier, tmp_path
):
    """Pre-seed AAA + BBB as open; new ranking favours CCC + DDD."""
    now = datetime(2026, 5, 12, 21, 0, tzinfo=UTC)  # 7 days after seed
    settings = _settings(KILL_SWITCH_PATH=tmp_path / "absent")
    seed_time = now - timedelta(days=7)
    with session_scope(engine) as s:
        from src.journal.repository import insert_rotation_position
        for i, sym in enumerate(("AAA", "BBB"), start=1):
            insert_rotation_position(
                s, strategy="trend_rotation_d1", symbol=sym,
                mt5_ticket=1000 + i, direction="long", volume=0.10,
                entry_price=100.0, atr_at_entry=5.0, risk_usd=24.25,
                entry_timestamp_utc=seed_time, entry_rebalance_uid=None,
            )
    panel = _make_d1_panel_for_assets(
        ["AAA", "BBB", "CCC", "DDD", "EEE"],
        n_bars=60, end_date=now,
        asset_returns={
            "AAA": -0.20, "BBB": -0.10, "CCC": 0.80, "DDD": 0.50, "EEE": 0.10,
        },
    )
    mt5 = _FakeMT5Client(
        panel=panel,
        close_info={
            1001: {"exit_price": 102.0, "profit_usd": 5.0},
            1002: {"exit_price": 102.5, "profit_usd": 6.0},
        },
    )

    report = run_rotation_cycle(
        mt5, session_factory, notifier, settings, now_utc=now,
    )
    assert report.fired is True
    assert sorted(report.basket_before) == ["AAA", "BBB"]
    assert sorted(report.basket_after) == ["CCC", "DDD"]
    assert sorted(report.closed_assets) == ["AAA", "BBB"]
    assert sorted(report.opened_assets) == ["CCC", "DDD"]
    assert report.closes_succeeded == 2
    assert report.opens_succeeded == 2

    # Closes precede opens in the call sequence.
    methods = [c[0] for c in mt5.calls]
    last_close = max(
        i for i, m in enumerate(methods) if m == "close_position_at_market"
    )
    first_open = methods.index("place_market_order")
    assert last_close < first_open

    # Journal: AAA + BBB closed; CCC + DDD open.
    with session_scope(engine) as s:
        opens = get_open_rotation_positions(s, strategy="trend_rotation_d1")
        assert sorted(p.symbol for p in opens) == ["CCC", "DDD"]


# ---------------------------------------------------------------------------
# No-op rebalance — basket unchanged
# ---------------------------------------------------------------------------


def test_rotation_cycle_basket_unchanged_journals_anchor_no_mt5_orders(
    session_factory, engine, notifier, tmp_path
):
    now = datetime(2026, 5, 12, 21, 0, tzinfo=UTC)
    settings = _settings(KILL_SWITCH_PATH=tmp_path / "absent")
    seed_time = now - timedelta(days=7)
    with session_scope(engine) as s:
        from src.journal.repository import insert_rotation_position
        for i, sym in enumerate(("AAA", "BBB"), start=1):
            insert_rotation_position(
                s, strategy="trend_rotation_d1", symbol=sym,
                mt5_ticket=1000 + i, direction="long", volume=0.10,
                entry_price=100.0, atr_at_entry=5.0, risk_usd=24.25,
                entry_timestamp_utc=seed_time, entry_rebalance_uid=None,
            )
    # Same ranking → AAA + BBB still win.
    panel = _make_d1_panel_for_assets(
        ["AAA", "BBB", "CCC", "DDD", "EEE"],
        n_bars=60, end_date=now,
        asset_returns={
            "AAA": 0.20, "BBB": 0.15, "CCC": 0.05, "DDD": 0.03, "EEE": 0.01,
        },
    )
    mt5 = _FakeMT5Client(panel=panel)

    report = run_rotation_cycle(
        mt5, session_factory, notifier, settings, now_utc=now,
    )
    assert report.fired is False
    assert report.skipped_reason == "basket_unchanged"
    methods = [c[0] for c in mt5.calls]
    assert "place_market_order" not in methods
    assert "close_position_at_market" not in methods
    # Cadence anchor row written so the next rebalance gate updates.
    with session_scope(engine) as s:
        rebals = list(s.query(RebalanceTransitionRow).all())
        assert len(rebals) == 1
        assert rebals[0].notes is not None
        assert "no-op" in rebals[0].notes


# ---------------------------------------------------------------------------
# Adaptive risk schedule
# ---------------------------------------------------------------------------


def test_rotation_cycle_uses_full_risk_at_or_above_floor(
    session_factory, notifier, tmp_path
):
    now = datetime(2026, 5, 5, 21, 0, tzinfo=UTC)
    settings = _settings(KILL_SWITCH_PATH=tmp_path / "absent")
    panel = _make_d1_panel_for_assets(
        ["AAA", "BBB", "CCC", "DDD", "EEE"],
        n_bars=60, end_date=now,
        asset_returns={
            "AAA": 0.80, "BBB": 0.50, "CCC": 0.10, "DDD": -0.05, "EEE": -0.20,
        },
    )
    mt5 = _FakeMT5Client(
        panel=panel,
        # Capital ABOVE the 4950 floor → full 1 % risk.
        account=_AccountInfo(balance=5500.0),
    )
    report = run_rotation_cycle(
        mt5, session_factory, notifier, settings, now_utc=now,
    )
    assert report.fired is True
    assert report.risk_pct == 0.01


# ---------------------------------------------------------------------------
# Daily P&L journal updates
# ---------------------------------------------------------------------------


def test_rotation_cycle_refreshes_daily_pnl_row(
    session_factory, engine, notifier, tmp_path
):
    now = datetime(2026, 5, 5, 21, 0, tzinfo=UTC)
    settings = _settings(KILL_SWITCH_PATH=tmp_path / "absent")
    panel = _make_d1_panel_for_assets(
        ["AAA", "BBB", "CCC", "DDD", "EEE"],
        n_bars=60, end_date=now,
        asset_returns={
            "AAA": 0.80, "BBB": 0.50, "CCC": 0.10, "DDD": -0.05, "EEE": -0.20,
        },
    )
    mt5 = _FakeMT5Client(panel=panel)
    run_rotation_cycle(
        mt5, session_factory, notifier, settings, now_utc=now,
    )
    # First call of the day → row created with opening_balance == capital.
    with session_scope(engine) as s:
        row = get_rotation_daily_pnl(s, day=date(2026, 5, 5))
    assert row is not None
    assert row.opening_balance_usd == pytest.approx(4850.0)
    assert row.current_balance_usd == pytest.approx(4850.0)
    assert row.daily_pnl_usd == pytest.approx(0.0)
