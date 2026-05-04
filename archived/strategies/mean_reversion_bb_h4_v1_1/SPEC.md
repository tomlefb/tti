# Mean reversion Bollinger H4 — bidirectional

> **Strategy spec v1.1 — gate 1 of `STRATEGY_RESEARCH_PROTOCOL.md`.**
> Second HTF candidate after the breakout-retest H4 archive
> (commit `2b98cd1`, archived per protocol §11.2). Pre-specified
> before any code is written, before any backtest is run.
>
> Anchored to commits `98d82c2` (protocol post-archive, with the
> four §11.2 lessons folded into §6) and the cadence pre-measure
> report `calibration/runs/cadence_premesure_mr_bollinger_h4_2026-05-03T20-11-01Z.md`
> (8 raw triggers/month/instrument across all three instruments,
> stationary across train and holdout).
>
> Pre-specification is the point: every numerical hypothesis below
> exists so post-hoc rationalisation is impossible. If the holdout
> contradicts the spec, the spec is wrong — not the holdout.

---

## 0. Modification log

- **v1.0** (commit `91cb2a2`, 2026-05-03) — initial spec.
- **v1.1** (this revision, post gate 3 + attrition diagnostic) —
  the §2.4 exhaustion-candle filter is **removed** from the v1
  pipeline, the §3.2 grid is broadened on `min_penetration_atr_mult`,
  and the §4 H1 / H5 bands are recalibrated against the measured
  attrition. See §2.4 "Removal rationale" and §4 "v1.1 anchor"
  for the per-section deltas.

  **Why** — the gate-3 attrition diagnostic
  (`calibration/runs/attrition_diagnostic_mr_bb_h4_2026-05-03T22-32-57Z.md`)
  measured 7 / 187 ≈ **3.7 % retention** on the exhaustion gate over
  NDX100 train (5 y), making it the single steepest filter in the
  chain and reducing the final setup count to 1 over 60 months. The
  grid §3.2 admission floor `n_closed ≥ 50` was therefore
  unreachable on **every** of the 9 (3×3) cells, regardless of the
  edge — gate 4 would have produced a NON-INFORMATIVE archive.
  Removing the filter brings the baseline to 23 setups / 5 y /
  instrument (still under the original H1 band, but measurable),
  and the broadened §3.2 grid lets the calibration explore cells
  that can clear the protocol §5.2 admission floor.

  **Why this is not HARKing** — the modification is documented
  *before* gate 4 is run; H1 and §3.2 are revised based on the
  measured attrition geometry, not on outcome (mean R). The
  pre-spec / verdict-rule discipline holds: §4 hypotheses are
  revised explicitly here and frozen by this commit before any
  backtest.

---

## 1. Overview

**Concept.** Capture H4 reversions to the mean after a confirmed
Bollinger-Band excess. Bidirectional, with no D1 bias filter:
**long** when the prior bar closes below the lower band and the
current bar closes back inside the bands; **short** when the prior
bar closes above the upper band and the current bar closes back
inside. Entry on the return bar's close, stop just beyond the
excess bar's extreme, take-profit at the BB midline (SMA20).

**Market hypothesis.** In the "ranging-with-drift" regimes that
dominate XAUUSD, NDX100 and SPX500 on H4, statistical excesses (BB
band breaches) revert to the mean roughly 60–70 % of the time
across a 5-year window. The bidirectional setup is the simplest
expression of this hypothesis; v1 deliberately refuses confluence,
news, ADX or HTF-bias filters so the underlying mean-reversion
edge can be measured in isolation.

**Why this strategy as #2.** Three reasons:

1. **HTF by classification (§2 of the protocol)**: every trigger
   is a closed H4 decision. Pre-flight `4fd4304` already validated
   close-H4 signal transferability at 9.8–15.5 % mismatch.
2. **The first archive's failure mode does not transfer here.**
   Breakout-retest v1 hit win-rate ≈ 33 % at RR 2.0 — exact §5.2
   chop signature, the §11.2 lesson #3 we now read explicitly.
   Mean reversion targets the BB midline so RR is computed (not
   pinned at 2.0); the breakeven win-rate moves to 50 % for RR ≈
   1.0, well above the chop fingerprint that disqualified v1.
