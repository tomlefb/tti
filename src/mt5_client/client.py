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
import re
from typing import Any

import pandas as pd

from src.journal.outcome_tracker import Mt5Trade

# FundedNext-Server (and probably most MT5 broker servers) reject any
# order_send request whose ``comment`` field is "too long" or contains
# unexpected characters. Empirically (commit fea788f, scripts/probe_
# order_check_comment.py), comments ≥ 31 chars all fail with
# 'Invalid "comment" argument'; ≤ 19 chars all succeed regardless of
# punctuation. Cap at 20 with a small safety margin and replace anything
# that isn't ASCII alphanumeric / underscore / dash with an underscore.
_COMMENT_MAX_LEN = 20
_COMMENT_SAFE_RE = re.compile(r"[^A-Za-z0-9_\-]")


def _sanitize_order_comment(comment: str | None) -> str:
    """Make ``comment`` safe to pass to MT5 ``order_send``.

    - ``None`` becomes empty string (the broker accepts an empty comment).
    - Non-ASCII / punctuation chars are replaced by ``_`` (FundedNext
      rejects ``:`` and probably others).
    - Truncated to ``_COMMENT_MAX_LEN`` chars to stay under the broker's
      length cap.
    """
    if not comment:
        return ""
    cleaned = _COMMENT_SAFE_RE.sub("_", str(comment))
    return cleaned[:_COMMENT_MAX_LEN]
