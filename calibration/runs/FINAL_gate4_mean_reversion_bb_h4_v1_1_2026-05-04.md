# Gate 4 — mean_reversion_bb_h4 v1.1 backtest principal Duk (2026-05-04T00:06:38Z)

Spec: `docs/strategies/mean_reversion_bb_h4.md` (commit ae61f70, v1.1 post-diagnostic). Protocol gate 4 of `docs/STRATEGY_RESEARCH_PROTOCOL.md`.

**Anti-data-dredging**: the 10 hypotheses (§4 of the spec, v1.1 anchor) and the train selection criteria (§3.2) are frozen at the spec commit and evaluated post-run, never tuned.

- **Global verdict**: FAIL — no instrument PROMOTE
  - XAUUSD: **ARCHIVE** (0 hypotheses PASS)
  - NDX100: **ARCHIVE** (0 hypotheses PASS)
  - SPX500: **ARCHIVE** (0 hypotheses PASS)
- **Wallclock**: 2215.8 s

## 1. Train grid (4×3 per instrument, v1.1 broadened)

Window: 2020-01-01 → 2024-12-31. Selection: `n_closed >= 50` AND `mean_r_ci_95.lower >= 0` AND `temporal_concentration < 0.4`; among those, max `mean_r` (tie-break: max `setups_per_month`).

### XAUUSD

| min_pen | sl_buffer | n_setups | n_closed | mean_r | CI low | CI high | win | setups/mo | tc | proj_annual | sel |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 0.0 | 0.5 | 93 | 93 | +0.278 | -0.169 | +0.780 | 29.0% | 1.55 | 0.465 | +5.2% |  |
| 0.0 | 1.0 | 92 | 92 | +0.222 | -0.188 | +0.675 | 30.4% | 1.53 | 0.526 | +4.1% |  |
| 0.0 | 2.0 | 89 | 89 | +0.233 | -0.171 | +0.685 | 32.6% | 1.48 | 0.560 | +4.1% |  |
| 0.1 | 0.5 | 64 | 64 | +0.132 | -0.371 | +0.713 | 28.1% | 1.07 | 1.056 | +1.7% |  |
| 0.1 | 1.0 | 63 | 63 | +0.114 | -0.347 | +0.631 | 30.2% | 1.05 | 1.063 | +1.4% |  |
| 0.1 | 2.0 | 64 | 64 | +0.171 | -0.303 | +0.707 | 32.8% | 1.07 | 0.992 | +2.2% |  |
| 0.2 | 0.5 | 44 | 44 | +0.290 | -0.372 | +1.084 | 29.5% | 0.73 | 0.857 | +2.6% |  |
| 0.2 | 1.0 | 43 | 43 | +0.213 | -0.387 | +0.902 | 30.2% | 0.72 | 0.939 | +1.8% |  |
| 0.2 | 2.0 | 44 | 44 | +0.329 | -0.291 | +1.034 | 34.1% | 0.73 | 0.652 | +2.9% |  |
| 0.3 | 0.5 | 35 | 35 | +0.452 | -0.355 | +1.382 | 31.4% | 0.58 | 0.598 | +3.2% |  |
| 0.3 | 1.0 | 34 | 34 | +0.368 | -0.366 | +1.201 | 32.4% | 0.57 | 0.689 | +2.5% |  |
| 0.3 | 2.0 | 35 | 35 | +0.520 | -0.225 | +1.383 | 37.1% | 0.58 | 0.518 | +3.6% |  |

**Selection**: no train cell met all three selection criteria (n_closed >= 50, ci_low >= 0, temporal_concentration < 0.4)

### NDX100

| min_pen | sl_buffer | n_setups | n_closed | mean_r | CI low | CI high | win | setups/mo | tc | proj_annual | sel |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 0.0 | 3.0 | 68 | 68 | +0.388 | -0.257 | +1.219 | 27.9% | 1.13 | 0.365 | +5.3% |  |
| 0.0 | 5.0 | 68 | 68 | +0.320 | -0.290 | +1.079 | 27.9% | 1.13 | 0.378 | +4.4% |  |
| 0.0 | 8.0 | 68 | 68 | +0.236 | -0.321 | +0.918 | 27.9% | 1.13 | 0.470 | +3.2% |  |
| 0.1 | 3.0 | 49 | 49 | +0.599 | -0.265 | +1.704 | 28.6% | 0.82 | 0.443 | +5.9% |  |
| 0.1 | 5.0 | 50 | 50 | +0.485 | -0.304 | +1.481 | 28.0% | 0.83 | 0.456 | +4.9% |  |
| 0.1 | 8.0 | 50 | 50 | +0.385 | -0.335 | +1.285 | 28.0% | 0.83 | 0.456 | +3.8% |  |
| 0.2 | 3.0 | 32 | 32 | +0.617 | -0.480 | +2.163 | 28.1% | 0.53 | 0.810 | +3.9% |  |
| 0.2 | 5.0 | 32 | 32 | +0.517 | -0.497 | +1.917 | 28.1% | 0.53 | 0.849 | +3.3% |  |
| 0.2 | 8.0 | 32 | 32 | +0.397 | -0.518 | +1.631 | 28.1% | 0.53 | 0.928 | +2.5% |  |
| 0.3 | 3.0 | 23 | 23 | +1.250 | -0.238 | +3.366 | 39.1% | 0.38 | 0.592 | +5.7% |  |
| 0.3 | 5.0 | 23 | 23 | +1.111 | -0.260 | +3.026 | 39.1% | 0.38 | 0.589 | +5.1% |  |
| 0.3 | 8.0 | 23 | 23 | +0.943 | -0.293 | +2.617 | 39.1% | 0.38 | 0.589 | +4.3% |  |

