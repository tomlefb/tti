# Investigation v1.1 holdout — bias / bug systematic test — FINAL

**Date**: 2026-05-04
**Subject**: trend_rotation_d1 v1.1, cell 126/5/3, holdout 2025-01 → 2026-04
**Headline**: mean_r = +2.017 R, projected annual +108.9 %, drift +1.361 R vs train
**Driver**: `calibration/investigate_trend_rotation_d1_v1_1.py`
**Run**: `calibration/runs/investigation_trend_rotation_d1_v1_1_2026-05-04T15-15-18Z.md` (gitignored)

---

## Synthèse

| H | Test | Verdict |
|---|---|:---:|
| H1 | return_r manual recalc on 5 trades | ✅ PASS (5/5 match within tolerance) |
| H2 | Look-ahead causality on 3 trades | ✅ PASS (3/3 entry/exit at close[T], no leak) |
| H3 | Walk-forward stationarity (7 sub-windows) | ⚠️ PARTIAL (4/7 above +0.3R, 6/7 above 0R) |
| H4 | Risk-parity vs equal-weight sizing | ⚠️ PARTIAL (eq-weight = +0.667 R vs rp +2.017 R) |
| H5 | Asset-level concentration / survivor | ⚠️ PARTIAL (top-3 share 67.9 % of \|R\|) |
| H6 | Granular per-instrument fees | ✅ PASS (mean cost 0.016 R/trade < $30 flat) |
| H7 | Slippage model | ✅ PASS (mean cost 0.103 R/trade) |

**Result**: **0 FAIL, 3 PARTIAL, 4 PASS**.

No bug or look-ahead leak detected. The +109 % headline projected annual return is **structurally consistent with the trade list**, but is **inflated by methodological choice (risk-parity sizing) and concentrated on 3 outlier-régime assets** (XAGUSD, XAUUSD, GER30). The corrected magnitude is materially smaller, in the 20–35 %/year range, depending on which correction is applied.

---

## H1 — return_r manual recalc (PASS, 5/5)

For 5 random holdout trades, recomputed entry/exit price and ATR(20) from D1 fixtures, then return_r = (exit − entry) / ATR(20). Matched stored values within 0.5 % / 5 % / 10 % tolerances.

| asset | entry → exit | stored R | manual R | match |
|---|---|---:|---:|:---:|
| NDX100 | 2025-01-01 → 2025-01-15 | +0.5713 | +0.5713 | ✅ |
| US30 | 2025-03-23 → 2025-03-26 | +0.2557 | +0.2557 | ✅ |
| BTCUSD | 2025-06-18 → 2025-06-22 | -0.1545 | -0.1545 | ✅ |
| GBPUSD | 2025-06-29 → 2025-07-02 | -0.7907 | -0.7907 | ✅ |
| XAGUSD | 2025-05-07 → 2025-08-10 | +8.6257 | +8.6257 | ✅ |

**Note on initial false-FAIL**: a first run of H1 used Wilder ATR (EWMA) for the manual recalc and produced 3/5 mismatches with 5–20 % delta on ATR. The pipeline uses **SMA(TR, 20)** per `src/strategies/trend_rotation_d1/volatility.py::compute_atr`. Switching the manual recalc to SMA aligned 5/5 trades exactly. The pipeline ATR convention is documented in the spec §3.1 ("ATR period 20 days, academic standard short-horizon volatility") but the SMA-vs-Wilder choice was implicit; the investigation surfaced it without ambiguity.

---

## H2 — Look-ahead causality (PASS, 3/3)

For 3 random trades, verified:
- entry_price = close[T] AND not close[T-1] or close[T+1].
- exit_price = close[exit_T] AND not close[exit_T+1].
- Score-from-close[<T] differs from score-from-close[≤T] (would-be leak), confirming the spec §2.2 anti-look-ahead window is operative.

| asset | entry | exit | entry=close[T] | exit=close[T] |
|---|---|---|:---:|:---:|
| XAGUSD | 2025-04-13 | 2025-05-04 | ✅ | ✅ |
| JP225 | 2025-08-10 | 2025-10-01 | ✅ | ✅ |
| NDX100 | 2025-11-26 | 2025-12-14 | ✅ | ✅ |

The gate-3 audit harness (Mode A truncated == Mode B full-frame, 8/8 PASS at commit `eabba4c`) is the structural evidence; H2 is a redundant 3-trade spot check.

---

## H3 — Walk-forward stationarity (PARTIAL)

Run cell 126/5/3 on 2019-12-22 → 2026-04-30, bucket exits by exit_timestamp into 7 sub-windows.