3. **The §11.2 lesson #1 has been paid this time.** H1 is
   pre-anchored on a measured trigger cadence, not on an intuitive
   guess. The cadence pre-measure (8 triggers/month, almost
   identical between train and holdout) gives an honest envelope
   for H1 before any code runs.

**Estimated cadence and edge** (a-priori, BEFORE any backtest —
see §4 for the full hypothesis table):

| Quantity | v1.0 range | **v1.1 range (post diagnostic)** |
|---|---|---|
| Setups / month / instrument | 3–5 | **0.5–2** |
| Mean R (closed, pre-cost) | +0.4 to +0.8 | +0.4 to +0.8 (unchanged) |
| Projected annual return @ 1 % risk | 20–35 % | **10–25 %** |

The v1.1 cadence floor follows the attrition diagnostic
(NDX100 train, 60 months): with the exhaustion filter removed and
``min_penetration_atr_mult`` swept across {0.0, 0.1, 0.2, 0.3},
the final setup count lands between 23 (pen 0.3) and 68 (pen 0.0)
— i.e. **0.4 to 1.1 / month / instrument**, with the broadened
§3.2 grid letting the train calibration choose the cell whose
trade-off between count and quality is admissible.

Concretely: a v1.1 that lands well below 0.5 / month / instrument
on holdout means the §2.3 ATR penetration filter is over-tuned;
above 2 / month means the calibration has weakened the filter
beyond statistical reliability. The widening from the v1.0 (3–5)
band reflects the structural cost of removing the §2.4 wick
filter — fewer "clean rejection" candles, but the surviving
excess pool is larger.

---

## 2. Pseudo-code

All loops below operate **only** on history available at the
cycle's `now` timestamp. No `df.loc[future_idx]`. No forward
iteration over the full dataframe. Audit (gate 3) verifies
streaming-vs-full-history bit-identical setup lists.

### 2.1 Bollinger Bands (H4)

```
compute_bollinger(close_h4: Series, period: int = 20,
                  mult: float = 2.0) -> BollingerBands:
    sma   = MA(close_h4, period)
    sigma = StdDev(close_h4, period)
    upper = sma + mult * sigma
    lower = sma - mult * sigma
    return BollingerBands(sma, upper, lower)
```

Bands are computed on **closed** H4 candles only. The SMA and
stddev at index `i` use bars `i - period + 1 .. i` inclusive.

### 2.2 Excess detection

An excess event fires on bar `i` when its close pierces a band
**and** the bar's **close timestamp** falls inside a configured
killzone.

```
detect_excess(ohlc_h4: list[Bar], bb: BollingerBands,
              london_kz: tuple[time, time],
              ny_kz: tuple[time, time]) -> ExcessEvent | None:
    bar = ohlc_h4[now_idx]
    bar_close_t = (bar.open_ts + 4h).time()
    in_london = london_kz[0] <= bar_close_t <= london_kz[1]
    in_ny     = ny_kz[0]     <= bar_close_t <= ny_kz[1]
    if not (in_london or in_ny):
        return None
    if bar.close > bb.upper[now_idx]:
        return ExcessEvent(idx=now_idx, direction="upper", bar=bar)
    if bar.close < bb.lower[now_idx]:
        return ExcessEvent(idx=now_idx, direction="lower", bar=bar)
    return None
```

The killzone windows are **`London = [08:00, 12:00] UTC`** and
**`NY = [13:00, 18:00] UTC`** — same definition as the archived
breakout-retest H4 strategy, so cross-strategy comparability holds.

Filter rule (Option A): a bar is in-killzone iff its **close
timestamp** is inside `[start, end]` (both ends inclusive). The
close timestamp is preferred over the open timestamp because the
detection decision is taken at the close.

Concretely, on the 4-hour grid anchored at UTC midnight, the
in-killzone bars per UTC day are:

