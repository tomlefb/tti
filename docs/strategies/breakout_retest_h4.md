# Breakout retest swing H4 — trend-following

> **Strategy spec — gate 1 of `STRATEGY_RESEARCH_PROTOCOL.md`.**
> First HTF candidate after the TJR pivot. Pre-specified before any
> code is written, before any backtest is run. Anchored to commits
> `01bc21a` (protocol), `4fd4304` (HTF transferability pre-flight),
> `06349f2` (BacktestResult §9 extensions).
>
> Pre-specification is the point: every numerical hypothesis below
> exists so post-hoc rationalisation is impossible. If the holdout
> contradicts the spec, the spec is wrong — not the holdout.

---

## 1. Overview

**Concept.** Long-bias only when the daily MA(50) is below close;
short-bias only when above; no trade in the rare exact-equality case.
On H4: detect the most recent confirmed swing high (long bias) or
swing low (short bias). Wait for a close-H4 break of that swing.
After the break, wait up to N bars for a retest of the broken level.
On a clean retest (touch + close on the right side), enter at the
retest's close, stop just beyond the retest extreme, take profit at
fixed RR.

**Why this strategy first.** Three reasons:

1. **HTF by classification (§2 of the protocol)**: every trigger is
   a closed H4 / D1 decision. The pre-flight (`4fd4304`) measured
   Duk-vs-MT5 signal mismatch on a trivial single-bar MA cross at
   9.8–15.5 % — "Bonne" verdict. Multi-bar pattern matching on H4
   may amplify or dampen this; gate 7 of the pipeline measures it
   for *this* strategy specifically.
2. **Operationally minimal**: no SL trailing, no pyramiding, no
   partial profit-taking, no dynamic sizing. The fewer moving parts,
   the harder to overfit and the easier to audit.
3. **Pre-flight already validates the load-bearing premise** that
   close-H4 signal series transfer. Ergo it is the lowest-risk first
   real strategy under the protocol.

**Estimated cadence and edge** (a-priori, BEFORE any backtest — see
§4 for the full hypothesis table):

| Quantity | A-priori range |
|---|---|
| Setups / month / instrument | 1–3 |
| Mean R (closed, pre-cost) | +0.6 to +1.2 |
| Projected annual return @ 1 % risk | 20–40 % |

---

## 2. Pseudo-code

All loops below operate **only** on history available at the cycle's
`now` timestamp. No `df.loc[future_idx]`. No forward iteration over
the full dataframe. Audit (gate 3) verifies streaming-vs-full-history
bit-identical setup lists.

### 2.1 Bias filter (D1)

```
bias_d1(close_d1: Series) -> Literal["bullish", "bearish", "neutral"]:
    ma50 = MA(close_d1, period=50)
    last_close = close_d1[-1]      # last CLOSED D1 candle
    last_ma    = ma50[-1]
    if last_close > last_ma:  return "bullish"
    if last_close < last_ma:  return "bearish"
    return "neutral"               # exact equality, skip cycle
```

### 2.2 Swing detection (H4)

Fractal-style: pivot is a bar whose high (resp. low) is strictly
greater (resp. less) than the highs (resp. lows) of the N_SWING bars
on each side. Confirmation requires N_SWING bars to the *right* of
the pivot, so a pivot at index i is only confirmed at index
i + N_SWING.

```
detect_swings_h4(ohlc_h4: list[Bar], lookback: int = N_SWING)
        -> tuple[list[Swing], list[Swing]]:
    swings_high, swings_low = [], []
    for i in range(lookback, len(ohlc_h4) - lookback):
        bar = ohlc_h4[i]
        left  = ohlc_h4[i - lookback : i]
        right = ohlc_h4[i + 1       : i + 1 + lookback]
        if bar.high > max(b.high for b in left) and \
           bar.high > max(b.high for b in right):
            swings_high.append(Swing(idx=i, price=bar.high, ts=bar.ts))
        if bar.low  < min(b.low  for b in left) and \
           bar.low  < min(b.low  for b in right):
            swings_low.append(Swing(idx=i, price=bar.low,  ts=bar.ts))
    return swings_high, swings_low
```

### 2.3 Breakout detection

The most recent **confirmed** swing in the bias direction.
"Confirmed" means N_SWING bars have closed since the pivot. After
the swing, scan H4 closes forward for the first close that crosses
the swing price. Once a swing has produced a breakout (whether
followed by a setup or not), it is **locked** — no further breakout
event can be raised on the same swing for the rest of the run. This
prevents the false-breakout pitfall (§5.1).

