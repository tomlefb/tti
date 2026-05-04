# Gates 6 / 7 / 8 corrected — FINAL verdict (trend_rotation_d1 v1.1, cell 126/5/3)

**Date**: 2026-05-04T22:34:12Z
**Cell**: 126/5/3 (gate-4-v1.1 selected, commit `efe599e`)
**Alignment**: protocol §6.5 (a)+(b) — UTC calendar-date normalisation + per-asset intersection across MT5 + Yahoo. Diagnosed in `investigation_top_k_divergence_2026-05-04T22-27-08Z.md` (commit pending), root cause of the original gate-7 22.7 % artefact.

## Global verdict: ✅ ALL PASS — Phase 1 deployment recommended

| Gate | Detail | Verdict |
|---|---|---|
| Gate 6 — MT5 sanity | MT5 mean_r +1.585, Yahoo mean_r +1.950, direction agreement 87.1% | ✅ PASS (excellent: < 30 % mismatch) |
| Gate 7 — top-K transferability | exact=81.5%, ≥K-1=99.7% | ✅ PASS — exact match > 70 % (corrected) |
| Gate 8 — granular fees | mean_r post-fee +1.572, proj annual +62.5 % | ✅ PASS — post-fee mean_r +1.572 R ≥ +0.3 R |

## Output files

- [gate6_corrected.md](gate6_corrected.md)
- [gate7_corrected.md](gate7_corrected.md)
- [gate8_corrected.md](gate8_corrected.md)

## Alignment loss diagnostic (protocol §6.5 (c))

Per-asset bar count before / after the H1+H2 alignment, on the gate window. Assets losing > 30 % of their bars are flagged — they remain available for trading but their aligned signal is computed on a meaningfully reduced sample.

| Asset | MT5 raw | Yahoo raw | common | dropped MT5 | dropped Yahoo | at risk |
|---|---:|---:|---:|---:|---:|:---:|
| NDX100 | 1806 | 1458 | 1458 | 348 (19.3%) | 0 (0.0%) | ✅ |
| SPX500 | 1806 | 1458 | 1458 | 348 (19.3%) | 0 (0.0%) | ✅ |
| US30 | 1806 | 1458 | 1458 | 348 (19.3%) | 0 (0.0%) | ✅ |
| US2000 | 1804 | 1458 | 1458 | 346 (19.2%) | 0 (0.0%) | ✅ |
| GER30 | 1800 | 1478 | 1475 | 325 (18.1%) | 3 (0.2%) | ✅ |
| UK100 | 1785 | 1464 | 1456 | 329 (18.4%) | 8 (0.5%) | ✅ |
| JP225 | 1803 | 1417 | 1413 | 390 (21.6%) | 4 (0.3%) | ✅ |
| EURUSD | 1507 | 1509 | 1373 | 134 (8.9%) | 136 (9.0%) | ✅ |
| GBPUSD | 1507 | 1509 | 1373 | 134 (8.9%) | 136 (9.0%) | ✅ |
| USDJPY | 1815 | 1509 | 1508 | 307 (16.9%) | 1 (0.1%) | ✅ |
| AUDUSD | 1815 | 1509 | 1508 | 307 (16.9%) | 1 (0.1%) | ✅ |
| XAUUSD | 1497 | 1460 | 1151 | 346 (23.1%) | 309 (21.2%) | ✅ |
| XAGUSD | 1804 | 1460 | 1460 | 344 (19.1%) | 0 (0.0%) | ✅ |
| USOUSD | 1803 | 1460 | 1460 | 343 (19.0%) | 0 (0.0%) | ✅ |
| BTCUSD | 1511 | 2120 | 1511 | 0 (0.0%) | 609 (28.7%) | ✅ |

## Action items

1. Subscribe Phase 1 Stellar Lite ($23 with VIBES). Budget 3 attempts max.
2. Branch the scheduler `src/strategies/` to integrate `trend_rotation_d1` v1.1 (cell 126/5/3, 1 % risk per trade, 15-asset universe).
3. Live-monitor: compare each MT5 trade with the simulation. If sustained divergence emerges (mean_r drift > 0.3 R over 30 trades), pause and investigate.