| Bar open | Bar close | London `[08:00, 12:00]` | NY `[13:00, 18:00]` | In-killzone? |
|---|---|:---:|:---:|:---:|
| 04:00 | 08:00 | ✓ (close == start) | — | **YES (London)** |
| 08:00 | 12:00 | ✓ (close == end)   | — | **YES (London)** |
| 12:00 | 16:00 | —                  | ✓ (16 ∈ [13, 18]) | **YES (NY)** |
| 16:00 | 20:00 | —                  | — (20 > 18)       | NO  |
| 20:00 | 00:00 | —                  | —                 | NO  |
| 00:00 | 04:00 | —                  | —                 | NO  |

Net: **3 H4 bars per day are in-killzone** (close 08:00 + close
12:00 for London; close 16:00 for NY). The asymmetry between
London (2 in-bars) and NY (1 in-bar) is a structural artefact of
the H4 grid not aligning with the NY 13:00 start; documented and
accepted as such — re-defining NY as `[12:00, 18:00]` would shift
the asymmetry without removing it.

Edge cases (close timestamp falling on a killzone bound):
- close `08:00` ∈ `[08:00, 12:00]` → IN London (start inclusive).
- close `12:00` ∈ `[08:00, 12:00]` → IN London (end inclusive).
- close `13:00` ∈ `[13:00, 18:00]` → IN NY (not on the H4 grid,
  but defined for synthetic / non-H4 audit fixtures).
- close `18:00` ∈ `[13:00, 18:00]` → IN NY (same).

### 2.3 Filter — minimum penetration (ATR-scaled)

Excesses where `close` is barely above (resp. below) the upper
(resp. lower) band do not represent a meaningful statistical
breach. Filter them via an ATR-relative threshold.

```
passes_penetration(excess, ohlc_h4, bb,
                   atr_period: int = 14,
                   min_pen_atr_mult: float) -> bool:
    atr_i = ATR(ohlc_h4, atr_period)[excess.idx]
    if excess.direction == "upper":
        penetration = excess.bar.close - bb.upper[excess.idx]
    else:
        penetration = bb.lower[excess.idx] - excess.bar.close
    return penetration >= min_pen_atr_mult * atr_i
```

`min_pen_atr_mult` is a calibrated parameter (§3.2).

### 2.4 ~~Filter — exhaustion candle (rejection wick)~~ — REMOVED v1.1

> **Status: REMOVED in v1.1 — see "Removal rationale" below.** The
> ``is_exhaustion_candle`` function remains in
> ``src/strategies/mean_reversion_bb_h4/filters.py`` for reference and
> is still covered by ``tests/strategies/mean_reversion_bb_h4/test_filters.py``,
> but the v1.1 pipeline never calls it.

The original v1.0 filter checked that the excess bar exhibited a
"rejection" pattern — long wick on the breach side, short body —
on the assumption that a clean mean reversion is preceded by such
a candle.

```
is_exhaustion(excess) -> bool:
    bar = excess.bar
    body  = abs(bar.close - bar.open)
    rng   = bar.high - bar.low
    if rng == 0:
        return False                           # flat bar, no signal
    if excess.direction == "upper":
        upper_wick = bar.high - max(bar.close, bar.open)
        return upper_wick >= 0.4 * rng and body <= 0.5 * rng
    else:
        lower_wick = min(bar.close, bar.open) - bar.low
        return lower_wick >= 0.4 * rng and body <= 0.5 * rng
```

Both ratios (0.4 and 0.5) were **fixed ex ante** in v1.0 — meant
as discriminator constants, not free parameters.

#### Removal rationale (v1.1)

The gate-3 attrition diagnostic
(`calibration/runs/attrition_diagnostic_mr_bb_h4_2026-05-03T22-32-57Z.md`,
NDX100 train 60 months) measured the per-stage attrition with v1.0
parameters:

| Stage | N | Retention vs prev |
|---|---:|---:|
| Excess (close pierces BB) in killzone | 376 | 9.7 % vs killzone |
| Pen filter pass (mult 0.3) | 187 | 49.7 % |
| **Exhaustion filter pass** | **7** | **3.7 %** |
| Return found in window | 2 | 28.6 % |
| Final setup | 1 | — |

