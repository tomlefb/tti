"""Setup builder — spec §2.6.

Mean-reversion has computed RR (TP at SMA), not pinned RR. The
``setup.risk_reward`` reported here may legitimately span 0.5–2.5;
filtering on ``min_rr`` is the invalidation layer's job, not this
function's.

Direction mapping:

- excess.direction == ``"upper"`` → SHORT setup (price reverts down).
- excess.direction == ``"lower"`` → LONG setup (price reverts up).
"""

from __future__ import annotations

from .types import ReturnEvent, Setup


def build_setup(
    return_event: ReturnEvent,
    *,
    instrument: str,
    sl_buffer: float,
) -> Setup:
    """Build a Setup from a confirmed return event (spec §2.6).

    Args:
        return_event: the return produced by ``detect_return``.
        instrument: instrument label, e.g. ``"XAUUSD"``.
        sl_buffer: instrument-priced buffer placed beyond the excess
            extreme (spec §3.2 calibrated grid).

    Returns:
        ``Setup`` with computed RR.

    Raises:
        ValueError: if the resulting risk is non-positive (degenerate
            setup where the return-bar close coincides with the
            excess extreme + buffer). Surfaced explicitly so the
            audit trail catches the bug instead of silently emitting
            a NaN RR.
    """
    excess = return_event.excess_event
    entry = return_event.return_bar_close
    tp = return_event.sma_at_return

    if excess.direction == "upper":
        direction = "short"
        sl = excess.high + sl_buffer
        risk = sl - entry
        reward = entry - tp
    else:
        direction = "long"
        sl = excess.low - sl_buffer
        risk = entry - sl
        reward = tp - entry

    if risk <= 0:
        raise ValueError(
            f"build_setup: non-positive risk ({risk}) — degenerate "
            f"return. entry={entry} sl={sl} direction={direction}"
        )

    rr = reward / risk

    return Setup(
        timestamp_utc=return_event.return_bar_timestamp,
        instrument=instrument,
        direction=direction,  # type: ignore[arg-type]
        entry_price=float(entry),
        stop_loss=float(sl),
        take_profit=float(tp),
        risk_reward=float(rr),
        excess_event=excess,
        return_event=return_event,
    )
