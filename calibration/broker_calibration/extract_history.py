"""Extract historical trades from FundedNext MT5 and produce a calibration report.

Run on the Windows host where the MT5 terminal is logged in:

    python -m calibration.broker_calibration.extract_history

Outputs (written to ``calibration/broker_calibration/``):

- ``raw_history.json``               — full deal/order/trade dump (gitignored)
- ``historical_trades_analysis.md``  — human-readable aggregated report

The pure parsing helpers (``deals_to_dicts``, ``orders_to_dicts``,
``reconstitute_trades``, ``aggregate_by_symbol``, ``format_report``)
are exported for unit tests in ``tests/calibration/broker_calibration/``.
They take/return plain dicts so tests do not need a live MT5 terminal.
"""

from __future__ import annotations

import json
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# --- MT5 deal entry codes (from MetaTrader5 documentation) -------------
# DEAL_ENTRY_IN = 0     (open / increase position)
# DEAL_ENTRY_OUT = 1    (close / decrease position)
# DEAL_ENTRY_INOUT = 2  (reverse — closes one side and opens the other)
# DEAL_ENTRY_OUT_BY = 3 (close-by, opposite ticket)
DEAL_ENTRY_IN = 0
DEAL_ENTRY_OUT = 1

# DEAL_TYPE_BUY = 0, DEAL_TYPE_SELL = 1 — the only two we care about
# at the entry stage. Higher codes are balance/credit operations and we
# filter them out by checking position_id != 0.
DEAL_TYPE_BUY = 0
DEAL_TYPE_SELL = 1

# Watched portfolio (matches CLAUDE.md rule 9). NDX100 may be exposed
# under broker-specific aliases (US100, USTEC, NAS100…) — we look at
# every symbol that shows up in the history.
WATCHED_SYMBOLS = ("XAUUSD", "NDX100", "US100", "USTEC", "NAS100")

OUTPUT_DIR = Path(__file__).resolve().parent
RAW_HISTORY_PATH = OUTPUT_DIR / "raw_history.json"
REPORT_PATH = OUTPUT_DIR / "historical_trades_analysis.md"


# ----------------------------------------------------------------------
# Pure parsing helpers (testable without MT5)
# ----------------------------------------------------------------------


def deals_to_dicts(deals: Any) -> list[dict[str, Any]]:
    """Convert an iterable of MT5 ``TradeDeal`` namedtuples to plain dicts.

    Skips balance/credit operations (``position_id == 0``).
    """
    out: list[dict[str, Any]] = []
    for d in deals or []:
        pos_id = int(_get(d, "position_id", 0))
        if pos_id == 0:
            continue
        out.append(
            {
                "ticket": int(_get(d, "ticket", 0)),
                "position_id": pos_id,
                "order": int(_get(d, "order", 0)),
                "time": int(_get(d, "time", 0)),
                "time_msc": int(_get(d, "time_msc", 0)),
                "symbol": str(_get(d, "symbol", "")),
                "type": int(_get(d, "type", 0)),
                "entry": int(_get(d, "entry", 0)),
                "volume": float(_get(d, "volume", 0.0)),
                "price": float(_get(d, "price", 0.0)),
                "commission": float(_get(d, "commission", 0.0)),
                "swap": float(_get(d, "swap", 0.0)),
                "profit": float(_get(d, "profit", 0.0)),
                "magic": int(_get(d, "magic", 0)),
                "comment": str(_get(d, "comment", "")),
                "reason": int(_get(d, "reason", 0)),
            }
        )
    return out


def orders_to_dicts(orders: Any) -> list[dict[str, Any]]:
    """Convert an iterable of MT5 ``TradeOrder`` namedtuples to plain dicts."""
    out: list[dict[str, Any]] = []
    for o in orders or []:
        out.append(
            {
                "ticket": int(_get(o, "ticket", 0)),
                "position_id": int(_get(o, "position_id", 0)),
                "time_setup": int(_get(o, "time_setup", 0)),
                "time_done": int(_get(o, "time_done", 0)),
                "symbol": str(_get(o, "symbol", "")),
                "type": int(_get(o, "type", 0)),
                "state": int(_get(o, "state", 0)),
                "volume_initial": float(_get(o, "volume_initial", 0.0)),
                "volume_current": float(_get(o, "volume_current", 0.0)),
                "price_open": float(_get(o, "price_open", 0.0)),
                "price_current": float(_get(o, "price_current", 0.0)),
                "sl": float(_get(o, "sl", 0.0)),
                "tp": float(_get(o, "tp", 0.0)),
                "magic": int(_get(o, "magic", 0)),
                "comment": str(_get(o, "comment", "")),
                "reason": int(_get(o, "reason", 0)),
            }
        )
    return out


