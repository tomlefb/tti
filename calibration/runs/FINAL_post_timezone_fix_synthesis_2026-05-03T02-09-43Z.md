# Post-timezone-fix synthesis — 2026-05-03T02-09-43Z

Synthesises the four diagnostics re-run on Mac after pulling the
Windows-side timezone fix (commit `e871b6d`, fixtures regenerated in
`f868793`). The MT5 historical export now writes UTC consistently; the
project's three calibration sources can finally be compared on equal
timing footing.

Reports superseded by this synthesis:

- `FINAL_3way_alignment_2026-05-02T15-55-27Z_*.md` (commit 379fc70)
- `FINAL_mt5_vs_databento_preflight_2026-05-02T13-16-05Z.md`
- `FINAL_mt5_vs_databento_tick_2026-05-02T11-43-04Z.md`

Reports promoted from this run:

- `FINAL_3way_alignment_2026-05-02T21-21-15Z_{raw_diff,verdict}.md`
- `FINAL_mt5_vs_databento_preflight_2026-05-02T21-22-46Z.md`
- `FINAL_mt5_vs_databento_tick_2026-05-02T21-24-37Z.md`

## 1. What was reported pre-fix

Pre-fix verdicts, on the narrower 10–17 month windows that the broken
MT5 export allowed before depth was extended:

- **3-way alignment**: 3/3 instruments labelled **B** (Duk≈DBN, MT5
  distinct). Body-direction agreement Duk-MT5 stuck near chance (0.51)
  on all three instruments. Pearson on returns Duk-MT5 was 0.92–0.97
  while Duk-DBN was 0.99+.
- **Preflight (timestamp alignment)**: peak return correlation off
  lag 0 (the smoking gun for a broker-time vs UTC-time offset).
- **Setup-level diff**: 100% mismatch on (date, killzone, direction)
  for all three instruments (n=7–12 per cell).
- **Decision narrative**: "MT5 is a third distinct source — adopt
  edge-on-2-of-3 as a robustness criterion; treat MT5 as production
  ground truth that does not transfer to/from Duk or DBN."

## 2. What is confirmed vs invalidated post-fix

### Invalidated

| Pre-fix claim | Post-fix evidence |
|---|---|
| MT5 is structurally distinct from Duk on M5 | Body sign agree Duk-MT5 jumped 0.51 → **0.89–0.96** across all three instruments. Pearson Duk-MT5 jumped to **0.993–0.999**. The pre-fix "distinct" label was an artefact of a ~1–24 hour timezone shear, not real microstructure divergence. |
| MT5 timestamps had a non-UTC offset | Lag scan ±360 min now peaks **at lag 0** with r = 0.78. No secondary peak at any other lag. Confirms UTC alignment on Mac after fixture regeneration. |
| 100% setup mismatch is the floor | Mismatch ratio drops to **81–96%** on the extended windows (NDX 81%, SPX 82%, XAU 96%). Improvement is real but partial. |
| "Edge on 2 of 3 sources" is the right standard | The three sources are now structurally equivalent at the M5 level (all Duk/MT5/DBN pairs > 0.98 Pearson on returns). The criterion still has value as a robustness check but should be reframed: it screens against single-source quirks, not against three genuinely different time series. |

### Confirmed

| Pre-fix claim | Post-fix evidence |
|---|---|
| Setup-level mismatch is dominated by something other than match-window tolerance | Tightening tolerance to ±5 min and widening the window to 3.5–6.4 years still leaves 81–96% of setups un-matched. The dominant residual is the Panama-adjusted-futures vs CFD **price-level offset** (see deep diagnosis: NDX divergent matched setup shows entry Δ −2345 pts = −13.5%). Same setup geometry on different price levels triggers on different bars. |
| Tick simulator gives the same answer the legacy detector did, modulo CI | The sources fire on largely disjoint days; they don't agree at the setup level. The structural cause is the price-level offset, not detector leakiness. Phase B audit's leak removal kept this story intact. |

### Newly emerged

| Finding | Evidence |
|---|---|
| **NDX100 has a CI-positive edge under the leak-free tick simulator on MT5 over 3.5 years** | n=27 closed, mean R = **+1.564**, bootstrap CI 95% = **[+0.366, +2.834]**. First instrument × source × window combination to clear `CI lower bound > 0`. Scenario B in the four-scenario rule. |
| XAUUSD partial retention | n=43, mean R **+0.291**, CI [−0.33, +0.95] — point estimate ≈ 51% of the legacy +0.58 reference. Not CI-positive but not collapsed either. The 6.4-year window cuts the retention vs the 11-month Sprint 6.5 reference because edge is not stationary. |
| SPX500 inconclusive | n=11, mean R +0.186 — too few closed trades for any verdict. |
| DBN cells are CI-flat or negative | DBN mean R: XAU −0.474, NDX +0.153 (CI [−0.53, +0.92]), SPX −0.078. None CI-positive. |

## 3. Implications for the project

### a. Source hierarchy

Pre-fix recommendation was "MT5 is a third distinct source, treat as
ground truth that does not transfer". Post-fix the picture changes:

- **Dukascopy** and **MT5** are structurally equivalent to each other
  on M5 (corr 0.99+, body-sign 0.89–0.96, sub-spread close diffs).
  Dukascopy is preferred for **calibration / historical backtests**:
  free, deterministic, ~14 years of depth on FX/metals/indices, no
  Windows dependency. MT5 is the **runtime source** for live trading.
- **Databento** (Panama-adjusted continuous front-month futures) is
  structurally equivalent **on returns** (corr 0.98+) but offset on
  absolute price levels. It functions as a long-term sanity check
  on a different market structure (futures), useful when an edge
  needs cross-market-structure confirmation, but the price-level
  offset means setup-level overlap with MT5 stays low.

