# NDX100 MT5 +1.56R robustness check — 2026-05-03T08-00-39Z

Stress-test of the post-timezone-fix headline result before authorising
a parameter sweep on the extended MT5 fixtures.

**Source**: `calibration/runs/mt5_vs_databento_tick_2026-05-02T21-24-37Z/mt5_NDX100_setups.jsonl`
(28 emitted setups; 27 closed after excluding `sl_before_entry`).

**Headline under scrutiny**: n=27 closed, mean R = +1.56, bootstrap CI 95%
[+0.37, +2.83], win rate 40.7%. Claimed as the first CI-strictly-positive
edge in the project.

> Note on internal arithmetic: this re-aggregation reads the same JSONL
> the BacktestResult was built from. Mean R recomputed here is +1.601,
> vs +1.564 in the BacktestResult JSON. The 0.04 R drift is due to a
> minor difference in the `closed-trade` filter at render time (probably
> the `open_at_horizon` row); n=27, n_wins=11, win-rate=40.7% match
> exactly. None of the conclusions below depend on which of the two
> figures is used.

---

## CHECK 1 — Long / Short decomposition

| Side | n | Mean R | Median R | Win rate | Bootstrap CI 95% |
|---|---:|---:|---:|---:|---|
| **Long**  | 11 | **+2.504** | +3.164 | 54.5% | **[+0.470, +4.642]** |
| **Short** | 16 | +0.980 | −1.000 | 31.2% | [−0.296, +2.404] |

- Long side is **CI-strictly-positive** (lower bound +0.47) on n=11.
  Median +3.16 — when longs win they win cleanly.
- Short side mean is positive but **CI crosses zero**. Median is
  −1.000: most shorts hit SL, the positive mean is dragged up by a few
  outsized winners (cf. CHECK 2).
- This is **not a pure long-only beta capture** — shorts also carry a
  positive point estimate — but the asymmetry (long IC > 0, short IC ⊆ 0)
  combined with NDX's +29 %/yr trend over the same window means a beta
  contribution is plausible. We cannot reject "long-side rides the
  trend, short-side gets occasionally lucky". Inconclusive direction
  signal.

## CHECK 2 — Outlier sensitivity

Bootstrap CI on the closed-trade R sample after removing the most
extreme observations.

| Variant | n | Mean R | Bootstrap CI 95% |
|---|---:|---:|---|
| All trades | 27 | +1.601 | [+0.424, +2.861] |
| Trim ±2 (rm 2 best + 2 worst) | 23 | +1.268 | [+0.166, +2.469] |
| Trim ±5 (rm 5 best + 5 worst) | 17 | +0.876 | **[−0.271, +2.093]** |
| Remove 2 best wins only | 25 | +1.087 | [+0.043, +2.215] |
| **Remove 5 best wins only** | 22 | **+0.450** | **[−0.469, +1.469]** |
| Remove 2 worst losses only | 25 | +1.809 | [+0.551, +3.141] |
| Remove 5 worst losses only | 22 | +2.192 | [+0.826, +3.625] |

The five largest wins have R values of +9.21, +6.84, +6.54, +5.51, +5.23
(total ≈ +33 R). The mean of the remaining 22 trades is +0.45 R with a
bootstrap CI **straddling zero**. A symmetric ±5 trim drops mean to
+0.88 and pushes the CI lower bound to **−0.27**.

This is a fragile distribution. A sample of 27 with a 75th-percentile of
−1.000 (i.e., **half-or-more of the trades are stops**) and a positive
mean only by virtue of a fat right tail. That can be a genuine
asymmetric setup (TJR's design intent is precisely that — pay 1R to
catch 3-6R), but on n=27 with the edge collapsing under "remove top 5"
we have **no statistical reason to believe the right tail will recur**.

## CHECK 3 — Temporal distribution

Per-quarter breakdown over the 14 quarters in the window
(2022-Q4 → 2026-Q2):

| Quarter | n | Mean R | Win rate | Outcomes |
|---|---:|---:|---:|---|
| 2022 Q4 | 0 | — | — | — |
| 2023 Q1 | 0 | — | — | — |
| 2023 Q2 | 2 | −1.000 | 0% | 2× sl_hit |
| 2023 Q3 | 2 | +2.113 | 50% | 1× sl, 1× tp_runner |
| 2023 Q4 | 2 | +2.254 | 50% | 1× sl, 1× tp_runner |
| 2024 Q1 | 2 | +1.371 | 50% | 1× sl, 1× tp_runner |
| 2024 Q2 | 1 | −1.000 | 0% | 1× sl |
| 2024 Q3 | 5 | +2.416 | 60% | 3× tp_runner, 2× sl |
| 2024 Q4 | 3 | −1.000 | 0% | 3× sl |
| 2025 Q1 | 3 | +0.870 | 33% | 2× sl, 1× tp_runner |
| 2025 Q2 | 3 | −0.667 | 0% | 2× sl, 1× open_at_horizon |
| **2025 Q3** | **1** | **+6.538** | **100%** | **1× tp_runner** |
| **2025 Q4** | **3** | **+6.171** | **100%** | **3× tp_runner** |
| 2026 Q1 | 0 | — | — | — |
| 2026 Q2 | 0 | — | — | — |

