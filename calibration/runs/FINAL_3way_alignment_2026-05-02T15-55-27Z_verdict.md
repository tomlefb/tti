# 3-way alignment — verdict (2026-05-02T15-55-27Z)

Verdict thresholds: corr(Duk, X) > 0.95 and corr exceeds the other pair by ≥ 0.02. Returns are 5-min close-to-close on the common-timestamp intersection of the sampled days.

## Verdict per instrument

| Instrument | Common window | N bars | corr(Duk,MT5) | corr(Duk,DBN) | corr(MT5,DBN) | Verdict |
|---|---|---:|---:|---:|---:|---|
| XAUUSD | 2025-06-20 → 2026-04-27 | 578 | 0.9573 | 0.9980 | 0.9566 | **B** (corr(Duk,DBN)=0.998 > 0.95 and exceeds corr(Duk,MT5)=0.957 b…) |
| NDX100 | 2025-06-20 → 2026-04-27 | 2419 | 0.9232 | 0.9911 | 0.9072 | **B** (corr(Duk,DBN)=0.991 > 0.95 and exceeds corr(Duk,MT5)=0.923 b…) |
| SPX500 | 2024-11-26 → 2026-04-29 | 2405 | 0.9729 | 0.9957 | 0.9692 | **B** (corr(Duk,DBN)=0.996 > 0.95 and exceeds corr(Duk,MT5)=0.973 b…) |

## Aggregate

- A (Duk ≈ MT5): **0/3** — 
- B (Duk ≈ DBN): **3/3** — XAUUSD, NDX100, SPX500
- C (Duk distinct): **0/3** — 

## Recommendation for the source hierarchy

**Majority B.** Dukascopy and Databento are two long-term datasets with similar structure (both diverge from broker CFD). MT5 remains the only source aligned with the production runtime; backtests on Duk + DBN measure futures-like behaviour and do not predict MT5 edge directly. The 'edge on 2+ sources' criterion under this regime would mean 'Duk and DBN' but the operator should treat MT5 as the ground truth for live decisions.

EURUSD, GBPUSD, US30, BTCUSD are not present in the Databento fixture and are excluded from this 3-way comparison. Their Dukascopy alignment with MT5 alone could be added later as a supplementary 2-way check.