@dataclass
class Trade:
    """Reconstituted trade — one entry deal + 1..N exit deals (partials)."""

    position_id: int
    symbol: str
    direction: str  # "long" or "short"
    open_time_unix: int
    close_time_unix: int | None
    entry_price: float
    exit_price_avg: float | None  # volume-weighted across partial closes
    volume: float
    sl_requested: float | None
    tp_requested: float | None
    requested_entry_price: float | None  # from the originating order (limit only)
    commission_total: float
    swap_total: float
    profit_net: float
    n_exit_deals: int
    duration_seconds: int | None
    is_closed: bool
    raw_deal_tickets: list[int] = field(default_factory=list)


def reconstitute_trades(
    deals: list[dict[str, Any]], orders: list[dict[str, Any]]
) -> list[Trade]:
    """Pair entry/exit deals into ``Trade`` records.

    - Groups deals by ``position_id``.
    - The first ``entry == DEAL_ENTRY_IN`` deal sets the open side.
    - All ``entry == DEAL_ENTRY_OUT`` deals are summed (volume-weighted
      exit price, sum of commission/swap/profit).
    - Originating-order metadata (sl/tp/requested entry) is taken from
      the order whose ``position_id`` matches and whose ``time_done``
      is closest to the entry deal time.
    """
    by_pos: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for d in deals:
        by_pos[d["position_id"]].append(d)

    orders_by_pos: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for o in orders:
        if o["position_id"] != 0:
            orders_by_pos[o["position_id"]].append(o)

    trades: list[Trade] = []
    for pos_id, ds in by_pos.items():
        ds_sorted = sorted(ds, key=lambda d: (d["time_msc"] or d["time"]))

        entries = [d for d in ds_sorted if d["entry"] == DEAL_ENTRY_IN]
        exits = [d for d in ds_sorted if d["entry"] == DEAL_ENTRY_OUT]
        if not entries:
            # Should not happen for a real position — skip defensively.
            continue
        entry = entries[0]

        direction = "long" if entry["type"] == DEAL_TYPE_BUY else "short"
        symbol = entry["symbol"]
        entry_price = entry["price"]
        # Volume of the position is the sum of all entry-deal volumes
        # (covers cases where the position was scaled into).
        volume = sum(d["volume"] for d in entries)

        # Aggregate exits: volume-weighted exit price, sum of money fields.
        exit_price_avg: float | None = None
        close_time_unix: int | None = None
        if exits:
            total_vol = sum(d["volume"] for d in exits) or 1.0
            exit_price_avg = (
                sum(d["price"] * d["volume"] for d in exits) / total_vol
            )
            close_time_unix = max(d["time"] for d in exits)

        commission_total = sum(d["commission"] for d in ds_sorted)
        swap_total = sum(d["swap"] for d in ds_sorted)
        profit_net = sum(d["profit"] for d in ds_sorted) + commission_total + swap_total

        # Find the originating order for SL/TP and requested price.
        sl_req: float | None = None
        tp_req: float | None = None
        req_entry: float | None = None
        candidate_orders = orders_by_pos.get(pos_id, [])
        if candidate_orders:
            best = min(
                candidate_orders,
                key=lambda o: abs((o["time_done"] or o["time_setup"]) - entry["time"]),
            )
            sl_req = best["sl"] or None
            tp_req = best["tp"] or None
            req_entry = best["price_open"] or None

        duration_seconds: int | None = None
        if close_time_unix is not None:
            duration_seconds = max(0, close_time_unix - entry["time"])

        trades.append(
            Trade(
                position_id=pos_id,
                symbol=symbol,
                direction=direction,
                open_time_unix=entry["time"],
                close_time_unix=close_time_unix,
                entry_price=entry_price,
                exit_price_avg=exit_price_avg,
                volume=volume,
                sl_requested=sl_req,
                tp_requested=tp_req,
                requested_entry_price=req_entry,
                commission_total=commission_total,
                swap_total=swap_total,
                profit_net=profit_net,
                n_exit_deals=len(exits),
                duration_seconds=duration_seconds,
                is_closed=bool(exits),
                raw_deal_tickets=[d["ticket"] for d in ds_sorted],
            )
        )

    trades.sort(key=lambda t: t.open_time_unix)
    return trades


