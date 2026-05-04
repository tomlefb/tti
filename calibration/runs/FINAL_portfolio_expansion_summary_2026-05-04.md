# Portfolio expansion test — verdict (2026-05-04)

**Hypothesis tested**: are the two HTF strategies that archived on
the validated portfolio (XAU + NDX + SPX) failing because of those
specific instruments, or because the underlying retail-technical
HTF approach is structurally edge-less on retail-accessible
instruments at H4 in 2020-2024?

**Test**: re-run the gate-4 grid + selection criteria of each
archived strategy on three out-of-portfolio instruments — EURUSD,
GBPUSD, BTCUSD — using identical pre-specified admission rules
(`n_closed >= 50` AND `mean_r_ci_95.lower >= 0` AND
`temporal_concentration < 0.4`).

**Pre-specified verdict rule** (locked before execution):
- 0 / 6 instrument×strategy combinations select → **VERDICT A**:
  HTF retail-technical approaches do not produce a measurable edge
  on retail instruments in this window.
- 1 / 6 → **SUSPECT**: probable statistical noise (multiple-testing
  on 6 outer × 9–12 inner cells each ≈ 1 expected false positive).
- 2+ on the same instrument → **PROMETTEUR for that instrument**.
- 2+ across different instruments → **MIXTE**.

---

## 1. Recap by instrument × strategy

| Strategy | Instrument | Cells passing 3 criteria | Best cell (closest miss) | Verdict per cell |
|---|---|---:|---|---|
| breakout_retest_h4 | EURUSD | **0 / 9** | none (every cell ci_low NEG; best mean_r = -0.104) | ARCHIVE |
| breakout_retest_h4 | GBPUSD | **0 / 9** | none (best mean_r ≈ 0, tc 0.6–26 on every cell) | ARCHIVE |
| breakout_retest_h4 | BTCUSD | **0 / 9** | tol=60 sl=200 (ci_low=-0.003, tc=0.370, mean_r=+0.106) | ARCHIVE (miss by 3 thousandths on ci_low) |
| mr_bb_h4_v1_1 | EURUSD | **0 / 12** | none (mean_r NEG on most cells; win 17-26 %) | ARCHIVE |
| mr_bb_h4_v1_1 | GBPUSD | **0 / 12** | none (best mean_r=+0.294 but ci_low=-0.128) | ARCHIVE |
| mr_bb_h4_v1_1 | BTCUSD | **0 / 12** | none (mean_r NEG on most cells; tc problems) | ARCHIVE |

**Total cells passing: 0 / 6 instrument×strategy combinations.**

## 2. Verdict (per pre-specified rule)

> **VERDICT A** — HTF retail-technical approaches do not produce
> a measurable edge on retail instruments in 2020-2024 at H4.

The rule reads "0 / 6 → VERDICT A". 0 / 6 measured. Verdict A.

The pre-spec rule is honoured even though the breakout-retest
BTC `tol=60 sl=200` cell missed `ci_low ≥ 0` by 0.003 R — well
within the 95 % CI noise floor. The rule is binary by design;
re-interpreting "miss by 3 thousandths" post-hoc as "essentially
PROMOTE" would re-introduce the data-dredging the rule is there
to prevent.

## 3. Observations

### 3.1 Win rates vs RR-implied breakeven

| Strategy (RR ≈ X) | Breakeven WR | EUR | GBP | BTC |
|---|---|---|---|---|
| breakout_retest_h4 (RR=2.0) | **33.3 %** | 26-30 % (BELOW) | 30-33 % (AT) | 32-37 % (AT/SLIGHTLY ABOVE) |
| mr_bb_h4_v1_1 (RR≈1.0) | **50.0 %** | 17-26 % (well below) | 31-43 % (well below) | 25-29 % (well below) |

Confirms the §11.2 / §11.3 chop fingerprint on the new portfolio:
every win-rate-vs-breakeven pair sits on or below breakeven,
direction-agnostic. The single small exception is the breakout-
retest H4 on BTC at large sl_buffer (200 USD), which prints
36-37 % win rate vs 33.3 % breakeven — a 3–4 pp edge that fails
the 95 % CI test on n ≈ 600-700 trades.

### 3.2 Mean R signal by instrument

- **BTC + breakout-retest, large SL**: only combination with
  positive mean R AND temporal_concentration < 0.4. Three cells
  (tol=60 sl=200, tol=100 sl=200, tol=30 sl=200) cluster at
  mean_r ≈ +0.09 to +0.11, ci_low ≈ -0.003 to -0.017. The 95 %
  CI rules them out as evidence; if the floor were 90 % CI
  (z ≈ 1.65 instead of 1.96) some of these cells would pass.
  *Note — operator decision*: tightening / loosening the CI
  threshold post-hoc is a rule change, not a re-interpretation;
  any such change would require a versioned protocol revision
  applied to all three archived strategies (TJR, breakout-retest,
  MR BB H4) for symmetry, not a one-off accommodation here.
