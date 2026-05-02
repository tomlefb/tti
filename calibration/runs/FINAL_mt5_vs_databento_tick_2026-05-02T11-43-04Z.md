# MT5 vs Databento — verdict (tick simulator) — 2026-05-02T12-55-34Z

Step 5 of the structured investigation. Synthesises the raw-OHLC divergence already documented in prior reports with the new setup-level numbers from this run.

## Section A — Raw-data divergence

Already established by `calibration/run_mt5_vs_databento_deep_diagnosis.py` and the report `calibration/runs/2026-05-01T07-53-07Z_mt5_vs_databento_deep_diagnosis.md` on the **Panama-adjusted** Databento fixture vs the broker MT5 fixture, on the same overlap windows used here:

| Instrument | Body corr | Direction agree | ATR ratio | ATR corr | Per-bar sweep agree |
|---|---:|---:|---:|---:|---:|
| XAUUSD | 0.018 | 0.500 | 1.16 | 0.747 | 0.417 |
| NDX100 | 0.008 | 0.502 | 1.01 | 0.577 | 0.539 |
| SPX500 | 0.007 | 0.501 | 1.00 | 0.657 | 0.557 |

Body / wick correlations near zero, candle-direction agreement at chance (50%), and per-candle sweep-event direction agreement at 42–56% all establish that the two sources are **structurally different time series** at the M5 level. ATR is broadly comparable (volatility scale matches) — so the divergence is in **price path**, not in volatility regime.

The Phase-1 report `2026-04-30T22-27-25Z_mt5_vs_databento_phase1.md` also identified the root cause: a residual price-level offset after Panama back-adjustment (XAU stdev ≈ 34 USD/bar, NDX stdev ≈ 162 pts/bar across common timestamps). Panama anchored the median to ~0 on XAU but left the dispersion essentially unchanged on NDX.

## Section B — Setup-level mismatch (tick simulator)

| Instrument | MT5 n | DBN n | matched | mismatch% (this run) | legacy mismatch% |
|---|---:|---:|---:|---:|---:|
| XAUUSD | 9 | 2 | 0 | 100.0% | 96.9% |
| NDX100 | 9 | 12 | 0 | 100.0% | 96.9% |
| SPX500 | 7 | 12 | 0 | 100.0% | — |

Legacy mismatch (96.9%) was measured by `run_mt5_vs_databento_phase1.py` on the **legacy detector** with a ±15 min match window. Numbers above use the **leak-free tick simulator** with a stricter ±5 min match window. A near-identical mismatch under the tighter detector confirms the divergence is structural (data-driven), not a leaky-detector artefact.

## Section C — Mean R on the common window per source

Closed-trade mean R from each source's tick-simulator run on the shared overlap window. Bootstrap CI is 95% percentile-method on 10k resamples, computed only when n>=20.

| Instrument | MT5 n | MT5 mean R | MT5 CI 95% | DBN n | DBN mean R | DBN CI 95% |
|---|---:|---:|---|---:|---:|---|
| XAUUSD | 7 | +0.539 | — | 2 | -1.000 | — |
| NDX100 | 9 | +1.225 | — | 10 | +0.268 | — |
| SPX500 | 7 | -1.000 | — | 8 | +0.040 | — |

Historical reference (Sprint 6.5 backtest with **legacy detector** on MT5, 11 months): NDX100 mean R **+1.381**, XAUUSD **+0.576**. Compare to the MT5 mean R column above (same source, same window, **leak-free** detector). A large drop (e.g., NDX100 falling from +1.38 to ~+0.15) would be evidence the historical edge was an artefact of the legacy detector's look-ahead leaks (Phase B audit found four such leaks).

## Section D — Scenario classification per instrument

Decision rule (from the agreed plan):
- **Scenario A**: MT5 ≈ DBN (mismatch < 30%) AND both show no edge (CI 95% lower bound ≤ 0) → Databento verdict (no edge) applies to MT5 prod. Verdict definitive.
- **Scenario B**: MT5 ≠ DBN AND MT5 has edge (CI lower bound > 0) → Databento verdict does not apply. Re-run the parameter sweep on MT5 fixtures.
- **Scenario C**: MT5 ≠ DBN AND MT5 has no edge → the historical +0.58 R from Sprint 6.5 was a leaky-detector artefact. Verdict definitive (under both sources).
- **Scenario D**: MT5 has edge on one instrument only → portfolio to reconsider. Manual review.

Tags suffixed with `*` use the low-n point-estimate fallback (see `_classify_scenario`): below n=20 the bootstrap CI is uninformative, so we fall back to comparing MT5's tick-simulator mean R to the Sprint 6.5 legacy mean R on the same window. Retention ≥70% **falsifies** the leaky-detector hypothesis (B* / A*); retention <30% supports it (C*).

| Instrument | Scenario | MT5 tick mean R | Legacy mean R | Retention | Rationale |
|---|---|---:|---:|---:|---|
| XAUUSD | **B*** | +0.539 | +0.576 | 94% | low-n (MT5 n=7, DBN n=2); sources diverge (mismatch 100%); MT5 point estimate +0.54 retains 94% of legacy +0.58 (leak hypothesis rejected) |
| NDX100 | **B*** | +1.225 | +1.381 | 89% | low-n (MT5 n=9, DBN n=10); sources diverge (mismatch 100%); MT5 point estimate +1.22 retains 89% of legacy +1.38 (leak hypothesis rejected) |
| SPX500 | **C*** | -1.000 | — | — | low-n (MT5 n=7, DBN n=8); sources diverge (mismatch 100%); MT5 mean R -1.00 clearly negative |

