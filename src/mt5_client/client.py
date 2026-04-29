"""Real MT5 wrapper used by the Sprint 6 scheduler.

Replaces the Sprint 5 stub. Wraps the ``MetaTrader5`` Python package
with:

- A clean ``connect`` / ``shutdown`` / ``is_connected`` lifecycle.
- ``fetch_ohlc`` returning a UTC-aware ``pandas.DataFrame``. Broker-time
  conversion is detected on connect and cached.
- ``get_account_info`` returning a typed dataclass for hard-stop checks.
- ``get_recent_trades`` returning :class:`src.journal.outcome_tracker.Mt5Trade`
  so the existing outcome tracker can be wired without translation.

NO order-placement code is present, ever — see CLAUDE.md rule #1 and
docs/04 §"Things Claude Code should NOT do".

Live tests run on the Windows host via ``scripts/test_mt5.py``. CI
exercises this module with a mocked ``MetaTrader5`` symbol.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from src.journal.outcome_tracker import Mt5Trade
from src.mt5_client.exceptions import (
    MT5AccountError,
    MT5ConnectionError,
    MT5DataError,
)
from src.mt5_client.time_conversion import (
    broker_naive_seconds_to_utc,
    detect_broker_offset_hours,
)

logger = logging.getLogger(__name__)


# Mapping from caller-facing strings to the integer constants the MT5
# package exposes. Integer fallbacks are the documented values for the
# common timeframes — used when the ``MetaTrader5`` import is mocked
# (CI) or when the constants drift in a future release.
_TIMEFRAME_CONSTANT_NAMES = {
    "M1": "TIMEFRAME_M1",
    "M5": "TIMEFRAME_M5",
    "M15": "TIMEFRAME_M15",
    "H1": "TIMEFRAME_H1",
    "H4": "TIMEFRAME_H4",
    "D1": "TIMEFRAME_D1",
}


@dataclass(frozen=True)
class AccountInfo:
    """Snapshot of the operator's MT5 account state.

    All values come straight from ``mt5.account_info()``. Used by the
    hard-stops layer to compute drawdown.
    """

    login_masked: str
    currency: str
    balance: float
    equity: float
    profit: float  # current floating P&L on open positions
    margin_level: float  # equity / margin × 100, or 0 if no open positions
    leverage: int


class MT5Client:
    """Adapter over the ``MetaTrader5`` package.

    Lifecycle::

        client = MT5Client(login=..., password=..., server=...)
        client.connect()
        try:
            df = client.fetch_ohlc("XAUUSD", "M5", 500)
            ...
        finally:
            client.shutdown()

    The client retains a cached broker-offset (in whole hours) detected
    once on connect so subsequent calls do not re-probe.
    """

    def __init__(
        self,
        login: int,
        password: str,
        server: str,
        path_to_terminal: str | None = None,
        *,
        mt5_module: Any | None = None,
    ) -> None:
        """Construct the client without touching MT5.

        Args:
            login: MT5 account login.
            password: MT5 account password.
            server: MT5 server name (broker-specific).
            path_to_terminal: optional explicit path to ``terminal64.exe``;
                when ``None`` the package auto-detects.
            mt5_module: injected for tests. ``None`` ⇒ import the real
                ``MetaTrader5`` package on connect.
        """
        self._login = int(login)
        self._password = str(password) if password is not None else ""
        self._server = str(server) if server is not None else ""
        self._path = path_to_terminal
        self._mt5: Any = mt5_module
        self._connected = False
        self._broker_offset_hours: int | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Initialize the MT5 terminal connection.

        Raises:
            MT5ConnectionError: import failed, ``mt5.initialize()`` returned
                False, or no symbol tick was retrievable for offset probing.
        """
        if self._mt5 is None:
            try:
                import MetaTrader5 as mt5  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover — host-dependent
                raise MT5ConnectionError(
                    "MetaTrader5 package is not installed. Run on the "
                    "Windows host with `pip install -r requirements.txt`."
                ) from exc
            self._mt5 = mt5

        kwargs = {
            "login": self._login,
            "password": self._password,
            "server": self._server,
        }
        if self._path is not None:
            kwargs["path"] = self._path

        ok = self._mt5.initialize(**kwargs)
        if not ok:
            err = self._safe_last_error()
            raise MT5ConnectionError(
                f"mt5.initialize() failed (login=****{str(self._login)[-4:]}, "
                f"server={self._server!r}). last_error={err!r}"
            )
        self._connected = True

        # Probe broker timezone offset using the first watched symbol's
        # tick. Falls back to Athens if the probe is unreachable.
        broker_now_seconds = self._probe_broker_now_seconds()
        self._broker_offset_hours = detect_broker_offset_hours(broker_now_seconds)
        logger.info(
            "MT5Client.connect: connected (server=%s, offset=UTC%+d)",
            self._server,
            self._broker_offset_hours,
        )

    def shutdown(self) -> None:
        """Close MT5. Idempotent — safe to call from a ``finally`` block."""
        if not self._connected or self._mt5 is None:
            return
        try:
            self._mt5.shutdown()
        except Exception:  # noqa: BLE001 — shutdown errors are non-fatal
            logger.exception("MT5Client.shutdown raised — swallowing")
        finally:
            self._connected = False
            logger.info("MT5Client.shutdown: connection closed")

    def is_connected(self) -> bool:
        """Quick health check — ``True`` between ``connect`` and ``shutdown``."""
        return self._connected

    # ------------------------------------------------------------------
    # Data access
    # ------------------------------------------------------------------

    def fetch_ohlc(self, symbol: str, timeframe: str, n_candles: int) -> pd.DataFrame:
        """Fetch the last ``n_candles`` for ``symbol`` on ``timeframe``.

        Args:
            symbol: MT5 symbol exactly as exposed by the broker terminal.
            timeframe: one of ``"M1"``, ``"M5"``, ``"M15"``, ``"H1"``,
                ``"H4"``, ``"D1"``.
            n_candles: how many candles to retrieve, counted backwards from
                the most recent available bar.

        Returns:
            DataFrame with columns ``time, open, high, low, close, volume``.
            ``time`` is UTC-aware ``Timestamp``. Sorted ascending.

        Raises:
            MT5DataError: unknown timeframe, empty fetch, or malformed rates.
            MT5ConnectionError: client is not connected.
        """
        self._require_connected()
        tf_const = self._timeframe_constant(timeframe)

        rates = self._mt5.copy_rates_from_pos(symbol, tf_const, 0, int(n_candles))
        if rates is None or len(rates) == 0:
            err = self._safe_last_error()
            raise MT5DataError(
                f"copy_rates_from_pos returned no data for {symbol!r}/{timeframe} "
                f"(n_candles={n_candles}). last_error={err!r}"
            )

        df = pd.DataFrame(rates)
        if "time" not in df.columns:
            raise MT5DataError(f"OHLC frame missing 'time' column: {df.columns!r}")

        # Convert each broker-naive POSIX timestamp to true UTC.
        offset = self._require_offset()
        df["time"] = df["time"].apply(lambda s: broker_naive_seconds_to_utc(s, offset))
        df["time"] = pd.to_datetime(df["time"], utc=True)

        # Normalise volume column name. MT5 exposes 'tick_volume' and/or
        # 'real_volume'; the rest of the codebase expects 'volume'.
        if "volume" not in df.columns:
            if "tick_volume" in df.columns:
                df["volume"] = df["tick_volume"]
            elif "real_volume" in df.columns:
                df["volume"] = df["real_volume"]
            else:
                df["volume"] = 0

        keep = ["time", "open", "high", "low", "close", "volume"]
        df = df[keep].sort_values("time").reset_index(drop=True)
        return df

    def get_account_info(self) -> AccountInfo:
        """Return the current account snapshot.

        Raises:
            MT5AccountError: ``mt5.account_info()`` returned ``None``.
            MT5ConnectionError: client is not connected.
        """
        self._require_connected()
        info = self._mt5.account_info()
        if info is None:
            err = self._safe_last_error()
            raise MT5AccountError(f"account_info() returned None. last_error={err!r}")

        login_str = str(getattr(info, "login", self._login))
        masked = "*" * max(0, len(login_str) - 4) + login_str[-4:]
        return AccountInfo(
            login_masked=masked,
            currency=str(getattr(info, "currency", "")),
            balance=float(getattr(info, "balance", 0.0)),
            equity=float(getattr(info, "equity", 0.0)),
            profit=float(getattr(info, "profit", 0.0)),
            margin_level=float(getattr(info, "margin_level", 0.0) or 0.0),
            leverage=int(getattr(info, "leverage", 0)),
        )

    def get_recent_trades(self, since: datetime) -> list[Mt5Trade]:
        """Fetch closed trades since ``since`` (UTC) for outcome reconciliation.

        Returns objects matching ``src.journal.outcome_tracker.Mt5Trade`` so
        the existing outcome tracker plugs in directly.

        Raises:
            MT5DataError: ``history_deals_get`` returned an unexpected error.
            MT5ConnectionError: client is not connected.
            ValueError: ``since`` is naive or in the future.
        """
        self._require_connected()
        if since.tzinfo is None:
            raise ValueError(f"`since` must be UTC-aware, got naive {since!r}")
        if since > datetime.now(tz=UTC):
            raise ValueError(f"`since` is in the future: {since!r}")

        # MT5 history_deals_get expects a broker-naive datetime range.
        offset = self._require_offset()
        since_broker_naive = since.astimezone(UTC).replace(tzinfo=None) + _hours(offset)
        until_broker_naive = datetime.now(tz=UTC).replace(tzinfo=None) + _hours(offset)

        deals = self._mt5.history_deals_get(since_broker_naive, until_broker_naive)
        if deals is None:
            err = self._safe_last_error()
            # ``None`` can mean "no trades" on some MT5 builds; treat as empty.
            if err and err != (1, "Success") and err[0] not in (1, 0):
                raise MT5DataError(f"history_deals_get returned None with error {err!r}")
            return []

        return list(_deals_to_trades(deals, offset_hours=offset))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _require_connected(self) -> None:
        if not self._connected or self._mt5 is None:
            raise MT5ConnectionError("MT5Client is not connected — call connect() first.")

    def _require_offset(self) -> int:
        if self._broker_offset_hours is None:
            raise MT5ConnectionError("Broker offset not detected — was connect() called?")
        return self._broker_offset_hours

    def _safe_last_error(self) -> Any:
        if self._mt5 is None or not hasattr(self._mt5, "last_error"):
            return None
        try:
            return self._mt5.last_error()
        except Exception:  # noqa: BLE001 — defensive
            return None

    def _timeframe_constant(self, timeframe: str) -> int:
        """Resolve a string timeframe to the MT5 integer constant."""
        try:
            attr_name = _TIMEFRAME_CONSTANT_NAMES[timeframe.upper()]
        except KeyError as exc:
            raise MT5DataError(
                f"unknown timeframe {timeframe!r}; supported: "
                f"{sorted(_TIMEFRAME_CONSTANT_NAMES)}"
            ) from exc
        if not hasattr(self._mt5, attr_name):
            raise MT5DataError(
                f"MT5 module has no attribute {attr_name!r} — package version mismatch?"
            )
        return int(getattr(self._mt5, attr_name))

    def _probe_broker_now_seconds(self) -> float | None:
        """Return ``mt5.symbol_info_tick(...).time`` as POSIX seconds, or ``None``."""
        try:
            tick = self._mt5.symbol_info_tick("XAUUSD")
        except Exception:  # noqa: BLE001 — symbol may differ; try fallbacks
            tick = None

        if tick is None:
            for fallback in ("EURUSD", "GBPUSD"):
                try:
                    tick = self._mt5.symbol_info_tick(fallback)
                except Exception:  # noqa: BLE001
                    tick = None
                if tick is not None:
                    break

        if tick is None:
            return None
        time_val = getattr(tick, "time", None)
        if time_val is None:
            return None
        return float(time_val)