```
detect_breakout(ohlc_h4, swings, bias, locked_swings) -> BreakoutEvent | None:
    if bias == "bullish":
        candidates = [s for s in swings.high
                      if s not in locked_swings and s.idx + N_SWING < now_idx]
        if not candidates: return None
        last = max(candidates, key=lambda s: s.idx)   # most recent
        for j in range(last.idx + N_SWING + 1, now_idx + 1):
            if ohlc_h4[j].close > last.price:
                locked_swings.add(last)
                return BreakoutEvent(direction="long", break_idx=j,
                                     level=last.price, swing=last)
    elif bias == "bearish":
        # symmetric on swings.low and ohlc_h4[j].close < last.price
        ...
    return None
```

### 2.4 Retest detection

Within `N_RETEST` H4 bars *after* the breakout bar, the price must
return to touch (or marginally pierce) the broken level, and that
same H4 bar must close back on the breakout side. The touch is
checked on the wick (`low` for long / `high` for short) plus a
`RETEST_TOLERANCE`. The hold check is on `close`. Both conditions
on the same H4 bar.

```
detect_retest(ohlc_h4, breakout: BreakoutEvent, max_bars: int = N_RETEST)
        -> RetestEvent | None:
    for j in range(breakout.break_idx + 1,
                   breakout.break_idx + 1 + max_bars):
        if j >= now_idx: break
        bar = ohlc_h4[j]
        if breakout.direction == "long":
            touched = bar.low  <= breakout.level + RETEST_TOLERANCE
            held    = bar.close >  breakout.level
        else:
            touched = bar.high >= breakout.level - RETEST_TOLERANCE
            held    = bar.close <  breakout.level
        if touched and held:
            return RetestEvent(retest_idx=j, bar=bar)
    return None
```

### 2.5 Setup construction (entry / SL / TP)

```
build_setup(breakout, retest) -> Setup:
    bar = retest.bar
    if breakout.direction == "long":
        entry = bar.close
        sl    = bar.low  - SL_BUFFER
        tp    = entry + (entry - sl) * RR_TARGET
    else:
        entry = bar.close
        sl    = bar.high + SL_BUFFER
        tp    = entry - (sl - entry) * RR_TARGET
    return Setup(entry, sl, tp, breakout.direction, ...)
```

### 2.6 Hard invalidation

Applied **after** `build_setup`, before the setup is committed:

- `abs(entry - sl) > MAX_RISK_DISTANCE` → skip (instrument-specific
  cap so a degenerate retest with a deep wick does not produce a
  giant-stop trade).
- News window: scheduled high-impact USD news ±30 min around `entry`
  → skip. (Source TBD at gate 2; if no clean source, ship without
  this filter and document.)
- Per-day cap: ≥ 2 setups already produced today on this instrument
  → skip.

---

## 3. Parameters

### 3.1 Fixed (pre-specified, NOT calibrated)

These are anchored ex ante. Changing them post-hoc to chase a result
is data dredging and disqualifies the run.

| Parameter | Value | Justification |
|---|---|---|
| Bias timeframe | D1 | Stable HTF, low noise |
| Bias method | MA50 close D1 | Deterministic, single param |
| Trade timeframe | H4 close | Sweet spot for setups/month vs noise |
| `RR_TARGET` | 2.0 | Moderate, achievable, leaves headroom for trailing in v2 |
| Risk per trade | 1 % | FundedNext standard, protocol §3 default |
| Max trades / day / instrument | 2 | Anti-overtrading |
| Direction mode (v1) | Trend-following only | No counter-trend in v1; revisit only if v1 shows a half-edge |
| Bias resolution | Last closed D1 candle | No intra-bar D1 bias; re-evaluated at each H4 close |

### 3.2 Calibrated (per-instrument, two-step procedure)

To avoid a 108-cell grid (3 × 4 × 3 × 3) with predictable overfit,
use a two-step calibration:

**Step A — anchor structural params**: fix `N_SWING = 5` and
`N_RETEST = 8` as a-priori medians. Rationale: 5-bar fractals are
the SMC standard; 8 H4 bars ≈ 1.5 trading days, the empirical upper
bound for clean retests before the move turns into a reversal.

**Step B — grid only the instrument-specific cost params**:

| Parameter | XAUUSD range | NDX100 range | SPX500 range | Justification |
|---|---|---|---|---|
| `RETEST_TOLERANCE` | 0.5 / 1.0 / 2.0 USD | 3 / 5 / 8 pts | 1 / 2 / 3 pts | Typical broker spread × multiplier |
| `SL_BUFFER` | 0.3 / 0.5 / 1.0 USD | 2 / 3 / 5 pts | 0.5 / 1 / 2 pts | One spread above broker reference |

