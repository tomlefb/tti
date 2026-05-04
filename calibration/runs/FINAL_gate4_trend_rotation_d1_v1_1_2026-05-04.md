# Gate 4 — trend_rotation_d1 v1.1 — FINAL

**Date**: 2026-05-04
**Spec**: `docs/strategies/trend_rotation_d1_v1_1.md` (commit `bb12a95`)
**Driver**: `calibration/run_trend_rotation_d1_v1_1_grid.py` (commit `ffe2a81`)
**Run**: `calibration/runs/gate4_trend_rotation_d1_v1_1_2026-05-04T14-47-15Z/` (gitignored)

---

## Verdict

**REVIEW** — 5 / 9 hypotheses PASS on holdout.

§3.6 holdout double-check: ✅ PASS (4.50 trades/mo, floor = 4.0).

**Drift train→holdout**: +1.361 R ⚠️ overfit-suspect (advisory; flag does NOT auto-override per spec §4 v1.1 verdict rule).

The verdict is REVIEW per the binary spec rule (3 ≤ PASS ≤ 5). Operator discussion required before any gate-5 cross-check or archive decision. Both the magnitude of the holdout edge and the magnitude of the train→holdout drift are flagged as critical context for the discussion.

---

## Pipeline summary

| Step | Outcome |
|---|---|
| Gate 3 audit (4 cells × 2 windows = 8 audits) | 8 / 8 PASS bit-identique |
| §3.6 pre-measure (18 cells, train) | **8 / 18** cells viable (trades/mo ≥ 4) |
| §3.4 selection (§3.5 class-B + §3.6 floors) | **1 / 18** cell passes all four floors: **mom=126, K=5, rebal=3** |
| Holdout (selected cell, 16 mo) | n=72, mean_r=+2.017, ci_low=+0.292, win=52.8 %, proj=+108.9 % |
| §3.6 holdout double-check | ✅ PASS (4.50 trades/mo) |
| §4 hypothesis count | **5 / 9 PASS** (H10 deferred to gate 7) |

---

## §3.6 pre-measure (full table)

Train 2019-12-22 → 2024-12-31 (≈ 60 months). §3.6 floor: trades/mo portfolio ≥ 4.

| momentum | K | rebalance | n_closed | trades/mo | §3.6 viable |
|---:|---:|---:|---:|---:|:---:|
| 63 | 3 | 3 | 275 | 4.51 | ✅ |
| 63 | 3 | 5 | 210 | 3.44 | ❌ |
| 63 | 3 | 7 | 186 | 3.05 | ❌ |
| 63 | 4 | 3 | 360 | 5.90 | ✅ |
| 63 | 4 | 5 | 294 | 4.82 | ✅ |
| 63 | 4 | 7 | 251 | 4.11 | ✅ |
| 63 | 5 | 3 | 410 | 6.72 | ✅ |
| 63 | 5 | 5 | 328 | 5.38 | ✅ |
| 63 | 5 | 7 | 285 | 4.67 | ✅ |
| 126 | 3 | 3 | 180 | 2.95 | ❌ |
| 126 | 3 | 5 | 143 | 2.34 | ❌ |
| 126 | 3 | 7 | 120 | 1.97 | ❌ |
| 126 | 4 | 3 | 240 | 3.93 | ❌ (just below) |
| 126 | 4 | 5 | 189 | 3.10 | ❌ |
| 126 | 4 | 7 | 167 | 2.74 | ❌ |
| 126 | 5 | 3 | 296 | 4.85 | ✅ |
| 126 | 5 | 5 | 234 | 3.84 | ❌ (just below) |
| 126 | 5 | 7 | 197 | 3.23 | ❌ |

**Pattern**: §3.6 is satisfied only by short-momentum (63 d) cells with K ≥ 3 at rebal ≤ 7 d, or by the maximum-cadence corner of the long-momentum side (126/5/3). The 126-momentum side mostly produces sub-floor cadence — confirming the spec §0 a-priori "cadence × 4 vs v1.0" only holds at the shortest rebalance + largest basket combinations.

