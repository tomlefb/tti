# Attrition diagnostic — mean_reversion_bb_h4 (2026-05-03T22-32-57Z)

- Instrument: NDX100; window: train (2020-01-01 → 2024-12-31, ≈60.0 months).
- Spec defaults (StrategyParams): bb_period=20, bb_multiplier=2.0, atr_period=14, min_penetration_atr_mult=0.3, max_return_bars=3, sl_buffer=5.0 (NDX median §3.2), min_rr=1.0, max_risk_distance=1e9.
- Read-only diagnostic; no spec / code change.

## 1. Baseline attrition (spec defaults)

| Stage | N | Retention vs prev | Retention vs killzone |
|---|---:|---:|---:|
| Total H4 bars | 7998 | — | — |
| In-killzone | 3872 | 48.4% | 100.0% |
| Excess (close pierces BB) | 376 | 9.7% | 9.7% |
| Pen filter pass | 187 | 49.7% | 4.8% |
| Exhaustion filter pass | 7 | 3.7% | 0.2% |
| Return found in window | 2 | 28.6% | 0.1% |
| build_setup non-degenerate | 2 | 100.0% | 0.1% |
| min_rr pass | 1 | 50.0% | 0.0% |
| max_risk_distance pass | 1 | 100.0% | 0.0% |

Final setups: **1** (0.017/month — target H1 = 3–5/month).

## 2. Sensitivity — min_penetration_atr_mult

| pen_mult | excess | pen pass | exhaust pass | return | setup | min_rr | max_risk | per month |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.0 | 376 | 376 | 33 | 17 | 15 | 10 | 10 | 0.17 |
| 0.1 | 376 | 297 | 19 | 8 | 8 | 5 | 5 | 0.08 |
| 0.2 | 376 | 229 | 11 | 4 | 4 | 3 | 3 | 0.05 |
| 0.3 | 376 | 187 | 7 | 2 | 2 | 1 | 1 | 0.02 |
| 0.5 | 376 | 117 | 3 | 0 | 0 | 0 | 0 | 0.00 |

## 3. Sensitivity — max_return_bars

| max_return_bars | excess | pen | exhaust | return | setup | min_rr | max_risk | per month |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 3 | 376 | 187 | 7 | 2 | 2 | 1 | 1 | 0.02 |
| 5 | 376 | 187 | 7 | 6 | 5 | 3 | 3 | 0.05 |
| 8 | 376 | 187 | 7 | 7 | 6 | 3 | 3 | 0.05 |
| 12 | 376 | 187 | 7 | 7 | 6 | 3 | 3 | 0.05 |

## 4. Sensitivity — exhaustion candle filter

| exhaustion | exhaust pass | return | setup | min_rr | max_risk | per month |
|---|---:|---:|---:|---:|---:|---:|
| active | 7 | 2 | 2 | 1 | 1 | 0.02 |
| disabled | 187 | 29 | 28 | 23 | 23 | 0.38 |

## 5. Combined relaxation (pen=0.0 + exhaustion off)

setups_final = **68** (1.13/month)

| Stage | N | Retention vs prev | Retention vs killzone |
|---|---:|---:|---:|
| Total H4 bars | 7998 | — | — |
| In-killzone | 3872 | 48.4% | 100.0% |
| Excess (close pierces BB) | 376 | 9.7% | 9.7% |
| Pen filter pass | 376 | 100.0% | 9.7% |
| Exhaustion filter pass | 376 | 100.0% | 9.7% |
| Return found in window | 91 | 24.2% | 2.4% |
| build_setup non-degenerate | 84 | 92.3% | 2.2% |
| min_rr pass | 68 | 81.0% | 1.8% |
| max_risk_distance pass | 68 | 100.0% | 1.8% |

## 6. Bottleneck ranking (retention vs prev step)

| Rank | Step | Retention | Comment |
|---:|---|---:|---|
| 1 | Exhaustion filter | 3.7% | **bottleneck** |
| 2 | Excess vs killzone | 9.7% | **bottleneck** |
| 3 | Return found | 28.6% |  |
| 4 | In-killzone vs total | 48.4% |  |
| 5 | Pen filter | 49.7% |  |
| 6 | min_rr pass | 50.0% |  |
| 7 | Build setup | 100.0% |  |
| 8 | max_risk pass | 100.0% |  |

## 7. Localisation summary

- Steepest single-step drop: **Exhaustion filter** (3.7% retention vs the previous gate).
- Disabling the penetration filter (pen=0.0) takes the final count from 1 to 10.
- Disabling the exhaustion filter takes the final count from 1 to 23.
- Combined relaxation (pen=0.0 + exhaustion off): 68 setups (1.13/month).

Cadence vs target H1 (3–5/month): the baseline produces **0.017/month** — 180× below the lower bound.

## 8. Wallclock

- Total: 2.8 s