**9 cells per instrument** (3 × 3). All other params fixed. Selection
criterion on the **train** set (§3.3): highest `mean_r` whose 95 %
CI lower bound is ≥ 0 AND `temporal_concentration < 0.4`. Tie-break:
highest `setups_per_month`. The selected cell is then carried —
unchanged — to the holdout.

**`MAX_RISK_DISTANCE`** is also instrument-specific but is fixed
ex ante at 3 × 30-day median range, computed at run-start, not
calibrated. (Documented anti-degenerate-trade guardrail, not a
free parameter.)

### 3.3 Train / holdout split

| Set | Window | Purpose |
|---|---|---|
| **Train** | 2020-01-01 → 2024-12-31 (5 y) | Param selection (Step B grid) |
| **Holdout** | 2025-01-01 → 2026-04-29 (~1.4 y) | Final §4 hypothesis check |

Calibration only on train. **All admission (§5 protocol) and Phase C
metrics (§5.5 protocol) are read from the holdout.** If the train
and holdout diverge sharply on `mean_r`, that is an overfit signal
— stop, do not promote. Quantitative rule: `|mean_r_train −
mean_r_holdout| > 0.5R` flags overfit; investigate before admission.

---

## 4. Pre-specified hypotheses (anti-data-dredging)

**Recorded BEFORE any backtest.** These define what counts as
success. The sheet is closed once this commit lands; reopening it
post-hoc to "loosen the criteria" disqualifies the run and forces
archive.

| # | Hypothesis | Target | Source / rationale |
|---|---|---|---|
| H1 | Setups / month / instrument | 1–3 | A-priori from H4 swing frequency × bias filter |
| H2 | Win rate (closed) | 40–55 % | Standard for trend-following at RR 2.0 |
| H3 | Mean R (pre-cost) | +0.4 to +1.2 | `WR × RR − (1 − WR) × 1` ≈ 0.4 at WR 47 %, RR 2.0 |
| H4 | Mean R (post-cost, Phase C) | +0.3 to +1.0 | Subtract ~0.05 R for spread + commission |
| H5 | `projected_annual_return_pct` (H4 × Setups/mo × 12) | 15–40 % | Derived from H1 + H4 |
| H6 | `mean_r_ci_95.lower` | > 0 | Without it, no measurable edge (§5.2 protocol) |
| H7 | `outlier_robustness.trim_5_5.mean_r` | > 0 | Without it, edge is thin-tailed-fragile |
| H8 | `temporal_concentration` | < 0.4 | Below the 0.5 regime-fitting flag |
| H9 | `vs_buy_and_hold.strategy_minus_bh_pct` | > 0 | Must beat passive on the same window |
| H10 | Transferability mismatch Duk vs MT5 (gate 7) | < 30 % | Coherent with single-bar pre-flight at 15 %; multi-bar may drift |

**Verdict rule on the HOLDOUT** (not train):

| Hypotheses satisfied | Decision |
|---|---|
| ≥ 6 / 10 | Edge probable → proceed to Phase C (gate 8) |
| 3 / 10 ≤ x ≤ 5 / 10 | Mixed signal → operator review before continuing |
| < 3 / 10 | No edge → mandatory archive (`archived/strategies/breakout_retest_h4/`) |

---

## 5. Anticipated pitfalls

### 5.1 False breakouts

A swing broken on a single H4 bar that immediately reverses is the
default failure mode in chop. The retest gate already absorbs part
of this (a fake breakout rarely produces a clean retest with a hold
close), but a swing can fire a second breakout days later as price
revisits the area. **Mitigation**: lock a swing once it has produced
any breakout event (§2.3). One swing → at most one setup attempt.

### 5.2 Choppy markets

Trend-following loses in ranges. The D1 bias filter helps but is not
a regime detector — D1 can be technically bullish for weeks while
H4 chops in a sideways channel. Result: edge concentrated in trends,
losses in ranges. Acceptable iff trends > ranges across the 5 y
train window. Watched via H8 (temporal_concentration) — a strategy
that needs one strong trending semester to be positive will fail H8.

### 5.3 `N_RETEST` selection

Too short (3 bars): we miss the slower, cleaner retests that often
produce the best risk/reward.
Too long (12+ bars): "retests" that are actually reversals — price
broke out, failed, came all the way back. Anchored at 8 in §3.2;
revisit only if v1 misses the band on H1 (too few setups) and gate 4
clearly identifies retest-window starvation as the cause.

### 5.4 HTF confluence (PDH/PDL, round numbers)

A breakout that fires straight into a daily PDH or a round number
has structurally less follow-through. **Out of scope for v1** (§7).
Adding a confluence filter in v1 would be both feature creep and
a vector for post-hoc fitting. If v1 produces a marginal edge, this
is the v2 first-look extension.