---

## §3.4 selection (§3.5 class-B + §3.6)

Floors: `n_closed ≥ 100` AND `ci_low ≥ -0.1` AND `tc < 0.6` AND `trades/mo ≥ 4`.

| mom | K | rebal | n_closed | mean_r | CI low | tc | trades/mo | BH-Δ % | floors | sel |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| 63 | 3 | 3 | 275 | +0.499 | -0.072 | 0.704 | 4.51 | +7.4 % | ✅✅❌✅ |  |
| 63 | 4 | 3 | 360 | +0.396 | -0.113 | 0.796 | 5.90 | +8.4 % | ✅❌❌✅ |  |
| 63 | 4 | 5 | 294 | +0.321 | -0.271 | 0.999 | 4.82 | -1.0 % | ✅❌❌✅ |  |
| 63 | 4 | 7 | 251 | +0.350 | -0.403 | 1.335 | 4.11 | -2.3 % | ✅❌❌✅ |  |
| 63 | 5 | 3 | 410 | +0.404 | -0.065 | 0.742 | 6.72 | +12.9 % | ✅✅❌✅ |  |
| 63 | 5 | 5 | 328 | +0.418 | -0.162 | 0.876 | 5.38 | +7.4 % | ✅❌❌✅ |  |
| 63 | 5 | 7 | 285 | +0.365 | -0.381 | 1.246 | 4.67 | +0.9 % | ✅❌❌✅ |  |
| **126** | **5** | **3** | **296** | **+0.656** | **+0.013** | **0.399** | **4.85** | **+18.6 %** | **✅✅✅✅** | **🎯** |

The §3.6-viable sub-grid produced exactly **1** cell that cleared all four floors — the 126/5/3 cell, with a strictly-positive train CI lower bound (+0.013). All seven cells from the short-momentum (63 d) side failed the `tc < 0.6` floor — they generate enough cadence but their R distribution is concentrated.

**Selected**: 126/5/3 by max strategy_minus_bh_pct (+18.6 % on train), with only one candidate.

---

## Holdout — selected cell (126/5/3)

Window: 2025-01-01 → 2026-04-30 (≈ 16 months).

| n_closed | mean_r | CI low | CI high | win | trades/mo | tc | proj_annual | trim_5_5 | BH-Δ % |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 72 | +2.017 | +0.292 | +4.247 | 52.8 % | 4.50 | 0.611 | +108.9 % | +0.717 | +101.1 % |

§3.6 holdout: 4.50 / 4.0 → ✅ PASS.

---

## §4 hypothesis evaluation v1.1 (frozen bands, never tuned)

| H | Bande v1.1 (figée bb12a95) | Holdout value | PASS |
|---|---|---|:---:|
| H1 | trades/mo ∈ [4, 8] | 4.50 | ✅ |
| H2 | win rate ∈ [48 %, 60 %] | 52.8 % | ✅ |
| H3 | mean R pre-cost ∈ [+0.1, +0.4] | **+2.017** | ❌ — **above band** |
| H4 | mean R post-cost ∈ [+0.0, +0.3] | **+1.987** | ❌ — **above band** |
| H5 | projected annual ∈ [5, 25] % | **+107.3 %** | ❌ — **above band** |
| H6 | mean_r_ci_95.lower > 0 strict | +0.292 | ✅ |
| H7 | trim_5_5.mean_r > 0 strict | +0.717 | ✅ |
| H8 | temporal_concentration < 0.6 (class B) | 0.611 | ❌ — borderline (just above) |
| H9 | strategy − BH > 0 | +101.1 % | ✅ |
| H10 | gate-7 transferability > 70 % | n/a | ⚠️ deferred |

**Score**: 5 / 9 PASS (H10 excluded from numerator and denominator at gate 4).

**Critical observation on the failures**:

- H3 / H4 / H5 fail **by EXCESS, not by deficit**. The actual edge magnitude is 5–7× the spec's v1.1 a-priori upper bound. The spec's H3 / H4 bands were calibrated on the principle "cadence × 4 vs v1 → R per trade ÷ 2–3" (spec §0). The holdout disproves the dilution-by-cadence hypothesis on this cell: cadence × 4 produced R *higher*, not lower, than the a-priori band.
- H8 fails by 0.011 (tc=0.611 vs floor 0.600). Borderline; would have passed at h8_max=0.65.

**Comparison to v1**:

| Metric | v1 holdout (mom=126, K=3, rebal=10) | v1.1 holdout (mom=126, K=5, rebal=3) |
|---|---|---|
| n_closed | 21 | 72 |
| mean_r | +5.31 (small-sample artefact) | +2.017 |
| CI lower | -0.36 | +0.292 |
| trades/mo | 1.31 | 4.50 |
| projected annual | +83.6 % | +108.9 % |
| BH-Δ | n/a | +101.1 % |
| §3.6 verdict | FAIL (1.31 < 4) | PASS (4.50 ≥ 4) |
| §4 hypotheses | 5 / 9 PASS (under §3.5 floors) | 5 / 9 PASS |

v1.1 produces a structurally cleaner result than v1: 3.4× the sample, ci_low strictly positive (vs negative under v1), §3.6 PASS, and the projected annual return is *higher*, not lower, despite cadence × 3.4. The v1.1 cell genuinely captures more of the underlying signal — but the magnitude (proj +108.9 %) is well outside any pre-spec bracket, and the `tc` (0.611) is at the regime-fitting border.

---

## Train ↔ holdout drift (advisory)

| mean_r train | mean_r holdout | Δ | flag |
|---:|---:|---:|---|
| +0.656 | +2.017 | **+1.361 R** | ⚠️ overfit-suspect (|Δ| > 0.3 R) |

The drift is +1.361 R — **4.5× the spec's 0.3 R drift threshold**. Per spec §3.5: "If the holdout `mean_r` diverges by more than 0.3 R from the selected cell's train `mean_r`, this is an overfit signal — the verdict is not auto-archived but flagged in the report and held for operator review".

This is the largest train↔holdout drift observed across the four strategies in §11.1–§11.4. For comparison:
- TJR: drift was within noise on each grid sweep
- breakout-retest H4 v1: no holdout (ARCHIVE pre-holdout)
- mean-reversion BB H4 v1.1: no holdout (ARCHIVE pre-holdout)
- trend_rotation_d1 v1: drift +4.7 R but on n=21 (small-sample artefact)
- **trend_rotation_d1 v1.1: drift +1.361 R on n=72 — n is comfortable, drift is large**

Two competing readings of the +1.361 R drift:

(A) **Edge amplification at higher cadence** — the v1.1 cell (126/5/3) is structurally more efficient at extracting CSM Sharpe than the v1 cell (126/3/10). The holdout 16-month window happens to be a CSM-favourable trending régime (2025-2026), and the higher cadence captures more of that régime's signal than v1 did. Under this reading, the drift is real edge revealed, not noise.

(B) **Régime-fit amplification at higher cadence** — the same trending 2025-2026 window flatters CSM, and v1.1's higher cadence × larger basket means more positions are exposed to that single régime. The +101.1 % BH-Δ suggests the strategy is actively differentiating from passive equal-weight — but the differentiation could be in the direction of the régime, not orthogonal to it. Under this reading, the v1.1 holdout is over-amplifying a single régime.

These two readings are not separable from the v1.1 holdout alone. Gate 5 (Databento partial cross-check on the NDX/SPX/DJI futures subset) is the next discriminator, plus a régime-decomposition analysis (semester breakdown of the 16-month holdout).

---

## §4 H5 path-decision (per spec)

The H5 holdout value is +107.3 % projected annual — above the spec §3 viability threshold of 20 % and above the v1.1 H5 upper bound of 25 %. Per spec §4 H5 path-decision (carried over from v1.0 §4 H5 note):

