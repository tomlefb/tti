"""Risk-parity position sizing — spec §2.5.

Each entry contributes the same dollar risk to the portfolio,
independent of the asset's absolute volatility:

    position_size = (capital × risk_fraction) / atr_at_entry

For a multi-asset basket spanning indices, FX, metals, oil and
crypto, this is **necessary** — without it, the same nominal lot
size on BTC and on USDJPY would produce wildly different dollar
risks. Risk parity equalises the risk contribution per asset and
matches the academic standard for cross-sectional momentum
implementations (Asness 2013).

Returns:
    ``float`` position size in instrument units, or ``None`` when
    the asset cannot be sized (ATR ≤ 0, ATR NaN). The caller
    (pipeline) is expected to skip the entry on ``None``.
"""

from __future__ import annotations

import math


def sizing_for_entry(
    *,
    capital: float,
    risk_fraction: float,
    atr_at_entry: float,
) -> float | None:
    """Risk-parity sizing — spec §2.5.

    Args:
        capital: account capital in dollars (FundedNext typical:
            100,000).
        risk_fraction: fraction of capital to risk per position
            (spec default 0.01 = 1 %).
        atr_at_entry: ATR(20) value at the entry date in the
            asset's price units. Must be > 0; NaN / 0 / negative
            handled explicitly.

    Returns:
        Position size in instrument units (e.g. lots, contracts,
        coins). ``None`` when sizing is impossible.

    Raises:
        ValueError: on negative ATR (upstream programming error).
    """
    if math.isnan(atr_at_entry):
        return None
    if atr_at_entry == 0.0:
        return None
    if atr_at_entry < 0:
        raise ValueError(f"atr_at_entry must be > 0, got {atr_at_entry}")
    risk_dollars = capital * risk_fraction
    return risk_dollars / atr_at_entry