The exhaustion gate was the steepest single-step drop in the chain
(by a factor 13× over the next-worst gate). Disabling it raised the
final count from 1 → 23 setups over 60 months on the same fixture
(no other parameter changed).

Empirically, the H4 NDX excesses that *do* close back inside the
bands the next bar are typically directional candles, not rejection
candles — the spec assumed a v0/v1.0 wick-pattern hypothesis that
is not supported on this timeframe / instrument family. Keeping
the filter would have produced a calibration grid that fails
``n_closed ≥ 50`` on every cell (gate-4 admission floor §5.2 of the
protocol), yielding a non-informative ARCHIVE verdict. Removing the
filter does not loosen the *edge* hypothesis (mean R bands in §4
are unchanged) — it only widens the sample so the edge is
measurable.

The function and its tests are kept in the codebase as v2 / v3
candidate filters that future iterations may re-introduce with
calibrated thresholds, once gate 4 establishes whether the v1.1
mean-reversion premise produces a measurable edge.

### 2.5 Return detection (setup trigger)

Within `MAX_RETURN_BARS` H4 bars after the excess, the first
in-killzone bar that closes back **inside** both bands fires the
setup.

```
detect_return(ohlc_h4, excess, bb, max_bars: int) -> ReturnEvent | None:
    for j in range(excess.idx + 1, excess.idx + 1 + max_bars):
        if j > now_idx:           break
        if ohlc_h4[j].ts.time() not in killzones:
            continue
        bar = ohlc_h4[j]
        if bb.lower[j] < bar.close < bb.upper[j]:
            return ReturnEvent(idx=j, bar=bar)
    return None                   # no return inside the window
```

If the window expires without a return-inside close, the excess is
discarded — no setup.

### 2.6 Setup construction (entry / SL / TP)

The mean-reversion philosophy implies a **computed RR**, not a
fixed one. The TP is structurally pinned at the BB midline at the
return bar; the SL is structurally pinned at the excess extreme.
RR is therefore variable across setups and across instruments.

```
build_setup(excess, ret, bb, sl_buffer) -> Setup:
    if excess.direction == "upper":      # short
        entry = ret.bar.close
        sl    = excess.bar.high + sl_buffer
        tp    = bb.sma[ret.idx]
    else:                                # long
        entry = ret.bar.close
        sl    = excess.bar.low  - sl_buffer
        tp    = bb.sma[ret.idx]
    risk   = abs(entry - sl)
    reward = abs(tp    - entry)
    rr     = reward / risk
    return Setup(entry, sl, tp, rr, direction=...)
```

A computed RR is a feature, not a bug: it auto-modulates by
volatility and by where the excess fires relative to the SMA. RR
is expected to span 0.5–2.5; the §2.7 floor at 1.0 trims the
worst.

### 2.7 Hard invalidation

Applied **after** `build_setup`, before the setup is committed:

- `rr < MIN_RR` → skip (computed RR too tight to be worth the
  risk).
- `abs(entry - sl) > MAX_RISK_DISTANCE` → skip (instrument-specific
  cap on degenerate-stop trades; same convention as §3.4 of the
  archived spec).
- Per-day cap: ≥ 2 setups already produced today on this
  instrument → skip.

---

## 3. Parameters

### 3.1 Fixed (pre-specified, NOT calibrated)

These are anchored ex ante. Changing them post-hoc to chase a
result is data dredging and disqualifies the run.