def aggregate_by_symbol(trades: list[Trade]) -> dict[str, dict[str, Any]]:
    """Per-symbol summary: counts, commission per lot, slippage estimates."""
    by_sym: dict[str, list[Trade]] = defaultdict(list)
    for t in trades:
        by_sym[t.symbol].append(t)

    out: dict[str, dict[str, Any]] = {}
    for sym, ts in by_sym.items():
        closed = [t for t in ts if t.is_closed]
        n = len(ts)
        n_closed = len(closed)

        total_volume = sum(t.volume for t in ts) or 0.0
        total_commission = sum(t.commission_total for t in ts)
        total_swap = sum(t.swap_total for t in ts)
        total_profit_net = sum(t.profit_net for t in closed)

        # Slippage on entry: |fill - requested| when the order had a
        # requested limit price. Limits should fill at-or-better, so any
        # adverse value is worth flagging.
        entry_slippages: list[float] = []
        for t in ts:
            if t.requested_entry_price is None or t.requested_entry_price == 0.0:
                continue
            raw = t.entry_price - t.requested_entry_price
            # For a long limit, adverse = filled HIGHER than requested.
            # For a short limit, adverse = filled LOWER than requested.
            adverse = raw if t.direction == "long" else -raw
            entry_slippages.append(adverse)

        commission_per_lot = (
            abs(total_commission) / total_volume if total_volume > 0 else 0.0
        )

        out[sym] = {
            "n_trades": n,
            "n_closed": n_closed,
            "total_volume_lots": round(total_volume, 4),
            "total_commission": round(total_commission, 2),
            "total_swap": round(total_swap, 2),
            "total_profit_net": round(total_profit_net, 2),
            "commission_per_lot_usd": round(commission_per_lot, 4),
            "avg_profit_per_trade": (
                round(total_profit_net / n_closed, 2) if n_closed else 0.0
            ),
            "entry_slippage_count": len(entry_slippages),
            "entry_slippage_mean": (
                round(sum(entry_slippages) / len(entry_slippages), 6)
                if entry_slippages
                else None
            ),
            "entry_slippage_max_adverse": (
                round(max(entry_slippages), 6) if entry_slippages else None
            ),
            "directions": {
                "long": sum(1 for t in ts if t.direction == "long"),
                "short": sum(1 for t in ts if t.direction == "short"),
            },
        }
    return out