def _hours(n: int):
    from datetime import timedelta

    return timedelta(hours=n)


def _deals_to_trades(deals: Any, *, offset_hours: int):
    """Pair MT5 history deals (entry + exit) into ``Mt5Trade`` objects.

    MT5 emits one deal per fill — an order has at least one entry deal
    (DEAL_ENTRY_IN) and one exit deal (DEAL_ENTRY_OUT). We pair them by
    ``position_id`` (the MT5 ticket of the underlying position).
    """
    by_position: dict[int, list[Any]] = {}
    for d in deals:
        pos_id = int(getattr(d, "position_id", 0))
        if pos_id == 0:
            continue
        by_position.setdefault(pos_id, []).append(d)

    for pos_id, ds in by_position.items():
        # Sort by time so DEAL_ENTRY_IN comes first.
        ds.sort(key=lambda d: float(getattr(d, "time", 0)))

        entry = next((d for d in ds if int(getattr(d, "entry", 0)) == 0), ds[0])
        exits = [d for d in ds if int(getattr(d, "entry", 0)) == 1]
        last_exit = exits[-1] if exits else None

        symbol = str(getattr(entry, "symbol", ""))
        # MT5 deal type: 0=BUY, 1=SELL.
        deal_type = int(getattr(entry, "type", 0))
        direction = "long" if deal_type == 0 else "short"

        entry_time_seconds = float(getattr(entry, "time", 0))
        entry_time_utc = broker_naive_seconds_to_utc(entry_time_seconds, offset_hours)
        entry_price = float(getattr(entry, "price", 0.0))

        exit_time_utc = None
        exit_price = None
        profit_usd: float | None = float(getattr(entry, "profit", 0.0))
        if last_exit is not None:
            exit_time_seconds = float(getattr(last_exit, "time", 0))
            exit_time_utc = broker_naive_seconds_to_utc(exit_time_seconds, offset_hours)
            exit_price = float(getattr(last_exit, "price", 0.0))
            # Sum profits across all exit deals (partial closes).
            profit_usd = float(sum(float(getattr(d, "profit", 0.0)) for d in exits))

        yield Mt5Trade(
            ticket=pos_id,
            symbol=symbol,
            direction=direction,
            entry_time_utc=entry_time_utc,
            entry_price=entry_price,
            exit_time_utc=exit_time_utc,
            exit_price=exit_price,
            profit_usd=profit_usd,
        )