| Sub-window | n | mean_r | win_rate | proj annual % | total_r |
|---|---:|---:|---:|---:|---:|
| 2019-12-22 → 2020-12-31 | 64 | -0.0987 | 54.7 % | -6.2 % | -6.32 R |
| 2021-01-01 → 2021-12-31 | 63 | **+1.4766** | 52.4 % | +93.3 % | +93.03 R |
| 2022-01-01 → 2022-12-31 | 62 | +0.2068 | 38.7 % | +12.9 % | +12.82 R |
| 2023-01-01 → 2023-12-31 | 56 | +0.1813 | 44.6 % | +10.2 % | +10.15 R |
| 2024-01-01 → 2024-12-31 | 51 | **+1.6542** | 60.8 % | +84.4 % | +84.36 R |
| 2025-01-01 → 2025-12-31 | 53 | **+1.2407** | 49.1 % | +66.0 % | +65.76 R |
| 2026-01-01 → 2026-04-30 | 20 | **+4.4380** | 55.0 % | +272.4 % | +88.76 R |

- Sub-windows mean_r > +0.3 R: **4 / 7**.
- Sub-windows mean_r > 0: **6 / 7**.
- Top-window total_r share: **25.8 %** (the 2021 +93 R, partly amplified by the post-COVID commodity rally).

**Reading**: the strategy is positive in 6 / 7 yearly buckets but the magnitude is **highly régime-dependent**. The four "high" buckets (2021, 2024, 2025, 2026-Q1) coincide with documented commodity / metal / silver rallies and the Fed pivot. The two "moderate" buckets (2022, 2023) sit at +0.2 R per trade. The 2020 bucket (post-COVID dislocation) was slightly negative.

The 2026-Q1 sub-window (4 months, +4.44 R/trade, +89 R total) is a particular concern — it sits just at the end of the holdout and contributes 25.8 % of total |R| despite only 4 months of duration. If the verdict were re-evaluated on holdout-minus-2026-Q1, projected annual would be substantially lower.

This is the **régime-fit reading** of the +1.361 R drift surfaced at gate 4: the holdout window contains 2 of the 4 "high" sub-windows (2025 + 2026-Q1) clustered consecutively, while the train window has 2 of the 4 "high" buckets (2021 + 2024) interleaved with moderate years. The 16-month holdout disproportionately samples the high-edge tail of the strategy's return distribution.

PARTIAL verdict (4/7 above +0.3 R) is between PASS (≥ 5) and FAIL (≤ 2). The strategy is not noise — but it is not stationary either.

---

## H4 — Risk-parity vs equal-weight sizing (PARTIAL)

Two return-R conventions on the same trade list:

| Convention | Formula | Mean R | × ratio vs rp |
|---|---|---:|---:|
| Risk-parity (stored) | `(exit − entry) / ATR(20)` | **+2.017** | 1.00 |
| Equal-weight | `pct_move / (K × risk_pct) = pct_move × 20` | **+0.667** | 0.331 |

**Δ = -1.350 R** — equal-weight gives ~1/3 the headline.

**Interpretation**: risk-parity sizing in the pipeline allocates `risk_dollars / atr_at_entry` instrument units per position. For an asset with low ATR-relative-to-price (e.g. XAGUSD at entry=32.45, ATR=0.66 → entry/ATR=49) the leverage is high; for an asset with high ATR-relative-to-price (e.g. BTCUSD at entry=104K, ATR=3.4K → entry/ATR=31) the leverage is moderate. A 1 % pct-move on XAG translates to 0.49 R; on BTC to 0.31 R. The strategy preferentially picks the assets with the largest momentum scores; in 2025 those happened to be the metals (XAG, XAU), which combine high momentum AND high entry/ATR ratio. Risk-parity sizing therefore amplifies the realised R 2-3× vs equal-weight.

**Methodological note**: risk-parity is the academic standard for cross-sectional momentum (Asness 2013) and is correctly implemented per spec §2.5. The +2.017 R is a faithful measurement of the spec; the +0.667 R is what an equal-weight portfolio would have produced. **Both are correct under their respective conventions** — the question is which is more representative for operator deployment economics.

**Equal-weight projected annual** (assuming cadence 4.50/mo): **+0.667 × 4.5 × 12 × 1 % = +36.0 %/year**. Still well above the §3 viability floor of 20 %, but ~3× smaller than the headline.

PARTIAL verdict: equal-weight is in the +0.3-1.0 R band, signalling that risk-parity is a meaningful lever on the result.

---

## H5 — Asset-level concentration (PARTIAL)