def format_report(
    aggregated: dict[str, dict[str, Any]],
    symbol_specs: dict[str, dict[str, Any]],
    *,
    window_from: datetime,
    window_to: datetime,
    n_total_trades: int,
    account_currency: str,
) -> str:
    """Render the human-readable markdown report."""
    lines: list[str] = []
    lines.append("# FundedNext historical trades — broker calibration")
    lines.append("")
    lines.append(
        f"Window: **{window_from.isoformat(timespec='seconds')}** → "
        f"**{window_to.isoformat(timespec='seconds')}**"
    )
    lines.append(f"Account currency: **{account_currency}**")
    lines.append(f"Total reconstituted trades: **{n_total_trades}**")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Per-symbol summary")
    lines.append("")

    if not aggregated:
        lines.append("_No trades found in the requested window._")
    else:
        # Sort: large samples first, then alphabetical.
        ordered = sorted(
            aggregated.items(), key=lambda kv: (-kv[1]["n_trades"], kv[0])
        )

        full = [(s, a) for s, a in ordered if a["n_trades"] >= 3]
        thin = [(s, a) for s, a in ordered if a["n_trades"] < 3]

        for sym, agg in full:
            lines.append(f"### {sym} (n={agg['n_trades']})")
            lines.append("")
            lines.append(f"- Closed trades: {agg['n_closed']}")
            lines.append(
                f"- Direction split: long={agg['directions']['long']}, "
                f"short={agg['directions']['short']}"
            )
            lines.append(f"- Total volume traded: {agg['total_volume_lots']} lots")
            lines.append(
                f"- Commission per lot: **${agg['commission_per_lot_usd']:.2f}** "
                f"(total ${abs(agg['total_commission']):.2f})"
            )
            lines.append(f"- Total swap: ${agg['total_swap']:.2f}")
            lines.append(f"- Net P&L (closed only): ${agg['total_profit_net']:.2f}")
            lines.append(
                f"- Avg P&L per closed trade: ${agg['avg_profit_per_trade']:.2f}"
            )
            if agg["entry_slippage_count"]:
                lines.append(
                    f"- Entry slippage (adverse, signed): mean="
                    f"{agg['entry_slippage_mean']}, "
                    f"max_adverse={agg['entry_slippage_max_adverse']} "
                    f"(over {agg['entry_slippage_count']} orders with a "
                    f"requested entry price)"
                )
            else:
                lines.append(
                    "- Entry slippage: no historical orders with a requested "
                    "price — likely all market orders or order metadata gone."
                )
            if sym in symbol_specs:
                lines.append(
                    f"- Specs (snapshot at extract time): see _Symbol specs_ "
                    f"section below."
                )
            lines.append("")

        if thin:
            lines.append("### Symbols with < 3 trades (insufficient data)")
            lines.append("")
            for sym, agg in thin:
                lines.append(
                    f"- **{sym}**: n={agg['n_trades']}, "
                    f"volume={agg['total_volume_lots']} lots, "
                    f"commission=${abs(agg['total_commission']):.2f}, "
                    f"P&L=${agg['total_profit_net']:.2f}"
                )
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## Symbol specs (snapshot)")
    lines.append("")
    if not symbol_specs:
        lines.append(
            "_No specs collected. Run with the watched symbols visible in MT5._"
        )
    else:
        for sym, spec in sorted(symbol_specs.items()):
            lines.append(f"### {sym}")
            lines.append("")
            for k, v in spec.items():
                lines.append(f"- `{k}`: {v}")
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "_Generated by `calibration/broker_calibration/extract_history.py`. "
        "Raw deal/order data is in the gitignored `raw_history.json`._"
    )
    return "\n".join(lines) + "\n"


def _get(obj: Any, attr: str, default: Any) -> Any:
    """Read ``attr`` from a namedtuple-like or dict, with a default."""
    if isinstance(obj, dict):
        return obj.get(attr, default)
    return getattr(obj, attr, default)


# ----------------------------------------------------------------------
# Symbol specs (live MT5 lookup — wrapper kept thin for testability)
# ----------------------------------------------------------------------


def symbol_info_to_spec(info: Any, current_spread_points: int | None) -> dict[str, Any]:
    """Render an ``mt5.symbol_info`` namedtuple as a flat spec dict."""
    return {
        "name": str(_get(info, "name", "")),
        "trade_contract_size": float(_get(info, "trade_contract_size", 0.0)),
        "point": float(_get(info, "point", 0.0)),
        "digits": int(_get(info, "digits", 0)),
        "tick_size": float(_get(info, "trade_tick_size", _get(info, "tick_size", 0.0))),
        "tick_value": float(
            _get(info, "trade_tick_value", _get(info, "tick_value", 0.0))
        ),
        "volume_min": float(_get(info, "volume_min", 0.0)),
        "volume_step": float(_get(info, "volume_step", 0.0)),
        "volume_max": float(_get(info, "volume_max", 0.0)),
        "trade_mode": int(_get(info, "trade_mode", -1)),
        "spread_current_points": current_spread_points,
        "spread_current_native": (
            (current_spread_points or 0) * float(_get(info, "point", 0.0))
        ),
        "swap_long": float(_get(info, "swap_long", 0.0)),
        "swap_short": float(_get(info, "swap_short", 0.0)),
        "swap_mode": int(_get(info, "swap_mode", 0)),
        "currency_base": str(_get(info, "currency_base", "")),
        "currency_profit": str(_get(info, "currency_profit", "")),
        "currency_margin": str(_get(info, "currency_margin", "")),
    }


# ----------------------------------------------------------------------
# Live MT5 orchestration (only runs when invoked as a script)
# ----------------------------------------------------------------------


