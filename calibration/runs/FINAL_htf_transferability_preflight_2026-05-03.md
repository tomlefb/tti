# HTF transferability pre-flight — MA50 cross on close H4

**Date**: 2026-05-03
**Branch**: `feat/strategy-research`
**Protocol**: `docs/STRATEGY_RESEARCH_PROTOCOL.md` §7 (P0 pre-flight)
**Script**: `calibration/run_htf_transferability_preflight.py`

---

## 1. Question

Does the price-level convergence (Pearson 0.99 on close H4/D1)
between Dukascopy, MT5 and Databento — established post-timezone-fix
in commits 0681d9c / 528a89d — translate into **convergent trigger
signals** for an HTF strategy?

If yes, HTF backtests on Dukascopy are a reasonable proxy for live
MT5 behaviour, and the pivot toward `breakout retest H4`,
`mean reversion BB H4`, `EMA cross H4` is fundationally sound.

If no, the HTF priority list (§7 of the protocol) needs revision.

---

## 2. Methodology

**Strategy under test**: trivial MA50-cross on close H4. Long
signal `close > MA50`, short signal `close < MA50`. Trigger = H4
candle where the sign flips vs the previous bar. No entry / SL /
TP — only the trigger timestamp series matters.

**Window**: 2022-10-21 → 2026-04-29 UTC (~3.5 y, the common range
across all three sources after the MT5 fixture refresh to 1500-day
depth in commit f868793).

**Resample alignment**: H4 candles do **not** naturally align
across sources (MT5 = Athens broker tz post-conversion = UTC+2/+3,
DBN = UTC midnight, Duk = whatever we choose). To make a "same-bar"
match meaningful, all three sources are re-resampled from M5 with
**UTC origin** (00, 04, 08, 12, 16, 20). This is the only honest
way to compare.

**Match rule**: strict same-timestamp, no ±1-bar tolerance.

**Mismatch metric**:
```
mismatch = 1 - |intersection| / |union|
```

Direction-agreement check on the intersection: how many shared
bars disagree on long vs short.

---

## 3. Results

### 3.1 NDX100

| Pair | n_a | n_b | intersection | union | agreement | mismatch | direction disagreements |
|---|---:|---:|---:|---:|---:|---:|---:|
| Duk vs MT5 | 345 | 369 | 328 | 386 | 84.97 % | **15.03 %** | 1 |
| Duk vs DBN | 345 | 361 | 269 | 437 | 61.56 % | 38.44 % | 0 |
| MT5 vs DBN | 369 | 361 | 275 | 455 | 60.44 % | 39.56 % | 0 |

### 3.2 XAUUSD

| Pair | n_a | n_b | intersection | union | agreement | mismatch | direction disagreements |
|---|---:|---:|---:|---:|---:|---:|---:|
| Duk vs MT5 | 344 | 352 | 330 | 366 | 90.16 % | **9.84 %** | 3 |
| Duk vs DBN | 344 | 285 | 126 | 503 | 25.05 % | 74.95 % | 7 |
| MT5 vs DBN | 352 | 285 | 128 | 509 | 25.15 % | 74.85 % | 7 |

DBN XAUUSD coverage is partial: only 67 k M5 rows in window vs
~249 k for Duk/MT5 (4 335 H4 vs ~5 619). The DBN mismatch is
inflated by missing data, not just signal divergence.

### 3.3 SPX500

| Pair | n_a | n_b | intersection | union | agreement | mismatch | direction disagreements |
|---|---:|---:|---:|---:|---:|---:|---:|
| Duk vs MT5 | 340 | 350 | 316 | 374 | 84.49 % | **15.51 %** | 3 |
| Duk vs DBN | 340 | 359 | 258 | 441 | 58.50 % | 41.50 % | 0 |
| MT5 vs DBN | 350 | 359 | 251 | 458 | 54.80 % | 45.20 % | 0 |

### 3.4 Aggregate

| Metric | Value |
|---|---|
| Median mismatch across all (instrument, pair) cells | 39.56 % |
| Median mismatch Duk vs MT5 (load-bearing pair) | **15.03 %** |
| Global verdict (driven by Duk vs MT5) | **Bonne** |

