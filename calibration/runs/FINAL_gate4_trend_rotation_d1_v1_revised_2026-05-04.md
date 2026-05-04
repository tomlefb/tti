# Gate 4 — trend_rotation_d1 backtest principal Duk (2026-05-04T13:12:23Z)

Spec: `docs/strategies/trend_rotation_d1.md` (commit `889f18c`). Protocol gate 4 of `docs/STRATEGY_RESEARCH_PROTOCOL.md`.

**Anti-data-dredging**: the 10 hypotheses (§4 of the spec) and the train selection criteria (§3.2) are frozen at the spec commit and evaluated post-run, never tuned.

- **Verdict**: **REVIEW** (5 / 9 hypotheses PASS)
- **Wallclock**: 9.1 s

## 1. Train grid (8 cells)

Window: 2019-12-22 → 2024-12-31. Selection: ``n_closed >= 50`` AND ``ci_low >= 0`` AND ``temporal_concentration < 0.4``; among those, max ``vs_buy_and_hold.strategy_minus_bh_pct`` (tie-break: max ``setups_per_month``).

| mom | K | rebal | n_closed | mean_r | CI low | CI high | win | setups/mo | tc | strategy − BH % | sel |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 63 | 3 | 10 | 156 | +0.596 | -0.352 | +1.771 | 45.5% | 2.56 | 0.888 | -1.3% |  |
| 63 | 3 | 21 | 109 | +0.579 | -0.848 | +2.362 | 45.0% | 1.79 | 1.343 | -7.2% |  |
| 63 | 4 | 10 | 203 | +0.391 | -0.474 | +1.380 | 43.8% | 3.33 | 1.375 | -4.0% |  |
| 63 | 4 | 21 | 146 | +0.323 | -0.987 | +1.847 | 40.4% | 2.39 | 2.581 | -10.3% |  |
| 126 | 3 | 10 | 106 | +1.338 | -0.016 | +2.882 | 56.6% | 1.74 | 0.420 | +8.3% | ✅ |
| 126 | 3 | 21 | 73 | +1.588 | -0.459 | +3.834 | 53.4% | 1.20 | 0.458 | +3.2% |  |
| 126 | 4 | 10 | 138 | +1.064 | -0.150 | +2.366 | 51.4% | 2.26 | 0.456 | +9.3% |  |
| 126 | 4 | 21 | 101 | +1.019 | -0.726 | +2.809 | 52.5% | 1.66 | 0.820 | +0.6% |  |

**Selection**: selected by max strategy_minus_bh_pct (+8.3 %); 1 cells passed all three filters

## 2. Holdout — selected cell

Window: 2025-01-01 → 2026-04-30. Selected cell run unchanged on the holdout.

| n_closed | mean_r | CI low | CI high | win | setups/mo | tc | proj_annual | trim_5_5 | strategy − BH % |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 21 | +5.348 | +1.785 | +9.882 | 66.7% | 1.31 | 0.478 | +84.2% | +2.528 | +76.4% |

### Train vs holdout consistency check

| mean_r train | mean_r holdout | Δ | overfit flag (Δ > 0.3R) |
|---:|---:|---:|:---:|
| +1.338 | +5.348 | +4.010 | ⚠️ |

## 3. Hypothesis evaluation (holdout — §4)

| Hypothesis | Value | PASS |
|---|---|:---:|
| **H1** Closed trades / month / portfolio in [0.7, 2.3] | 1.31 | ✅ |
| **H2** Win rate (closed) in [50 %, 60 %] | 66.7% | ❌ |
| **H3** Mean R (pre-cost) per closed in [+0.2, +0.6] | +5.348 | ❌ |
| **H4** Mean R (post-cost) per closed in [+0.1, +0.5] | +5.318 | ❌ |
| **H5** Projected annual return % in [5, 15] | +83.8% | ❌ |
| **H6** mean_r_ci_95.lower > 0 | +1.785 | ✅ |
| **H7** outlier_robustness.trim_5_5.mean_r > 0 | +2.528 | ✅ |
| **H8** temporal_concentration < {h8_max} | 0.478 | ✅ |
| **H9** vs_buy_and_hold.strategy_minus_bh_pct > 0 | +76.4% | ✅ |
| **H10** Top-K agreement Duk vs MT5 (gate 7) > 70 % | n/a | ⚠️ deferred |

## 4. Verdict

Verdict rule (spec §4 holdout):

- ≥ 6 PASS → **PROMOTE** (candidate gate 5)
- 3 ≤ PASS ≤ 5 → **REVIEW** (operator discussion)
- < 3 PASS → **ARCHIVE** (mandatory)

(H10 is gate-7-specific (top-K agreement Duk vs MT5) and deferred from the gate-4 count — ``pass=None`` excludes it from both numerator and denominator. Max gate-4 score is 9 / 9.)

- **Verdict**: **REVIEW** (5 / 9 PASS)

## 5. Suggested next

REVIEW (5 hypotheses PASS). Operator discussion required on the borderline hypotheses before proceeding to gate 5 or archiving.