def _connect_mt5():  # pragma: no cover — needs live terminal
    try:
        import MetaTrader5 as mt5  # type: ignore[import-not-found]
    except ImportError as exc:
        raise SystemExit(
            "[FAIL] MetaTrader5 package is not installed. "
            "Run on the Windows host."
        ) from exc

    from config.secrets import MT5_LOGIN, MT5_PASSWORD, MT5_SERVER

    ok = mt5.initialize(
        login=int(MT5_LOGIN),
        password=str(MT5_PASSWORD),
        server=str(MT5_SERVER),
    )
    if not ok:
        raise SystemExit(f"[FAIL] mt5.initialize() failed. last_error={mt5.last_error()!r}")
    return mt5


def _collect_symbol_specs(mt5: Any, symbols: tuple[str, ...]) -> dict[str, dict[str, Any]]:  # pragma: no cover
    specs: dict[str, dict[str, Any]] = {}
    for sym in symbols:
        info = mt5.symbol_info(sym)
        if info is None:
            logger.info("symbol_info(%s) returned None — skipping", sym)
            continue
        spread_pts = int(_get(info, "spread", 0)) or None
        specs[sym] = symbol_info_to_spec(info, spread_pts)
    return specs


def main() -> int:  # pragma: no cover — entrypoint, exercised manually
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    mt5 = _connect_mt5()
    try:
        account = mt5.account_info()
        if account is None:
            raise SystemExit(f"[FAIL] account_info() returned None. {mt5.last_error()!r}")
        currency = str(account.currency)

        until = datetime.now(tz=UTC)
        since = until - timedelta(days=183)  # 6 months
        logger.info(
            "Fetching history %s → %s",
            since.isoformat(timespec="seconds"),
            until.isoformat(timespec="seconds"),
        )

        # NB: history_deals_get / history_orders_get accept naive datetimes
        # interpreted in broker time. A 1-day buffer either side covers the
        # typical UTC↔broker offset (±3h max).
        from_dt = since - timedelta(days=1)
        to_dt = until + timedelta(days=1)

        raw_deals = mt5.history_deals_get(from_dt, to_dt)
        raw_orders = mt5.history_orders_get(from_dt, to_dt)
        if raw_deals is None and raw_orders is None:
            logger.warning("history_deals_get/orders_get both returned None — %r", mt5.last_error())

        deals = deals_to_dicts(raw_deals)
        orders = orders_to_dicts(raw_orders)
        logger.info("Got %d deals (post-filter), %d orders", len(deals), len(orders))

        trades = reconstitute_trades(deals, orders)
        logger.info("Reconstituted %d trades", len(trades))

        symbol_specs = _collect_symbol_specs(mt5, WATCHED_SYMBOLS)

        # Persist raw dump (gitignored).
        raw_payload = {
            "extracted_at_utc": datetime.now(tz=UTC).isoformat(),
            "window_from_utc": since.isoformat(),
            "window_to_utc": until.isoformat(),
            "account_currency": currency,
            "deals": deals,
            "orders": orders,
            "trades": [t.__dict__ for t in trades],
            "symbol_specs": symbol_specs,
        }
        RAW_HISTORY_PATH.write_text(json.dumps(raw_payload, indent=2, default=str))
        logger.info("Wrote raw dump: %s", RAW_HISTORY_PATH)

        aggregated = aggregate_by_symbol(trades)
        report = format_report(
            aggregated,
            symbol_specs,
            window_from=since,
            window_to=until,
            n_total_trades=len(trades),
            account_currency=currency,
        )
        REPORT_PATH.write_text(report)
        logger.info("Wrote report: %s", REPORT_PATH)

        # Console summary
        print()
        print(f"=== Extraction summary ===")
        print(f"Trades reconstituted: {len(trades)}")
        for sym, agg in aggregated.items():
            print(
                f"  {sym:>10}: n={agg['n_trades']:<3} "
                f"vol={agg['total_volume_lots']:<6} "
                f"commission/lot=${agg['commission_per_lot_usd']:.2f}"
            )
        print(f"Report: {REPORT_PATH}")
        print(f"Raw  : {RAW_HISTORY_PATH} (gitignored)")
        return 0
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    sys.exit(main())