- **GBP + MR BB H4**: positive mean R on 9/12 cells (best
  +0.294 at pen=0.0 sl=0.0003), but `ci_low` always negative
  (best -0.128) — sample insufficient. Win rate 31-43 % is
  below the RR-1.0 breakeven of 50 %.
- **All other 4 combinations**: mean R negative or near-zero,
  ci_low strongly negative, win rate well below breakeven.

### 3.3 Setups/month on the new instruments

| Strategy | EUR | GBP | BTC |
|---|---:|---:|---:|
| breakout_retest_h4 | 9.2-10.1 | 9.5-10.2 | 10.9-11.9 |
| mr_bb_h4_v1_1 (pen=0.0 best) | 1.0-1.1 | 1.6 | 2.6-3.0 |

Cadence on the new portfolio is similar to the original — the
strategies emit setups, just not setups with edge. The §11.2
lesson #1 ("setups/month underestimated in pre-specs") would
have applied here too if pre-specs had been written before the
portfolio expansion (which they weren't — this run is the
expansion test, not a new strategy spec).

## 4. Wallclock

| Run | Wallclock | Cells |
|---|---:|---:|
| breakout_retest_h4 EUR/GBP/BTC | 6 875.1 s ≈ 1 h 54 | 27 (3 × 9) |
| mr_bb_h4_v1_1 EUR/GBP/BTC | 2 840.4 s ≈ 47 min | 36 (3 × 12) |
| **Total (parallel)** | **≈ 1 h 54** (parallel) | **63** |

## 5. What this rules out

The portfolio expansion test was designed to discriminate between
two hypotheses:

- **H_portfolio**: the two archives are specific to XAU/NDX/SPX —
  these instruments don't produce edge under HTF retail-technical
  patterns, but other instruments would.
- **H_method**: the HTF retail-technical pattern itself is
  edge-less in this window, regardless of instrument.

VERDICT A → **H_method holds** (provisionally, on this 5-year
window with these two strategies). The same surface signature
(win rate ≈ RR breakeven, ci_low < 0 on every cell) shows up on
6 / 6 new combinations, identical to the 3 / 3 archived
combinations. 9 / 9 instrument×strategy combinations across the
two archives + portfolio expansion now fail the same admission
gate for the same structural reason.

## 6. What this does NOT rule out

- **LTF strategies (M5 / M15)**. The two archived strategies are
  HTF — no LTF candidate has been tested. The protocol §7
  Backlog has LTF wick-sensitive items pending.
- **Strategies with explicit regime gates** (ADX / ATR / VIX
  filter ON DAY OF TRADE). All v1 specs in this archive set
  explicitly omitted regime detection per §7 out-of-scope. A v3
  HTF candidate that pre-specs an active-trend or active-range
  filter has not been tested.
- **A different time window**. 2020-2024 was uniquely
  trend-dominated on indices (post-COVID + 2022 down + 2023-2024
  recovery) and choppy on FX. A 2010-2019 window might show
  different cadence / edge profile — but the protocol's data
  hierarchy starts at 2020 (Duk fixture coverage).

## 7. Suggested next — operator discussion

VERDICT A puts the operator at a strategic decision point:

- **Option A1 — third HTF candidate with explicit regime gate
  pre-spec**'d. Smallest deviation from the pipeline. Must
  resist the "win-rate ≈ RR breakeven" outcome before gate 4 by
  having the regime filter materially change which trades fire.
  Candidate: "BB mean reversion + ADX D1 < 25 (ranging-only)".
  Budget: 5–8 days per protocol §8.

- **Option A2 — LTF pivot**. The HTF window is exhausted on
  this dataset; move to M5/M15 strategies where sample size
  per cell is 5-10× larger and the chop fingerprint may not
  apply (the archived TJR was LTF, edge-less for different
  reasons). Requires tick-replay infrastructure (already in
  place: `src/backtest/tick_simulator.py`).

- **Option A3 — re-anchor on an external strategy**. The two
  HTF candidates were derived from textbook retail patterns
  (breakout retest + BB mean reversion). VERDICT A challenges
  the assumption that *any* such pattern produces edge in this
  window. A re-anchor could come from (a) a published
  paper / strategy with documented out-of-sample edge on similar
  instruments, (b) a third-party signal source (proprietary
  feed) used as a starting point rather than re-deriving from
  zero, or (c) a fundamentally different timeframe (D1 swing,
  W1 macro).

- **Option A4 — pause strategy research, pivot to TJR**. The
  CLAUDE.md project goal is automated trading on FundedNext
  demo (Sprint 7+). Two HTF candidates archived + 1 portfolio
  expansion test failed to find edge. The TJR strategy
  (project's original target) was archived for *different*
  reasons (LTF, slow stress-test) — revisiting it with the
  protocol discipline now in place may be more productive
  than a third HTF pre-spec.

No action taken — these are recommendations for the operator's
next session. The branch `feat/strategy-research` is in a
stable state: 2 strategies archived, 1 portfolio expansion test
adjudicated, protocol up-to-date with all transferable learnings.
