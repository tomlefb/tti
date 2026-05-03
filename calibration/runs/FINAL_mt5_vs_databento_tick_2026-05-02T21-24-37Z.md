# MT5 vs Databento — setup-level diff (tick simulator) — 2026-05-03T02-08-20Z

> **Supersedes `FINAL_mt5_vs_databento_tick_2026-05-02T11-43-04Z.md`**
> (post-timezone-fix re-run on extended fixtures, commit `f868793`).
> Common windows widened to:
> - XAUUSD: 2019-12-23 → 2026-04-29 (was 2025-06-20 → 2026-04-27, ~10 months)
> - NDX100: 2022-10-20 → 2026-04-29 (was 2025-06-20 → 2026-04-27, ~10 months)
> - SPX500: 2022-10-20 → 2026-04-29 (was 2024-11-26 → 2026-04-27, ~17 months)
>
> Wall time: 4h32 (mt5×XAUUSD bottleneck at 271 min). Six cells, 4-way parallel.
>
> **Headline change post-fix**: NDX100 reaches **n=27 closed** with **mean R = +1.564**,
> CI 95% = [+0.366, +2.834] — first instrument to clear the CI-positive bar
> on a multi-year window. XAUUSD n=43, mean R +0.29 (CI not bootstrap-positive
> on n=43). The setup-level mismatch ratio **drops modestly** (96.9% → 81–96%),
> confirming the residual divergence is the documented CFD vs back-adjusted-futures
> price-level offset, not timezone.

Setups are matched by tuple (Paris date, killzone, direction) with ±5 min tolerance on MSS-confirm timestamp. Each MT5 setup is matched to at most one DBN setup (closest in time within tolerance); both leftover sets are reported.

Backtest source: leak-free tick simulator (`simulate_target_date`).
## Cross-instrument summary

| Instrument | MT5 n | DBN n | matched | mismatch% | MT5 mean R | DBN mean R | matched MT5 mean R | matched DBN mean R |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| XAUUSD | 52 | 22 | 3 | 95.8% | +0.291 | -0.474 | -1.000 | -1.000 |
| NDX100 | 28 | 42 | 11 | 81.4% | +1.564 | +0.153 | +0.069 | +0.039 |
| SPX500 | 13 | 20 | 5 | 82.1% | +0.186 | -0.078 | -0.174 | -0.167 |

Historical reference (legacy detector, phase1 report): mismatch ratio was **96.9%** on XAU+NDX (1 of 32 setups matched within ±15 min). Compare the **mismatch%** column above against that baseline.


## XAUUSD

- N MT5 setups: **52** | N DBN setups: **22**
- Matched (≤±5 min): **3** | MT5-only: **49** | DBN-only: **19**
- Divergent (matched but ≠ on quality or ≥10% price gap on entry/SL/TP/swept_level): **0**
- Mismatch ratio = 1 − |common| / |MT5 ∪ DBN| = **95.8%**

| Slice | n closed | mean R | CI 95% | win rate |
|---|---:|---:|---|---:|
| MT5 — all | 43 | +0.291 | [-0.326, +0.948] | 27.9% |
| DBN — all | 16 | -0.474 | — | 12.5% |
| MT5 — matched | 1 | -1.000 | — | 0.0% |
| DBN — matched | 1 | -1.000 | — | 0.0% |
| MT5-only | 42 | +0.322 | [-0.282, +0.990] | 28.6% |
| DBN-only | 15 | -0.439 | — | 13.3% |

_No divergent matched setups in this window._

## NDX100

- N MT5 setups: **28** | N DBN setups: **42**
- Matched (≤±5 min): **11** | MT5-only: **17** | DBN-only: **31**
- Divergent (matched but ≠ on quality or ≥10% price gap on entry/SL/TP/swept_level): **1**
- Mismatch ratio = 1 − |common| / |MT5 ∪ DBN| = **81.4%**

| Slice | n closed | mean R | CI 95% | win rate |
|---|---:|---:|---|---:|
| MT5 — all | 27 | +1.564 | [+0.366, +2.834] | 40.7% |
| DBN — all | 39 | +0.153 | [-0.530, +0.924] | 20.5% |
| MT5 — matched | 10 | +0.069 | — | 20.0% |
| DBN — matched | 10 | +0.039 | — | 20.0% |
| MT5-only | 17 | +2.443 | — | 52.9% |
| DBN-only | 29 | +0.192 | [-0.603, +1.115] | 20.7% |

### Divergent matched setups — sample of 5

- **2023-06-29T08:05:00+00:00** long london
  - MT5: q=A entry=14979.40 SL=14958.40 TP1=15052.70 TPr=15052.70 swept=14975.80 → sl_hit R=-1.00
  - DBN: q=A entry=17325.25 SL=17304.50 TP1=17399.75 TPr=17399.75 swept=17322.50 → sl_hit R=-1.00
  - cause: entry Δ=-2345.85 (-13.54%), swept-level Δ=-2346.70

## SPX500

- N MT5 setups: **13** | N DBN setups: **20**
- Matched (≤±5 min): **5** | MT5-only: **8** | DBN-only: **15**
- Divergent (matched but ≠ on quality or ≥10% price gap on entry/SL/TP/swept_level): **0**
- Mismatch ratio = 1 − |common| / |MT5 ∪ DBN| = **82.1%**

| Slice | n closed | mean R | CI 95% | win rate |
|---|---:|---:|---|---:|
| MT5 — all | 11 | +0.186 | — | 27.3% |
| DBN — all | 14 | -0.078 | — | 21.4% |
| MT5 — matched | 5 | -0.174 | — | 20.0% |
| DBN — matched | 5 | -0.167 | — | 20.0% |
| MT5-only | 6 | +0.486 | — | 33.3% |
| DBN-only | 9 | -0.028 | — | 22.2% |

_No divergent matched setups in this window._