72 holdout trades, by asset (sorted by |sum_r|):

| Asset | n | mean_r | sum_r | win | share total |R\| |
|---|---:|---:|---:|---:|---:|
| **XAGUSD** | 8 | +7.95 | **+63.56** | 62.5 % | 35.2 % |
| **XAUUSD** | 3 | +11.29 | **+33.88** | 100 % | 18.8 % |
| **GER30** | 1 | +25.11 | **+25.11** | 100 % | 13.9 % |
| USOUSD | 3 | +4.24 | +12.72 | 33.3 % | 7.0 % |
| UK100 | 12 | -0.91 | -10.89 | 58.3 % | 6.0 % |
| JP225 | 4 | +2.08 | +8.30 | 75.0 % | 4.6 % |
| EURUSD | 3 | +2.06 | +6.18 | 100 % | 3.4 % |
| US2000 | 3 | +1.86 | +5.59 | 66.7 % | 3.1 % |
| AUDUSD | 3 | -1.27 | -3.82 | 0 % | 2.1 % |
| NDX100 | 7 | +0.41 | +2.90 | 42.9 % | 1.6 % |
| GBPUSD | 5 | +0.57 | +2.83 | 40.0 % | 1.6 % |
| SPX500 | 5 | -0.53 | -2.63 | 40.0 % | 1.5 % |
| BTCUSD | 6 | +0.29 | +1.74 | 33.3 % | 1.0 % |
| USDJPY | 5 | -0.05 | -0.23 | 40.0 % | 0.1 % |
| US30 | 4 | -0.01 | -0.04 | 50.0 % | 0.0 % |

**Top-3 share of total |R|: 67.9 %** (just below the 70 % FAIL threshold).

Sensitivity analysis:
- Mean R excluding **BTCUSD**: +2.174 (+7.8 % vs overall) — counterintuitively *higher*; BTC was a small-impact name in 2025 holdout.
- Mean R excluding **BTCUSD + NDX100**: +2.382 (+18.1 % vs overall) — also higher; both were neutral-to-negative carrier assets.
- Mean R excluding **top-3 (XAG + XAU + GER)**: **+0.378 R** (n=60).

**Reading**: the strategy's edge in holdout is genuinely concentrated in the 3 commodity / European-equity outliers. Removing them collapses mean R to +0.378 R — at projected 4.50 trades/mo cadence, that's +20.4 %/year, sitting at the §3 viability floor.

