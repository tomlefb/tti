# Strategy Research Protocol

> Canonical reference for testing any new strategy in this project.
> Distilled from the strategy-research phase (May 2026, branch
> `feat/strategy-research`, commits `14715da → 12fef06`) where TJR was
> the pilot subject. Read this before specifying, implementing, or
> backtesting any new strategy. This is a working document — checklists,
> chiffres, templates — not a theoretical essay.

---

## 0. TL;DR

1. **Hierarchy**: MT5 = ground truth runtime. Dukascopy = primary
   backtest. Databento = cross-check. Setup-level mismatch on
   wick-sensitive M5 detectors stays > 80 % across sources, even after
   timezone fixes; only price-level convergence (close H4/D1) is
   reliable (Pearson 0.99+).
2. **Viability gate**: any candidate must project ≥ 20 % annual
   return at 1 % risk/trade. Formula:
   `Mean R × Setups/month × 12 × 0.01 ≥ 0.20`.
3. **Pipeline**: 7 mandatory gates (spec → audit → Duk backtest →
   DBN cross-check → MT5 sanity → transferability → Phase C). Stop
   on first failed gate.
4. **Time budget**: 5–8 days per strategy, hard ceiling 12 days
   before mandatory archive.
5. **TJR pivot**: no measurable edge after eliminating 4 detector
   leaks, 1 timezone bug, 1 round of parametric variants, 1 round of
   outlier/regime stress tests. Verdict stable; archived as
   methodological reference.

---

## 1. Data sources — validated hierarchy

| Source | Role | Depth available | Granularity | Structure | Limits |
|---|---|---|---|---|---|
| **MT5 (FundedNext)** | Runtime ground truth | ~1.4 y M5 broker history | M5 OHLC, tick on demand | Athens broker tz (EET/EEST), DST-affected | Shallow history; broker-specific microstructure (spread/slippage/fills) cannot be reproduced offline |
| **Dukascopy** | Primary backtest | 14 y M5, tick-by-tick | M5 OHLC, full tick | Bank-aggregated mid; UTC-native | Wick differs from MT5 broker on M5 (>80 % setup-level mismatch persists post-tz fix) |
| **Databento** | Cross-check | 10 y M5 futures back-adjusted | M5 OHLC | Panama back-adjusted (level offset, structure preserved) | Futures != spot/CFD; level offset must be modelled; n=78 trades on TJR pilot |

**Empirical convergence (post timezone fix, May 2026)**:

| Comparison | Price level (close H4/D1) | Setup level (M5 sweeps, FVG bounds) |
|---|---|---|
| MT5 vs Duk | Pearson ≥ 0.99 | 81–96 % mismatch (irreducible with retail-accessible data) |
| MT5 vs DBN | Pearson ≥ 0.99 (modulo Panama offset) | 81–96 % mismatch |
| Duk vs DBN | Pearson ≥ 0.99 | < 30 % mismatch (both UTC, both tick-derived) |

**Implication**: a strategy that depends on the *exact wick* of an
M5 candle (sweep depth, FVG entry boundary, OB tag) is **not
validatable** against MT5 ahead of live deployment. Only HTF
strategies (decisions on closed H4/D1 candles) inherit the price-
level convergence.

---

## 2. Strategy classification — transferability

Before running any backtest, classify the candidate into one of
three buckets. Each bucket has a distinct validation path.

| Class | Definition | Transferability Duk → MT5 | Validation path |
|---|---|---|---|
| **HTF** | All decisions taken on closed H4 or D1 candles. Entry/SL/TP can be on lower TF, but trigger conditions are H4/D1 closes. | High (to be verified empirically per strategy via the pre-flight in §7). | Duk backtest is a reasonable proxy for live MT5 performance. |
| **LTF wick-sensitive** | Trigger condition depends on the M5 wick precisely (sweep depth, FVG inside-bar boundary, OB tag at low). | Low. > 80 % setup-level mismatch — Duk backtest **does not predict** MT5 behaviour. | Final validation only by **MT5 demo**, run for ≥ 6 months at planned cadence. Backlog if no demo capacity. |
| **Hybrid** | Trigger on H4/D1 + refinement on M5 wick. | Case by case. | Trigger validated on Duk; refinement layer must be measured separately on MT5. Only proceed if the trigger alone produces the bulk of the edge. |

**Rule**: when in doubt, classify down. A strategy assumed HTF
that turns out to read M5 wicks for entry confirmation is LTF
wick-sensitive in disguise.

---

## 3. Viability criterion — projected annual return

**Threshold**: 20 % projected annual return at 1 % risk/trade. Below
that, the strategy does not justify the engineering and operational
effort versus passive ETF World allocation (long-run reference
~6–8 % real, near-zero personal time).

**Formula**:

```
projected_annual_return = Mean R × Setups/month × 12 × risk_fraction
                       (default risk_fraction = 0.01)
```

To clear 20 % at 1 % risk, `Mean R × Setups/month ≥ 1.67`.

| Mean R | Min Setups/month | Comment |
|---|---|---|
| 0.20 | 8.4 | Hard to hit on HTF; requires LTF |
| 0.30 | 5.6 | Plausible HTF if multi-instrument |
| 0.40 | 4.2 | Comfortable HTF zone |
| 0.50 | 3.4 | Strong HTF zone |
| 0.75 | 2.3 | Very strong, quality > frequency |
| 1.00 | 1.7 | Excellent; check for survivorship/overfit |
| ≥ 1.5 | 1.2 | Suspect — investigate before believing |

**Frequency sweet spot** (operator decision, 2026-05-03 — folded
in after cadence pre-measure for the mean-reversion BB H4 spec):

| Setups/month/instrument | Diagnostic |
|---|---|
| < 1 | Insufficient — not viable |
| 1–3 | Hard to measure; viable only with very high Mean R (> 1R) |
| 3–5 | OK — comfortable sample size |
| **5–15** | **Sweet spot** — edge measurable in 6–12 months of holdout, capital actively working |
| 15–25 | High — costs start to bite seriously |
| > 25 | Too high — costs dominate the edge |

**Methodological notes**:

- Any spec that targets **< 3 or > 25 setups/month** must justify
  the choice explicitly (cite the structural reason: HTF D1
  cadence floor, anti-cost discipline, etc.).
- The **5–15 sweet spot maximises information per unit of time**
  and is the **default target** unless a structural reason
  argues otherwise (e.g. a D1-anchored strategy that naturally
  produces 1–2/month).
- The mean-reversion BB H4 v1 spec targets 3–5/month, **just
  below the sweet spot — assumed conservative in v1 for filter
  discipline**. If v1 admits, v2 should consider relaxing the
  filter stack to push cadence into the 5–15 band.

