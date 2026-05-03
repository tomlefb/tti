# 3-way alignment — verdict (2026-05-02T21-21-15Z)

> **Supersedes 379fc70** (3-way report from 2026-05-02T15-55-27Z, run pre timezone fix).
> Re-run with the corrected MT5 timezone fixtures (commit `f868793`),
> 30-day sample (was 10), and extended common windows
> (XAUUSD: 2019-12 → 2026-04, ~6.4 years; NDX/SPX: 2022-10 → 2026-04, ~3.5 years).
>
> **Important re-interpretation of the auto-emitted "C" labels.** The script's
> verdict labels "C" because no pair beats the others by ≥ 0.02. Pre-fix that
> meant "three distinct sources". Post-fix it means the opposite: **all three
> sources now correlate ≥ 0.98 on returns** and are essentially structurally
> equivalent on the M5 timescale. The "All three are distinct market
> structures" sentence in the auto-recommendation below is therefore WRONG
> for this run and should be ignored — see `post_timezone_fix_synthesis_*.md`
> for the corrected interpretation.

Verdict thresholds: corr(Duk, X) > 0.95 and corr exceeds the other pair by ≥ 0.02. Returns are 5-min close-to-close on the common-timestamp intersection of the sampled days.

## Verdict per instrument

| Instrument | Common window | N bars | corr(Duk,MT5) | corr(Duk,DBN) | corr(MT5,DBN) | Verdict |
|---|---|---:|---:|---:|---:|---|
| XAUUSD | 2019-12-23 → 2026-04-29 | 1544 | 0.9994 | 0.9982 | 0.9976 | **C** (corr(Duk,MT5)=0.999, corr(Duk,DBN)=0.998…) |
| NDX100 | 2022-10-20 → 2026-04-29 | 7053 | 0.9967 | 0.9926 | 0.9897 | **C** (corr(Duk,MT5)=0.997, corr(Duk,DBN)=0.993…) |
| SPX500 | 2022-10-20 → 2026-04-29 | 6450 | 0.9928 | 0.9905 | 0.9833 | **C** (corr(Duk,MT5)=0.993, corr(Duk,DBN)=0.991…) |

## Aggregate

- A (Duk ≈ MT5): **0/3** — 
- B (Duk ≈ DBN): **0/3** — 
- C (Duk distinct): **3/3** — XAUUSD, NDX100, SPX500

## Recommendation for the source hierarchy

**Majority C.** All three are distinct market structures. Adopt the **'edge on 2 of 3 sources'** rule as the standard for any strategy: a setup must hold on at least two of {Duk, MT5, DBN} to be considered robust. Cross-source robustness becomes a first-class screening criterion — strategies that only profit on one source are likely overfit to that source's quirks.

EURUSD, GBPUSD, US30, BTCUSD are not present in the Databento fixture and are excluded from this 3-way comparison. Their Dukascopy alignment with MT5 alone could be added later as a supplementary 2-way check.
