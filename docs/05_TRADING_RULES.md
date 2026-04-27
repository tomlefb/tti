# 05 — Trading Rules & Risk Management

These rules encode the operator's account constraints and risk discipline.
The system enforces them as **hard stops**: when a limit is reached, the
system stops sending notifications for the rest of the day (or session).

---

## Account context

- **Broker / prop firm**: Funded Next
- **Plan**: Stellar Lite 2-Step Challenge — Phase 1
- **Account size**: $5,000
- **Account type**: Swap
- **Addon**: EA enabled (used only to allow Python scripts to read MT5,
  NOT to auto-trade — see rule below)

### Phase 1 objectives (as observed on dashboard)

| Metric | Limit |
|---|---|
| Profit target | $400 (8% of $5K) |
| Daily loss limit | $200 (4% of $5K) |
| Max loss limit | $400 (8% of $5K, trailing or static — verify on dashboard) |
| Minimum trading days | 5 (already met) |

> **Verify these values directly from the FundedNext dashboard before going
> live.** Plan rules can change between cohorts.

---

## Per-trade risk

- **Risk per trade**: 1% of account balance.
  At $5K account → $50 per trade.
- **Position sizing**: computed from SL distance and risk amount.
  The system **displays** the recommended lot size in the notification,
  but the human still types it manually into MT5.

---

## Hard stops (enforced by the system)

The scheduler checks these before each detection cycle. If any is true,
the system **suppresses notifications** until the next reset.

1. **Daily loss reached**: today's realized loss ≥ 80% of daily limit.
   - At $200 daily limit → stop at $160 lost. Buffer prevents the next
     trade's SL from busting the limit.
   - Resets at 23:00 Paris (broker rollover; verify per broker).

2. **Max loss critical**: cumulative loss ≥ 80% of max loss limit.
   - The system sends a **critical Telegram alert** and stops permanently
     until the operator manually resets via a config flag.

3. **Daily trade count**: 2 trades already taken today.
   - Prevents over-trading and revenge trading after losses.

4. **Consecutive SL count**: 2 SL hit today across all pairs.
   - Forces a cool-off. Resets at next day's killzone.

5. **Per-pair count**: 2 setups already taken on the same pair today.

---

## News blackout (initial implementation)

V1: a manual on/off flag in `settings.py` per day.

- Operator sets `NEWS_BLACKOUT_TODAY = True` in the morning if NFP / FOMC /
  CPI / ECB / BoE is scheduled.
- When `True`, no notifications fire ±30 minutes around configured news times.

V2 (later): integrate a news calendar API (e.g., ForexFactory CSV scrape).
Out of scope for v1.

---

## What the system NEVER does

1. **Place orders.** No code path calls `mt5.order_send()`,
   `mt5.order_modify()`, or `mt5.order_close()`. The operator is always
   the one clicking the button in the MT5 terminal.
2. **Move SL or TP.** Once the operator places the trade, the system has
   no authority over it. If the operator wants to move SL to BE, they do
   it manually.
3. **Recommend deviating from the plan.** No "this looks like a good trade
   but doesn't meet criteria — take it anyway." Either the criteria are met
   or no notification fires.

---

## What the operator commits to

These commitments are part of the system's value proposition. They are
enforced by self-discipline, not by code, but they are part of why the
system can work.

1. **No charts outside notifications.** The operator does not open
   TradingView or MT5 charts to "look around" between notifications.
2. **Set SL and TP at entry.** Both are placed when the order is opened,
   never after.
3. **No SL/TP modification after entry.** No moving to BE manually,
   no taking partials early, no extending TP. Set and forget.
4. **Close the platform after entering.** Once a trade is live, the
   operator closes MT5 and does not reopen until the trade is done
   (TP, SL, or end-of-killzone).
5. **Honor system stops.** When the system stops sending notifications
   because a limit was hit, the operator does not look for trades manually
   to compensate.

---

## Failure modes & responses

- **System down, missed notification**: the operator does not retroactively
  hunt for the missed setup. Move on.
- **Notification arrives but operator can't take it (busy, away)**: skip
  it. The next valid setup will come.
- **Notification arrives but operator disagrees**: skip it. Log the
  reason in the journal (Sprint 5 will add a note field). Use disagreements
  to refine the strategy or system over time.
- **System detects setup, operator takes it, it loses**: it's expected.
  RR 3:1 means a 33% win rate is breakeven. Don't second-guess after one
  trade. Review weekly, not per-trade.

---

## Funded Next-specific gotchas

- **Trading hours**: some plans restrict trading to certain hours. Verify
  on the dashboard. The system's killzones (London, NY) should fall within
  any restrictions, but confirm.
- **Weekend holding**: Stellar Lite typically allows weekend holding, but
  swap account means swap fees. Strategy is intraday so this is moot, but
  no positions should ever be open at session end.
- **EAs and bots**: the addon allows EA usage, but Funded Next has rules
  against "toxic" behaviors (latency arb, tick scalping, hedging across
  accounts, copy trading from external signal sources). The detector +
  manual execution model is well clear of these.
- **Consistency rule** (some plans): a single day's profit cannot exceed
  X% of total profit. With our 1% risk and 3:1 RR, hitting +3% in one day
  is a 6-trade winning streak — possible but not the typical day. Check
  the rule for Stellar Lite.

The operator must read the **full FundedNext rulebook** for the active
plan before treating this system's output as actionable.