- **Active quarters: 11 out of 14** (boundary quarters empty, expected).
- **Losing quarters: 4** (2023Q2, 2024Q2, 2024Q4, 2025Q2 — all mean
  ≈ −1.0).
- **Profitable spike — 2025 H2** (Q3 + Q4): **4 trades, all winners,
  mean R = +6.27**. These four trades alone contribute **~25 R** out of
  the total ≈ 41 R sum.
- Excluding the 2025-H2 spike: n=23, mean = **+0.79**, **CI 95%
  [−0.26, +1.94]** — back to non-significant.

This is a **strong regime-fit warning**. Roughly 60% of the cumulative
R sits in two consecutive quarters covering 14 % of the window. The
edge before that period was at best mildly positive (mean +0.79 across
2 years, CI brackets zero). A grid search on this same data risks
optimising for whatever microstructure quirk produced the 2025-H2
spike — exactly the kind of overfit Sprint 6.5 was meant to guard
against.

## CHECK 4 — Buy-and-hold baseline

Over the exact same window (2022-10-20 → 2026-04-29) on the same
MT5 fixture:

- NDX MT5 close at start: **11 038.20**
- NDX MT5 close at end:   **27 123.53**
- **Total return: +145.7 %**
- **Annualised: +29.1 %/yr**

TJR cadence over the same window:

- 27 trades / 3.52 years = **7.7 trades/year**
- Mean R = 1.56 → **~12.0 R/year**
- At a constant 1% risk-per-trade ⇒ ~ +12 %/yr (linear, before
  compounding) — **less than half** of buy-and-hold's +29 %/yr
- TJR matches buy-and-hold at **~ 2.4 % risk-per-trade**, which would
  exceed FundedNext's daily-loss / max-drawdown bands on a single
  bad-day cluster (4 stops × 2.4 % = −9.6 % daily, blows the prop
  account).

The strategy under-performs naïve long-NDX-and-hold at the risk levels
the prop firm allows. This does not by itself invalidate TJR (TJR
trades both sides; buy-and-hold concentrates risk on a single market
in a single direction; FundedNext disallows naïve B&H sizing), but it
sets a high bar: the *value-add* of the strategy must come from
something other than catching upside on NDX, because catching upside
was free.

---

## Verdict

**(c) Mixed — leaning artefactuel.**

The +1.56 R / CI-positive headline is technically true on the full
sample but **does not survive any of the standard robustness probes**:

1. ✗ Direction balance: positive long edge (CI > 0), inconclusive
   short edge (CI ⊆ 0). NDX rallied +145 % over the window. We cannot
   distinguish "structural setup edge" from "long-side trend
   capture".
2. ✗ Outlier robustness: removing the top 5 wins drops the mean to
   +0.45 with a CI that brackets zero. The edge is thin-tailed-fragile
   on n=27.
3. ✗ Temporal stability: 60 % of cumulative R comes from 4 trades in
   2025-Q3+Q4. Excluding that spike → mean +0.79, CI bracketing zero.
   Pre-2025-H2 edge is statistically indistinguishable from random.
4. ✗ Vs buy-and-hold: at the prop-firm-compliant 1 % risk-per-trade,
   TJR returns ~12 %/yr against NDX +29 %/yr. The strategy must add
   something B&H doesn't to justify its operational cost.

What survives:

- ✓ Long-only IC is strictly positive (n=11, [+0.47, +4.64]) — but the
  beta-vs-edge ambiguity in CHECK 1 means this is not yet a TJR
  validation.
- ✓ Setups are spread across 11 of 14 quarters — not a one-month
  artefact, even if the magnitude is concentrated in two quarters.

## Recommendations

**Do NOT proceed with a full MT5 grid search on NDX100.** That would
optimise a fragile distribution and almost certainly find a parameter
combination that boosts the 2025-H2 spike further while degrading the
prior 9-quarter behaviour.

Instead, in this priority order:

1. **Operator discussion before any further calibration spend.** This
   robustness check materially weakens the post-timezone-fix
   conclusion, which is the strongest evidence currently in favour of
   keeping NDX in the live portfolio. The Sprint 6.6 portfolio
   validation that kept NDX was on an 11-month window and an arguably
   leaky detector; the post-fix 3.5-year tick-simulator window
   doesn't cleanly confirm it. **Decision needed**: hold-out tests,
   reduce-risk paper-trade, or remove NDX.
2. **If the operator wants to keep exploring**, run a *minimal*
   3-variant sanity check (not a grid):
   - Baseline (current settings) — already done.
   - Long-only — confirm CHECK 1's CI-positive long edge holds out of
     sample on, say, the 2026 ytd window (held out of this run).
   - Stricter A+ only — does removing A-grade noise concentrate the
     edge or kill it? If it kills it, the edge is in the noise.
3. **Strategy-research branch decision**: this is a strong nudge
   toward `STRATEGY_RESEARCH_PROTOCOL.md` work and exploring
   alternative HTF strategies, rather than further extracting from
   TJR. The post-fix data has not produced a clean TJR-edge signal
   even on the most favourable cell of the comparison.