This is partly **edge-discovery working as intended** (the strategy correctly identified XAG, XAU, GER as 2025's top performers via momentum scoring) and partly **concentration risk** (1 GER30 trade with +25 R is a single-event tail; 3 XAU trades with mean +11 R is a small sample). On a longer holdout or different régime, the concentration could collapse or invert.

PARTIAL: top-3 share 67.9 % is at the FAIL boundary. Excluding top-3 leaves a still-positive but barely-viable mean R.

---

## H6 — Granular per-instrument fees (PASS)

Mean cost across 72 trades: **0.0164 R/trade** — *lower* than the gate-4 flat $30 = 0.030 R approximation.

| Quantity | Pre-fee | Post-fee |
|---|---:|---:|
| Mean R | +2.017 | +2.000 |

Per-asset fee model (round-trip as fraction of notional): indices 0.01 %, FX 0.01-0.015 %, metals 0.03-0.05 %, oil 0.04 %, BTC 0.10 %. Conservative FundedNext-like estimates.

Reading: the gate-4 cost model (flat $30) was *over-estimated* for this trade mix; the granular model gives slightly less cost. The fee level does not threaten the headline.

---

## H7 — Slippage model (PASS)

Mean slippage cost: **0.1026 R/trade** (round-trip, applied symmetrically).

| Quantity | Pre-slip | Post-slip |
|---|---:|---:|
| Mean R | +2.017 | +1.914 |

Slippage per leg: indices/FX 0.05 %, metals/oil 0.10 %, BTC 0.20 %.

Reading: slippage costs ~5 % of the headline mean R. Notable but not destructive.

---

## Combined post-corrections (best-estimate matrix)

Three reasonable "best-estimate" magnitudes depending on which corrections are applied:

| Scenario | Mean R | Cadence | Proj annual @ 1 % risk |
|---|---:|---:|---:|
| Headline (risk-parity, gross) | +2.017 | 4.50/mo | +109 % |
| Risk-parity, post H6 + H7 | +1.898 | 4.50/mo | +102 % |
| Equal-weight (H4 correction), pre-cost | +0.667 | 4.50/mo | **+36 %** |
| Equal-weight, post H6 + H7 (approx) | ~+0.55 | 4.50/mo | ~+30 % |
| Risk-parity excl. top-3 (H5) | +0.378 | ~3.5/mo | ~+16 % |
| Equal-weight excl. top-3 (combined H4 + H5) | ~+0.13 | ~3.5/mo | ~+5 % |

The last two rows are the most conservative readings: strip the leverage effect (H4) and the concentration effect (H5) simultaneously, and the strategy's edge falls to the noise floor.

**The honest range for "real magnitude" is therefore 5-36 %/year**, contingent on which régime + which sizing convention is used. The headline 109 % is not a bug — it is the spec's risk-parity convention applied to a régime-favorable, concentration-favorable holdout window. Removing either of those amplifiers brings the magnitude into a much more conventional range.

---

## Key findings

1. **No bug, no look-ahead leak** — H1 (after correcting the manual ATR convention to SMA) and H2 both PASS. The pipeline is faithfully implementing the spec.

2. **Risk-parity sizing inflates returns ~3×** vs equal-weight on this trade list. This is methodologically intentional (academic standard for CSM), but it is the single largest source of magnification. Equal-weight equivalent: +0.667 R, +36 %/year.

3. **Edge is concentrated in 3 régime-favoured assets** (XAGUSD silver bull 2025, XAUUSD gold bull 2025, single GER30 trade at +25 R). Top-3 share = 67.9 % of total |R|. Excluding them: mean R = +0.378 R, +20 %/year (sitting on the §3 viability floor).

4. **Walk-forward shows régime dependence**: 6/7 sub-windows positive but only 4/7 above +0.3 R; 2 of those 4 (2021 + 2024) coincide with documented commodity rallies. The holdout 2025 + 2026-Q1 sit consecutively in the high-edge tail of the distribution.

5. **Costs and slippage are minor** — H6 + H7 net effect ~-0.12 R/trade on the headline.

6. **The +1.361 R train→holdout drift surfaced at gate 4 is consistent with régime-fit**: the holdout window over-samples the strategy's high-edge tail (2 of 4 "high" sub-windows), and the high-edge assets (XAG, XAU) had their 2025 commodity rally inside the holdout.

---

## Best-estimate verdict

**The edge is real but materially smaller than the headline +109 %**. A conservative, methodologically-corrected reading puts the strategy at **+30 to +40 %/year on equal-weight sizing pre-cost**, dropping to **+15 to +20 %/year** if régime-favoured outliers are stripped.

This range straddles the §3 protocol viability threshold (20 %). The strategy may be viable on its merits but the headline magnitude is misleading and should not be the basis for a deployment decision.

---

## Recommendation

The investigation did not find a "smoking gun" bug or leak. It did find that the headline magnitude is amplified by:

1. risk-parity sizing (academic-standard, but a 3× lever),
2. asset concentration on 3 régime-favoured names, and
3. holdout window over-sampling the strategy's high-edge sub-window distribution.

**Operator decision branches** (consistent with gate 4 REVIEW verdict):

(A) **Continue to gate 5 (Databento partial cross-check)** with explicit operator awareness that:
    - The "real" expected magnitude is +30-40 %/year, not +109 %.
    - The strategy's edge is heavily régime-dependent (2 of 4 strong-yearly sub-windows coincide with commodity rallies).
    - Concentration on metals + 1 European equity is intentional (CSM correctly identifies winners) but concentration risk applies to live deployment.
    - Gate 5 will test whether DBN futures subset (NDX/SPX/DJI) reproduces a similar magnitude — but the metals/FX/crypto subset is unmeasurable on DBN, so gate 5 cannot validate the H5 concentrated edge.

(B) **Archive on combined evidence** — H3 PARTIAL (régime dependence), H4 PARTIAL (sizing leverage), H5 PARTIAL (concentration), drift +1.361 R. The cumulative weight of three PARTIALS plus the gate-4 drift signal favours archive even though no single test FAILED. Per spec v1.1 footer, the strategy class is then declared structurally non-viable. 5th archive of the strategy-research phase.

(C) **Walk-forward extension** — extend the panel to 20+ years on an external long-history source (Yahoo Finance, OECD, Bloomberg long-term) and re-run with the same cell 126/5/3. If the 6.4 y train+holdout edge magnitude survives 20 y of régime variation, (A) or (B) becomes a much firmer call. This is consistent with the protocol's "if uncertain, measure more" discipline.

The decision is operator's. The investigation has surfaced the data; no further compute is proposed before that discussion.

---

**Pytest count**: 587 (unchanged — investigation is calibration-only, no `src/` changes).