- (a) Continue gates 5–8 anyway as a methodological learning, no Sprint-7 deployment commitment.
- (b) Archive with the explicit note "edge measurable but suspected régime-fit, drift > 4× protocol threshold" — adds a fourth case study.
- (c) Revise the v1.1 spec §4 bands and re-run with new criteria — **disqualified by anti-data-dredging discipline** (changing bands post-hoc to chase a result is exactly what the spec footer prohibits).

---

## Operator decision points

The verdict is REVIEW, locked by the binary §4 rule. The operator now needs to choose:

1. **Continue to gate 5 (Databento partial cross-check)** to discriminate edge-amplification (A) vs régime-fit (B). This is consistent with path (a) above and is the methodologically straightforward next step. If gate 5 confirms edge magnitude on the futures subset (within ±50 %), the régime-fit reading is weakened. If gate 5 produces a much smaller magnitude or a different sign, régime-fit is strongly suggested.

2. **Archive directly under "REVIEW → archive due to overfit-suspect drift"** — the +1.361 R drift is unprecedented in the strategy-research phase. The spec §0 v1.1 ARCHIVE-class footer would apply: "the strategy class HTF cross-sectional momentum multi-asset is structurally non-viable for the operator's deployment context." This is path (b) above.

3. **Defer the decision pending gate 5 + a régime-decomposition** (split the 16-month holdout into 4 quarterly buckets, check whether the +2.017 R is concentrated in 1-2 quarters or spread across the window). This combines (a) with a precautionary measurement before gate 5.

The choice is between a rigorous gate 5 + régime check (option 3, 1-2 days of work) and an immediate archive on drift discipline (option 2, 0 additional compute). Option 1 is the same as option 3 minus the régime decomposition.

The §4 verdict rule does not auto-decide — it surfaces the data and hands off to operator judgement, which is exactly the design intent.

---

## Methodological notes

1. **The spec v1.1 §4 bands were calibrated against a hypothesis (cadence-edge dilution) that the holdout disproved**. This is not a spec failure — it is the spec correctly surfacing a result outside its anticipated range. The disprove direction (edge stronger than anticipated, not weaker) is the more informative outcome: the bands functioning as a falsification test passed their job, even though all three excess-direction hypotheses fail.

2. **§3.6 worked**. v1 archived for §3.6 cadence floor; v1.1 expanded the grid to satisfy §3.6, and the §3.6-viable sub-grid produced exactly one §3.4-passing cell with strictly-positive train CI. The cadence-as-primary-filter mechanism (§11.5) selected a structurally different cell from v1, with 3.4× the sample, and produced a measurable gate-4-clean candidate.

3. **The drift size (+1.361 R) is the most informative single number from this run**. It dominates the conversation about whether v1.1 represents real edge or régime amplification. Any operator decision should treat the drift as a primary input, not a footnote.

4. **No spec change is being proposed**. The §4 verdict bands stay as written; the §3.4 floors stay as written; the §3.6 holdout double-check passed. The verdict is REVIEW and the spec rules surfaced exactly the information they were designed to surface.

---

## Suggested next

Operator discussion. The three branching paths are listed above. No further compute or commits are made before that discussion.

If the operator chooses **option 1 or 3** (continue): build a régime-decomposition diagnostic on the 16-month holdout (semester-level mean R), then run gate 5 Databento partial cross-check on the NDX / SPX / DJI futures subset (per spec §6) with the same selected cell.

If the operator chooses **option 2** (archive on drift discipline): apply the spec v1.1 footer's class-non-viability clause, move `src/strategies/trend_rotation_d1/` → `archived/strategies/trend_rotation_d1_v1_1/`, update protocol §11.4 / §11.5 with the v1.1 archive case study (5th archive in the strategy-research phase), and pivot to the next strategy class per §11.5 backlog (HTF single-asset wick-sensitive variants).
