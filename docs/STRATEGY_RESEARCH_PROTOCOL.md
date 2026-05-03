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

**Operational rule**: every `BacktestResult` must report the field
`projected_annual_return_pct` and the gate compares it to **20.0**
(see §9).

---

## 4. The 7-gate pipeline

Each strategy walks through these gates in order. **Failure at any
gate = stop. Do not proceed to the next gate.** If a gate fails for
a reason that points to a fixable bug, fix the bug, re-run all prior
gates that the bug could have invalidated, then continue.

| # | Gate | Deliverable | Inputs | Outputs | Pass criteria |
|---|---|---|---|---|---|
| 1 | **Specification** | `docs/strategies/<name>.md` | Strategy idea, classification per §2 | Pseudo-code of detector + entry/SL/TP rules; class (HTF/LTF/Hybrid); expected setups/month and Mean R | Reviewer can implement it without ambiguity |
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

Each pitfall below was paid for in time during the TJR pilot. Re-read
this list at gate 3 and gate 4.

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

---

*Last revised: 2026-05-03 (second strategy archived). Update on
every strategy archive — archived strategies' "Transferable
learnings" feed back here.*