**Selection**: no train cell met all three selection criteria (n_closed >= 50, ci_low >= 0, temporal_concentration < 0.4)

### SPX500

| min_pen | sl_buffer | n_setups | n_closed | mean_r | CI low | CI high | win | setups/mo | tc | proj_annual | sel |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 0.0 | 1.0 | 74 | 74 | -0.403 | -0.735 | -0.006 | 14.9% | 1.23 | 0.302 | -6.0% |  |
| 0.0 | 2.0 | 74 | 74 | -0.372 | -0.693 | -0.010 | 17.6% | 1.23 | 0.327 | -5.5% |  |
| 0.0 | 3.0 | 77 | 77 | -0.423 | -0.712 | -0.089 | 16.9% | 1.28 | 0.276 | -6.5% |  |
| 0.1 | 1.0 | 54 | 54 | -0.422 | -0.766 | -0.019 | 16.7% | 0.90 | 0.263 | -4.6% |  |
| 0.1 | 2.0 | 54 | 54 | -0.360 | -0.700 | +0.021 | 20.4% | 0.90 | 0.309 | -3.9% |  |
| 0.1 | 3.0 | 55 | 55 | -0.392 | -0.716 | -0.031 | 20.0% | 0.92 | 0.278 | -4.3% |  |
| 0.2 | 1.0 | 38 | 38 | -0.179 | -0.633 | +0.362 | 23.7% | 0.63 | 0.589 | -1.4% |  |
| 0.2 | 2.0 | 38 | 38 | -0.206 | -0.650 | +0.308 | 23.7% | 0.63 | 0.511 | -1.6% |  |
| 0.2 | 3.0 | 39 | 39 | -0.250 | -0.670 | +0.243 | 23.1% | 0.65 | 0.411 | -1.9% |  |
| 0.3 | 1.0 | 25 | 25 | +0.043 | -0.586 | +0.802 | 28.0% | 0.42 | 3.731 | +0.2% |  |
| 0.3 | 2.0 | 25 | 25 | +0.007 | -0.594 | +0.736 | 28.0% | 0.42 | 20.943 | +0.0% |  |
| 0.3 | 3.0 | 25 | 25 | -0.024 | -0.606 | +0.678 | 28.0% | 0.42 | 6.265 | -0.1% |  |

**Selection**: no train cell met all three selection criteria (n_closed >= 50, ci_low >= 0, temporal_concentration < 0.4)

## 2. Holdout — selected params per instrument

Window: 2025-01-01 → 2026-04-29. Selected (min_pen, sl_buffer) cell from §1 run unchanged.

| Instrument | min_pen | sl_buffer | n_setups | n_closed | mean_r | CI low | CI high | win | setups/mo | tc | proj_annual | trim_5_5 mean_r | strategy − BH % |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| XAUUSD | — | — | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| NDX100 | — | — | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| SPX500 | — | — | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |

### Train vs holdout consistency check

| Instrument | mean_r train | mean_r holdout | Δ | overfit flag (Δ > 0.3R) |
|---|---:|---:|---:|:---:|
| XAUUSD | n/a | n/a | n/a | n/a |
| NDX100 | n/a | n/a | n/a | n/a |
| SPX500 | n/a | n/a | n/a | n/a |

## 3. Hypothesis evaluation (holdout only — v1.1 §4)

| Hypothesis | XAUUSD | NDX100 | SPX500 |
|---|---|---|---|
| **H1** Setups / month / instrument in [0.5, 2] | n/a | n/a | n/a |
| **H2** Win rate (closed) in [55 %, 70 %] | n/a | n/a | n/a |
| **H3** Mean R (pre-cost) in [+0.4, +0.8] | n/a | n/a | n/a |
| **H4** Mean R (post-cost) in [+0.3, +0.7] | n/a | n/a | n/a |
| **H5** Projected annual return % in [10, 25] | n/a | n/a | n/a |
| **H6** mean_r_ci_95.lower > 0 | n/a | n/a | n/a |
| **H7** outlier_robustness.trim_5_5.mean_r > 0 | n/a | n/a | n/a |
| **H8** temporal_concentration < 0.4 | n/a | n/a | n/a |
| **H9** vs_buy_and_hold.strategy_minus_bh_pct > 0 | n/a | n/a | n/a |
| **H10** Transferability mismatch Duk vs MT5 < 30 % | n/a | n/a | n/a |

## 4. Verdict per instrument

Verdict rule (spec v1.1 §4 holdout):

- ≥ 6 PASS → **PROMOTE** (candidate gate 5)
- 3 ≤ PASS ≤ 5 → **REVIEW** (operator discussion)
- < 3 PASS → **ARCHIVE** (mandatory)

(Hypotheses with `pass=None` — e.g. H10 unavailable — are excluded from both numerator and denominator.)

- **XAUUSD**: ARCHIVE (0 / 0 PASS)
- **NDX100**: ARCHIVE (0 / 0 PASS)
- **SPX500**: ARCHIVE (0 / 0 PASS)

## 5. Suggested next

All instruments ARCHIVE (XAUUSD, NDX100, SPX500). Move to `archived/strategies/mean_reversion_bb_h4_v1_1/` with the post-mortem README per protocol §8 and pick the next HTF candidate from the backlog.

