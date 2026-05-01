"""Diagnostic: inspect MT5 order types in the historical extract.

Reads ``raw_history.json`` (produced by ``extract_history.py``) and
prints, per order, the order type label, requested ``price_open``,
SL/TP, and the human-readable setup time. Then prints a per-type
count summary so the operator can see, at a glance, whether the 33
historical trades were placed as market orders, pending limits, or
stop orders.

Why this matters: ``extract_history.py`` reports "no requested entry
price" for every trade. Either the orders were all market orders
(so requested ≈ filled by definition), or the metadata was lost.
This script settles the question.

Usage:
    python -m calibration.broker_calibration.inspect_order_types
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

# MT5 ORDER_TYPE_* enum values (from the MetaTrader5 docs).
ORDER_TYPE_LABELS = {
    0: "BUY",            # market BUY
    1: "SELL",           # market SELL
    2: "BUY_LIMIT",
    3: "SELL_LIMIT",
    4: "BUY_STOP",
    5: "SELL_STOP",
    6: "BUY_STOP_LIMIT",
    7: "SELL_STOP_LIMIT",
    8: "CLOSE_BY",
}

# A market order is type 0 or 1 — anything else is a pending order
# whose ``price_open`` is the operator's requested price.
MARKET_TYPES = {0, 1}
LIMIT_TYPES = {2, 3}
STOP_TYPES = {4, 5, 6, 7}

RAW_HISTORY_PATH = Path(__file__).resolve().parent / "raw_history.json"


def main() -> int:
    if not RAW_HISTORY_PATH.exists():
        print(f"[FAIL] {RAW_HISTORY_PATH} not found. Run extract_history.py first.")
        return 1

    payload = json.loads(RAW_HISTORY_PATH.read_text(encoding="utf-8"))
    orders = payload.get("orders", [])
    print(f"Loaded {len(orders)} orders from {RAW_HISTORY_PATH.name}")
    print()

    # Sort chronologically for a clean readout.
    orders_sorted = sorted(orders, key=lambda o: o.get("time_setup", 0))

    print(f"{'#':>3}  {'ticket':>10}  {'symbol':<8}  {'type':<16}  "
          f"{'price_open':>12}  {'sl':>10}  {'tp':>10}  setup_utc")
    print("-" * 110)

    type_counter: Counter[str] = Counter()
    bucket_counter: Counter[str] = Counter()

    for i, o in enumerate(orders_sorted, start=1):
        otype = int(o.get("type", -1))
        label = ORDER_TYPE_LABELS.get(otype, f"UNKNOWN({otype})")
        type_counter[label] += 1
        if otype in MARKET_TYPES:
            bucket_counter["market"] += 1
        elif otype in LIMIT_TYPES:
            bucket_counter["limit"] += 1
        elif otype in STOP_TYPES:
            bucket_counter["stop"] += 1
        else:
            bucket_counter["other"] += 1

        ts = int(o.get("time_setup", 0))
        ts_iso = (
            datetime.fromtimestamp(ts, tz=UTC).isoformat(timespec="seconds")
            if ts else "n/a"
        )
        print(
            f"{i:>3}  {o.get('ticket', 0):>10}  {o.get('symbol', ''):<8}  "
            f"{label:<16}  {o.get('price_open', 0):>12.2f}  "
            f"{o.get('sl', 0):>10.2f}  {o.get('tp', 0):>10.2f}  {ts_iso}"
        )

    print()
    print("=== Type breakdown ===")
    for label, n in sorted(type_counter.items(), key=lambda kv: -kv[1]):
        print(f"  {label:<18}: {n}")

    print()
    print("=== Bucket summary ===")
    total = sum(bucket_counter.values()) or 1
    for bucket, n in sorted(bucket_counter.items(), key=lambda kv: -kv[1]):
        pct = 100.0 * n / total
        print(f"  {bucket:<8}: {n:>3}  ({pct:5.1f}%)")

    print()
    print(
        "Verdict: 'requested entry price' in extract_history.py is only "
        "meaningful for limit/stop orders. Market orders fill at the "
        "current bid/ask so requested == filled by definition."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