Concretely:

- Promote Dukascopy to primary calibration source from now on.
- Keep MT5 as the runtime ground truth (live trading).
- Use Databento sparingly: depth is ~10 years, but residual
  mismatch on setup overlap (81–96%) means Databento backtests
  predict MT5 setup PnL only weakly. Useful for direction/regime
  cross-checks, not for absolute R estimation transfer.

### b. The "edge on 2+ sources" criterion

Pre-fix this rule was a hard requirement (otherwise the strategy was
likely overfit to one source's quirks). Post-fix the rule is still
useful but for a different reason:

- A strategy that holds on Duk **and** MT5 confirms the edge survives
  the tiny structural noise between the two (broker microstructure,
  spread profile, weekend boundary handling).
- A strategy that holds on Duk **and** DBN confirms the edge survives
  a fundamental change of market structure (CFD → futures + Panama).

**New phrasing**: "edge on Duk + sanity MT5" is the de-facto deployment
gate — if Dukascopy backtest is positive and MT5 backtest on the same
window stays within ~0.5σ of the Duk point estimate, ship. Databento is
a discretionary tiebreaker rather than a required leg.

### c. Reconsideration of the TJR verdict

Sprint 6.5 dropped most pairs because the 10-year Databento backtest
(then the longest available) did not show edge after look-ahead leaks
were removed. The Sprint 6.6 portfolio validation kept XAU+NDX on the
basis of an 11-month MT5 fixture window where the legacy detector
showed mean R +0.58 / +1.38.

Post-fix, on a clean 3.5–6.4 year MT5 window with the **leak-free
tick simulator**, the picture is:

- **NDX100**: mean R **+1.564** with CI lower bound **+0.366** over
  3.5 years. This is the first multi-year, CI-positive edge under
  the leak-free detector. The Sprint 6.6 decision to keep NDX is
  **strongly confirmed** — and probably under-stated by the 11-month
  point estimate.
- **XAUUSD**: mean R +0.29, no CI bracket. Partial retention vs the
  Sprint 6.5 +0.58 reference; the 6.4-year window picks up earlier
  regimes where the edge was weaker. Hold but flag for closer
  review on rolling 6-month bins.
- **SPX500**: too few closed trades (n=11 over 3.5 years) to form
  any view. Was never in the live portfolio; this run does not
  change that.

Critically, all three earlier "no edge" verdicts on the **Databento**
fixture (Sprint 6.5) are now far less informative than they looked:
the residual price-level offset between DBN and MT5 means DBN absence
of edge does not imply MT5 absence of edge. The DBN cells in this run
(XAU −0.47, NDX +0.15, SPX −0.08) do not CI-positive on any
instrument, but that no longer carries the weight of "Databento is
the long-term truth".

### d. What this changes operationally

- The roadmap statement "Databento is the gold-standard 10y
  baseline" can be retired. Databento is one cross-check among
  three; the new gold-standard for backtests is **Dukascopy**
  (longer than the previous MT5 export, free, deterministic).
- Strategy research is now unblocked on a much wider MT5 window
  (1500 days). The Sprint 6.6 NDX edge can be re-validated on
  3.5 years rather than 11 months — and it holds.
- The "edge on 2 of 3 sources" criterion remains in the protocol
  but is reframed (see § 3.b).

## 4. Open questions / next steps

1. **Re-run the parameter sweep on the extended MT5 fixtures**
   (Scenario B for NDX100). The Sprint 6.5 grid was on 11 months;
   the post-fix window covers 3.5 years on NDX, 6.4 years on XAU.
   This is the single highest-value calibration follow-up.
2. **Rolling 6-month bins** on XAUUSD MT5 mean R, to detect
   regime-shifted edge degradation hiding in the 6.4-year average.
3. **Codify** the new source hierarchy in
   `docs/STRATEGY_RESEARCH_PROTOCOL.md` (does not exist yet — to
   draft as part of the strategy-research branch).
4. **Document** the residual price-level offset in
   `docs/04_PROJECT_RULES.md` as a structural fact about Databento:
   it agrees on returns, not on price levels, so setup-level
   overlap with MT5 stays at ~80–95%. This is a feature, not a
   bug.

## 5. Numbers at a glance — pre-fix vs post-fix

### 3-way alignment (Pearson on M5 returns / body-sign agreement Duk-MT5)

| Instrument | Window pre / post | Pearson Duk-MT5 pre / post | Body sign agree pre / post |
|---|---|---:|---:|
| XAUUSD | 10mo / 6.4y | 0.957 → **0.999** | 0.510 → **0.887** |
| NDX100 | 10mo / 3.5y | 0.923 → **0.997** | 0.511 → **0.955** |
| SPX500 | 17mo / 3.5y | 0.973 → **0.993** | 0.508 → **0.930** |

### Preflight lag scan (NDX100, ±360 min)

| | Pre-fix | Post-fix |
|---|---|---|
| Peak lag | non-zero | **0** |
| Peak r | (smoking gun) | **+0.78** |
| r at lag 0 | low | **+0.78** |

### Setup-level diff (tick simulator, MT5 vs DBN)

| Instrument | Mismatch pre / post | MT5 n pre / post | MT5 mean R pre / post | MT5 CI 95% pre / post |
|---|---:|---:|---:|---|
| XAUUSD | 100% → **96%** | 7 → **43** | +0.539 → +0.291 | — / [−0.33, +0.95] |
| NDX100 | 100% → **81%** | 9 → **27** | +1.225 → +1.564 | — / **[+0.366, +2.834]** |
| SPX500 | 100% → **82%** | 7 → **11** | −1.000 → +0.186 | — / — |