| Parameter | Value | Justification |
|---|---|---|
| Trade timeframe | H4 close | Same HTF anchor as the archived strategy; matches the cadence pre-measure |
| `BB_PERIOD` | 20 | Standard, no DOF dredging |
| `BB_MULT` | 2.0 | Standard, captures ≈ 95 % of the in-distribution moves |
| `ATR_PERIOD` | 14 | Standard |
| Exhaustion wick ratio | ≥ 0.4 of range | Discriminator constant, §2.4 |
| Exhaustion body ratio | ≤ 0.5 of range | Discriminator constant, §2.4 |
| Risk per trade | 1 % | FundedNext standard, protocol §3 default |
| `MIN_RR` | 1.0 | Anti-low-RR floor |
| Max trades / day / instrument | 2 | Anti-overtrading |
| Direction mode (v1) | Bidirectional, no HTF bias | Operator decision pre-spec; §7 lists the v2 candidates |
| Killzones | London `[08:00, 12:00]` UTC; NY `[13:00, 18:00]` UTC; bar in-killzone iff its **close timestamp** ∈ window (Option A, both-ends inclusive — see §2.2 for the per-day in-bar table) | Liquidity-window filter; same definition as the archived breakout-retest H4 strategy for cross-spec comparability |

### 3.2 Calibrated (per-instrument, two-step procedure)

To avoid a 27-cell grid (3 × 3 × 3) with predictable overfit, use
a two-step calibration mirroring the archived spec:

**Step A — anchor structural params**: fix `MAX_RETURN_BARS = 3`
as the a-priori median. Rationale: a clean reversion that needs
more than 3 H4 bars (≈ 12 hours) to print a return-inside close is
no longer a reversion — it is a fade against a continuation.
Three bars is the upper bound observed in the cadence pre-measure
hot-month detail (consecutive-bar excesses tend to cluster within
1–3 bars of each other before either continuing or reverting).

**Step B — grid only the discriminator + cost params** (v1.1):

| Parameter | XAUUSD range | NDX100 range | SPX500 range |
|---|---|---|---|
| `MIN_PEN_ATR_MULT` | 0.0 / 0.1 / 0.2 / 0.3 | 0.0 / 0.1 / 0.2 / 0.3 | 0.0 / 0.1 / 0.2 / 0.3 |
| `SL_BUFFER` | 0.5 / 1.0 / 2.0 USD | 3 / 5 / 8 pts | 1 / 2 / 3 pts |

**12 cells per instrument** (4 × 3). v1.1 broadening rationale:
the attrition diagnostic (NDX100 train, exhaustion off) measured
68 / 23 / 9 / 3 final setups at `MIN_PEN_ATR_MULT` ∈ {0.0, 0.1,
0.2, 0.3} — i.e. the v1.0 lower edge (0.2) was already at 9 setups
over 60 months, well below the `n_closed ≥ 50` admission floor.
Adding 0.1 and 0.0 to the grid lets the calibration explore the
laxer end where the floor is reachable; the upper end (0.5) is
dropped because at 3 setups over 60 months it is structurally
admission-blocked. `MIN_PEN_ATR_MULT` does not vary per instrument
— it is a unit-free ATR multiplier and the same physical
discrimination applies across instruments. `SL_BUFFER` is
instrument-specific and shadows the archived spec's broker-spread
heuristic.

Selection criterion on the **train** set (§3.3): highest `mean_r`
whose 95 % CI lower bound is ≥ 0 AND `temporal_concentration < 0.4`
AND `n_closed ≥ 50`. Tie-break: highest `setups_per_month`. The
selected cell is then carried — unchanged — to the holdout.

**`MAX_RISK_DISTANCE`** is also instrument-specific but is fixed
ex ante at 3 × 30-day median range, computed at run-start, not
calibrated. (Anti-degenerate-trade guardrail, not a free
parameter.)

### 3.3 Train / holdout split

| Set | Window | Purpose |
|---|---|---|
| **Train** | 2020-01-01 → 2024-12-31 (5 y) | Param selection (Step B grid) |
| **Holdout** | 2025-01-01 → 2026-04-29 (~1.4 y) | Final §4 hypothesis check |

Same split as the archived spec — same Dukascopy fixture surface,
same cadence pre-measure window, same comparability for
cross-strategy review. Calibration only on train; **all admission
(§5 protocol) and Phase C metrics (§5.5 protocol) are read from
the holdout**. If train and holdout diverge sharply on `mean_r`,
that is an overfit signal — stop, do not promote. Quantitative
rule: `|mean_r_train − mean_r_holdout| > 0.3R` flags overfit (the
band is tighter than the archived spec's 0.5R because the cadence
is much more stationary across the two windows — see the cadence
pre-measure §1 table, train/holdout drift < 5 %).

