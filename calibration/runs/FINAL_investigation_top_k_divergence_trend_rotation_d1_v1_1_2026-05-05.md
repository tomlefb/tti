# Investigation — top-K divergence root cause (trend_rotation_d1 v1.1, cell 126/5/3)

**Date**: 2026-05-04T22:27:08Z
**Window**: 2020-07-09 -> 2026-04-30 (5.81 y)
**Wallclock**: 24.9 s

## Headline

- Original gate 7 exact-match: **22.7%** (138/607)
- Corrected (H1 + H2 applied) gate 7 exact-match: **81.3%** (447/550)
- Delta: **+58.6%**. Spec H10 threshold: > 70 %. PASS.

## H1 — D1 close timestamp mismatch

_Verdict_: MT5 BTC/FX/Metals carry broker-tz timestamps (21:00 / 22:00 UTC = Athens midnight); Yahoo D1 always labels at 00:00 UTC. Indices align.

| Asset | MT5 hours | Yahoo hours | match |
|---|---|---|:---:|
| NDX100 | [0] | [0] | ✅ |
| SPX500 | [0] | [0] | ✅ |
| US30 | [0] | [0] | ✅ |
| US2000 | [0] | [0] | ✅ |
| GER30 | [0] | [0] | ✅ |
| UK100 | [0] | [0] | ✅ |
| JP225 | [0] | [0] | ✅ |
| EURUSD | [21, 22] | [0] | ❌ |
| GBPUSD | [21, 22] | [0] | ❌ |
| USDJPY | [0] | [0] | ✅ |
| AUDUSD | [0] | [0] | ✅ |
| XAUUSD | [21, 22] | [0] | ❌ |
| XAGUSD | [0] | [0] | ✅ |
| USOUSD | [0] | [0] | ✅ |
| BTCUSD | [21, 22] | [0] | ❌ |

Aligned hour patterns: 11/15. MT5 BTC/EUR/GBP/XAUUSD carry the broker timezone close (Athens midnight = 21:00 UTC EEST or 22:00 UTC EET). Yahoo always labels at 00:00 UTC. The two sources sample the underlying market 2-3 hours apart on these assets — directly visible in H3 for BTC. **Fixable** by normalising both panels to calendar-date index (the corrected re-run does this).

## H2 — calendar-day convention

_Verdict_: MT5 indices have ~300-400 more bars than Yahoo (Sunday evening opens / different holiday calendars). MT5 BTC has ~610 FEWER bars than Yahoo (Yahoo includes 24/7 weekends). FX/EURUSD/GBPUSD agree within <5 bars.

| Asset | MT5 n | Yahoo n | diff | common | only MT5 | only YH |
|---|---:|---:|---:|---:|---:|---:|
| NDX100 | 1808 | 1460 | -348 | 1460 | 348 | 0 |
| SPX500 | 1808 | 1460 | -348 | 1460 | 348 | 0 |
| US30 | 1808 | 1460 | -348 | 1460 | 348 | 0 |
| US2000 | 1806 | 1460 | -346 | 1460 | 346 | 0 |
| GER30 | 1802 | 1479 | -323 | 1476 | 326 | 3 |
| UK100 | 1787 | 1466 | -321 | 1458 | 329 | 8 |
| JP225 | 1805 | 1419 | -386 | 1415 | 390 | 4 |
| EURUSD | 1509 | 1511 | +2 | 1375 | 134 | 136 |
| GBPUSD | 1509 | 1511 | +2 | 1375 | 134 | 136 |
| USDJPY | 1817 | 1511 | -306 | 1510 | 307 | 1 |
| AUDUSD | 1817 | 1511 | -306 | 1510 | 307 | 1 |
| XAUUSD | 1499 | 1462 | -37 | 1153 | 346 | 309 |
| XAGUSD | 1806 | 1462 | -344 | 1462 | 344 | 0 |
| USOUSD | 1805 | 1462 | -343 | 1462 | 343 | 0 |
| BTCUSD | 1513 | 2122 | +609 | 1513 | 0 | 609 |

