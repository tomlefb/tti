# Timestamp alignment check — MT5 vs Databento — 2026-05-02T13-15-02Z

Tests whether MT5 and Databento M5 candles are aligned on the same UTC minute, or whether a broker-time vs exchange-time offset is polluting the comparison. 10 random NDX100 timestamps from the common window (seed=42).

## Per-sample candle comparison

| Timestamp UTC | MT5 close | DBN close | Δ close | Δ % | MT5 body | DBN body | same dir |
|---|---:|---:|---:|---:|---:|---:|:---:|
| 2025-06-30T19:50:00+00:00 | 22639.96 | 23116.00 | +476.04 | +2.103% | -1.65 | +32.50 | ✗ |
| 2025-07-28T20:45:00+00:00 | 23334.66 | 23694.25 | +359.59 | +1.541% | -8.60 | +0.00 | ✗ |
| 2025-07-31T03:00:00+00:00 | 23587.16 | 23968.00 | +380.84 | +1.615% | -0.10 | -4.50 | ✓ |
| 2025-08-11T07:00:00+00:00 | 23645.16 | 23946.00 | +300.84 | +1.272% | -2.30 | +0.25 | ✗ |
| 2025-09-09T16:00:00+00:00 | 23772.26 | 23984.25 | +211.99 | +0.892% | -32.40 | +13.75 | ✗ |
| 2025-09-17T04:25:00+00:00 | 24289.26 | 24471.00 | +181.74 | +0.748% | +0.40 | +1.00 | ✓ |
| 2025-09-26T17:00:00+00:00 | 24443.26 | 24645.75 | +202.49 | +0.828% | -32.10 | -4.50 | ✓ |
| 2026-02-06T18:05:00+00:00 | 24873.31 | 25044.50 | +171.19 | +0.688% | +22.12 | -20.00 | ✗ |
| 2026-03-13T08:05:00+00:00 | 24552.77 | 24353.00 | -199.77 | -0.814% | -3.37 | -13.75 | ✓ |
| 2026-03-23T11:25:00+00:00 | 23683.50 | 24722.25 | +1038.75 | +4.386% | -7.12 | -87.25 | ✓ |

## Aggregate

- Mean Δ close: **+312.37** | median: +256.42 | stdev: 297.14
- Mean Δ %    : **+1.326%** | median: +1.082%
- Direction agreement on same UTC minute: **50%**

## Cross-correlation of close-to-close returns across lags

On 52174 common UTC minutes (full overlap window, not just the 10-sample probe). A timezone offset would surface as a correlation peak at lag ≠ 0; a peak at lag 0 confirms the two sources are aligned on UTC and the Δ close magnitude is a level-only offset.

| Lag (M5 bars) | Lag (min) | Pearson r |
|---:|---:|---:|
| -12 | -60 | +0.0245 |
| -11 | -55 | -0.0030 |
| -10 | -50 | -0.0048 |
| -9 | -45 | +0.0012 |
| -8 | -40 | -0.0069 |
| -7 | -35 | -0.0020 |
| -6 | -30 | +0.0032 |
| -5 | -25 | +0.0002 |
| -4 | -20 | +0.0067 |
| -3 | -15 | +0.0161 |
| -2 | -10 | +0.0042 |
| -1 | -5 | +0.0079 |
| 0 | +0 | +0.0600 |
| 1 | +5 | +0.0089 |
| 2 | +10 | +0.0052 |
| 3 | +15 | -0.0041 |
| 4 | +20 | +0.0028 |
| 5 | +25 | +0.0045 |
| 6 | +30 | +0.0162 |
| 7 | +35 | -0.0020 |
| 8 | +40 | +0.0039 |
| 9 | +45 | -0.0061 |
| 10 | +50 | +0.0051 |
| 11 | +55 | +0.0007 |
| 12 | +60 | -0.0125 |

- Lag 0 correlation: **+0.0600**
- Peak lag: **0** (+0 min) | peak r = +0.0600