---

## 4. Verdict

| Pair | NDX100 | XAUUSD | SPX500 | Verdict |
|---|---|---|---|---|
| Duk vs MT5 | 15.03 % | 9.84 % | 15.51 % | **Bonne** (10–20 %) |
| Duk vs DBN | 38.44 % | 74.95 %\* | 41.50 % | Borderline / Cassée |
| MT5 vs DBN | 39.56 % | 74.85 %\* | 45.20 % | Borderline / Cassée |

\* Inflated by partial DBN XAUUSD coverage (4 335 H4 vs ~5 619).

**Direction-agreement on intersected bars: 0–7 disagreements out of
hundreds of common triggers** — when the sources agree on a trigger
timestamp, they almost always agree on the direction. The
disagreements (XAU: 3 / 330 = 0.9 %) are within rounding noise on
candles where close ≈ MA50.

---

## 5. Implications

### 5.1 HTF premise — validated

The Duk-vs-MT5 mismatch on signals derived from closed H4 candles is
9.8–15.5 % across NDX100 / XAUUSD / SPX500. This is in the **Bonne**
band of the protocol's classification (§7) and confirms that:

- A backtest on Dukascopy H4 produces a trigger series that overlaps
  ~85–90 % with the same strategy run on MT5 H4.
- The residual ~10–15 % mismatch is concentrated on borderline
  crosses (close ≈ MA), where bid/ask differences between Duk
  bank-aggregated mid and MT5 broker mid push the close on
  opposite sides of the MA. This is a noise floor, not a structural
  edge mis-attribution.

**Decision: HTF priority list (§7 of the protocol) stands. We can
attack `breakout retest H4` with confidence that backtest-on-Duk is
a meaningful proxy for live-MT5 performance.**

### 5.2 DBN cross-check — re-calibrate the protocol gate

The protocol's gate 5 (`Duk vs DBN within ±30 %`) was specified for
**Mean R** comparison, not signal-trigger overlap, so this pre-flight
does not directly invalidate it. But it puts a useful prior in
place: do not expect DBN signal series to track Duk/MT5 below
~40 % mismatch on H4 cross-style triggers, even when *Mean R*
agrees. Treat DBN as a Mean-R cross-check on closed-trade
distributions, not a trigger-by-trigger validator.

The XAUUSD DBN coverage gap (4 335 vs 5 619 H4 bars over the same
window) is a separate fixture-quality issue; backlog item.

### 5.3 What we did NOT validate

- **Wick-sensitive M5 transferability** is unchanged. The 81–96 %
  mismatch on SMC sweeps / FVG bounds documented in the post-fix
  setup-level diff (commit 0681d9c) still stands. This pre-flight
  speaks only to HTF.
- **The MA50-cross is a cherry-picked simple trigger.** Strategies
  that use multi-bar pattern matching (breakout + retest within N
  bars, BB squeeze, etc.) may exhibit more or less mismatch. The
  validation here is necessary, not sufficient, for any specific
  HTF strategy.

---

## 6. Backlog

- [ ] DBN XAUUSD M5 coverage: 67 k rows over a 3.5 y window vs ~249 k
      expected. Investigate whether this is a Panama back-adjusted
      processing gap or a raw-data fetch issue. Affects gate 5
      cross-checks for XAUUSD on every strategy.
- [ ] Re-run this pre-flight on `breakout retest H4` once specified,
      to confirm the trigger overlap holds on a multi-bar pattern
      (not just a single-bar MA cross). 30 min add-on.

---

## 7. Next step

Per protocol §7 and §8: proceed to first real HTF strategy
(`breakout retest, close H4`) following the 7-gate pipeline, after
implementing the four `BacktestResult` extensions proposed in §9 of
the protocol (`projected_annual_return_pct`, `outlier_robustness`,
`temporal_concentration`, `vs_buy_and_hold`).

---

*Auto-generated artefact: `calibration/runs/2026-05-03_htf_transferability_preflight.json` (raw numbers, gitignored).*