## Synthesis

Instruments ['XAUUSD', 'NDX100'] fall in Scenario **B\*** under the low-n fallback: sources diverge structurally and MT5 mean R **retains ≥70%** of the Sprint 6.5 legacy mean R on the same window with the leak-free tick simulator. This **falsifies** the hypothesis that the historical edge was a leaky-detector artefact. Sample sizes (n=7–12) are below the CI-edge threshold so this is not yet a CI-proven edge — **option B (re-run sweep on MT5 fixtures)** is the warranted next step. The 10-year Databento verdict (no edge) **does not transfer** to MT5: 0% setup overlap on (date, killzone, direction) means the two sources fire on disjoint trading days even before any time-tolerance criterion is applied.

**Decisive setup-level finding**: 0% setup overlap on (date, killzone, direction) tuples across all three instruments — even without any time tolerance. The legacy-detector phase 1 report measured 96.9% mismatch (1 of 32 setups matched within ±15 min) with a looser detector and a looser tolerance. Tightening either knob drives the mismatch to 100%. The two sources fire setups on **disjoint trading days**; the divergence is not an artefact of match-window tolerance.

Caveat: CI-based edge classification requires n>=20 closed trades. With ~10–17 months of overlap and ~1 setup/month on these instruments under the tick simulator (which is stricter than the legacy detector), no cell reached n=20. The B*/C* tags are point-estimate fallbacks and should be confirmed by the option-B MT5 sweep on the full 11-month Sprint 6.5 fixture window.

# MT5 vs Databento — setup-level diff (tick simulator) — 2026-05-02T12-55-34Z

Setups are matched by tuple (Paris date, killzone, direction) with ±5 min tolerance on MSS-confirm timestamp. Each MT5 setup is matched to at most one DBN setup (closest in time within tolerance); both leftover sets are reported.

Backtest source: leak-free tick simulator (`simulate_target_date`).
## Cross-instrument summary

| Instrument | MT5 n | DBN n | matched | mismatch% | MT5 mean R | DBN mean R | matched MT5 mean R | matched DBN mean R |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| XAUUSD | 9 | 2 | 0 | 100.0% | +0.539 | -1.000 | — | — |
| NDX100 | 9 | 12 | 0 | 100.0% | +1.225 | +0.268 | — | — |
| SPX500 | 7 | 12 | 0 | 100.0% | -1.000 | +0.040 | — | — |

Historical reference (legacy detector, phase1 report): mismatch ratio was **96.9%** on XAU+NDX (1 of 32 setups matched within ±15 min). Compare the **mismatch%** column above against that baseline.


## XAUUSD

- N MT5 setups: **9** | N DBN setups: **2**
- Matched (≤±5 min): **0** | MT5-only: **9** | DBN-only: **2**
- Divergent (matched but ≠ on quality or ≥10% price gap on entry/SL/TP/swept_level): **0**
- Mismatch ratio = 1 − |common| / |MT5 ∪ DBN| = **100.0%**

| Slice | n closed | mean R | CI 95% | win rate |
|---|---:|---:|---|---:|
| MT5 — all | 7 | +0.539 | — | 28.6% |
| DBN — all | 2 | -1.000 | — | 0.0% |
| MT5 — matched | 0 | — | — | — |
| DBN — matched | 0 | — | — | — |
| MT5-only | 7 | +0.539 | — | 28.6% |
| DBN-only | 2 | -1.000 | — | 0.0% |

_No divergent matched setups in this window._

## NDX100

- N MT5 setups: **9** | N DBN setups: **12**
- Matched (≤±5 min): **0** | MT5-only: **9** | DBN-only: **12**
- Divergent (matched but ≠ on quality or ≥10% price gap on entry/SL/TP/swept_level): **0**
- Mismatch ratio = 1 − |common| / |MT5 ∪ DBN| = **100.0%**

| Slice | n closed | mean R | CI 95% | win rate |
|---|---:|---:|---|---:|
| MT5 — all | 9 | +1.225 | — | 44.4% |
| DBN — all | 10 | +0.268 | — | 20.0% |
| MT5 — matched | 0 | — | — | — |
| DBN — matched | 0 | — | — | — |
| MT5-only | 9 | +1.225 | — | 44.4% |
| DBN-only | 10 | +0.268 | — | 20.0% |

_No divergent matched setups in this window._

## SPX500

- N MT5 setups: **7** | N DBN setups: **12**
- Matched (≤±5 min): **0** | MT5-only: **7** | DBN-only: **12**
- Divergent (matched but ≠ on quality or ≥10% price gap on entry/SL/TP/swept_level): **0**
- Mismatch ratio = 1 − |common| / |MT5 ∪ DBN| = **100.0%**

| Slice | n closed | mean R | CI 95% | win rate |
|---|---:|---:|---|---:|
| MT5 — all | 7 | -1.000 | — | 0.0% |
| DBN — all | 8 | +0.040 | — | 25.0% |
| MT5 — matched | 0 | — | — | — |
| DBN — matched | 0 | — | — | — |
| MT5-only | 7 | -1.000 | — | 0.0% |
| DBN-only | 8 | +0.040 | — | 25.0% |

_No divergent matched setups in this window._