**Verdict — alignment OK but coupling is weak**: lag 0 is the peak but its r (+0.060) is low. The two sources are aligned in time but their micro-structure is genuinely different. Consistent with the deep_diagnosis report's body correlation ≈ 0 finding.

# Mismatch detail — 5 MT5 NDX setups vs Databento — 2026-05-02T13-15-02Z

For each sampled MT5 NDX setup, what did Databento see at the same timestamp? Goal: identify whether the 100% mismatch is data-driven (DBN's chain of bias / sweep / MSS / FVG simply did not fire on this date because the price path was different) or analysis-driven.

## Case 1 — 2025-08-08 london long

**MT5 setup**: ts=2025-08-08T09:25:00+00:00 | direction=long | quality=A | swept=23411.86 | entry=23435.46 | SL=23395.16 | TP1=23559.96 | TPr=23559.96 | R=+3.09 | outcome=tp_runner_hit

**DBN candle at same UTC minute**: O=23760.75 H=23760.75 L=23749.25 C=23754.75
  - Δ close vs MT5 same minute: +309.69 pts (+1.321%)

**DBN setups on 2025-08-08 london**: NONE — DBN's bias/sweep/MSS chain did not fire any setup in this killzone on this date.

**24h H1 close drift before setup**: MT5 +34.10 | DBN -55.00 | same direction: ✗

**Cause probable**: DBN emitted no setup in this killzone — pre-MSS chain didn't form · large price-level gap at minute T (+310 pts)

## Case 2 — 2025-08-21 london short

**MT5 setup**: ts=2025-08-21T08:10:00+00:00 | direction=short | quality=A | swept=23262.76 | entry=23246.56 | SL=23274.86 | TP1=23105.06 | TPr=22954.06 | R=-1.00 | outcome=sl_hit

**DBN candle at same UTC minute**: O=23538.50 H=23538.75 L=23517.25 C=23531.50
  - Δ close vs MT5 same minute: +286.74 pts (+1.234%)

**DBN setups on 2025-08-21 london**: NONE — DBN's bias/sweep/MSS chain did not fire any setup in this killzone on this date.

**24h H1 close drift before setup**: MT5 -47.00 | DBN -134.25 | same direction: ✓

**Cause probable**: DBN emitted no setup in this killzone — pre-MSS chain didn't form · large price-level gap at minute T (+287 pts)

## Case 3 — 2025-11-07 london short

**MT5 setup**: ts=2025-11-07T10:20:00+00:00 | direction=short | quality=A | swept=25234.76 | entry=25218.56 | SL=25247.16 | TP1=25085.66 | TPr=25085.66 | R=+4.65 | outcome=tp_runner_hit

**DBN candle at same UTC minute**: O=25161.50 H=25177.75 L=25144.50 C=25151.50
  - Δ close vs MT5 same minute: -61.56 pts (-0.244%)

**DBN setups on 2025-11-07 london**: NONE — DBN's bias/sweep/MSS chain did not fire any setup in this killzone on this date.

**24h H1 close drift before setup**: MT5 -402.70 | DBN -528.25 | same direction: ✓

**Cause probable**: DBN emitted no setup in this killzone — pre-MSS chain didn't form

## Case 4 — 2026-03-09 ny short

**MT5 setup**: ts=2026-03-09T15:40:00+00:00 | direction=short | quality=A+ | swept=24425.56 | entry=24384.73 | SL=24437.23 | TP1=24122.23 | TPr=23977.77 | R=-1.00 | outcome=sl_hit

**DBN candle at same UTC minute**: O=24548.50 H=24567.00 L=24538.00 C=24553.00
  - Δ close vs MT5 same minute: +211.39 pts (+0.868%)

**DBN setups on 2026-03-09 ny**: NONE — DBN's bias/sweep/MSS chain did not fire any setup in this killzone on this date.

**24h H1 close drift before setup**: MT5 +75.96 | DBN +336.25 | same direction: ✓

**Cause probable**: DBN emitted no setup in this killzone — pre-MSS chain didn't form · large price-level gap at minute T (+211 pts)

## Case 5 — 2026-04-09 ny long

**MT5 setup**: ts=2026-04-09T14:40:00+00:00 | direction=long | quality=A | swept=24827.04 | entry=24840.01 | SL=24799.76 | TP1=24982.15 | TPr=24982.15 | R=-1.00 | outcome=sl_hit

**DBN candle at same UTC minute**: O=25209.75 H=25219.00 L=25203.25 C=25210.25
  - Δ close vs MT5 same minute: +356.99 pts (+1.436%)

**DBN setups on 2026-04-09 ny**: NONE — DBN's bias/sweep/MSS chain did not fire any setup in this killzone on this date.

**24h H1 close drift before setup**: MT5 -184.90 | DBN -43.75 | same direction: ✓

**Cause probable**: DBN emitted no setup in this killzone — pre-MSS chain didn't form · large price-level gap at minute T (+357 pts)

# Cell stats — explicit N + CI per (source, instrument) — 2026-05-02T13-15-02Z

Re-aggregated from prior run `mt5_vs_databento_tick_2026-05-02T11-43-04Z` BacktestResult JSONs without re-running the backtest. CI is bootstrap 95% percentile-method, 10k resamples, seed=42 (taken from the JSON field `mean_r_ci_95`).

**Edge-defensibility rule** (from operator spec): a cell shows a CI-defensible edge if and only if `n_closed >= 20` AND CI lower bound > 0. Below n=20 the bootstrap CI is wide and uninformative — those cells are flagged `inconclusive`.

| Source | Inst | Period | n total | n closed | mean R | CI 95% | win rate | setups/mo | edge? |
|---|---|---|---:|---:|---:|---|---:|---:|---|
| mt5 | XAUUSD | 2025-06-20→2026-04-27 | 9 | 7 | +0.539 | [-1.000, +2.371] | 28.6% | 0.82 | 🚧 inconclusive (n<20) |
| mt5 | NDX100 | 2025-06-20→2026-04-27 | 9 | 9 | +1.225 | [-0.419, +2.929] | 44.4% | 0.82 | 🚧 inconclusive (n<20) |
| mt5 | SPX500 | 2024-11-26→2026-04-27 | 7 | 7 | -1.000 | [-1.000, -1.000] | 0.0% | 0.39 | 🚧 inconclusive (n<20) |
| dbn | XAUUSD | 2025-06-20→2026-04-27 | 2 | 2 | -1.000 | [-1.000, -1.000] | 0.0% | 0.18 | 🚧 inconclusive (n<20) |
| dbn | NDX100 | 2025-06-20→2026-04-27 | 12 | 10 | +0.268 | [-1.000, +2.085] | 20.0% | 1.09 | 🚧 inconclusive (n<20) |
| dbn | SPX500 | 2024-11-26→2026-04-27 | 12 | 8 | +0.040 | [-1.000, +1.597] | 25.0% | 0.67 | 🚧 inconclusive (n<20) |

## Verdict on edge defensibility

- CI-defensible cells: **0 / 6**
- Inconclusive (n<20) cells: **6 / 6**

**No cell reaches n=20.** The +1.225 / +0.539 mean R numbers for MT5 NDX / XAU under the tick simulator are point estimates with wide bootstrap CIs (lower bound below zero). They are **suggestive** of a surviving edge (89-94% retention vs the Sprint 6.5 legacy mean R) but **not statistically defensible** on this sample. Larger n is required to convert the suggestion into a proven edge.

Two ways to grow n: (a) extend the MT5 fixture window beyond the current ~10–17 months — most retail brokers retain at least 1y of M5 history, possibly more; (b) run the parameter sweep (`baseline_tjr_variants.py`) on MT5 fixtures and aggregate across variants if and only if the variants are interpretable as parameter-perturbation neighbours of the operator-validated baseline (in which case pooled n grows but the strategy under test is the variant family, not a single setting).