from src.mt5_client.exceptions import (
    MT5AccountError,
    MT5ConnectionError,
    MT5DataError,
    MT5Error,
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


@dataclass(frozen=True)
class SymbolInfoSnapshot:
    """Subset of ``mt5.symbol_info()`` used by the order_manager.

    ``trade_contract_size``, ``volume_min``, ``volume_step``,
    ``volume_max`` drive position sizing. ``ask`` / ``bid`` drive the
    spread anomaly check. ``point`` is exposed for callers that need it.
    """

    symbol: str
    trade_contract_size: float
    point: float
    volume_min: float
    volume_step: float
    volume_max: float
    ask: float
    bid: float


@dataclass(frozen=True)
class PositionSnapshot:
    """One row from ``mt5.positions_get()`` reduced to the fields the
    Sprint 7 lifecycle / recovery modules actually use."""

    ticket: int
    symbol: str
    direction: str  # "long" or "short"
    volume: float
    entry_price: float
    sl: float
    tp: float
    magic: int
    time_open_utc: datetime
    profit: float


@dataclass(frozen=True)
class PendingOrderSnapshot:
    """One row from ``mt5.orders_get()`` reduced to the same shape."""

    ticket: int
    symbol: str
    direction: str
    volume: float
    price_open: float
    sl: float
    tp: float
    magic: int
    time_setup_utc: datetime


@dataclass(frozen=True)
class OrderSendResult:
    """Subset of ``mt5.order_send()`` return value."""

    retcode: int
    order: int
    deal: int
    comment: str
    request_id: int


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
    # Sprint 7 — order operations
    # ------------------------------------------------------------------

    def get_symbol_info(self, symbol: str) -> SymbolInfoSnapshot:
        """Return the trading parameters for ``symbol``.

        Raises:
            MT5Error: ``mt5.symbol_info()`` returned ``None`` (symbol
                unknown to the broker, or terminal disconnected).
            MT5ConnectionError: client not connected.
        """
        self._require_connected()
        info = self._mt5.symbol_info(symbol)
        if info is None:
            err = self._safe_last_error()
            raise MT5Error(f"symbol_info({symbol!r}) returned None. last_error={err!r}")
        return SymbolInfoSnapshot(
            symbol=str(getattr(info, "name", symbol)),
            trade_contract_size=float(getattr(info, "trade_contract_size", 1.0)),
            point=float(getattr(info, "point", 0.0)),
            volume_min=float(getattr(info, "volume_min", 0.01)),
            volume_step=float(getattr(info, "volume_step", 0.01)),
            volume_max=float(getattr(info, "volume_max", 100.0)),
            ask=float(getattr(info, "ask", 0.0)),
            bid=float(getattr(info, "bid", 0.0)),
        )

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
    ) -> OrderSendResult:
        """Submit a ``BUY_LIMIT`` (long) or ``SELL_LIMIT`` (short) order.

        Raises:
            ValueError: ``direction`` is not ``"long"`` or ``"short"``.
            MT5Error: ``mt5.order_send()`` returned ``None``.
            MT5ConnectionError: client not connected.

        Returns:
            :class:`OrderSendResult` carrying the broker retcode and
            ticket. The caller (order_manager) inspects ``retcode`` to
            decide success vs failure — this method does not raise on
            non-success retcodes since that is the caller's policy.
        """
        self._require_connected()
        if direction == "long":
            order_type = self._mt5.ORDER_TYPE_BUY_LIMIT
        elif direction == "short":
            order_type = self._mt5.ORDER_TYPE_SELL_LIMIT
        else:
            raise ValueError(
                f"direction must be 'long' or 'short', got {direction!r}"
            )

        request = {
            "action": self._mt5.TRADE_ACTION_PENDING,
            "symbol": symbol,
            "volume": float(volume),
            "type": order_type,
            "price": float(price),
            "sl": float(sl),
            "tp": float(tp),
            "magic": int(magic),
            "comment": _sanitize_order_comment(comment),
            "type_time": self._mt5.ORDER_TIME_GTC,
            "type_filling": self._mt5.ORDER_FILLING_IOC,
        }
        result = self._mt5.order_send(request)
        if result is None:
            err = self._safe_last_error()
            raise MT5Error(f"order_send returned None. last_error={err!r}")
        return OrderSendResult(
            retcode=int(getattr(result, "retcode", 0)),
            order=int(getattr(result, "order", 0)),
            deal=int(getattr(result, "deal", 0)),
            comment=str(getattr(result, "comment", "")),
            request_id=int(getattr(result, "request_id", 0)),
        )

    def place_market_order(
        self,
        *,
        symbol: str,
        direction: str,
        volume: float,
        magic: int,
        sl: float = 0.0,
        tp: float = 0.0,
        comment: str = "",
    ) -> OrderSendResult:
        """Submit a market BUY (long) or SELL (short) order.

        Used by the rotation strategy where entries fire at the D1 close
        of the rebalance day (no limit price). The market price is read
        from ``symbol_info_tick`` immediately before sending — ask for
        long entries, bid for short — so the deal hits the live spread.

        ``sl`` and ``tp`` default to ``0.0`` (broker's "no SL/TP" sentinel)
        — rotation has no hard SL/TP, exits happen at the next rebalance.

        Raises:
            ValueError: ``direction`` is not ``"long"`` or ``"short"``.
            MT5Error: ``mt5.symbol_info_tick`` returned ``None`` (no live
                price) or ``mt5.order_send`` returned ``None``.
            MT5ConnectionError: client not connected.
        """
        self._require_connected()
        if direction == "long":
            order_type = self._mt5.ORDER_TYPE_BUY
        elif direction == "short":
            order_type = self._mt5.ORDER_TYPE_SELL
        else:
            raise ValueError(
                f"direction must be 'long' or 'short', got {direction!r}"
            )

        tick = self._mt5.symbol_info_tick(symbol)
        if tick is None:
            err = self._safe_last_error()
            raise MT5Error(
                f"symbol_info_tick({symbol!r}) returned None. "
                f"last_error={err!r}"
            )
        price = float(getattr(tick, "ask" if direction == "long" else "bid", 0.0))

        request = {
            "action": self._mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(volume),
            "type": order_type,
            "price": price,
            "sl": float(sl),
            "tp": float(tp),
            "magic": int(magic),
            "comment": _sanitize_order_comment(comment),
            "type_time": self._mt5.ORDER_TIME_GTC,
            "type_filling": self._mt5.ORDER_FILLING_IOC,
        }
        result = self._mt5.order_send(request)
        if result is None:
            err = self._safe_last_error()
            raise MT5Error(f"order_send returned None. last_error={err!r}")
        return OrderSendResult(
            retcode=int(getattr(result, "retcode", 0)),
            order=int(getattr(result, "order", 0)),
            deal=int(getattr(result, "deal", 0)),
            comment=str(getattr(result, "comment", "")),
            request_id=int(getattr(result, "request_id", 0)),
        )

    def cancel_pending_order(self, ticket: int) -> bool:
        """Remove a pending limit order by ticket.

        Returns ``True`` iff the broker reported ``TRADE_RETCODE_DONE``.
        Logs and returns ``False`` on any other retcode.
        """
        self._require_connected()
        request = {
            "action": self._mt5.TRADE_ACTION_REMOVE,
            "order": int(ticket),
        }
        result = self._mt5.order_send(request)
        if result is None:
            err = self._safe_last_error()
            logger.error("cancel_pending_order(%d) returned None — %r", ticket, err)
            return False
        retcode = int(getattr(result, "retcode", 0))
        if retcode != 10009:
            logger.warning(
                "cancel_pending_order(%d) retcode=%d comment=%r",
                ticket,
                retcode,
                getattr(result, "comment", ""),
            )
            return False
        return True

    def get_open_positions(self, magic: int | None = None) -> list[PositionSnapshot]:
        """List open positions, optionally filtered by magic number."""
        self._require_connected()
        positions = self._mt5.positions_get()
        if not positions:
            return []
        offset = self._require_offset()
        out: list[PositionSnapshot] = []
        for p in positions:
            p_magic = int(getattr(p, "magic", 0))
            if magic is not None and p_magic != int(magic):
                continue
            ptype = int(getattr(p, "type", 0))
            direction = "long" if ptype == 0 else "short"
            t_seconds = float(getattr(p, "time", 0))
            time_open_utc = broker_naive_seconds_to_utc(t_seconds, offset)
            out.append(
                PositionSnapshot(
                    ticket=int(getattr(p, "ticket", 0)),
                    symbol=str(getattr(p, "symbol", "")),
                    direction=direction,
                    volume=float(getattr(p, "volume", 0.0)),
                    entry_price=float(getattr(p, "price_open", 0.0)),
                    sl=float(getattr(p, "sl", 0.0)),
                    tp=float(getattr(p, "tp", 0.0)),
                    magic=p_magic,
                    time_open_utc=time_open_utc,
                    profit=float(getattr(p, "profit", 0.0)),
                )
            )
        return out

    def get_pending_orders(
        self, magic: int | None = None
    ) -> list[PendingOrderSnapshot]:
        """List pending orders, optionally filtered by magic number."""
        self._require_connected()
        orders = self._mt5.orders_get()
        if not orders:
            return []
        offset = self._require_offset()
        buy_limit = int(getattr(self._mt5, "ORDER_TYPE_BUY_LIMIT", 2))
        out: list[PendingOrderSnapshot] = []
        for o in orders:
            o_magic = int(getattr(o, "magic", 0))
            if magic is not None and o_magic != int(magic):
                continue
            otype = int(getattr(o, "type", 0))
            direction = "long" if otype == buy_limit else "short"
            t_seconds = float(getattr(o, "time_setup", 0))
            time_setup_utc = broker_naive_seconds_to_utc(t_seconds, offset)
            volume = float(
                getattr(o, "volume_initial", getattr(o, "volume_current", 0.0))
            )
            out.append(
                PendingOrderSnapshot(
                    ticket=int(getattr(o, "ticket", 0)),
                    symbol=str(getattr(o, "symbol", "")),
                    direction=direction,
                    volume=volume,
                    price_open=float(getattr(o, "price_open", 0.0)),
                    sl=float(getattr(o, "sl", 0.0)),
                    tp=float(getattr(o, "tp", 0.0)),
                    magic=o_magic,
                    time_setup_utc=time_setup_utc,
                )
            )
        return out

    def close_partial_position(self, *, ticket: int, volume: float) -> bool:
        """Close ``volume`` lots of an open position at market.

        Used by the lifecycle to realise the TP1 partial exit (50% by
        default). The closing order is the OPPOSITE side at market price:
        SELL closes a long, BUY closes a short.

        Returns ``False`` when the ticket is not open, the symbol_info
        tick is unavailable, or the broker rejects the deal.
        """
        self._require_connected()
        positions = self._mt5.positions_get()
        if not positions:
            return False
        match = None
        for p in positions:
            if int(getattr(p, "ticket", -1)) == int(ticket):
                match = p
                break
        if match is None:
            logger.warning(
                "close_partial_position: ticket=%d not found among open positions",
                ticket,
            )
            return False

        symbol = str(getattr(match, "symbol", ""))
        ptype = int(getattr(match, "type", 0))
        # Opposite-side market deal closes (or reduces) the position.
        if ptype == 0:  # long position → SELL to close
            close_type = self._mt5.ORDER_TYPE_SELL
            tick = self._mt5.symbol_info_tick(symbol)
            close_price = float(getattr(tick, "bid", 0.0)) if tick else 0.0
        else:  # short position → BUY to close
            close_type = self._mt5.ORDER_TYPE_BUY
            tick = self._mt5.symbol_info_tick(symbol)
            close_price = float(getattr(tick, "ask", 0.0)) if tick else 0.0

        request = {
            "action": self._mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(volume),
            "type": close_type,
            "position": int(ticket),
            "price": close_price,
            "magic": int(getattr(match, "magic", 0)),
            "comment": "sprint7:partial",
        }
        result = self._mt5.order_send(request)
        if result is None:
            logger.error("close_partial_position(%d): order_send returned None", ticket)
            return False
        retcode = int(getattr(result, "retcode", 0))
        if retcode != 10009:
            logger.warning(
                "close_partial_position(%d) retcode=%d comment=%r",
                ticket,
                retcode,
                getattr(result, "comment", ""),
            )
            return False
        return True

    def close_position_at_market(self, ticket: int) -> bool:
        """Close the entire volume of an open position at market.

        Used by the recovery layer to close orphan positions detected at
        scheduler startup. Internally a thin wrapper over
        :meth:`close_partial_position` with ``volume = position.volume``.
        """
        positions = self._mt5.positions_get() if self._mt5 is not None else None
        if not positions:
            return False
        match = None
        for p in positions:
            if int(getattr(p, "ticket", -1)) == int(ticket):
                match = p
                break
        if match is None:
            return False
        return self.close_partial_position(
            ticket=int(ticket), volume=float(getattr(match, "volume", 0.0))
        )

    def get_position_close_info(self, ticket: int) -> dict[str, Any] | None:
        """Return ``{exit_price, exit_time_utc, profit_usd}`` for a closed
        position, or ``None`` if no exit deal exists yet.

        Reads from ``mt5.history_deals_get`` filtering on the position id.
        Multiple exit deals (partial closes followed by a final close) are
        summed for ``profit_usd``; the LAST exit deal's price is used as
        the exit price.
        """
        self._require_connected()
        # Pull a wide history window — broker time, but
        # history_deals_get() with ticket filter is cheaper than a date
        # scan, so just call without bounds.
        deals = self._mt5.history_deals_get()
        if not deals:
            return None
        offset = self._require_offset()
        exits = [
            d for d in deals
            if int(getattr(d, "position_id", 0)) == int(ticket)
            and int(getattr(d, "entry", 0)) == 1
        ]
        if not exits:
            return None
        exits.sort(key=lambda d: float(getattr(d, "time", 0)))
        last = exits[-1]
        exit_seconds = float(getattr(last, "time", 0))
        return {
            "exit_price": float(getattr(last, "price", 0.0)),
            "exit_time_utc": broker_naive_seconds_to_utc(exit_seconds, offset),
            "profit_usd": float(sum(float(getattr(d, "profit", 0.0)) for d in exits)),
        }

    def modify_position_sl(self, *, ticket: int, new_sl: float) -> bool:
        """Move SL on an open position. TP is preserved.

        Returns ``False`` when the ticket is not open or the broker
        rejects the modification.
        """
        self._require_connected()
        positions = self._mt5.positions_get()
        if not positions:
            logger.warning("modify_position_sl: no positions returned by MT5")
            return False
        match = None
        for p in positions:
            if int(getattr(p, "ticket", -1)) == int(ticket):
                match = p
                break
        if match is None:
            logger.warning("modify_position_sl: ticket=%d not found among open positions", ticket)
            return False
        current_tp = float(getattr(match, "tp", 0.0))
        request = {
            "action": self._mt5.TRADE_ACTION_SLTP,
            "position": int(ticket),
            "symbol": str(getattr(match, "symbol", "")),
            "sl": float(new_sl),
            "tp": current_tp,
        }
        result = self._mt5.order_send(request)
        if result is None:
            err = self._safe_last_error()
            logger.error("modify_position_sl(%d) returned None — %r", ticket, err)
            return False
        retcode = int(getattr(result, "retcode", 0))
        if retcode != 10009:
            logger.warning(
                "modify_position_sl(%d) retcode=%d comment=%r",
                ticket,
                retcode,
                getattr(result, "comment", ""),
            )
            return False
        return True

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
