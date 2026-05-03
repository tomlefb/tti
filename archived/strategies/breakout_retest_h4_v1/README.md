# breakout_retest_h4_v1 — archived

**Status**: archived per protocol §8. The strategy did not produce a
measurable edge under its pre-specified §4 hypotheses on
Dukascopy 2020-2024 train.

## Timeline

| | Commit | Date |
|---|---|---|
| Spec frozen | `b14e054` | 2026-04-29 |
| Implementation (gate 2) | `9db76df` | 2026-05-03 |
| Look-ahead audit (gate 3) PASS | `29286fb` | 2026-05-03 |
| Backtest principal (gate 4) FAIL | `2b98cd1` | 2026-05-03 |

Total elapsed: ≈ 4 days from spec freeze to archive — well inside
the 12-day hard stop-loss the spec set in §8.

## Failure step

**Gate 4 — train calibration**.

Across the 27-cell grid (3 instruments × 9 cells), **zero** cells
satisfied the three pre-specified selection criteria simultaneously:

1. `n_closed >= 50` (protocol §5 admission)
2. `mean_r_ci_95.lower >= 0`
3. `temporal_concentration < 0.4`

Holdout was therefore not run — without a calibrated model there is
nothing to validate. See `POSTMORTEM.md` for the full per-cell grid.

## Key numbers

| Metric | Spec band (§4) | Best observed (across 27 cells) | Worst |
|---|---|---|---|
| Setups / month | 1–3 | 7.98 (XAU low) | 8.97 (XAU high) |
| Win rate | 40–55 % | 34.7 % (SPX best) | 29.9 % (NDX worst) |
| Mean R (pre-cost) | +0.4 to +1.2 | +0.040 (SPX) | -0.104 (NDX) |
| `mean_r_ci_95.lower` | > 0 | -0.079 (SPX best) | -0.223 (NDX worst) |
| Temporal concentration | < 0.4 | 0.255 (NDX, several cells) | 7.667 (XAU) |

The strategy hovers near the RR-2.0 breakeven win rate (33.3 %)
pre-cost and below it post-cost. CI lower never crosses zero on any
cell on train — there is no parameter combination in the pre-specified
grid that produces a 95 %-CI-positive mean R on this five-year window.

## Hypotheses that could have saved it

These extensions were **explicitly out-of-scope for v1** per the spec
itself (§5.4, §7) — invoking any of them post-hoc to "rescue" the
result would have disqualified the run. They are recorded here as
candidate v2 extensions only:

- **HTF confluence filter** (PDH / PDL / round numbers) — spec §5.4.
  A breakout firing into a daily PDH or a round number has
  structurally less follow-through; filtering these out should drop
  the lowest-quality 50–80 % of setups while preserving the
  high-conviction ones.
- **Tighter retest window** (`n_retest < 8`) and / or larger swing
  lookback (`n_swing > 5`) — would reduce the cadence (currently
  3× the §4 H1 band) and might raise per-setup quality. To be
  pre-specified before any backtest if attempted in v2.
- **Counter-trend mode** in clearly-mean-reverting regimes — spec
  §7. A regime detector beyond the D1 MA50 bias would be required;
  not free to design and a vector for post-hoc tuning.

None of these is a guaranteed save: the underlying observation
(34 % win rate at RR 2.0 ≈ breakeven) is consistent with "the
breakout-retest pattern itself does not predict more winners than a
coin flip on these instruments at H4 in 2020-2024", in which case
no filtering will produce a positive expectancy. A v2 must
re-evaluate the premise, not just tighten parameters.

## Transferable learnings (feed back to the protocol)

These are the structural take-aways for the next strategy / the
protocol document itself:

1. **Setups/month estimates in pre-specs are systematically low**.
   The spec §1 expected 1–3 setups/month. The strategy fired ~8/month.
   3× discrepancy is a calibration miss, not a strategy bug.
   *Mitigation*: before pre-specifying H1, run the bare swing /
   trigger detector on a few months of history to *measure* the
   expected cadence — even without the full setup logic, the trigger
   density alone is a fair upper bound. Add this step to protocol
   §6 (pitfall list).

2. **D1 trend filter alone does not separate continuation from chop**.
   Spec §5.2 (chop) was anticipated; gate 4 confirmed it. A bullish
   D1 bias holds for weeks while H4 ranges sideways — the strategy
   keeps firing breakouts into chop and accumulates losses. The
   bias filter is necessary but insufficient.
   *Mitigation*: future trend-following pre-specs should add an
   *active trend* filter (e.g. ATR-expansion, ADX threshold,
   higher-high / higher-low confirmation on H4 itself) before
   committing to the §4 hypothesis bands.

3. **The "spec §5.2 chop pitfall" materialises when win rate aligns
   with RR-breakeven**. Across the entire 27-cell grid, win rates
   sat in 30–34 % — RR-2.0 breakeven is 33.3 %. This is a
   diagnostic signature of a strategy that doesn't time entries
   better than chance. The 5.2 paragraph in the spec called the
   *risk* but didn't articulate this diagnostic; updating
   protocol §6 with it would speed up future archives.

4. **Pre-specification + binary verdict worked as designed**. The
   anti-data-dredging contract held: no parameter was retuned
   post-hoc, no hypothesis was loosened. The verdict came in within
   budget (≈ 92 min wallclock for gate 4, 4 days end-to-end). Two
   pilots now archived (TJR pivot + this one) with **opposite
   surface signatures** (TJR archived after long stress-test cycle
   on a strategy that *looked* edge-positive; breakout retest H4
   archived after one calibration run on a strategy that produced
   no signal at all). The methodology distinguishes between them
   without ceremony — the binary calibration → holdout → §4 check
   handles both.

## What stays in the live tree

The archived strategy's spec lives here. The implementation and
tests stay in the live tree as architectural reference for the next
HTF strategy:

- `src/strategies/breakout_retest_h4/` — pure-function pipeline
  with cycle-by-cycle state management; gate-3-audited leak-free.
  Reusable scaffold for the next HTF candidate.
- `tests/strategies/breakout_retest_h4/` — 57 unit + integration
  tests covering swing detection, breakout, retest, setup builder,
  invalidation, pipeline orchestration. Reusable test patterns.
- `tests/calibration/test_audit_breakout_retest_h4.py` — the
  look-ahead audit harness regression guard.
- `calibration/audit_breakout_retest_h4.py` — gate-3 audit script.
  Same template can wrap the next strategy.
- `calibration/run_breakout_retest_h4_grid.py` — gate-4 grid +
  holdout + verdict runner. Same template for the next one.
- `calibration/runs/gate4_breakout_retest_h4_*` — raw grid JSONs
  (gitignored — reproducible from the script + Duk fixtures).
- `calibration/runs/FINAL_gate4_breakout_retest_h4_*.md` — the
  committed verdict report (referenced by `POSTMORTEM.md` here).

If the next HTF strategy reuses any of these, link the archive
README from there so the empirical "trend-following on H4 with
trivial bias filter does not produce edge on these three
instruments" finding stays visible.
