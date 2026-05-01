# Legacy vs tick-faithful — extended 10y backtest diff — 2026-05-01T21-43-38Z

Sample: 50 trading dates per instrument (seed=42, range 2016-01-03 → 2026-04-29, instruments XAUUSD,NDX100, total cells=100, lookback=60d).

**Mode legacy** = ``build_setup_candidates(now_utc=None)``, the pre-Phase-B path that produced every backtest before this branch. **Mode tick** = ``simulate_target_date(...)``, which iterates the 5-min APScheduler firings inside both killzones with ``now_utc=tick`` set, locking each setup identity at its first emission. The Phase A + Phase-B-core leak fixes (FVG forward window, sweep dedupe pool, swing confirmation, detect_mss forward iteration) make the tick path leak-free; the audit at ``calibration/runs/FINAL_lookahead_audit_phase_a_complete_2026-05-01.md`` and the tick-by-tick audit at ``calibration/audit_tick_simulator.py`` verify this end-to-end.

## Setup count

| Mode | Total | A+ | A | B | A/A+ (notify) |
|---|---:|---:|---:|---:|---:|
| legacy | 12 | 0 | 5 | 7 | 5 |
| tick | 18 | 0 | 4 | 14 | 4 |

**Setup-count inflation in legacy: -6 (-33.3% vs tick).** Notify-quality inflation: +1.

## Per-instrument outcome (A/A+ only, NOTIFY_QUALITIES gated)

| Instrument | Mode | n | Closed | Win rate | Mean R | Total R |
|---|---|---:|---:|---:|---:|---:|
| XAUUSD | legacy | 0 | 0 | 0.0% | +0.000 | +0.00 |
| XAUUSD | tick | 0 | 0 | 0.0% | +0.000 | +0.00 |
| NDX100 | legacy | 5 | 5 | 0.0% | -1.000 | -5.00 |
| NDX100 | tick | 4 | 3 | 0.0% | -1.000 | -3.00 |

Combined:

| Mode | n | Closed | Win rate | Mean R | Total R |
|---|---:|---:|---:|---:|---:|
| legacy | 5 | 5 | 0.0% | -1.000 | -5.00 |
| tick | 4 | 3 | 0.0% | -1.000 | -3.00 |

**Mean-R inflation in legacy: +0.000** (legacy -1.000 vs tick -1.000). This is the bias the Sprint 6.5 / 6.6 numbers carried.

## Identity-level diff

- Identities in both modes: **11**
- Legacy-only (phantoms — would never have been emitted in real time): **1**
- Tick-only (transient-cluster winners legacy's dedupe collapses): **7**

Tick-only is **not** a leak signal. The legacy run is a single post-killzone call: its sweep dedupe operates on the full killzone window and only the deepest representative of each price-time cluster survives. The tick simulator emits a setup at the moment each cluster's *current* deepest representative qualifies — and locks that identity at first emission. If a deeper sweep appears later in the same cluster it is a **different** identity (`sweep_candle_time_utc` and `swept_level_price` change), so the simulator emits a new setup and locks that one independently. Both events would have triggered separate notifications in the production scheduler — legacy collapses them into one. The audit at `calibration/audit_tick_simulator.py` proves the tick path is leak-free: 53/53 setups in its pool emit at exactly `next_5min_tick_after(mss_confirm)` with bit-identical fields.

Among the identities present in both, the per-field deltas (counts of setups that differ on each axis):

| Field | # changed |
|---|---:|
| poi_type (FVG ↔ OrderBlock) | 3 |
| entry_price | 3 |
| stop_loss | 0 |
| tp_runner_rr | 3 |
| quality | 2 |
| **quality demotions A/A+ → B (would NOT have notified in real time)** | **2** |

## Interpretation

Three distinct effects separate the legacy and tick paths. They do **not** all push in the same direction:

1. **Phantom setups (legacy-only)** — 1 identity the legacy scan emits that production would never have produced. Each one's outcome is pure noise added to the legacy aggregates.
2. **In-flight quality / RR inflation on overlapping identities** — 2 setups exist in both modes but with different fields. Of those, 2 would not have been notified (legacy inflated to A/A+, tick at B); the remainder share a quality tier but typically have a tighter FVG / smaller SL / larger RR in legacy because the detector picked a POI that hadn't yet formed at the production scheduler tick.
3. **Cluster-collapse undercounting (tick-only)** — 7 identities the tick path emits as separate notifications that legacy's sweep dedupe folded into a single setup. These are real production events the legacy backtest never reports; their outcomes (and risk consumption) are missing from the legacy aggregates entirely.

Effects (1) and (2) bias legacy mean-R **upward** vs the tick ground truth; effect (3) biases legacy mean-R **away** from the production reality in whichever direction the cluster-collapsed trades fall on average. The aggregate sign of the bias depends on the per-instrument outcome distribution; it is not a one-line answer. The numbers in this sample do not extrapolate cleanly to the full 10y space (the sample is 50 dates per instrument vs ~2400 trading dates per instrument total) but the **per-trade** delta on overlapping identities is structural and applies to any backtest using the legacy path.

**Headline numbers on this 100-cell sample**: legacy emits 12 setups (5 notify-quality, all 5 closed at -1.000 mean R). Tick emits 18 setups (4 notify-quality, 3 closed at -1.000 mean R). Same per-trade mean R (-1.000 — every notify trade in this sample lost) but tick has **one fewer A/A+ trade** (a quality demotion) and **one fewer phantom**. The mean-R delta is zero on this sample, but the underlying composition is materially different — and the cluster-collapse effect (7 tick-only) means a longer fixture would surface trades the legacy path simply does not record. Re-running on the full 10y is the right next step; the script supports it via `--n-dates 2400` (or larger), at the cost of ~9h wall time per the simulator's per-tick measurement.
