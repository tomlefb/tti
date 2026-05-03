# Gate 4 — breakout_retest_h4 backtest principal Duk (2026-05-03T19:40:10Z)

Spec: `docs/strategies/breakout_retest_h4.md` (commits b14e054 / 689287f). Protocol gate 4 of `docs/STRATEGY_RESEARCH_PROTOCOL.md`.

**Anti-data-dredging**: the 10 hypotheses (§4 of the spec) and the train selection criteria (§3.2) are frozen at the spec commit and evaluated post-run, never tuned.

- **Global verdict**: FAIL — no instrument PROMOTE
  - XAUUSD: **ARCHIVE** (no train cell passed selection — calibration impossible)
  - NDX100: **ARCHIVE** (no train cell passed selection — calibration impossible)
  - SPX500: **ARCHIVE** (no train cell passed selection — calibration impossible)
- **Wallclock**: 5516.3 s

## 1. Train grid (3×3 per instrument)

Window: 2020-01-01 → 2024-12-31. Selection: `n_closed >= 50` AND `mean_r_ci_95.lower >= 0` AND `temporal_concentration < 0.4`; among those, max `mean_r` (tie-break: max `setups_per_month`).

### XAUUSD

| retest_tol | sl_buffer | n_setups | n_closed | mean_r | CI low | CI high | win_rate | setups/mo | temp_conc | proj_annual | selected |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 0.5 | 0.3 | 479 | 479 | -0.023 | -0.148 | +0.102 | 32.6% | 7.98 | 1.727 | -2.2% |  |
| 0.5 | 0.5 | 479 | 479 | +0.008 | -0.117 | +0.134 | 33.6% | 7.98 | 4.750 | +0.8% |  |
| 0.5 | 1.0 | 479 | 479 | +0.021 | -0.104 | +0.146 | 34.0% | 7.98 | 1.900 | +2.0% |  |
| 1.0 | 0.3 | 501 | 501 | -0.048 | -0.168 | +0.078 | 31.7% | 8.35 | 0.958 | -4.8% |  |
| 1.0 | 0.5 | 501 | 501 | -0.012 | -0.138 | +0.114 | 32.9% | 8.35 | 3.833 | -1.2% |  |
| 1.0 | 1.0 | 501 | 501 | +0.006 | -0.114 | +0.132 | 33.5% | 8.35 | 7.667 | +0.6% |  |
| 2.0 | 0.3 | 538 | 538 | -0.063 | -0.180 | +0.054 | 31.2% | 8.97 | 0.529 | -6.8% |  |
| 2.0 | 0.5 | 538 | 538 | -0.035 | -0.152 | +0.087 | 32.2% | 8.97 | 0.947 | -3.8% |  |
| 2.0 | 1.0 | 538 | 538 | -0.030 | -0.147 | +0.093 | 32.3% | 8.97 | 1.125 | -3.2% |  |

**Selection**: no train cell met all three selection criteria (n_closed >= 50, ci_low >= 0, temporal_concentration < 0.4)

### NDX100

| retest_tol | sl_buffer | n_setups | n_closed | mean_r | CI low | CI high | win_rate | setups/mo | temp_conc | proj_annual | selected |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 3.0 | 2.0 | 502 | 502 | -0.092 | -0.205 | +0.028 | 30.3% | 8.37 | 0.326 | -9.2% |  |
| 3.0 | 3.0 | 502 | 502 | -0.086 | -0.205 | +0.034 | 30.5% | 8.37 | 0.349 | -8.6% |  |
| 3.0 | 5.0 | 502 | 502 | -0.086 | -0.205 | +0.034 | 30.5% | 8.37 | 0.349 | -8.6% |  |
| 5.0 | 2.0 | 509 | 509 | -0.092 | -0.210 | +0.026 | 30.3% | 8.48 | 0.255 | -9.4% |  |
| 5.0 | 3.0 | 509 | 509 | -0.092 | -0.210 | +0.026 | 30.3% | 8.48 | 0.255 | -9.4% |  |
| 5.0 | 5.0 | 509 | 509 | -0.092 | -0.210 | +0.026 | 30.3% | 8.48 | 0.255 | -9.4% |  |
| 8.0 | 2.0 | 529 | 529 | -0.104 | -0.217 | +0.009 | 29.9% | 8.82 | 0.291 | -11.0% |  |
| 8.0 | 3.0 | 529 | 529 | -0.104 | -0.223 | +0.009 | 29.9% | 8.82 | 0.291 | -11.0% |  |
| 8.0 | 5.0 | 529 | 529 | -0.098 | -0.217 | +0.015 | 30.1% | 8.82 | 0.269 | -10.4% |  |

**Selection**: no train cell met all three selection criteria (n_closed >= 50, ci_low >= 0, temporal_concentration < 0.4)

### SPX500