### 3.4 Implementation note — runtime state surface

To be authored at gate 2. By analogy with the archived strategy,
the cycle-spanning state container `MeanReversionState` is
expected to carry:

- **`pending_excesses: dict[instrument, list[ExcessEvent]]`** —
  excesses observed but not yet returned-inside; the per-cycle
  pipeline iterates this list when a new H4 bar closes.
- **`trades_today: dict[(instrument, date_utc), int]`** —
  per-instrument-per-day counter, identical role to the archived
  spec.

No bias-freeze field is needed (no HTF bias in v1). If gate 2
discovers an additional load-bearing state field, this section is
amended in a follow-up commit and the change is recorded in git
history.

---

## 4. Pre-specified hypotheses (anti-data-dredging)

**Recorded BEFORE any backtest.** These define what counts as
success. The sheet is closed once this commit lands; reopening it
post-hoc to "loosen the criteria" disqualifies the run and forces
archive.

| # | Hypothesis | v1.0 target | **v1.1 target** | Source / rationale |
|---|---|---|---|---|
| H1 | Setups / month / instrument | 3–5 | **0.5–2** | Attrition diagnostic (NDX train, exhaustion off): 23 setups @ pen 0.3 → 68 @ pen 0.0, i.e. 0.4–1.1 / month. Lower bound 0.5, upper bound 2 to allow inter-instrument variance. |
| H2 | Win rate (closed) | 55–70 % | 55–70 % (unchanged) | Mean-reversion regime, computed RR averaging 1.0–1.5 |
| H3 | Mean R (pre-cost) | +0.4 to +0.8 | +0.4 to +0.8 (unchanged) | `WR × avg_RR − (1 − WR) × 1` ≈ +0.5 at WR 60 %, RR̄ 1.3 — independent of cadence |
| H4 | Mean R (post-cost, Phase C) | +0.3 to +0.7 | +0.3 to +0.7 (unchanged) | Subtract ~0.05 R for spread + commission |
| H5 | `projected_annual_return_pct` | 20–35 % | **10–25 %** | Derived from H1 (v1.1) × H4 across 3 instruments at 1 % risk: floor ≈ 0.5/mo × 0.3R × 12 × 1% × 3 = 5.4 %; ceiling ≈ 2/mo × 0.7R × 12 × 1% × 3 = 50 %. Tightened to 10–25 % to keep the band defensible. |
| H6 | `mean_r_ci_95.lower` (≥ 1 instrument, holdout) | > 0 | > 0 (unchanged) | Without it, no measurable edge (§5.2 protocol) |
| H7 | `outlier_robustness.trim_5_5.mean_r` (selected cells) | > 0 | > 0 (unchanged) | Edge must survive trimming top/bottom 5 % |
| H8 | `temporal_concentration` | < 0.4 | < 0.4 (unchanged) | Below the regime-fitting flag |
| H9 | `vs_buy_and_hold.strategy_minus_bh_pct` (≥ 1 instrument) | > 0 | > 0 (unchanged) | Strategy must beat passive on the same window |
| H10 | Transferability mismatch Duk vs MT5 (gate 7) | < 30 % | < 30 % (unchanged) | Same band as archived spec; mean-reversion triggers may drift more than swing breaks, watch carefully |

**v1.1 anchor** — H1 and H5 are recalibrated against the gate-3
attrition diagnostic; H2, H3, H4, H6–H10 are unchanged because
they describe the *edge geometry*, which is independent of how
many setups the filter chain emits. Removing the §2.4 exhaustion
filter widens the sample but does not change the per-trade
edge hypothesis. The verdict rule below is unchanged.

**Verdict rule on the HOLDOUT** (not train):

| Hypotheses satisfied | Decision |
|---|---|
| ≥ 6 / 10 | Edge probable → proceed to Phase C (gate 8) |
| 3 / 10 ≤ x ≤ 5 / 10 | Mixed signal → operator review before continuing |
| < 3 / 10 | No edge → mandatory archive (`archived/strategies/mean_reversion_bb_h4_v1/`) |

