# Gates 6 / 7 / 8 — final verdict (trend_rotation_d1 v1.1, cell 126/5/3)

**Date**: 2026-05-04T22:19:21Z
**Cell**: 126/5/3 (gate-4-v1.1 selected)

## Global verdict: ⚠️ DISCUSSION — only 1/3 PASS; serious doubt before deployment

| Gate | Detail | Verdict |
|---|---|---|
| Gate 6 — MT5 sanity | MT5 mean_r +1.166, Yahoo mean_r +2.098, n_mt=337, n_yh=231 | ⚠️ REVIEW — direction agreement 63.1% < 70 % |
| Gate 7 — top-K transferability | exact=22.7%, ≥K-1=79.7%, shared≥1=100.0% | ⚠️ REVIEW — exact match < 70 %, but ≥ K-1 overlap > 70 % (rotation transferability acceptable, edge probably reduced) |
| Gate 8 — granular fees | mean_r post-fee +1.152, proj annual post-fee +67.1 % | ✅ PASS — post-fee mean_r +1.152 R ≥ +0.3 R |

## Output files

- [gate6_mt5_sanity.md](gate6_mt5_sanity.md)
- [gate7_top_k_transferability.md](gate7_top_k_transferability.md)
- [gate8_granular_fees.md](gate8_granular_fees.md)

## Action items

1. Discuss the gate(s) flagged before any Phase 1 subscription. The REVIEW outcomes are surfaced for operator judgement, not auto-archived.
2. If the discussion concludes against deployment: archive the strategy under `archived/strategies/trend_rotation_d1_v1_1/` with the post-mortem appropriate to the failing gate.