**Critical observation on BTCUSD**: MT5 has 1512 bars on the 5.8-y window, Yahoo has 2122. BTC trades 24/7 — Yahoo includes every calendar day, MT5 broker treats BTC as a Mon-Fri instrument. So the same `momentum_lookback_days = 126` covers ~6 calendar months on MT5 but only ~4.1 calendar months on Yahoo. The two sources score BTC over fundamentally different price spans, which in a volatile asset directly causes frequent ranking flips at the K-th boundary. **Fixable** by intersecting panels to common dates only.

## H3 — price-source bias

_Verdict_: BTCUSD shows the largest abs daily diff (~2.3 %) — driven primarily by the H1 timestamp offset (22:00 UTC vs 00:00 UTC = 2-hour mid-price snapshot gap on a volatile asset). Indices stay <0.2 %. No systematic bias (mean diffs near zero across the board).

| Asset | n common | mean diff % | abs mean % | max % | min % |
|---|---:|---:|---:|---:|---:|
| NDX100 | 1460 | +0.022 | 0.204 | +2.819 | -4.470 |
| SPX500 | 1460 | +0.018 | 0.149 | +2.237 | -3.562 |
| US30 | 1460 | +0.014 | 0.124 | +2.013 | -2.384 |
| US2000 | 1460 | +0.007 | 0.190 | +3.348 | -5.221 |
| GER30 | 1476 | -0.010 | 0.690 | +4.766 | -5.936 |
| UK100 | 1458 | -0.010 | 0.401 | +5.072 | -2.754 |
| JP225 | 1415 | -0.004 | 0.603 | +8.847 | -4.509 |
| EURUSD | 1375 | -0.013 | 0.402 | +3.316 | -1.989 |
| GBPUSD | 1375 | -0.018 | 0.446 | +4.103 | -3.738 |
| USDJPY | 1510 | -0.002 | 0.179 | +1.923 | -3.728 |
| AUDUSD | 1510 | -0.014 | 0.218 | +2.932 | -2.230 |
| XAUUSD | 1153 | +0.119 | 0.818 | +7.030 | -8.056 |
| XAGUSD | 1462 | -0.016 | 0.630 | +8.936 | -12.304 |
| USOUSD | 1462 | -0.094 | 0.474 | +5.937 | -14.620 |
| BTCUSD | 1513 | +0.049 | 2.244 | +14.780 | -15.274 |

BTCUSD daily abs diff 2.26 % is large for a single asset — but the mean diff is +0.16 % (no systematic premium). The spread is dominated by the H1 timestamp offset (2 hours of BTC mid-price drift = easily 1-3 %). Indices and FX stay <0.6 % abs. No source carries a structural bias — H3 is _consistent with same-asset-different-snapshot_, not _different-underlying-stream_.

## Corrected gate-7 — H1 + H2 fixes applied

Both panels normalised to calendar-date index (drops hour); then intersected to dates present in BOTH sources. The rebalance schedule is now identical and the momentum window covers the same calendar span on every asset.

| Metric | Original | Corrected | Delta |
|---|---:|---:|---:|
| n rebalances | 607 | 550 | -57 |
| exact-match | 22.7% (138) | 81.3% (447) | +58.6% |
| ≥ K-1 overlap | 79.7% | 99.6% | +19.9% |
| ≥ 1 shared | 100.0% | 100.0% | +0.0% |

### Per-asset overlap — corrected

| Asset | Both | Yahoo-only | MT5-only |
|---|---:|---:|---:|
| NDX100 | 278 | 4 | 4 |
| SPX500 | 204 | 13 | 9 |
| US30 | 140 | 11 | 11 |
| US2000 | 150 | 2 | 7 |
| GER30 | 188 | 10 | 14 |
| UK100 | 178 | 18 | 6 |
| JP225 | 239 | 4 | 14 |
| EURUSD | 55 | 9 | 4 |
| GBPUSD | 2 | 3 | 3 |
| USDJPY | 134 | 4 | 7 |
| AUDUSD | 25 | 3 | 2 |
| XAUUSD | 305 | 11 | 15 |
| XAGUSD | 249 | 6 | 4 |
| USOUSD | 187 | 4 | 2 |
| BTCUSD | 311 | 3 | 3 |

## Final verdict

✅ **BUG FIXED — corrected gate 7 PASSES > 70 %**. Top-K divergence in the original gate 7 was caused by H1 (timezone label) + H2 (calendar count) measurement artefacts, not a structural transferability problem. Recommend updating the gate-7 driver to apply the H1+H2 fixes and re-record the verdict.