---

## 5. Anticipated pitfalls

### 5.1 Mean reversion against a strong trend

A strict bidirectional mean-reversion strategy will print **longs
in down-trends** and **shorts in up-trends** by construction. On
NDX in 2022-H2 → 2026 that asymmetry is potentially severe: the
long/short balance from the cadence pre-measure (NDX holdout
44 % long vs 56 % short) shows that triggers themselves are not
heavily skewed, but the *outcome* of the counter-trend leg is the
question.

**Mitigation v1**: none. The operator decided ex ante to omit any
ADX / D1-bias / active-trend gate. This is a measured choice, not
an oversight. **§11.2 lesson #2** of the protocol says trend
filters help separate continuation from chop in trend-following
specs; the converse is true here — for mean reversion, an
active-trend gate would *help* by suppressing the contra-trend
leg, but adding it now would also be a free parameter sneaking
into v1. Decision: ship without it, measure asymmetry, and treat
"mean R long ≪ 0 while mean R short ≈ 0" (or vice versa) as
explicit v2 evidence rather than a v1 rescue.

### 5.2 Choppy markets without a defined range

Mean reversion presumes a midline to revert to. In real breakouts
(price excess + continuation, no return-inside close inside the
window), the strategy never fires — the §2.5 window expiry
discards the excess. The risk is the opposite: a real breakout
that returns-inside *just* enough to trigger the setup, then
continues the original direction and stops the trade.

**Mitigation v1**: the §2.4 exhaustion candle filter is the
discriminator. If win rate < 50 %, the filter is insufficient and
v2 must add range-detection (e.g. ADX < 25 gate, or BB-width
contraction).

### 5.3 Regime fitting (§11.2 lesson confirmation)