### 5.5 Regime fitting (NDX bullish window 2022–2026)

The pre-flight window happened to be very long-NDX-friendly. Under
the bias filter, every NDX setup over 2022-H1 → 2026-H1 will likely
be long. If we observe `mean_r_NDX = +1.2` and `mean_r_XAU/SPX ≈ 0`
on holdout, this is the regime fit, not a strategy edge. Mitigation:
H8 (`temporal_concentration < 0.4`) and the per-instrument
admission per §5.2 of the protocol. A strategy that is positive
across all three instruments OR positive on two with neutral on the
third is admissible; positive on one alone is not.

### 5.6 D1 bias re-eval cadence

The bias is re-evaluated at each H4 cycle from the *last closed* D1
candle. On the H4 that closes around 21:00 UTC on a Sunday/weekday
boundary, the bias may flip mid-setup-construction (breakout fires
under bullish, retest waits, D1 closes flipping bias to bearish).
**Decision**: the bias evaluated at the breakout bar is locked into
the `BreakoutEvent` and used for the entire setup lifecycle; bias
flips after that do not invalidate an in-flight setup. This is also
audited (gate 3).

---

## 6. Validation plan — mapping to protocol gates

| Gate | Action | Pass criterion |
|---|---|---|
| **3** Audit look-ahead | `calibration/audit_breakout_retest_h4.py` runs the detector in (a) full-history mode and (b) cycle-by-cycle streaming mode on the same Duk fixture; diff the produced setup lists | 100 % bit-identical |
| **4** Backtest Duk | Tick simulator on train → param selection (Step B grid) → re-run on holdout per instrument with selected params; emit `BacktestResult` per (instrument, set) cell | All 10 hypotheses (§4) measured on the holdout |
| **5** Cross-check DBN | Same Step-B-selected params on Databento, same holdout window | Mean R within ±30 % of Duk (per §5.3 protocol) |
| **6** Sanity MT5 | Same params on MT5 (~1.4 y depth — overlaps the holdout) | Same direction sign as Duk; no violent contradiction |
| **7** Transferability (this strategy's triggers) | Re-run the pre-flight script's logic on the *trigger timestamps of this strategy*, Duk vs MT5 | Mismatch < 30 % per H10 |
| **8** Phase C realistic costs | Add FundedNext spread + commission per instrument; recompute holdout `BacktestResult` | `mean_r_post_costs > 0`, CI lower > −0.05, `projected_annual_return_pct ≥ 20`, `vs_buy_and_hold > 0` |
| **9** Decision | Operator review of the §4 verdict + Phase C results | Promote to Sprint 7 demo or archive |

---

## 7. Out of scope (v1)

Explicit list of what we will NOT build into v1, to keep scope
honest and prevent rescue-by-feature when results disappoint:

- **No pyramiding** — one entry per setup.
- **No SL trailing** — fixed SL at the retest extreme + buffer.
- **No partial profit-taking** — TP is a single 2 R target.
- **No counter-trend setups** — trend-following only.
- **No multi-instrument confluence** — one instrument at a time.
- **No news filter beyond high-impact scheduled USD** — best-effort
  via §2.6, not a blocker if no source available at gate 2.
- **No VIX / regime detector** — D1 bias is the only macro filter.
- **No dynamic position sizing** — flat 1 % risk.
- **No HTF confluence (PDH / PDL / round numbers)** — see §5.4.

If v1 admits, every one of these is a candidate for v2 and gets its
own pre-spec. None of them is to be added during v1 to "save" a
marginal result.

---

## 8. Budget — per protocol §8

| Phase | Target |
|---|---|
| Specification (this doc) | 2–4 h |
| Implementation + unit tests | 1–2 d |
| Audit (gate 3) | 0.5–1 d |
| Backtests Duk + DBN + MT5 (gates 4–6) | 1–2 d |
| Transferability (gate 7) | 0.5 d |
| Phase C (gate 8) | 0.5 d |
| Decision (gate 9) | 2 h |
| **Total target** | **5–8 d** |

**Hard stop-loss: 12 days from this commit to admission.** Beyond
that, mandatory move to `archived/strategies/breakout_retest_h4/`
with the post-mortem README per §8 of the protocol.

---

*Spec frozen at this commit. Any change to fixed parameters (§3.1),
calibration grid (§3.2), train/holdout split (§3.3), or pre-spec
hypotheses (§4) requires either a new strategy (v2) or an explicit
operator-approved revision recorded in commit history. Quietly
adjusting a number to chase a result disqualifies the run.*