**Operational rule**: every `BacktestResult` must report the field
`projected_annual_return_pct` and the gate compares it to **20.0**
(see §9).

### 3.5 Selection criteria adaptation by strategy class

(Added 2026-05-04 after the trend-rotation D1 v1 archive §11.4 —
see that section for the worked example and §11.4.1 for the class
taxonomy.)

The §5.2 train-grid selection criteria (`n_closed`, `ci_low`,
`temporal_concentration`) were calibrated empirically on the
single-asset wick-sensitive HTF strategies of §11.1–§11.3 (TJR,
breakout-retest H4, mean-reversion BB H4 v1.1). Those floors are
**conservative and appropriate for that class**.

For **cross-sectional momentum multi-asset** strategies (class B
per §11.4.1), the same floors are too tight for two structural
reasons:

(a) **Per-trade variance is structurally higher**. A multi-asset
    basket mixes equity indices, FX, metals, oil and crypto with
    very different absolute volatilities. Risk-parity sizing
    normalises the dollar risk per asset but the signed-PnL
    distribution remains more dispersed than for a single-asset
    SL/TP-based strategy. With per-trade R distribution wider, an
    `n_closed` of 50–100 produces a 95 % bootstrap CI that
    typically covers zero even when the point-estimate `mean_r`
    is well above 1.0.

(b) **Temporal concentration is structural**. Cross-sectional
    momentum is regime-dependent by construction — wins cluster
    in trending periods, losses cluster in regime turns. The
    Moskowitz–Ooi–Pedersen (2012) *time-series momentum across
    asset classes* paper documents this on 30 years of futures
    data: the strategy's profit is concentrated in a minority of
    months. A `temporal_concentration < 0.4` floor calibrated on
    single-asset signal-vs-noise discrimination is the wrong
    measuring stick here.

**Class-B revised floors** (cross-sectional momentum multi-asset):

| Criterion | Class A (§5.2 standard) | Class B revised (§3.5) | Rationale |
|---|---:|---:|---|
| `n_closed` | ≥ 50 | **≥ 100** | Doubles the sample to compensate for the wider per-trade R distribution. |
| `ci_low_95` | ≥ 0 | **≥ -0.1 R** | Allows a small negative tail in the 95 % CI when the point-estimate is positive — recognises the per-trade variance structure. The strategy must still show a positive `mean_r` and beat buy-and-hold (H9). |
| `temporal_concentration` | < 0.4 | **< 0.6** | Adapts to regime-dependence. The class's profit clusters in trending months by construction; tc=0.5 is normal here, not an overfit flag. |

**§4 hypothesis evaluation on holdout** is *partially* relaxed
in line with §3.5:

- H6 (`mean_r_ci_95.lower > 0`): **strict**, unchanged. The
  holdout is the final-judge gate; if even there the CI cannot
  pinch zero off the negative side, the edge is not measurable.
- H7 (`outlier_robustness.trim_5_5.mean_r > 0`): **strict**,
  unchanged. Trimming the top/bottom 5 % is a per-trade
  robustness check, independent of class.
- H8 (`temporal_concentration < 0.4` → revised to **< 0.6** for
  class B): consistent with the §3.5 selection threshold.

**Universal applicability**. The §3.5 thresholds apply to **any**
strategy classified in §2 / §11.4.1 as cross-sectional momentum
multi-asset, not just `trend_rotation_d1`. The class is declared
in the spec §1; once declared, the §3.5 thresholds replace the
§5.2 defaults for that strategy.

**Anti-data-dredging**. This sub-section is dated 2026-05-04 and
the trend-rotation D1 v1 archive (§11.4) is the worked example
that motivated it. The original verdict on `trend_rotation_d1`
was ARCHIVE (commit `c2ddce2`) under the §5.2 standard floors;
the §3.5 revision is documented before any re-evaluation runs,
the H6 / H7 holdout floors stay strict, and the §4 verdict-rule
binary is unchanged. A revised verdict (commit to follow) is then
a class-corrected reading of the same train grid, not a
post-hoc rescue of the earlier outcome.

### 3.6 Operator viability constraint — minimum cadence

(Added 2026-05-04 after the trend-rotation D1 v1.0 holdout
re-evaluation under §3.5 produced a 1.31 trades/mo cadence on the
selected cell — see §11.4 final verdict and §11.5 transversal
lesson.)

En complément des floors statistiques §3–3.5, toute stratégie
doit respecter un floor opérationnel minimal:

(a) **Floor cadence**: 4 trades/mois portfolio minimum sur la
    fenêtre holdout.

(b) **Floor distribution**: les trades doivent être distribués
    sur 3+ semaines distinctes du mois moyen. Une stratégie qui
    produit 4 trades en 1 jour puis rien pendant 4 semaines ne
    satisfait pas ce critère, parce que statistiquement ces 4
    trades mesurent un seul événement.

**Justification**:

(i) Pour que les performances mesurées soient statistiquement
    représentatives sur des fenêtres courtes (1–3 mois live), il
    faut un sample-size minimal mensuel. À 1–2 trades/mois, une
    mauvaise séquence de 2–3 mois (très probable par variance pure)
    peut faire descendre le compte sous le drawdown limit de la
    phase de challenge.

(ii) Pour FundedNext Stellar Lite Phase 1: 5K capital, drawdown
     journalier 4 %, drawdown total 8 %. Une stratégie à 1 trade/mois
     avec mean R apparent +0.5 mais variance per-trade σ = 2R peut
     faire 3 mois consécutifs négatifs avec probabilité non-
     négligeable, busting la phase avant que l'edge ne se manifeste.

(iii) L'opérateur a besoin d'un feedback loop régulier pour ajuster
      comportement et règles. À 1 trade/mois, il faut 6 mois pour
      avoir 6 datapoints; à 4–5 trades/mois, ce sont 6 semaines.

**Application**:

Le critère §3.6 est checké en pre-measure §1.0 AVANT spec, sur le
grid candidat. Une classe de stratégies dont aucune cellule viable
ne dépasse 4 trades/mois portfolio est éliminée du backlog avant
spec.

Les classes structurellement éliminées par §3.6:

- HTF cross-sectional momentum multi-asset à rebalance ≥ 10 j
  (`trend_rotation_d1` v1, archived).
- HTF macro/carry multi-asset à rebalance mensuel ou supérieur.

Les classes qui passent §3.6 par construction:

- HTF single-asset wick-sensitive à 8–15 setups/mo/instrument
  (univers 5+ → 40+/mo).
- LTF single-asset (M5 / M15).
- HTF cross-sectional rotation à rebalance ≤ 7 j et K ≥ 4.

