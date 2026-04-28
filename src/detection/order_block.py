"""Order Block detection — fallback POI when no qualifying FVG exists.

Per docs/01 §5 Step 3 and docs/07 §1.1 (definition is exact once
"displacement" is defined; therefore pure logic).

Definition:

- Bullish setup (``MSS.direction == "bullish"``) ⇒ OB is the **most
  recent BEARISH candle** (close < open) BEFORE
  ``mss.displacement_candle_time_utc``.
- Bearish setup ⇒ most recent BULLISH candle before the displacement.

Proximal/distal convention (mirrors FVG):

- Bullish OB: ``proximal = candle.high`` (the price-entry side — a
  pullback from above retests this first), ``distal = candle.low``
  (SL-side).
- Bearish OB: ``proximal = candle.low``, ``distal = candle.high``.

Bounded lookback: at most 20 M5 candles before the displacement. Beyond
that we consider "no recent OB" and let the orchestrator skip the setup
rather than reach for an OB so old it isn't structurally meaningful.

Heuristic per docs/07 §1.3 — the FVG-vs-OB priority used by the setup
orchestrator. Alternatives: take both, prefer overlap, prefer the one
closer to OTE. Revisit on integration data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import pandas as pd

from .mss import MSS

_DEFAULT_LOOKBACK_CANDLES = 20


@dataclass(frozen=True)
class OrderBlock:
    """One detected Order Block. ``proximal``/``distal`` per the module docstring."""

    direction: Literal["bullish", "bearish"]
    proximal: float
    distal: float
    candle_time_utc: datetime


def detect_order_block(
    df_m5: pd.DataFrame,
    mss: MSS,
    *,
    lookback_candles: int = _DEFAULT_LOOKBACK_CANDLES,
) -> OrderBlock | None:
    """Find the last opposite-coloured candle before the displacement leg.

    Args:
        df_m5: M5 OHLC frame (UTC ``time``).
        mss: MSS event whose ``displacement_candle_time_utc`` anchors the
            backwards search.
        lookback_candles: search at most this many candles before the
            displacement. Default 20.

    Returns:
        ``OrderBlock`` or ``None`` if no qualifying candle in the window.
    """
    if lookback_candles < 1:
        raise ValueError(f"lookback_candles must be >= 1, got {lookback_candles}")
    if len(df_m5) == 0:
        return None

    times = pd.to_datetime(df_m5["time"], utc=True)
    times_py = [pd.Timestamp(t).to_pydatetime() for t in times]
    opens = df_m5["open"].to_numpy(dtype="float64")
    closes = df_m5["close"].to_numpy(dtype="float64")
    highs = df_m5["high"].to_numpy(dtype="float64")
    lows = df_m5["low"].to_numpy(dtype="float64")
    n = len(df_m5)

    # Locate the displacement candle's index. Equality on tz-aware
    # datetime is safe because everything is normalised to UTC upstream.
    disp_idx: int | None = None
    for i in range(n):
        if times_py[i] == mss.displacement_candle_time_utc:
            disp_idx = i
            break
    if disp_idx is None:
        return None

    if mss.direction == "bullish":

        def is_opposite(idx: int) -> bool:
            return closes[idx] < opens[idx]

    elif mss.direction == "bearish":

        def is_opposite(idx: int) -> bool:
            return closes[idx] > opens[idx]

    else:  # pragma: no cover
        raise ValueError(f"unexpected mss.direction: {mss.direction!r}")

    earliest = max(0, disp_idx - lookback_candles)
    for k in range(disp_idx - 1, earliest - 1, -1):
        if not is_opposite(k):
            continue
        if mss.direction == "bullish":
            proximal = float(highs[k])
            distal = float(lows[k])
        else:
            proximal = float(lows[k])
            distal = float(highs[k])
        return OrderBlock(
            direction=mss.direction,
            proximal=proximal,
            distal=distal,
            candle_time_utc=times_py[k],
        )
    return None