If mean R looks positive on train but collapses on holdout, that
is regime-fit. The cadence pre-measure already shows the
*trigger* distribution is stationary across the two windows (drift
< 5 %); the *edge* distribution may not be. If
`|mean_r_train − mean_r_holdout| > 0.3R` on the selected cell, the
result is rejected per §3.3. The tighter 0.3R band (vs the
archived spec's 0.5R) reflects the higher stationarity of this
strategy's input distribution.

### 5.4 Win-rate aligning with RR breakeven (§11.2 lesson #3)

Mean-reversion at average RR 1.0 (excess fires close to the SMA)
implies a breakeven win rate of 50 %. At average RR 1.5
(excess fires far from the SMA), breakeven moves to 40 %.
**Diagnostic signature**: if the train grid produces win rates
landing within ±5 percentage points of the cell's RR-implied
breakeven, that is the §5.2-protocol chop signature (the same
fingerprint that sank breakout-retest v1) — archive precociously
rather than waiting for the holdout.

### 5.5 Sample size on holdout (v1.1)

Under the v1.1 H1 (0.5–2 setups/month/instrument):

- **Train** (60 months × 3 instruments = 180 instrument-months):
  expected `n_closed` per instrument 30–120. The §3.2 selection
  floor `n_closed ≥ 50` is reachable only on the upper half of
  this band — laxer cells (pen=0.0, pen=0.1) are needed to clear
  it. The v1.1 grid 4 × 3 (§3.2) is sized so at least one cell
  per instrument has a fighting chance.
- **Holdout** (16 months): expected `n_closed` per instrument
  8–32. **Below the n ≥ 50 admission floor in absolute terms.**
  Resolution: the n_closed floor applies to the train grid only
  (gate-4 selection); the holdout is read for hypothesis-pass
  count (§4 verdict rule), and the §4 bands use CI lower-bound
  checks rather than point-estimate checks — so finite-sample
  uncertainty is already baked into H6 / H7.

If the holdout lands at < 8 setups on every instrument, the
verdict shifts toward "no measurable signal under the v1.1
geometry"; that outcome is itself an admissible result of gate 4
(REVIEW or ARCHIVE per §4 verdict rule), not a reason to reopen
the spec.

### 5.6 Killzone boundary off-by-one

The H4 grid is anchored at UTC midnight (closes at 04 / 08 / 12 /
16 / 20 / 00). With killzone filtering by **close timestamp** in
`[start, end]` both-ends-inclusive (Option A, §2.2), the in-killzone
bars per day are: close 08:00 (London), close 12:00 (London),
close 16:00 (NY) — three bars total. The 16:00 / 20:00 / 00:00 /
04:00 closes are out (close 04:00 falls on the boundary of nothing,
close 20:00 > 18:00 NY end).

Audit (gate 3) must verify that the streaming detector and the
full-history reference produce the same in-killzone subset on
edge days (month-boundary, DST transition). DST is the bigger
hazard: the broker / data feed timestamps are UTC by convention
in this project (CLAUDE.md rule 6), so DST should not shift the
killzone — but if a fixture leaks broker-local time into the
``time`` column, the close-timestamp comparison silently misclassifies.
The audit harness pins this by re-running on the same fixture with
two timezone hypotheses and asserting bit-identical setup lists.

---

## 6. Validation plan — mapping to protocol gates

| Gate | Action | Pass criterion |
|---|---|---|
| **3** Audit look-ahead | New `calibration/audit_mean_reversion_bb_h4.py`; reuse the streaming-vs-full-history skeleton from the archived `audit_breakout_retest_h4.py` (one-line swap per §11.2 closing note) | 100 % bit-identical setup lists |
| **4** Backtest Duk | Tick simulator on train → param selection (Step B grid, 9 cells × 3 instruments) → re-run on holdout per instrument with selected params; emit `BacktestResult` per (instrument, set) cell | All 10 hypotheses (§4) measured on the holdout |
| **5** Cross-check DBN | Same Step-B-selected params on Databento, same holdout window | Mean R within ±30 % of Duk (per §5.3 protocol) |
| **6** Sanity MT5 | Same params on MT5 (~1.4 y depth — overlaps the holdout) | Same direction sign as Duk; no violent contradiction |
| **7** Transferability (this strategy's triggers) | Re-run pre-flight logic on the trigger timestamps of this strategy, Duk vs MT5 | Mismatch < 30 % per H10 |
| **8** Phase C realistic costs | FundedNext spread + commission per instrument; recompute holdout `BacktestResult` | `mean_r_post_costs > 0`, CI lower > −0.05, `projected_annual_return_pct ≥ 20`, `vs_buy_and_hold > 0` |
| **9** Decision | Operator review of the §4 verdict + Phase C results | Promote to Sprint 7 demo or archive |

---

## 7. Out of scope (v1)

Explicit list of what we will NOT build into v1, to keep scope
honest and prevent rescue-by-feature when results disappoint:

- **No HTF / D1 bias filter.** Bidirectional pure. (v2 candidate
  if §5.1 asymmetry materialises.)
- **No ADX / range-regime gate.** (v2 candidate if §5.2 win rate
  < 50 %.)
- **No FVG / SR / round-number confluence.** (v2 if v1 marginal.)
- **No news filter.** (v2 if a calendar feed is wired.)
- **No partial profit-taking, no trailing SL.** TP at the BB
  midline, single exit.
- **No pyramiding.** One entry per setup.
- **No dynamic position sizing.** Flat 1 % risk.
- **No per-instrument tuning of `MAX_RETURN_BARS` or BB period
  / multiplier.** The structural params are project-wide.

If v1 admits, every one of these is a candidate for v2 and gets
its own pre-spec. None is to be added during v1 to "save" a
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
that, mandatory move to `archived/strategies/mean_reversion_bb_h4_v1/`
with the post-mortem README per §8 of the protocol.

---

*Spec frozen at this commit. Any change to fixed parameters
(§3.1), calibration grid (§3.2), train/holdout split (§3.3), or
pre-spec hypotheses (§4) requires either a new strategy (v2) or
an explicit operator-approved revision recorded in commit history.
Quietly adjusting a number to chase a result disqualifies the run.*