| retest_tol | sl_buffer | n_setups | n_closed | mean_r | CI low | CI high | win_rate | setups/mo | temp_conc | proj_annual | selected |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 1.0 | 0.5 | 499 | 497 | -0.028 | -0.149 | +0.099 | 32.4% | 8.32 | 1.500 | -2.8% |  |
| 1.0 | 1.0 | 499 | 497 | +0.014 | -0.113 | +0.141 | 33.8% | 8.32 | 2.857 | +1.4% |  |
| 1.0 | 2.0 | 499 | 497 | +0.014 | -0.113 | +0.141 | 33.8% | 8.32 | 2.857 | +1.4% |  |
| 2.0 | 0.5 | 512 | 510 | -0.024 | -0.147 | +0.100 | 32.5% | 8.53 | 1.667 | -2.4% |  |
| 2.0 | 1.0 | 512 | 510 | +0.024 | -0.100 | +0.147 | 34.1% | 8.53 | 2.000 | +2.4% |  |
| 2.0 | 2.0 | 512 | 510 | +0.035 | -0.088 | +0.159 | 34.5% | 8.53 | 1.167 | +3.6% |  |
| 3.0 | 0.5 | 533 | 531 | -0.034 | -0.153 | +0.090 | 32.2% | 8.88 | 1.167 | -3.6% |  |
| 3.0 | 1.0 | 533 | 531 | +0.028 | -0.090 | +0.153 | 34.3% | 8.88 | 1.867 | +3.0% |  |
| 3.0 | 2.0 | 533 | 531 | +0.040 | -0.079 | +0.164 | 34.7% | 8.88 | 1.048 | +4.2% |  |

**Selection**: no train cell met all three selection criteria (n_closed >= 50, ci_low >= 0, temporal_concentration < 0.4)

## 2. Holdout — selected params per instrument

Window: 2025-01-01 → 2026-04-29. The selected (retest_tolerance, sl_buffer) cell from §1 is run unchanged on the holdout.

| Instrument | retest_tol | sl_buffer | n_setups | n_closed | mean_r | CI low | CI high | win_rate | setups/mo | temp_conc | proj_annual | trim_5_5 mean_r | strategy − BH % |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| XAUUSD | — | — | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| NDX100 | — | — | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| SPX500 | — | — | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |

## 3. Hypothesis evaluation (holdout only — §4)

| Hypothesis | XAUUSD | NDX100 | SPX500 |
|---|---|---|---|
| **H1** Setups / month / instrument in [1, 3] | n/a | n/a | n/a |
| **H2** Win rate (closed) in [40 %, 55 %] | n/a | n/a | n/a |
| **H3** Mean R (pre-cost) in [+0.4, +1.2] | n/a | n/a | n/a |
| **H4** Mean R (post-cost) in [+0.3, +1.0] | n/a | n/a | n/a |
| **H5** Projected annual return % in [15, 40] | n/a | n/a | n/a |
| **H6** mean_r_ci_95.lower > 0 | n/a | n/a | n/a |
| **H7** outlier_robustness.trim_5_5.mean_r > 0 | n/a | n/a | n/a |
| **H8** temporal_concentration < 0.4 | n/a | n/a | n/a |
| **H9** vs_buy_and_hold.strategy_minus_bh_pct > 0 | n/a | n/a | n/a |
| **H10** Transferability mismatch Duk vs MT5 < 30 % | n/a | n/a | n/a |

## 4. Verdict per instrument

Verdict rule (spec §4 holdout):

- ≥ 6 PASS → **PROMOTE** (candidate gate 5)
- 3 ≤ PASS ≤ 5 → **REVIEW** (operator discussion)
- < 3 PASS → **ARCHIVE** (mandatory)

(Hypotheses with `pass=None` — e.g. H10 unavailable — are excluded from both numerator and denominator.)

- **XAUUSD**: ARCHIVE (no train cell met `n_closed >= 50` AND `ci_low >= 0` AND `temporal_concentration < 0.4` — calibration could not produce a candidate setting)
- **NDX100**: ARCHIVE (idem)
- **SPX500**: ARCHIVE (idem)

The "no train cell selected" outcome is itself a strong verdict: across the 27 cells (3 × 3 × 3), no parameter combination produces a 95 % CI lower bound above zero on the train window with sub-0.4 temporal concentration. Holdout cannot rescue this — there is no calibrated model to evaluate.

## 5. Suggested next

All three instruments ARCHIVE. Move to `archived/strategies/breakout_retest_h4/` with the post-mortem README per protocol §8 and pick the next HTF candidate from the backlog.

The post-mortem should record:

- **What assumptions in §4 were violated by the data**:
  - H1 (1–3 setups/month): the strategy fires ≈ 8 setups/month per instrument across the grid — roughly 3× the upper band. The fractal-swing pre-spec was too permissive.
  - H2 (40–55 % win rate): observed 30–34 % across all instruments and cells. With RR 2.0 a win rate ≥ 33 % is the breakeven; the strategy hovers right at it pre-cost and below it post-cost. The "trend-following with retest" hypothesis does not hold in 2020-2024 on these three instruments at H4.
  - H3 (mean R 0.4–1.2 pre-cost): observed -0.10 to +0.04 — an order of magnitude below the band.
  - H6 (CI lower > 0): no cell across 27 reaches it.
  - H8 (temporal_concentration < 0.4): only 6 of 27 cells (all NDX100) pass this gate alone — and they fail H6.

- **Structural findings useful for v2 / next strategy**:
  - 5-bar fractal + N_RETEST=8 produces too many spurious "retests" of failed breakouts. A lookback bump (N_SWING ≥ 7) and tighter retest window may filter chop, but doing so post-hoc on this spec disqualifies the run — the next strategy should pre-spec these.
  - The "most recent unlocked swing" rule from spec §2.3 is structurally stable (gate 3 audit confirmed no leak), but its setups/month rate is incompatible with the §4 cadence band. Next strategy should reconcile these *before* committing to the §4 hypotheses.
  - HTF-confluence filter (PDH/PDL, round numbers — explicitly out-of-scope for v1 per §5.4) is the most natural way to drop ~75 % of low-quality setups while keeping the high-conviction ones; this is a strong v2 signal.

- **Hard stop-loss honoured**: gate 4 reached its FAIL verdict within budget (~92 min wallclock) — no need to invoke the 12-day spec stop-loss.