**Universal applicability**. §3.6 applies to **any** strategy
class, not only the cross-sectional momentum class that motivated
it. The pre-measure §1.0 step now reports both a raw-trigger
cadence and a §3.6 portfolio-cadence projection per candidate
grid cell. A spec is admissible only if at least one cell of its
§3.2 grid satisfies §3.6 by construction.

---

## 4. The 7-gate pipeline

Each strategy walks through these gates in order. **Failure at any
gate = stop. Do not proceed to the next gate.** If a gate fails for
a reason that points to a fixable bug, fix the bug, re-run all prior
gates that the bug could have invalidated, then continue.

**Pre-spec preparation** (before gate 1, added 2026-05-04 after the
MR BB H4 v1.1 archive — §11.3 lesson #1):

| # | Step | Deliverable | Why |
|---|---|---|---|
| 1.0 | **Cadence pre-measure** on raw triggers (no filters) | `calibration/runs/cadence_premesure_<name>_<TS>.md` | Anchor the spec H1 setups/month band on a *measured* number, not an intuitive guess (§11.2 lesson #1). |
| 1.5 | **Attrition diagnostic** through the proposed filter chain | `calibration/runs/attrition_diagnostic_<name>_<TS>.md` | Cumulative retention through every spec'd filter, before freezing. Catches "spec geometry produces n_closed << 50 on every cell" *before* gate 4 archives non-informatively (§11.3 lesson #1). The two pre-spec steps together calibrate H1 and ensure the §3.2 grid has at least one admissible cell per instrument. |

Gates 1-9:

| # | Gate | Deliverable | Inputs | Outputs | Pass criteria |
|---|---|---|---|---|---|
| 1 | **Specification** | `docs/strategies/<name>.md` | Strategy idea, classification per §2; cadence + attrition outputs from steps 1.0 / 1.5 | Pseudo-code of detector + entry/SL/TP rules; class (HTF/LTF/Hybrid); expected setups/month and Mean R | Reviewer can implement it without ambiguity |
| 2 | **Implementation** | `src/strategies/<name>/` + `tests/strategies/test_<name>.py` | Spec | Module + ≥ 1 unit test per detector branch | All unit tests green; type-checks pass |
| 3 | **Audit (look-ahead)** | `calibration/audit_<name>.py` | Implementation | Setup list when running streaming vs full-history modes | **100 % bit-identical**; not 99 % |
| 4 | **Backtest Duk** | `calibration/runs/<date>_<name>_duk.md` + JSON | Implementation, Dukascopy 5–10 y M5 + tick | `BacktestResult` per instrument | See §5 admission criteria |
| 5 | **Cross-check DBN** | `calibration/runs/<date>_<name>_dbn.md` | Same as #4, Databento window | `BacktestResult` per instrument | Mean R within ±30 % of Duk on overlapping window |
| 6 | **MT5 sanity** | `calibration/runs/<date>_<name>_mt5.md` | Implementation, MT5 1.4 y M5 | `BacktestResult` per instrument | Same direction as Duk; no violent contradiction (e.g. Duk = +0.5R / MT5 = -0.5R) |
| 7 | **Transferability** | `calibration/runs/<date>_<name>_transfer.md` | Setup lists from #4 and #6 | Setup-level overlap matrix | HTF: mismatch < 30 %. LTF: mismatch > 70 % expected — flag for demo-only validation |
| 8 | **Phase C (realistic costs)** | `calibration/runs/<date>_<name>_phaseC.md` | #4 results, broker spread/commission/slippage tables | `BacktestResult` post-cost | See §5 phase-C criteria |
| 9 | **Decision** | One-line entry in `docs/03_ROADMAP.md` + commit | All above | Either: promote to Sprint 7 demo, or archive | Operator approval |

(Yes, that's 9 lines for "7 gates". 8 and 9 are bookkeeping tied
to gate 7's outcome.)

---

## 5. Per-gate decision criteria

### 5.1 Audit gate (#3)

- [ ] Streaming mode (cycle-by-cycle) and full-history mode produce
      bit-identical setup lists.
- [ ] Every detector primitive (FVG, sweep, swing, MSS) iterates only
      on history available at the cycle's `now` timestamp.
- [ ] No `df.loc[future_idx]` patterns; no `range(0, len(df))` over the
      full dataframe in cycle-time loops.
- [ ] Test fixtures include a dataset with a known late-bar that
      should *not* be used; the audit confirms it isn't.

### 5.2 Backtest admission (#4)

- [ ] `n_closed ≥ 50` per instrument (else not measurable).
- [ ] `mean_r_ci_95.lower > -0.10`.
- [ ] **Outlier robustness**: removing the 5 best and 5 worst trades
      does not flip the Mean R sign.
- [ ] **Temporal distribution**: ≥ 60 % of semesters individually
      positive (`fraction_positive_semesters ≥ 0.60`).
- [ ] No single semester contributes > 50 % of cumulative R.
- [ ] `setups_per_month ≥ 1.0` (else non-viable regardless of Mean R).
- [ ] **Buy-and-hold benchmark** computed on the same window: strategy
      must beat it (or trivially differentiate, e.g. directional
      neutrality vs long-only beta).

### 5.3 Cross-source validation (#5, #6)

- [ ] Duk vs DBN: `|mean_r_duk − mean_r_dbn| / |mean_r_duk| ≤ 0.30`
      on the overlapping date window.
- [ ] MT5 sanity: same sign on Mean R; if opposite signs, **stop** and
      investigate (do not pretend the bigger sample wins).

### 5.4 Transferability (#7)

- [ ] HTF strategies: setup-level mismatch Duk vs MT5 < 30 %. If above,
      either reclassify as LTF wick-sensitive or identify the wick
      dependency in the trigger.
- [ ] LTF wick-sensitive: mismatch > 70 % is expected. Document it,
      then **gate the strategy on demo-MT5 validation** before live.

### 5.5 Phase C (#8)

- [ ] `mean_r_post_costs > 0`.
- [ ] `mean_r_ci_95_post_costs.lower > -0.05`.
- [ ] `projected_annual_return_pct ≥ 20.0`.
- [ ] Survives a trivial baseline (buy-and-hold on the same window,
      or random entry with same SL/TP/cadence).

---

## 6. Methodological pitfalls — operational checklist

Each pitfall below was paid for in time during the TJR pilot or a
later archive (referenced inline). Re-read this list at gates 1
(spec freeze), 3 (audit) and 4 (backtest).

- [ ] **Pre-spec attrition blind spot** (§11.3 lesson, MR BB H4
      v1.1): the cadence pre-measure (§4 step 1.0) only counts raw
      triggers; the cumulative retention through every spec'd
      filter can be 100× tighter than the intuitive estimate that
      anchors H1. **Always run the attrition diagnostic (§4 step
      1.5) BEFORE freezing the spec.** A v1.0 that ships without
      this measurement risks gate 4 archiving for *cause of n*
      rather than cause of edge — a non-informative outcome.
- [ ] **Win rate ≈ RR breakeven is a chop fingerprint, regardless
      of strategy direction** (§11.2 + §11.3 lessons). Trend-
      following at RR 2.0 → breakeven 33.3 %; mean-reversion at
      RR 1.0 → breakeven 50 %. If the train grid produces win
      rates landing within ±5 pp of the implied breakeven on
      every cell, the strategy doesn't time entries better than
      chance — archive precociously.
- [ ] **Detector look-ahead**: forward iteration on FVG / sweep / swing /
      MSS. Audit at gate 3 must be 100 % bit-identical.
- [ ] **Broker timezone bug**: MT5 broker times are Athens (EET/EEST)
      with DST. Convert at connect-time, not at parse-time, and store
      the offset along with each fixture so re-conversion is auditable.
- [ ] **Integration tests with hardcoded bounds**: parameterize on
      fixture depth (already fixed in commit `7b444a9`). Re-applying:
      never write `assert len(setups) == 42` against a fixture whose
      length is implicit.
- [ ] **Legacy non-tick backtests**: any backtest on M5 OHLC alone,
      without tick-replay, **systematically inflates** Mean R because
      same-bar SL/TP races are arbitrated optimistically. Use
      `src/backtest/tick_simulator.py`.
- [ ] **Parametric sweep without pre-specified hypothesis**: data
      dredging. Either declare the hypothesis before the sweep, or
      treat the sweep as exploratory and re-validate the winner on a
      held-out window.
- [ ] **Regime fitting on favourable windows**: NDX bull run 2023–2025
      flatters everything. Always cut the window into ≥ 2 regimes
      (semesters at minimum) and require both to be positive.
- [ ] **Buy-and-hold benchmark omitted**: a strategy that returns
      +30 % annual on NDX during a +60 % NDX year is not edge, it is
      undertrading beta. Always compute the baseline.
- [ ] **Outliers**: with `n < 100`, the top/bot 5 trades can swing the
      verdict. Robustness check is mandatory (see §5.2).
- [ ] **Survivorship in instrument selection**: do not pick the top
      instrument across a grid as "the result". Pre-specify the
      portfolio.
- [ ] **Unit ambiguity**: store R as a fraction (1.0 = one R), not a
      percent. Project-wide convention; mismatches caused two re-runs
      during TJR.

### 6.5 Cross-source comparison alignment protocol

(Added 2026-05-05 after the trend_rotation_d1 v1.1 gate 7 false-archive
incident — see §11.4.2.)

For any **class-B** strategy (HTF multi-asset cross-sectional
momentum per §11.4.1), all cross-source comparisons run at gates 5
(Databento partial cross-check), 6 (MT5 sanity), and 7
(transferability) **MUST** apply the alignment protocol below
**before** any metric is computed. Class-A and class-C strategies
should follow it whenever their gate-5/6/7 comparison spans assets
with heterogeneous trading hours (24/7 crypto vs Mon-Fri equities,
broker-tz FX vs UTC indices, etc.).

**(a) Timestamp normalisation**: every bar in every source must be
re-labelled to the same UTC convention before comparison. The
project standard is **00:00 UTC** for D1 bars (equivalent to
Yahoo's convention; MT5 broker-stored times must be normalised
to calendar-date by dropping the broker-tz hour, not by shifting
the price). Sources that natively label at a different hour
(Athens-broker midnight on FX/metals/crypto = 21:00/22:00 UTC)
are normalised by `df.index = df.index.normalize()` after the
canonical broker→UTC conversion.

**(b) Calendar intersection**: each per-asset frame is restricted
to dates present in **all** sources being compared. Bars exclusive
to one source (Yahoo BTC weekend bars vs MT5 broker Mon-Fri-only
BTC; MT5 broker Sunday-evening index opens vs Yahoo's NYSE
calendar; etc.) are dropped. The intersection step is per-asset,
not per-panel — different assets retain different common-date
sets, and each asset is scored on its own intersected frame.

**(c) Per-asset diagnostic**: before publishing a gate-5 / 6 / 7
verdict, the report MUST list, per asset, the bar-count loss
caused by the (a)+(b) alignment. Any asset losing > 30 % of its
bars is flagged "at risk of residual sample bias" — the gate may
still pass, but the verdict report explicitly notes which assets
are downsampled and how much.

**Justification**: comparing bars sampled at different timestamps
(or covering different calendar densities) is comparing different
samples of the same underlying market and produces artefacts that
look like transferability failures without being one. The
trend_rotation_d1 v1.1 gate 7 false-archive (raw exact-match
22.7 %, corrected 81.5 %) was caused entirely by this — same cell,
same cell, same window, same code; the only thing that changed was
applying (a)+(b). See §11.4.2 case study.

**Universal application**: this protocol applies to any future
strategy classified class-B in §11.4.1. The TJR-class (class-A)
HTF wick-sensitive strategies were less exposed because gate-7's
"setup-level mismatch" metric is intrinsically looser than the
"top-K basket equality" metric used by class-B — but the same
alignment is mandatory if class-A or class-C strategies expand
their universe to include 24/7 instruments alongside Mon-Fri
ones.

**Implementation reference**:
`calibration/run_gates_678_corrected.py` (commit pending) is the
worked example. The two helpers
`calibration.investigate_top_k_divergence.normalise_to_calendar_date`
and `intersect_panels` are the canonical implementation of (a)
and (b); reuse them rather than re-implementing.

---

## 7. Prioritised strategies (post-TJR pivot)

### Priority 1 — HTF candidates (estimated 2–5 setups/month)

1. **Breakout retest, close H4** — break of prior H4 swing, retest
   within N candles, entry on confirmation candle.
2. **Mean reversion Bollinger H4** — close H4 outside ±2σ band,
   re-entry trigger inside band.
3. **Momentum EMA cross H4** — EMA(20) cross EMA(50) on H4 close,
   filtered by D1 trend. (Lower-bound frequency; check projected
   annual return early.)

### Priority — Infrastructure pre-flight (before §1 implementations)

**P0**: validate empirically that the price-level Pearson 0.99 between
Duk and MT5 on closed H4/D1 candles **translates into similar
trigger signals** for an HTF strategy. Test case: "long when
close H4 > MA50(H4)". Compare the trigger timestamp series Duk vs
MT5 over 1.4 y. Expected mismatch < 5 % if the convergence holds at
the signal level. If it does not, HTF transferability is also
suspect and the priority-1 list must be reconsidered.

Estimated effort: 30–60 min. **This precedes all priority-1 work.**

### Backlog — LTF wick-sensitive

- Pure ICT (BPR, IFVG, mitigation blocks)
- Wyckoff intraday
- Order-flow imbalance (OFI)

Reconsider only if a path to measure MT5 transferability without a
6-month demo cycle is identified (e.g. broker-grade tick recording
service, or empirical evidence that mismatch decays for a specific
sub-class).

---

## 8. Time budget and archive rule

| Phase | Target |
|---|---|
| Specification | 2–4 h |
| Implementation + unit tests | 1–2 d |
| Audit (gate 3) | 0.5–1 d |
| Backtests Duk + DBN + MT5 (gates 4–6) | 1–2 d |
| Transferability (gate 7) | 0.5 d |
| Phase C (gate 8) | 0.5 d |
| Decision (gate 9) | 2 h |
| **Total** | **5–8 d** |

**Hard stop-loss**: 12 days from spec to admission. Beyond that,
**mandatory archive**.

### Archive structure

`archived/strategies/<name>/` with a README containing:

```markdown
# <name> — archived

- Started: YYYY-MM-DD
- Archived: YYYY-MM-DD
- Failed at gate: <number, name>
- Reason: <one paragraph>

## Numbers
- Mean R, n, setups/month, projected annual return
- Cross-source deltas
- Outlier robustness result

## Hypotheses that could have saved it
- Bullet list of "if I had tried X first…"

## Transferable learnings
- Bullet list. These feed back into this protocol on the next revision.
```

---

## 9. `BacktestResult` standard format — implemented extensions

Dataclass: `src/backtest/result.py` (`BacktestResult`).

Four protocol-driven derived metrics are implemented and unit-tested
(commits `efb7282`, `ff8ef3b`, `46bafc1`, `0cd2ffc`, `53e6123`):

| Field | Type | Surface | Computation | Gate |
|---|---|---|---|---|
| `projected_annual_return_pct` | `float` (property) | Recomputed on access; surfaced in JSON | `mean_r × setups_per_month × 12 × risk_per_trade_pct` | §3 viability — must be `≥ 20.0` at default 1 % risk |
| `risk_per_trade_pct` | `float` field, default `1.0` | Stored | Caller-overridable risk fraction in percent feeding the property above | §3 |
| `outlier_robustness` | `dict[str, dict[str, float] \| None]` | Stored | `trim_0_0` / `trim_2_2` / `trim_5_5`: mean R after removing the K best and K worst closed trades, with bootstrap CI on the trimmed sample. `trim_2_2` and `trim_5_5` are `None` when `n_closed < 20`. | §5.2 — verdict must not flip directional sign across levels |
| `temporal_concentration` | `float \| None` | Stored | `max(\|semester_R\|) / \|total_R\|` across H1/H2 buckets. `None` when no closed trades or `total_r == 0`. | §5.2 — `> 0.5` flags regime fitting |
| `vs_buy_and_hold` | `dict[str, float] \| None` | Stored | Populated when caller passes `bh_close_start` / `bh_close_end` to `from_setups`. Returns `bh_total_return_pct`, `bh_annualized_pct` (geometric annualisation), `strategy_annualized_pct`, `strategy_minus_bh_pct`. | §5.2 — `strategy_minus_bh_pct > 0` required to clear admission |

**Decoupling**: `result.py` does not load fixtures itself. Callers
pull the buy-and-hold close prices from Dukascopy (or whichever
source is appropriate) and pass them in.

**Backwards compatibility**: legacy run JSONs (no protocol §9 keys)
load via field defaults. The `projected_annual_return_pct` property
recomputes from `mean_r × setups_per_month × 12 × 1.0` so it is
always available; the other three fields default to empty / `None`
on legacy payloads. Sanity-validated on the 42 existing run JSONs
under `calibration/runs/` (commit `53e6123`).

Existing fields used by gates: `n_setups`, `mean_r`, `mean_r_ci_95`,
`setups_per_month`, `fraction_positive_semesters`. No removals.

---

## 10. Technical debt backlog

Tracked here so it does not get lost; not blocking new strategies
unless flagged.

- **Slow tick-simulator tests** (2 currently): mark with
  `@pytest.mark.slow` and configure `pyproject.toml` to exclude by
  default; run explicitly in CI.
- **DST live runtime edge case**: `broker_naive_seconds_to_utc`
  captures the offset at connect-time. If the live process spans a
  DST flip, the offset goes stale. Action: re-fetch on each cycle
  start, or schedule a daily reconnect at 02:00 broker time.
- **TJR archival**: move `src/strategies/tjr/` →
  `archived/strategies/tjr/` once operator validates the pivot. Keep
  the test file in place until the move is rebased onto `main`.
- **Sprint 7 production**: decide between (a) park the demo run as
  dormant (operator can resume), or (b) full stop and remove the
  scheduler from boot config. Tied to TJR archival decision.

---

## 11. Archived strategies — case studies

Two pilots have run this protocol end-to-end as of 2026-05-03 — TJR
(below) and breakout-retest H4 v1 (further down). They reached
**ARCHIVE verdicts via opposite surface signatures**, which is the
empirical evidence that the methodology distinguishes both failure
modes without ceremony:

- **TJR** spent weeks looking edge-positive on MT5 NDX (apparent
  +1.56 R), then decomposed into outliers + regime + look-ahead
  bugs as the audit / cross-source gates were enforced.
- **breakout-retest H4 v1** produced no signal on any of 27 grid
  cells; gate 4 admission failed in 92 minutes wallclock without
  ever reaching holdout.

The same gate sequence handled both. Pre-specification + binary
verdict held under both pressure shapes — slow erosion of an
apparent edge in one case, immediate refusal of calibration in the
other.

### 11.1 TJR archive

**Status**: pivoted away from. Kept as the canonical methodological
reference for this protocol.

**Pilot subject of the strategy-research phase** (May 2026, branch
`feat/strategy-research`).

**Verdict**: no measurable edge, on any source, after methodical
elimination of:

- 4 detector look-ahead leaks (FVG, sweep, swing, MSS forward
  iteration);
- 1 broker timezone bug (Athens EET/EEST not converted to UTC at
  ingest);
- 1 round of parametric sweep variants (8 grids on sweep
  parameters);
- 1 round of stress-tests against the apparent +1.56 R headline on
  NDX MT5, which decomposed into outliers + a 2025-H2 spike + bullish-
  beta bias on the available MT5 window.

Stable across 10 y Databento (n = 78), Dukascopy 14 y, and MT5
re-measurement post-fix.

**Apprentissages distilled into this protocol**:

| Source | Now lives in |
|---|---|
| Look-ahead leaks | §5.1 audit gate, §6 pitfall list |
| Timezone bug | §6 pitfall list, §10 backlog |
| Parametric sweep dredging | §6 pitfall list |
| Outlier sensitivity | §5.2 admission, §9 BacktestResult extension |
| Regime fitting | §6 pitfall list |
| Buy-and-hold benchmark | §5.2 admission, §9 BacktestResult extension |
| MT5 vs Duk wick mismatch | §1 hierarchy, §2 classification, §7 transferability |
| Time-budget discipline | §8 archive rule |

The TJR codebase remains in `src/strategies/tjr/` until operator
greenlights the move to `archived/strategies/tjr/`.

### 11.2 breakout-retest H4 v1 archive

**Status**: archived after gate 4 (commit `2b98cd1`, 2026-05-03).
Spec, postmortem, and README live under
`archived/strategies/breakout_retest_h4_v1/`.

**Failure step**: gate 4 train calibration. Across 27 cells (3
instruments × 9), zero passed the
``n_closed >= 50 ∧ ci_low >= 0 ∧ temporal_concentration < 0.4``
selection trio. Holdout never ran — there was no calibrated model
to evaluate on it.

**Surface signature**: win rate 30–34 % on RR 2.0 = right at the
33 % breakeven, consistent with "the breakout-retest pattern
itself does not predict more winners than chance on these
instruments at H4 in 2020-2024". `mean_r_ci_95.lower` was
negative on every one of 27 cells.

**Cadence note** (added 2026-05-03 after §3 sweet-spot revision):
the ≈ 8 setups/month observed on this strategy were perfectly
acceptable under the revised 5–15 sweet spot — the archive driver
was the **near-zero Mean R**, not the cadence. The §11.2 lesson
#1 ("setups/month systematically too low in pre-specs") is about
the *pre-spec H1* being miscalibrated, not about the *measured*
cadence being out of the viable band.

**Apprentissages distilled into this protocol**:

| Source | Now lives in |
|---|---|
| Setups/month estimates systematically too low in pre-specs | §6 pitfall list — pre-measure trigger cadence on a few months of history before locking H1 |
| D1 trend filter alone insufficient to separate continuation from chop | §6 pitfall list — trend-following pre-specs should add an active-trend gate (ATR / ADX / HH-HL on the trade timeframe) before committing to §4 bands |
| Win-rate aligning with RR breakeven is a diagnostic of the §5.2 chop pitfall | §6 pitfall list — call this signature explicitly so future archives spot it earlier |
| Pre-spec + binary verdict holds when calibration produces no signal | §4 hypothesis verdict rule — the existing rule already covers it; this archive is the empirical confirmation |

The implementation, tests, audit harness, and gate-4 runner stay
in the live tree (`src/strategies/breakout_retest_h4/`,
`tests/strategies/breakout_retest_h4/`,
`calibration/audit_breakout_retest_h4.py`,
`calibration/run_breakout_retest_h4_grid.py`) as architectural and
test-pattern reference for the next HTF candidate. Re-using the
audit harness skeleton on the next strategy is roughly a one-line
swap.

### 11.3 mean-reversion BB H4 v1.1 archive

**Status**: archived after gate 4 (commit `ec4bdd4`, 2026-05-04).
Spec, postmortem, attrition diagnostic, and README live under
`archived/strategies/mean_reversion_bb_h4_v1_1/`. Second
consecutive HTF archive after `breakout_retest_h4_v1`.

**Failure step**: gate 4 train calibration. Across the v1.1
broadened 36-cell grid (3 instruments × 12 cells, `min_pen ∈
{0.0, 0.1, 0.2, 0.3}` × instrument-specific `sl_buffer`), zero
cells passed the `n_closed >= 50 ∧ ci_low >= 0 ∧
temporal_concentration < 0.4` selection trio. The binding gate
was `ci_low >= 0` — every cell's 95 % CI on mean R covered
zero. Holdout never ran.

**Surface signature**: win rate 28–37 % (XAU/NDX) and 14–28 %
(SPX), well below the strategy's RR ≈ 1.0 breakeven (~50 %).
SPX `pen=0.0` cells had `ci_high` *negative* on 1.0 / 2.0 / 3.0
sl_buffer — measurably losing, not merely indistinguishable.
NDX `pen=0.3 sl=3.0` showed `mean_r=+1.250` at face value but
`n=23` (below admission floor) and `ci_low=-0.238` —
small-sample artefact. Same broad family as breakout-retest's
chop fingerprint.

**Process step that worked**: the v1.1 modification (commit
`ae61f70`) made the strategy *measurable* — H1 cadence target
hit on XAU/NDX at `pen=0.0` (1.55 / 1.13 month). What v1.1 could
not fix is the absence of edge in the underlying mean-reversion
premise on these three instruments at H4 in 2020-2024.

**Apprentissages distilled into this protocol**:

| Source | Now lives in |
|---|---|
| Pre-spec attrition blind spot — cadence pre-measure on raw triggers systematically under-estimates cumulative filter attrition | §1 (new step 1.5 "attrition diagnostic") + §6 pitfall list — measure per-stage retention BEFORE freezing H1 |
| Chop fingerprint applies to mean reversion too, not only trend-following | §6 pitfall list — extended to "win rate ≈ RR breakeven (whatever direction the strategy takes)" as a precocious-archive marker |
| The "modification documented before gate 4 on the basis of a diagnostic" pattern is methodologically defensible | §11.3 (this section) records the v1.0 → v1.1 transition as a worked example; §4 verdict-rule discipline holds because hypotheses are revised explicitly with a versioned spec change, not loosened post-hoc |
| `n_closed >= 50` admission floor protects against small-sample apparent edges (e.g. NDX +1.25 R on n=23) | §5.2 — keep the floor; consider raising it for HTF strategies with short holdout windows |

The implementation, tests, audit harness, and gate-4 runner stay
in the live tree (`src/strategies/mean_reversion_bb_h4/`,
`tests/strategies/mean_reversion_bb_h4/`,
`calibration/audit_mean_reversion_bb_h4.py`,
`calibration/run_mean_reversion_bb_h4_grid.py`) as architectural
reference. The v1.1 spec changelog (§0 of the archived SPEC.md)
documents the modification log pattern for future iterations.

### 11.4 trend_rotation_d1 v1: archive with a different signature

**Final verdict trend_rotation_d1 v1: ARCHIVE — cadence
insufficient.**

The selected cell (mom = 126, K = 3, rebal = 10) produced
1.31 trades/mo on holdout, which is 3× below the operator
viability floor §3.6 (4 trades/mo minimum, distributed across 3+
weeks).

This verdict supersedes the original "REVIEW (5/9 hypotheses
PASS)" assessment from gate 4. The hypotheses-PASS verdict was
mathematically valid but operationally meaningless: a strategy
that doesn't produce enough trades cannot be deployed, regardless
of mean R magnitude.

The +84 % projected annual return on holdout (n = 21) was not
discounted as régime-fit per se, but as structurally non-actionable
on this cadence. Even if the edge is real, 1–2 trades/mo cannot be
deployed on a 5K Phase 1 challenge with daily/total drawdown
limits.

A v1.1 with cadence-oriented parameter expansion (rebalance ≤ 7 d,
K = 4–5) is being explored to preserve potential edge while
satisfying §3.6. Modification documented in the
`trend_rotation_d1_v1.1` spec, frozen before re-run per §11.3 #3
pattern.

---

**Status (technical history)**: technical archive after gate 4
(commit `c2ddce2`, 2026-05-04) under the §5.2 standard floors;
class-adapted re-evaluation under §3.5 documented in commits
`e36ab00` / `e5fc221` / `411aa89`; final ARCHIVE verdict on §3.6
cadence-floor recorded by this commit. Spec, postmortem, and
README to live under `archived/strategies/trend_rotation_d1_v1/`.

**Failure step (under §5.2 standards)**: gate 4 train calibration.
Across the 8 cells of the §3.2 grid, **zero** cleared the
``n_closed >= 50 ∧ ci_low >= 0 ∧ temporal_concentration < 0.4``
trio. Holdout never ran under the standard rule.

**Surface signature is qualitatively different from §11.1–§11.3**.
The three prior archives shared the chop fingerprint (win rate
≈ RR-implied breakeven, mean R ≈ 0). This one does not:

| Cell (mom/K/rebal) | n_closed | mean_r  | ci_low  | tc    | win   | bh-Δ  |
|-------------------:|---------:|--------:|--------:|------:|------:|------:|
| 63 / 3 / 10        |     156  | +0.596  | -0.352  | 0.888 | 45.5% | -1.3% |
| 63 / 3 / 21        |     109  | +0.579  | -0.848  | 1.343 | 45.0% | -7.2% |
| 63 / 4 / 10        |     203  | +0.391  | -0.474  | 1.375 | 43.8% | -4.0% |
| 63 / 4 / 21        |     146  | +0.323  | -0.987  | 2.581 | 40.4% |-10.3% |
| **126 / 3 / 10**   |    106   | **+1.338** | **-0.016** | **0.420** | 56.6% | +8.3% |
| 126 / 3 / 21       |      73  | +1.588  | -0.459  | 0.458 | 53.4% | +3.2% |
| 126 / 4 / 10       |    138   | +1.064  | -0.150  | 0.456 | 51.4% | +9.3% |
| 126 / 4 / 21       |     101  | +1.019  | -0.726  | 0.820 | 52.5% | +0.6% |

The **mom=126** cells (4 of 8) show:

- mean_r between +1.0 and +1.6 R — **6× higher than the §3 floor
  for viability** at a typical 1 setup/month cadence;
- win rate 51–57 % (within the §4 H2 budget [50, 60]);
- 4 of 4 beat the equal-weight buy-and-hold basket (H9 spirit
  PASS); the 126/4/10 cell beats EW by +9.3 %.

The strategy has **measurable signal**. It fails the §5.2 standard
floors not because the edge is absent, but because the floors are
calibrated for a different strategy class.

**Operator decision (commit `[next]`, 2026-05-04)**: introduce
**§3.5 class-adapted selection criteria**. Under the revised
class-B floors (`n_closed ≥ 100`, `ci_low ≥ -0.1 R`, `tc < 0.6`),
two cells pass selection (126/3/10 and 126/4/10); a revised
verdict reading of the same train grid is then computed by
running the holdout on the §3.5-selected cell. **Documented
before any re-evaluation run** to keep the verdict-rule
discipline; H6 / H7 holdout floors stay strict; the §4 binary
verdict rule (≥ 6 / 10 PROMOTE) is unchanged.

**Apprentissages distilled into this protocol**:

| Source | Now lives in |
|---|---|
| Fourth archive, fingerprint not chop. The strategy class governs which floors are appropriate. | §3.5 (this commit) — class-adapted selection criteria. §11.4.1 — explicit class taxonomy. |
| Per-trade variance is structurally higher on multi-asset baskets than on single-asset SL/TP setups; n=50–100 is too small to pinch zero off the CI even with mean_r > 1 | §3.5 lesson (a) + revised n_closed ≥ 100 |
| Cross-sectional momentum is regime-dependent by construction (Moskowitz–Ooi–Pedersen 2012); tc > 0.4 is normal, not overfit | §3.5 lesson (b) + revised tc < 0.6 |
| Sub-section §3.5 must be applied universally to all class-B strategies, not ad-hoc to trend_rotation_d1 | §11.4.1 taxonomy + §3.5 wording "applies to **any** strategy classified as cross-sectional momentum multi-asset" |

The implementation, tests, audit harness, and gate-4 runner stay
in the live tree (`src/strategies/trend_rotation_d1/`,
`tests/strategies/trend_rotation_d1/`,
`calibration/audit_trend_rotation_d1.py`,
`calibration/run_trend_rotation_d1_grid.py`) as architectural
reference for class-B candidates. The pipeline scaffold (panel
dict, cycle_dates union, TradeExit aggregation,
`rebalance_close` outcome on `BacktestResult`) is reusable for
v2 / v3 candidates.

### 11.4.1 Strategy classification taxonomy

The §3 / §3.5 selection criteria depend on the strategy class.
Three classes are recognised:

| Class | Strategies | Selection floors |
|---|---|---|
| **A** — HTF single-asset wick-sensitive | TJR (§11.1), breakout-retest H4 (§11.2), MR BB H4 (§11.3); future: any single-asset HTF candidate with discrete SL/TP setups | §5.2 standard: n_closed ≥ 50, ci_low ≥ 0, tc < 0.4. H8 strict at < 0.4. |
| **B** — HTF multi-asset cross-sectional momentum | trend_rotation_d1 (§11.4); future: futures rotation variants, multi-period momentum, regime-gated rotation | §3.5 revised: n_closed ≥ 100, ci_low ≥ -0.1 R, tc < 0.6. H8 relaxed to < 0.6. H6 / H7 strict (unchanged). |
| **C** — LTF single-asset (M5 / M15) | None tested yet; backlog candidates per §7 | TBD. The §5.2 standard is provisionally inherited but the per-trade R distribution shape is likely different again — the first class-C archive will calibrate the floors. |

**Spec requirement**: every new strategy spec §1 declares its
class explicitly. The class determines which §3 / §3.5 floors
apply at gate 4 and which H8 threshold applies in the §4
holdout evaluation.

### 11.4.2 trend_rotation_d1 v1.1 — gates 6/7/8 PASS after measurement-artefact fix

(Added 2026-05-05 alongside §6.5 — captures the "false archive
narrowly avoided" case study that motivated §6.5.)

The v1.1 cadence-oriented re-spec (rebalance ≤ 7 d, K ≤ 5,
spec commit `bb12a95`) selected cell **126/5/3** at gate 4 with
a REVIEW verdict (5/9 hypotheses, drift +1.361 R train→holdout;
commit `efe599e`). Operator path was option (B): walk-forward 20-y
on Yahoo (commit `93cd60a`) and excl-BTC sanity (commit `1b1c36b`)
returned a stationarity-confirmed edge at magnitude 15-35 %/year.
Operational risk simulation (commit `1644e55`) classified the
strategy as **risky-but-acceptable** for Phase-1 (54.3 % attempt-pass
at 1 % risk). Economic simulations baseline + pyramidal (commits
`9dac82c` / `f282332`) recommended `PROMOTE gate 6 MT5 sanity`.

**Gates 6/7/8 first run** (commit pending; 2026-05-05) on the
raw MT5 + Yahoo panels produced a structurally surprising result:

| Gate | Raw-panel verdict | Detail |
|---|---|---|
| 6 — MT5 sanity | ⚠️ REVIEW | direction agreement 63.1 % < 70 % threshold |
| 7 — top-K transferability | ❌ ARCHIVE per spec | exact-match **22.7 %** (138/607); spec H10 demanded > 70 % |
| 8 — granular FundedNext fees | ✅ PASS | post-fee mean_r +1.152 R, fees only 1.2 % of edge |

The raw-panel gate-7 22.7 % matched the operator's pre-registered
"STOP and report" condition ("top-K agreement 20 %"). Investigation
(commit pending; `investigation_top_k_divergence_*`) decomposed
the divergence into three pre-spec hypotheses:

| H | Cause | Confirmed? |
|---|---|---|
| H1 | MT5 broker-tz timestamps (21:00/22:00 UTC) vs Yahoo 00:00 UTC labels on BTC/EUR/GBP/XAU | ✅ — 4 of 15 assets misaligned |
| H2 | Calendar-day asymmetry: Yahoo includes BTC weekends (24/7), MT5 broker treats BTC as Mon-Fri; MT5 has Sunday-evening index opens Yahoo skips | ✅ — BTC: MT5 1512 bars vs Yahoo 2122 bars over the 5.8-y window; indices have inverse skew |
| H3 | Different price-source streams (Yahoo Coinbase-anchored vs MT5 broker aggregator) | ❌ — no systematic bias; mean diff ≈ 0 across the board, daily abs diff dominated by H1 timestamp offset |

H1 + H2 are pure measurement artefacts. The same `momentum_lookback_days = 126`
spans **6 calendar months on MT5 BTC** but only **4.1 calendar
months on Yahoo BTC** because MT5 has fewer per-week bars on a
24/7 instrument — directly causing K-th-slot ranking flips for
the most volatile asset of the universe. H3 is consistent with
"same asset, different snapshot time", not "different underlying
stream".

**Gates 6/7/8 corrected re-run** (commit pending) under the
§6.5 alignment protocol:

| Gate | Raw | Corrected | Verdict |
|---|---:|---:|---|
| 6 — MT5 sanity (mean R mismatch) | 44.4 % | **18.7 %** | ✅ PASS |
| 6 — direction agreement | 63.1 % | **87.1 %** | ✅ PASS (> 70 %) |
| 7 — top-K exact match | 22.7 % | **81.5 %** | ✅ PASS (> 70 %) |
| 7 — ≥ K-1 overlap | 79.7 % | **99.6 %** | ✅ PASS |
| 8 — mean R post-fee | +1.152 R | +1.572 R | ✅ PASS (≥ +0.3 R) |

**3/3 PASS post-correction.** The full v1.1 stack — gate 4
REVIEW + investigation + 20-y walk-forward + operational risk +
economic simulation + gates 6/7/8 corrected — clears Phase-1
deployment. Strategy v1.1 PROMOTE.

**Lesson distilled into §6.5**: a pre-frozen H10 threshold
(top-K > 70 %) was nearly going to archive a strategy whose
underlying transferability was 81.5 %. The threshold was correct;
the comparison protocol used to measure against it was buggy.
Without the §6.5 alignment, the v1.1 verdict would have been
"5th archive in the strategy-research phase, class non-viable
footer applies" — overcommitted on a measurement bias. With
§6.5, future class-B strategies measure transferability on
calendar-aligned, intersection-only panels and avoid the same
trap.

**Status**: PROMOTE to gate-9 / Sprint-7 deployment. Implementation
in `src/strategies/trend_rotation_d1/` stays in the live tree; the
scheduler integration (replacing or complementing TJR) is the
operator's next decision.

### 11.5 Cadence as primary viability filter

(Added 2026-05-04 alongside §3.6 — this section captures the
transversal lesson distilled from the 4-strategy archive sequence.)

Pre-measure cadence §1.0 must check §3.6 floor BEFORE spec
finalisation. The 4-strategy archive sequence revealed that
cadence-floor failure was the binding constraint on 1 of the 4
archives (`trend_rotation_d1` v1). The other three (TJR §11.1,
breakout-retest H4 §11.2, mean-reversion BB H4 §11.3) all
satisfied cadence by construction; their archive drivers were
edge-related (chop fingerprint, look-ahead leaks, regime-fit),
not cadence.

**Lesson**: in any future strategy backlog, the first triage
question is "Does the class structurally produce ≥ 4 trades/mo
portfolio?". Classes that fail this test are eliminated from the
backlog before any spec work.

**Applied to current backlog** (post-§11.4 archive):

- ✅ **HTF single-asset wick-sensitive variants** (structure
  breaks H1, OB H4, FVG mitigation H4, etc.) — passes by
  construction. Univers ≥ 5 instruments × 8–15 setups/mo each.
- ✅ **LTF single-asset (M5 / M15)** — passes by construction
  (with Duk-MT5 transferability concerns documented separately
  per §1 / §2).
- ❌ **HTF cross-sectional rotation rebalance ≥ 10 j** —
  eliminated. `trend_rotation_d1` v1 archive is the worked
  example.
- ⚠️ **HTF cross-sectional rotation rebalance ≤ 7 j** — needs
  §3.6 pre-measure verification per cell. Explored in
  `trend_rotation_d1_v1.1`.

**Operational implication**: the pre-measure step §1.0 template
now emits two numbers — raw trigger cadence (anchors H1) AND
projected portfolio cadence at the spec's §3.2 default operating
point (gate-checks §3.6). A spec whose default cell projects
< 4 trades/mo portfolio cannot be frozen as-is.

---

*Last revised: 2026-05-04 (fourth strategy ARCHIVE finalised on
cadence floor; §3.6 + §11.4 final-verdict preamble + §11.5
added). Update on every strategy archive — archived strategies'
"Transferable learnings" feed back here.*
