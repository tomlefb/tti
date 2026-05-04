# Cross-sectional momentum — multi-asset rotation D1 (v1.1)

> **Strategy spec — gate 1 of `STRATEGY_RESEARCH_PROTOCOL.md`,
> v1.1 cadence-oriented re-spec.**
>
> v1.0 spec (this directory's `trend_rotation_d1.md`) reached
> ARCHIVE verdict on the §3.6 cadence floor — see protocol
> §11.4 final-verdict preamble. v1.1 is a cadence-oriented
> parameter expansion documented BEFORE any re-run, per the
> §11.3 lesson #3 modification pattern.
>
> Anchored to:
>
> - Protocol commit `4f905b3` — §3.6 operator viability
>   constraint and §11.5 cadence-as-primary-filter formalised
>   alongside the v1 final ARCHIVE.
> - v1 spec `docs/strategies/trend_rotation_d1.md` (commit
>   `889f18c`) — fixed parameters, universe, pseudo-code, and
>   audit harness reused unchanged.
> - v1 holdout outcome — 1.31 trades/mo on the selected cell
>   (mom = 126, K = 3, rebal = 10), 3× below §3.6 floor.
>
> Pre-specification is the point: the §4 hypothesis bands below
> are calibrated **ex ante on principle** (cadence-edge dilution
> + CSM academic literature), NOT on v1 holdout numbers. If the
> v1.1 holdout contradicts the spec, the spec is wrong — not the
> holdout.

---

## 0. Why v1.1

The v1.0 spec produced a measurable cross-sectional momentum
signal on its train grid (mom = 126 cells: mean R 1.0–1.6, win
rate 51–57 %, beat equal-weight basket on 4/4 cells). Under the
§3.5 class-adapted floors the strategy passed gate 4 selection
on cells (126/3/10) and (126/4/10). The holdout on the
operator-default cell (126/3/10) produced 5/9 hypotheses PASS
with +84 % projected annual return on n = 21.

**Why v1 is archived anyway**: the holdout cadence was 1.31
trades/mo portfolio. §3.6 (added at protocol commit `4f905b3`)
sets the operator viability floor at 4 trades/mo distributed
across 3+ weeks. 1.31/mo is 3× below floor — the strategy cannot
be deployed on a 5K Phase 1 challenge with 4 % daily / 8 % total
drawdown limits, regardless of mean R magnitude on a 21-trade
sample.

**v1.1 hypothesis to test**: the cross-sectional momentum edge
exists but was obscured by a cadence too low to satisfy operator
deployment constraints. v1.1 trades off **per-trade R magnitude**
against **statistical power and operability** by expanding the
grid towards higher-frequency cells (rebalance 3–7 d, K up to 5).

**Trade-off acknowledged ex ante**:

- Higher cadence → each individual trade captures a shorter,
  weaker momentum signal → lower mean R per trade is expected.
- Higher cadence → higher cumulative round-trip costs (spread +
  commission scale linearly with rebalance count).
- Higher cadence → larger n on identical train/holdout windows
  → tighter CIs → easier to pinch zero off the negative tail
  even with smaller mean R.

The spec is calibrated to satisfy §3.6 (4–8 trades/mo target)
while preserving meaningful per-trade R (>= +0.1 net of costs).
If both can be true simultaneously, v1.1 admits. If diluting the
edge through cadence collapses mean R below the cost stack,
v1.1 archives — and **the strategy class "cross-sectional
momentum multi-asset" is structurally non-viable** on this
operator's deployment context (see footer).

---

## 1. Overview

**Concept, universe, classification, sizing, anti-look-ahead
discipline**: identical to v1.0. See
`docs/strategies/trend_rotation_d1.md` §§ 1, 2.1–2.6 for the
canonical reference. The pseudo-code (`compute_momentum`,
`select_top_k`, `detect_rebalance_trades`, `sizing_for_entry`,
volatility regime filter) is unchanged.

**Class** (per protocol §11.4.1): **B — HTF multi-asset
cross-sectional momentum**. §3.5 class-adapted floors apply at
gate 4 selection.

**Estimated cadence and edge** — v1.1 a-priori (revised vs v1.0):

| Quantity | v1.0 a-priori | v1.1 a-priori | Source |
|---|---|---|---|
| Closed trades / month / portfolio | 0.7–2.3 | **4–8** | §3.6 floor + grid v1.1 toward shorter rebalance |
| Win rate (per closed trade) | 50–60 % | 48–60 % | Slightly widened low edge: shorter momentum captures noisier signals |
| Mean R per closed trade (pre-cost) | +0.2 to +0.6 | **+0.1 to +0.4** | Edge dilution by cadence (principle, not v1 holdout) |
| Mean R per closed trade (post-cost) | +0.1 to +0.5 | **+0.0 to +0.3** | Cumulative round-trip costs scale with rebalance count |
| Projected annual return @ 1 % risk | 5–15 % | **5–25 %** | Cadence × 4 vs v1; R per trade × 0.5 → expected ≈ B&H or 2× |

The 5–25 % v1.1 band is intentionally wider than v1.0's. If
shortening rebalance dilutes per-trade edge proportionally, the
projection lands near 5–10 %. If the rebalance acceleration also
captures a larger fraction of the underlying CSM Sharpe (because
v1's 10-day rebalance was leaving signal on the table between
rebalances), the projection can clear 15–25 %. Neither outcome
is HARKing — both fall inside the pre-spec band.

---

## 2. Pseudo-code

Identical to v1.0 §§ 2.1–2.6, including the §2.6 volatility
regime filter (ATR > 5 × 90-d median → exclude this rebalance).

The audit harness `calibration/audit_trend_rotation_d1.py`
remains the gate 3 reference; v1.1 introduces no new code paths,
only new grid points to evaluate at gate 4.

---

## 3. Parameters

### 3.1 Fixed (pre-specified, NOT calibrated) — unchanged from v1.0

| Parameter | Value | Justification |
|---|---|---|
| Universe | 15 assets (per v1.0 §1) | Tradable on FundedNext + ≥ 6.4 y D1 coverage |
| Decision timeframe | D1 close | Strategy class anchor |
| Risk per trade | 1 % | Protocol §3 default |
| Position sizing | Risk parity, ATR(20)-D1 | Cross-asset homogenisation |
| Volatility regime filter | ATR(20) > 5 × median(ATR(20), 90 d) → exclude | Anti flash-crash; v1 hard rule |
| ATR period | 20 days | Academic standard |
| Insufficient-history filter | < lookback + 1 D1 closes → skip asset | v1 hard rule |
| Direction | Long-only | v2 candidate per v1.0 §7 |
| Train window | 2019-12-22 → 2024-12-31 (≈ 5.0 y) | XAUUSD-anchored intersection (v1.0 §3.4) |
| Holdout window | 2025-01-01 → 2026-04-30 (≈ 1.4 y) | v1.0 §3.4 |

### 3.2 Calibrated grid — v1.1 cadence expansion

| Axis | v1.0 values | **v1.1 values** | Justification (v1.1) |
|---|---|---|---|
| `momentum_lookback` (days) | {63, 126} | **{63, 126}** | Unchanged — academic sweet spot bracket. 6-month is the canonical anchor; 3-month preserved as faster sensitivity axis. |
| `K` (basket size) | {3, 4} | **{3, 4, 5}** | Extended upward. K = 5 raises cadence (more entries / exits per rebalance on average) and improves diversification on the 5–6 effective-bet universe. |
| `rebalance_frequency` (days) | {10, 21} | **{3, 5, 7}** | Reduced. v1.0's 10-day rebalance produced 1.31 trades/mo on holdout (well under §3.6); the v1.1 grid targets 4–8 trades/mo at the shortest rebalance. Quarterly (63 d) and monthly (21 d) are eliminated by §3.6 by construction. |

**18 cells per run** (2 × 3 × 3).

**§3.6 pre-measure (mandatory before final spec freeze)**: for
each of the 18 cells, project portfolio cadence on the train
window. Cells with projected `< 4 trades/mo portfolio` are
flagged "non-viable §3.6" and excluded from the gate 4 selection
pool. The pre-measure deliverable is
`calibration/runs/premeasure_trend_rotation_d1_v1_1_<TS>.md`.

The §3.6 pre-measure is run **before any backtest**, so the
selection pool is known ex ante and frozen with this spec.

### 3.3 Default operating point

`momentum_lookback = 126 d`, `K = 4`, `rebalance = 5 d`.

- 6-month momentum is the academic anchor (preserved from v1.0
  default).
- K = 4 is a balance between focused basket and §3.6-favouring
  cadence.
- Rebalance = 5 d (≈ weekly) is the middle of the v1.1 grid;
  rebalance = 3 d targets cadence ceiling, rebalance = 7 d
  targets cadence floor of the §3.6-compliant range.
- Compared with v1.0 default (126/3/10): same momentum lookback,
  K + 1, rebalance ÷ 2 — yielding ~4× the rebalance count over
  the same window.
- Used by `calibration/audit_trend_rotation_d1.py` (gate 3) as
  the reference cell for streaming-vs-full-history bit-identity.

### 3.4 Selection criteria on the train grid

Class B per §11.4.1 — §3.5 class-adapted floors apply, plus the
§3.6 cadence floor:

| Floor | Threshold | Source |
|---|---|---|
| `n_closed` | ≥ 100 | §3.5 |
| `mean_r_ci_95.lower` | ≥ −0.1 R | §3.5 |
| `temporal_concentration` | < 0.6 | §3.5 |
| **Trades / month / portfolio (train)** | **≥ 4** | **§3.6 (NEW)** |

Selection rule: among cells clearing all four floors, max
`mean_r`. Tie-break: max `vs_buy_and_hold.strategy_minus_bh_pct`.

### 3.5 Train / holdout split — unchanged from v1.0

Train 2019-12-22 → 2024-12-31; holdout 2025-01-01 → 2026-04-30.

If the holdout `mean_r` diverges by more than 0.3 R from the
selected cell's train `mean_r`, this is an overfit signal — the
verdict is not auto-archived but flagged for operator review.

---

## 4. Pre-specified hypotheses (anti-data-dredging)

**Recorded BEFORE any backtest.** Calibrated ex ante on
principles — cadence-edge dilution + CSM academic literature —
NOT on v1.0 holdout numbers (which were measured on n = 21 and
are not generalisable to the v1.1 cadence regime).

The bands below specifically widen H5 (`projected_annual_return`)
and tighten H3 / H4 (`mean R`) versus v1.0, to reflect the
expected dilution-by-cadence trade-off. Extreme outcomes in either
direction (very high cadence + collapsed R, OR retained R at
higher cadence) should land inside the bands. Anything outside is
either a measurement artefact (gate 4 attrition) or genuine
signal to investigate.

| # | Hypothesis | v1.1 band | Justification |
|---|---|---|---|
| H1 | Closed trades / month / portfolio (holdout) | **4–8** | §3.6 floor (≥ 4) + grid §3.2 ceiling (rebalance = 3 d × K = 5 ≈ 8/mo). |
| H2 | Win rate (closed) | **48–60 %** | CSM academic literature 50–60 % (Asness 2013); slight widen at low end to absorb shorter-momentum noise. |
| H3 | Mean R (pre-cost) per closed trade | **+0.1 to +0.4** | Edge dilution: cadence × 4 vs v1.0 → R/trade ≈ ÷ 2–3. v1.0 a-priori range halved on the upper bound. |
| H4 | Mean R (post-cost) per closed trade | **+0.0 to +0.3** | Cumulative round-trip costs scale linearly with rebalance count. Expect 0.05–0.15 R per trade cost stack at FundedNext rates × 4 cadence. |
| H5 | `projected_annual_return_pct` | **5–25 %** | Wider band: H4 × H1 × 12 × 1 %. Lower edge (5 %): edge fully diluted, similar to B&H. Upper edge (25 %): cadence acceleration captures additional Sharpe. Both ends of the band are physically plausible without HARKing. |
| H6 | `mean_r_ci_95.lower` (≥ 1 cell, holdout) | **> 0** strict | Without it, no measurable edge. §3.5 H6 stays strict. |
| H7 | `outlier_robustness.trim_5_5.mean_r` (selected cells) | **> 0** strict | Per-trade robustness, class-independent. §3.5 H7 stays strict. |
| H8 | `temporal_concentration` (selected cells) | **< 0.6** | Class B per §3.5 (relaxed from < 0.4 of class A). |
| H9 | `vs_buy_and_hold.strategy_minus_bh_pct` (≥ 1 cell) | **> 0** | Strategy must beat passive equal-weight basket on holdout. §11.4.1 chop-equivalent detector for class B. |
| H10 | Top-K agreement Duk vs MT5 (gate 7) | **> 70 %** of rebalances | Rotation-specific transferability per v1.0 §6 H10. Measured at gate 7. |

**Verdict rule on the HOLDOUT** (FROZEN BEFORE re-run):

| Hypotheses satisfied (out of 10) | Decision |
|---|---|
| ≥ 6 / 10 | **PROMOTE** to gate 5+ |
| 3 / 10 ≤ x ≤ 5 / 10 | **REVIEW** — operator path-decision before continuing |
| < 3 / 10 | **ARCHIVE** — v1.1 + class non-viable footer applies (§ footer) |

**On H5 specifically** — the v1.1 band 5–25 % straddles the
protocol §3 viability threshold of 20 %. If H5 materialises
in [20, 25] % AND ≥ 6 / 10 hypotheses PASS, the strategy clears
both viability and verdict — promote. If H5 materialises in [5,
20) % AND ≥ 6 / 10 PASS, the operator decision tree is the same
as v1.0 §4 H5 note (continue gates 5–8 / archive with note /
revise §3 viability per class). The verdict count is unaffected
by H5 outcome — H5 is one of 10 hypotheses, evaluated on its
band as written.

---

## 5. Anticipated pitfalls — v1.1 specific

### 5.1 Whipsaw cost erosion at rebalance = 3 d

At 3-day rebalance, ~415 rebalances over 5 y train, vs ~125 at
v1.0's 10-day. Cost stack scales linearly. Some assets will
enter / exit the basket within 3–6 days, so each round trip pays
the full spread + commission for marginal alpha capture.

**Mitigation**: gate 8 Phase C with realistic FundedNext costs
per asset is the test. If `mean_r` post-cost on the selected
v1.1 cell collapses to ~0 vs the pre-cost reading, this pitfall
materialises and the v1.1 archive verdict applies. v1.1
explicitly accepts this risk because the alternative (slow
rebalance) failed §3.6.

### 5.2 Régime fit even at high cadence

The 1.4 y v1.0 holdout (2025-01-01 → 2026-04-30) was a trending
régime favouring cross-sectional momentum. Acceleration of
rebalance does not mechanically reduce régime sensitivity — a
trending régime with weekly rotations can still produce the same
"all signal in one direction" pattern that flatters the strategy.

**Mitigation**: H8 (`temporal_concentration < 0.6` per §3.5) on
the selected cell. If H8 fails on holdout while H6 / H7 pass,
verdict is REVIEW, not auto-PROMOTE — operator must run the
gate-5 cross-check on Databento partial coverage to disambiguate
edge from régime.

### 5.3 Sample-size variance across cells

With rebalance = 3 d, n_closed per cell on train can reach
400+. Sample comfortable in absolute terms but variance
**inter-cell** (across the 18-cell grid) likely high, and the
selection rule "max mean_r" may pick a cell that won the random
sweep rather than the cell with the strongest underlying signal.
The §3.4 selection rule mitigates by requiring all four floors
plus tie-break on `vs_buy_and_hold` — but the operator should
read the full grid table at gate 4 before concluding.

### 5.4 Universe-rebalance interaction

A 3-day rebalance on a 15-asset universe with 5–6 effective
independent bets means the basket of K = 5 covers the entire
effective universe — the strategy degenerates to "weighted
buy-and-hold of the whole tradable set with a momentum tilt".
H9 (`strategy_minus_bh_pct > 0`) detects this: if the v1.1
selected cell on holdout is barely distinguishable from EW
basket, the rotation premise is washed out by cadence and
universe size.

### 5.5 Pitfalls 5.3, 5.4 from v1.0 still apply

US-indices cluster dominance (v1.0 §5.3) and BTCUSD volatility
outliers (v1.0 §5.4) are unchanged in v1.1. The §2.6 volatility
filter handles BTC tail; the cluster pitfall is an accepted v1
risk per v1.0 §7 (v2 candidate filter).

---

## 6. Validation plan — mapping to protocol gates

Identical to v1.0 §6, with two amendments:

| Gate | v1.1 amendment vs v1.0 §6 |
|---|---|
| **3** Audit | Reference cell updated to 126/4/5 per §3.3. Audit harness unchanged. |
| **4** Backtest Duk | 18-cell grid (§3.2) on train; selection per §3.4 (§3.5 class-B floors + §3.6 cadence floor); holdout single-cell evaluation per §4 verdict rule. |
| **5** Cross-check DBN | Same as v1.0 §6 — DBN coverage partial, ±50 % band. |
| **6** Sanity MT5 | Same as v1.0 §6 — direction-sign agreement. |
| **7** Transferability | Top-K rebalance-level agreement > 70 % per H10. The metric is unchanged; the rebalance count quadruples vs v1.0, so the agreement statistic is built on more datapoints — tighter measurement. |
| **8** Phase C | Same as v1.0 §6 — FundedNext per-asset spread + commission model. **Critical for v1.1** because cumulative cost grows ~4× vs v1.0. |
| **9** Decision | Operator review of §4 verdict + §3.6 cadence + §3 viability + Phase C. |

---

## 7. Out-of-scope (v1.1)

Same as v1.0 §7. v1.1 deliberately does not introduce v2-scope
features (cluster filter, macro régime gate, multi-period
momentum, long/short) — the cadence expansion is the only
adjustment. If v1.1 archives, the strategy class is considered
non-viable on the operator's deployment context (footer).

---

## 8. Budget — per protocol §8

| Phase | Target |
|---|---|
| Specification (this doc) | 1–2 h |
| §3.6 pre-measure on 18-cell grid | 1–2 h |
| Re-run gates 2–4 with v1.1 grid | 0.5–1 d |
| Gates 5–8 on selected cell | 1–2 d |
| Decision (gate 9) | 2 h |
| **Total target v1.1** | **2–4 d** |

**Hard stop-loss: 5 days from this commit to v1.1 admission.**
Tighter than v1.0's 10-day envelope because the audit harness,
grid driver, panel scaffold, and BacktestResult format are all
reusable — only the grid points change.

Beyond 5 days: mandatory move to
`archived/strategies/trend_rotation_d1_v1_1/` with the
post-mortem README per protocol §8.

---

## 9. Lessons from §11 incorporated

The five lessons distilled from §11.1–§11.4 archives are all
materialised in this spec, plus the new §3.6 lesson:

1. **§11.2 lesson #1 — pre-measure cadence on raw triggers**.
   v1.0 pre-measure (`2026-05-04T07-22-14Z`) is the anchor;
   v1.1 amendment is the §3.6 portfolio-cadence pre-measure on
   the v1.1 grid.

2. **§11.3 lesson #1 — pre-spec attrition diagnostic**. v1.0
   diagnostic (`2026-05-04T08-13-11Z`) covered the v1.0 grid;
   v1.1 inherits the methodology and the §3.6 pre-measure (this
   spec §3.2) is the v1.1 equivalent of the §1.5 attrition
   diagnostic, scoped to cadence.

3. **§11.2 / §11.3 lesson #2 — chop fingerprint is direction-
   agnostic**. Class-B equivalent (top-K basket return ≈ EW
   basket return) carried over via H9.

4. **§11.3 lesson #3 — modification pattern with explicit
   versioning**. This spec IS the worked example: v1.0 → v1.1
   with the modification log (§0), why (§§ 0, 5.1), and §4
   hypothesis revisions documented, all BEFORE re-run.

5. **§11.3 lesson #4 — n_closed ≥ 50 floor protects against
   small-sample apparent edges**. §3.5 raises to ≥ 100 for
   class B; §3.4 of this spec applies it.

6. **§11.4 / §11.5 lesson — cadence as primary viability filter
   (NEW with this spec)**. §3.6 (≥ 4 trades/mo distributed
   across 3+ weeks) is the binding constraint that drove v1.0
   to ARCHIVE. v1.1 §3.2 grid is calibrated to satisfy §3.6 by
   construction, with a mandatory pre-measure verification of
   the projected cadence per cell before final selection.

---

*Spec v1.1 frozen at this commit. Hypothesis bands §4 calibrated
ex ante on principle (cadence-edge dilution + CSM academic
literature), NOT on v1 holdout numbers. Any change to fixed
parameters §3.1, calibration grid §3.2, train/holdout split
§3.5, or pre-spec hypotheses §4 requires either v1.2 spec or
explicit operator-approved revision recorded in commit history.
Quietly adjusting a number to chase a result disqualifies the
run.*

*This is the final v1.x iteration of `trend_rotation_d1`. If
v1.1 verdict is ARCHIVE, the strategy class "cross-sectional
momentum multi-asset" is considered structurally non-viable for
the operator's deployment context, and future strategies will be
selected from other classes (HTF single-asset wick-sensitive,
LTF single-asset, or HTF cross-sectional rotation rebalance ≤ 7
d with K ≥ 4 only if v1.1 produces a viable cell at exactly
those settings).*
