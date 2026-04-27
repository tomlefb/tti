# 00 — Project Context

## Why this project exists

The operator is a SMC/ICT trader using a strategy popularized by TJR Trades.
In manual trading, the operator's strategy execution is poor (consistently
breakeven over months) — not because the strategy lacks edge, but because
emotional execution problems dominate:

- **FOMO entries**: jumping in before confirmation, getting wicked out
- **Premature exits**: closing winners early when a counter-bar appears
- **Over-monitoring**: spending hours on charts looking for setups that aren't there
- **Skipped daily bias step**: trading sweeps in both directions blindly

The hypothesis behind this project: **a strict mechanical filter that only
notifies the operator when objective setup criteria are met will remove the
opportunity for emotional sabotage** by removing the operator from the chart
between notifications.

The operator has explicitly committed to:

- Not opening charts outside of incoming notifications
- Setting SL/TP at entry and not modifying them
- Closing the platform after each trade

## What this project is — and is not

**Is**: a Python-based detector that watches markets, identifies TJR-style
setups using a mix of pure logic, calibrated rules, and heuristics, and
pushes a notification to Telegram with all info needed for the operator to
decide quickly (chart screenshot, key levels, RR, score).

**Is not**:

- An auto-trader. Trades are placed manually by the human.
- A backtesting framework. (Backtests will be done ad hoc, but the system
  itself is forward-looking only.)
- A signal-selling product. Strictly personal use.
- A replacement for trader skill. The system can only filter; it cannot
  improve the operator's strategy or judgment.

## Honest framing

The operator has **not yet established a measured edge** with this strategy
manually. This system will not create an edge that does not exist; at best
it will surface the strategy's true performance more cleanly by removing
emotional noise.

Therefore the project is dual-purpose:

1. **Practical**: build a tool that helps the operator trade better.
2. **Educational**: build a real-world hybrid AI/deterministic system as a
   learning exercise in agentic architecture, structured detection,
   calibrated rules, human-in-the-loop validation, and post-hoc analysis.

If the strategy turns out to have no edge even with perfect execution, the
system's value becomes its diagnostic: it will reveal that fact through
clean data, faster than years of breakeven manual trading.

## Operator profile

- Developer (front-end alternant), comfortable reading Python, will write
  most code via Claude Code.
- Trades a Funded Next Stellar Lite 2-Step Challenge P1 5K account.
- Has been trading the TJR strategy manually for months with breakeven results.
- Trades from a Mac (dev machine); MT5 runs on a separate Windows desktop.

## Success criteria for the project itself

- **Sprint 0–5 done**: a working end-to-end system that detects setups,
  sends notifications, logs decisions, and tracks outcomes.
- **Calibration phase**: each calibrated detector validated against
  manually-marked reference charts; the full system runs paper-trade for
  2–3 weeks; metrics collected on detection precision/recall vs operator's
  manual judgment.
- **Validation**: only after the above does the operator use the system for
  real trades on the Funded Next account.
- **Stretch (Sprint 7)**: optional LLM qualifier layer with measured calibration.
