# mean_reversion_bb_h4_v1_1 — archived

**Status**: archived per protocol §8. The strategy did not produce a
measurable edge under its v1.1 §4 hypotheses on Dukascopy 2020-2024
train. Second consecutive HTF archive after `breakout_retest_h4_v1`.

## Timeline

| | Commit | Date |
|---|---|---|
| Cadence pre-measure | (script `run_cadence_premesure_mr_bollinger_h4.py`, untracked) | 2026-05-03 |
| Spec v1.0 frozen | `91cb2a2` | 2026-05-03 |
| Implementation (gate 2) | `3bb6872` | 2026-05-03 |
| Killzone NY 13–18 UTC fix | `69f2933` | 2026-05-03 |
| Look-ahead audit (gate 3) PASS | `29d3a3a` | 2026-05-03 |
| Attrition diagnostic | `diagnose_attrition_mr_bb_h4.py` (script, untracked) | 2026-05-03 |
| Spec v1.1 (exhaustion filter removed, grid broadened, H1/H5 recalibrated) | `ae61f70` | 2026-05-03 |
| Backtest principal (gate 4) FAIL | `ec4bdd4` | 2026-05-04 |

Total elapsed: ≈ 1 day from spec freeze to archive — well inside
the 12-day hard stop-loss the spec set in §8.

## Failure step

**Gate 4 — train calibration**.

Across the v1.1 broadened 36-cell grid (3 instruments × 12 cells:
`min_pen ∈ {0.0, 0.1, 0.2, 0.3}` × instrument-specific `sl_buffer`),
**zero** cells satisfied the three pre-specified selection criteria
simultaneously:

1. `n_closed >= 50` (protocol §5 admission)
2. `mean_r_ci_95.lower >= 0`
3. `temporal_concentration < 0.4`

The binding constraint was **`mean_r_ci_95.lower >= 0`** — on every
instrument and every cell, the 95 % CI on mean R covers zero. Holdout
was therefore not run; the §4 hypothesis count is 0 / 0 PASS by
construction. See `POSTMORTEM.md` for the full per-cell grid and
`ATTRITION_DIAGNOSTIC.md` for the pre-spec-v1.1 diagnostic that
informed the spec revision.

## Key numbers

| Metric | v1.1 spec band (§4) | Best observed (across 36 cells) | Worst |
|---|---|---|---|
| Setups / month | 0.5 – 2 | 1.55 (XAU pen=0.0 sl=0.5) | 0.38 (NDX pen=0.3) |
| Win rate | 55–70 % | 39.1 % (NDX pen=0.3, n=23) | 14.9 % (SPX pen=0.0 sl=1.0, n=74) |
| Mean R (pre-cost) | +0.4 to +0.8 | +1.250 (NDX pen=0.3 sl=3.0, n=23) | -0.423 (SPX pen=0.0 sl=3.0) |
| `mean_r_ci_95.lower` | > 0 | -0.169 (XAU pen=0.0 sl=0.5) | -0.766 (SPX pen=0.1 sl=1.0) |
| Temporal concentration | < 0.4 | 0.263 (SPX pen=0.1 sl=1.0) | 20.943 (SPX pen=0.3 sl=2.0) |

The v1.1 cadence target H1 ([0.5, 2] / month) is **hit** on XAU and
NDX at `pen=0.0` (1.55 and 1.13 / month) — the v1.1 attrition fix
worked as intended; the strategy is now measurable. What v1.1 cannot
fix is the **absence of edge** in the underlying mean-reversion
premise on these instruments at H4 in this window.

SPX500 is a stronger signal: at `pen=0.0`, `mean_r` is **negative**
on every cell, and the CI **upper** bound is also negative on the
1.0 / 2.0 / 3.0 sl_buffer cells — the strategy is measurably
*losing* on the lower-pen / higher-cadence cells, not merely
indistinguishable from chance.

NDX `pen=0.3 sl=3.0` shows an apparent +1.25 R mean, but `n=23`
(below the n ≥ 50 admission floor) and `ci_low=-0.238` — small-sample
artefact, not a measurable edge.

## Hypotheses that could have saved it

These extensions were **explicitly out-of-scope for v1** per the spec
itself (§7) — invoking any of them post-hoc to "rescue" the result
would have disqualified the run. They are recorded here as candidate
v2 extensions only:

- **D1 trend / regime filter** (ADX, ATR-expansion, HH-HL on D1) —
  spec §5.1, §7. A bidirectional pure mean reversion takes longs in
  down-trends and shorts in up-trends by construction; a regime gate
  would suppress the contra-trend leg. The v1.1 §5.1 explicitly
  framed the omission as a measurable choice ("ship without it,
  measure asymmetry"). The asymmetry is now measurable on holdout —
  but holdout never ran because train didn't admit. v2 should pre-
  spec this filter on the v1.1 train data; the gate-4 grid JSONs
  contain enough per-trade direction info to inform the design.
- **HTF confluence** (FVG / SR / round numbers) — spec §7. A reversion
  bouncing off an HTF support / round number has structurally more
  follow-through than one fired in mid-range. v2 candidate.
- **Tighter exhaustion candle threshold** (the v1.0 filter at
  wick≥0.4 / body≤0.5 was 3.7 % retention — too tight). A v2 with
  e.g. wick≥0.25 / body≤0.7 might keep the discriminator without
  collapsing the sample. Pre-spec the loosened thresholds before
  any backtest, document in a v2 modification log.

None of these is a guaranteed save: with `mean_r_ci_95.lower < 0` on
every v1.1 cell, the per-trade edge is statistically undetectable —
no filter operating on the *same* trades can fix that. A v2 must
re-evaluate the premise (e.g. test mean reversion only in confirmed
ranging regimes, where the contra-trend leg is the *target* of the
strategy rather than its cost).

## Transferable learnings (feed back to the protocol)

Distilled into protocol `STRATEGY_RESEARCH_PROTOCOL.md` §11.3.
Summary:

1. **Diagnostic d'attrition obligatoire entre pré-mesure cadence et
   gel de spec.** The v1.0 spec froze H1 = 3–5 / month based on 8
   raw triggers / month × an *intuitive* 38–63 % filter retention.
   The actual retention on the §2.4 exhaustion gate alone was
   3.7 %. A pre-spec attrition pass through every filter would
   have caught this *before* gate 4 archived for cause of n. The
   v1.1 fix made the strategy measurable — but the cadence
   pre-measure pattern alone (§11.2 lesson #1) was insufficient
   to anchor H1.

2. **The §11.2 chop fingerprint applies to mean reversion too.**
   Win rates 28–37 % (XAU/NDX) and 15–28 % (SPX) sit at or below
   the RR-implied breakeven (≈ 50 % for RR ≈ 1.0). Same diagnostic
   signature as breakout-retest's 30–34 % at RR 2.0. Future MR
   pre-specs should treat "win rate ≈ RR breakeven" as a
   precocious-archive marker on the train grid, regardless of
   trend-following / mean-reversion direction.

3. **The v1.1 modification pattern (documented before gate 4 on
   the basis of an empirical diagnostic) is methodologically
   defensible.** It is *not* HARKing because (a) the modification
   targets the attrition geometry, not the outcome edge, and
   (b) the §4 hypotheses are revised explicitly and frozen by the
   modification commit before any new backtest runs. The
   verdict-rule discipline holds. Future strategies that surface
   structural attrition issues at gate 3 should follow the same
   pattern: explicit modification log + documented anchor on a
   diagnostic + binary verdict on holdout.

4. **The `n_closed >= 50` admission floor protects against
   small-sample apparent edges.** NDX `pen=0.3 sl=3.0` showed
   `mean_r = +1.250` — would have looked promotable as a point
   estimate, but `n=23` and `ci_low=-0.238`. Without the floor,
   the protocol would risk anchoring follow-up work on a
   small-sample artefact. Keep the floor; consider pushing it to
   75 or 100 for HTF strategies whose holdout windows are short.

## What stays in the live tree

The archived strategy's spec lives here as `SPEC.md`. The
implementation and tests stay in the live tree as architectural
reference for the next HTF strategy:

- `src/strategies/mean_reversion_bb_h4/` — pure-function pipeline
  with cycle-by-cycle state management; gate-3-audited leak-free.
  Reusable scaffold for the next HTF candidate.
- `tests/strategies/mean_reversion_bb_h4/` — 52 unit + integration
  tests covering BB compute, excess detection, filters, return
  detection, setup builder, invalidation, pipeline orchestration.
- `tests/calibration/test_audit_mean_reversion_bb_h4.py` — the
  look-ahead audit harness regression guard.
- `calibration/audit_mean_reversion_bb_h4.py` — gate-3 audit script.
- `calibration/run_mean_reversion_bb_h4_grid.py` — gate-4 grid +
  holdout + verdict runner.
- `calibration/runs/gate4_mean_reversion_bb_h4_v1_1_*` — raw grid
  JSONs (gitignored, reproducible from the script + Duk fixtures).
- `calibration/runs/FINAL_gate4_mean_reversion_bb_h4_v1_1_*.md` —
  the committed verdict report (also linked from `POSTMORTEM.md`
  here).

If the next HTF strategy reuses any of these, link this archive
README from the new spec so the empirical "mean reversion BB
bidirectional pure does not produce a 95 %-CI-positive edge on
XAU/NDX/SPX at H4 in 2020-2024" finding stays visible.
