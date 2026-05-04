# Gate 4 — trend_rotation_d1 backtest principal Duk (2026-05-04T12:26:36Z)

Spec: `docs/strategies/trend_rotation_d1.md` (commit `889f18c`). Protocol gate 4 of `docs/STRATEGY_RESEARCH_PROTOCOL.md`.

**Anti-data-dredging**: the 10 hypotheses (§4 of the spec) and the train selection criteria (§3.2) are frozen at the spec commit and evaluated post-run, never tuned.

- **Verdict**: **ARCHIVE** (0 / 0 hypotheses PASS)
- **Wallclock**: 8.6 s

## 1. Train grid (8 cells)

Window: 2019-12-22 → 2024-12-31. Selection: ``n_closed >= 50`` AND ``ci_low >= 0`` AND ``temporal_concentration < 0.4``; among those, max ``vs_buy_and_hold.strategy_minus_bh_pct`` (tie-break: max ``setups_per_month``).

| mom | K | rebal | n_closed | mean_r | CI low | CI high | win | setups/mo | tc | strategy − BH % | sel |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 63 | 3 | 10 | 156 | +0.596 | -0.352 | +1.771 | 45.5% | 2.56 | 0.888 | -1.3% |  |
| 63 | 3 | 21 | 109 | +0.579 | -0.848 | +2.362 | 45.0% | 1.79 | 1.343 | -7.2% |  |
| 63 | 4 | 10 | 203 | +0.391 | -0.474 | +1.380 | 43.8% | 3.33 | 1.375 | -4.0% |  |
| 63 | 4 | 21 | 146 | +0.323 | -0.987 | +1.847 | 40.4% | 2.39 | 2.581 | -10.3% |  |
| 126 | 3 | 10 | 106 | +1.338 | -0.016 | +2.882 | 56.6% | 1.74 | 0.420 | +8.3% |  |
| 126 | 3 | 21 | 73 | +1.588 | -0.459 | +3.834 | 53.4% | 1.20 | 0.458 | +3.2% |  |
| 126 | 4 | 10 | 138 | +1.064 | -0.150 | +2.366 | 51.4% | 2.26 | 0.456 | +9.3% |  |
| 126 | 4 | 21 | 101 | +1.019 | -0.726 | +2.809 | 52.5% | 1.66 | 0.820 | +0.6% |  |

**Selection**: no train cell met all three selection criteria (n_closed >= 50, ci_low >= 0, temporal_concentration < 0.4)

## 2. Holdout — selected cell

Window: 2025-01-01 → 2026-04-30. Selected cell run unchanged on the holdout.

Holdout was not run (no train cell selected).

## 3. Hypothesis evaluation (holdout — §4)

Holdout not evaluated (no cell selected).

## 4. Verdict

Verdict rule (spec §4 holdout):

- ≥ 6 PASS → **PROMOTE** (candidate gate 5)
- 3 ≤ PASS ≤ 5 → **REVIEW** (operator discussion)
- < 3 PASS → **ARCHIVE** (mandatory)

(H10 is gate-7-specific (top-K agreement Duk vs MT5) and deferred from the gate-4 count — ``pass=None`` excludes it from both numerator and denominator. Max gate-4 score is 9 / 9.)

- **Verdict**: **ARCHIVE** (0 / 0 PASS)

## 5. Suggested next

ARCHIVE. Move to ``archived/strategies/trend_rotation_d1_v1/`` with the post-mortem README per protocol §8 + update §11.4 with the transferable learnings.

