# Cross-sectional momentum — multi-asset rotation D1

> **Strategy spec — gate 1 of `STRATEGY_RESEARCH_PROTOCOL.md`.**
> Fourth strategy after three consecutive HTF retail-technical
> single-asset archives (TJR §11.1, breakout-retest H4 §11.2,
> mean-reversion BB H4 v1.1 §11.3). Pre-specified before any code
> is written, before any backtest is run.
>
> Anchored to:
>
> - Protocol commit `960bc37` — §1.5 attrition diagnostic step
>   formalised post-MR-archive.
> - Cadence pre-measure
>   `calibration/runs/premeasure_trend_rotation_d1_2026-05-04T07-22-14Z.md`
>   (15-asset universe, 6.4 y common window, 0.78 portfolio
>   entries / month at the academic-default operating point).
> - Fixture extension commits `7fc13b4` + `385d75a` (15 assets,
>   D1 + H1, ≥ 6.4 y coverage on the FundedNext-tradable set).
> - Attrition diagnostic
>   `calibration/runs/attrition_diagnostic_trend_rotation_d1_2026-05-04T08-13-11Z.md`
>   (24-cell candidate grid → 13 / 24 cells clear the n_closed ≥ 50
>   admission floor; the §3.2 grid in this spec is the §11.3-lesson-1-
>   compliant subset).
>
> Pre-specification is the point: every numerical hypothesis below
> exists so post-hoc rationalisation is impossible. If the holdout
> contradicts the spec, the spec is wrong — not the holdout.

---

## 0. Why this strategy after three archives

The first three HTF candidates all archived for the same surface
reason — win rate ≈ RR-implied breakeven on a single-asset
intraday-to-H4 pattern (chop fingerprint, §11.2 / §11.3 lesson #2).
A portfolio expansion test on the same two archived strategies
extended to EUR / GBP / BTC (commit `ce2a592`) found 0 / 6 cells
admissible — the chop fingerprint is *direction-agnostic and
portfolio-agnostic on retail-technical patterns at this timeframe*.

Cross-sectional momentum on a multi-asset universe is structurally
different:

- The "trade" is a basket-membership transition, not a setup-level
  win/loss vs SL/TP. There is no RR-breakeven concept on a single
  position; the verdict is the basket's net cumulative return.
- The edge is documented in academic literature with out-of-sample
  evidence going back to **Jegadeesh & Titman (1993, JoF)**,
  **Asness, Moskowitz & Pedersen (2013, JoF)** and **Moskowitz, Ooi
  & Pedersen (2012, JFE) — "time-series momentum across asset classes"**.
  The class has paid Sharpe 0.4–0.8 net of costs for three decades.
- Diversification is built into the basket — single-asset chop is
  averaged out across 15 assets. The §11.2 chop fingerprint should
  not transfer.

This is why the spec deliberately escapes the retail-technical
pattern lineage that produced the three archives.

---

## 1. Overview

**Concept.** At each rebalance date, score every asset in the
universe by a momentum lookback (cumulative return over the past
N days), rank descending, and hold the top-K. On the next
rebalance, re-rank, close positions on assets dropping out, open
positions on new entrants. Equal-risk-weighted, no short side, no
SL/TP at the position level — the rebalance ranking is the only
exit mechanism.

**Universe (FundedNext-tradable, 15 assets):**

| Class | Assets |
|---|---|
| Equity indices (7) | NDX100, SPX500, US30, US2000, GER30, UK100, JP225 |
| FX majors (4) | EURUSD, GBPUSD, USDJPY, AUDUSD |
| Metals (2) | XAUUSD, XAGUSD |
| Energy (1) | USOUSD (WTI Light Crude) |
| Crypto (1) | BTCUSD |

Universe size is **5–6 effective independent bets** after
collapsing the 4 highly-correlated clusters identified in the
pre-measure (US-indices block 0.85–0.93, EUR/GBP/AUD vs USD block
0.70–0.83, USDJPY anti-correlated to FX, XAU/XAG metals 0.59).
This is acceptable diversification for K = 3 / 4; finer
cluster-based filtering is v2 scope (§7).

**Classification (§2 of protocol)**: HTF — every decision is on
closed D1 prices. Cross-asset HTF transferability is assumed good
on Duk vs MT5 but **gate 7 is rebalance-level, not setup-level**:
the meaningful comparison is "did Duk and MT5 pick the same top-K
on the same rebalance date" rather than setup-list mismatch (see
§6 H10).

**Estimated cadence and edge** (a-priori, before any backtest —
see §4 for the full hypothesis table):

| Quantity | A-priori range | Source |
|---|---|---|
| Closed trades / month / portfolio | 0.7–2.3 | Attrition diagnostic, viable cells only |
| Win rate (per closed trade) | 50–60 % | Jegadeesh-Titman / Asness empirical |
| Mean R per closed trade | +0.2 to +0.6 | Sharpe 0.4–0.8 × per-trade volatility |
| Projected annual return @ 1 % risk | **5–15 %** | Below the §3 protocol viability threshold of 20 %; see §4 H5 note |

The 5–15 % projected-annual band is **deliberately below the
protocol's 20 % viability threshold**. The class's academic Sharpe
implies that natively. §4 H5 documents the operator decision
deferred to post-gate-4.

---

## 2. Pseudo-code

All loops below operate **only** on history available at the
cycle's `now_utc` timestamp. No `df.loc[future_idx]`. Audit
(gate 3) verifies streaming-vs-full-history bit-identical
basket-transition lists.

### 2.1 Universe and data

```
UNIVERSE = [
    "NDX100", "SPX500", "US30", "US2000", "GER30", "UK100", "JP225",
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD",
    "XAUUSD", "XAGUSD",
    "USOUSD",
    "BTCUSD",
]

for each asset in UNIVERSE:
    load D1 close from tests/fixtures/historical/<asset>_D1.parquet
    timestamp normalised to calendar-day UTC at 00:00
    panel cells = Σ asset closes, intersected on common dates
```

The 15-asset panel is intersected on the largest common date
window — 2019-12-22 → 2026-04-30 (≈ 6.4 y, limited by XAUUSD
start). All cross-asset operations run on this intersected panel
to avoid asymmetric basket sizes pre-XAUUSD.

### 2.2 Momentum score

```
def compute_momentum(close_d1: pd.Series, lookback: int) -> float | None:
    """Cumulative return over the last `lookback` days.

    Anti-look-ahead: the score at date t is a function of
    close[t - lookback] and close[t]. Strict; no use of close[t+1].
    """
    if len(close_d1) < lookback + 1:
        return None
    past = close_d1.iloc[-lookback - 1]
    now  = close_d1.iloc[-1]
    return (now - past) / past
```

`lookback ∈ {63, 126}` per §3.2 grid. The 6-month value (`126`)
is the academic anchor; the 3-month value (`63`) is the faster
variant kept on the grid as a sensitivity axis.

### 2.3 Ranking and top-K selection

```
def select_top_k(scores: dict[str, float | None], K: int) -> list[str]:
    valid = [(a, s) for a, s in scores.items() if s is not None]
    ranked = sorted(valid, key=lambda x: x[1], reverse=True)
    return [a for a, _ in ranked[:K]]
```

Assets with insufficient history at the cycle date are dropped
(returned `None` by ``compute_momentum``). The top-K is chosen
from the remaining valid set.

### 2.4 Rebalance trade detection

```
def detect_rebalance_trades(
    current_basket: set[str],
    new_basket:     set[str],
) -> tuple[set[str], set[str]]:
    """Compare baskets to identify closed and opened positions.

    Returns:
        closed: assets dropping out of the top-K at this rebalance
                (their position from the prior cycle is now closed)
        opened: assets entering the top-K
                (new position to be opened at this rebalance close)
    """
    closed = current_basket - new_basket
    opened = new_basket - current_basket
    return closed, opened
```

`n_closed` over the train window (the protocol §5.2 admission
floor) is `Σ |closed|` across all rebalances after the first.
The first rebalance has no prior basket, so it contributes `K`
opens and zero closes. Assets that stay in the top-K throughout
the window are still **open** at window end and contribute zero
closes — by the protocol's "fully closed trade" convention.

### 2.5 Position sizing — risk parity

At each rebalance, for every asset entering the basket:

```
def sizing_for_entry(
    close_d1:       pd.Series,
    atr_d1:         float,
    capital:        float,
    risk_fraction:  float = 0.01,
) -> float:
    """Risk-parity sizing: each entry contributes `risk_fraction`
    of capital × per-trade volatility scaling."""
    risk_dollars = capital * risk_fraction
    return risk_dollars / atr_d1
```

`atr_d1` is the standard 20-day ATR on D1 OHLC. Risk parity is
**necessary** for a multi-asset basket — without it, a 1 lot
position on BTC and 1 lot on USDJPY would contribute very
different dollar risks (BTC daily ATR ≈ thousands of USD, USDJPY
daily ATR ≈ tens of pips). Equalising the risk contribution per
asset is the academic standard for cross-sectional momentum
implementations (Asness 2013).

### 2.6 Hard invalidation

No SL / TP at the position level — exits are exclusively driven
by the rebalance ranking (§2.4). However, two universe-level
exclusions apply at each rebalance:

- **Insufficient history**: assets with fewer than
  `lookback + 1` D1 closes at the cycle date are skipped.
- **Volatility regime filter**: an asset whose D1 ATR(20) is
  > 5 × its 90-day median ATR is **temporarily excluded** from
  the universe at this rebalance only. Captures flash-crash /
  exchange-event days (BTC ±30 %, NDX -7 % gap) where the
  rebalance signal is dominated by noise. The asset re-enters
  on the next rebalance once volatility normalises.

These filters are applied **before** ranking. The selection set
is therefore a subset of the 15-asset universe — possibly < 15 on
exclusion days. The basket is still K assets if at least K assets
pass; if fewer than K pass (very rare on this universe), the
basket holds K_actual ≤ K positions for that period.

---

## 3. Parameters

### 3.1 Fixed (pre-specified, NOT calibrated)

These are anchored ex ante. Changing them post-hoc to chase a
result is data dredging and disqualifies the run.

| Parameter | Value | Justification |
|---|---|---|
| Universe | 15 assets (§1) | Tradable on FundedNext + ≥ 6.4 y D1 coverage post-extension |
| Decision timeframe | D1 close | Strategy class anchor; all rebalances at calendar-day UTC close |
| Risk per trade | 1 % | FundedNext standard, protocol §3 default |
| Position sizing | Risk parity, ATR(20)-D1 | Cross-asset homogenisation (§2.5) |
| Volatility regime filter | ATR(20) > 5 × median(ATR(20), 90 d) → exclude this rebalance | Anti flash-crash; v1 hard rule, no calibration |
| ATR period for sizing & filter | 20 days | Academic standard short-horizon volatility |
| Insufficient-history filter | < lookback + 1 D1 closes → skip asset | Anti unbounded score; v1 hard rule |
| Direction | Long-only | No short side in v1; v2 candidate (§7) |

### 3.2 Calibrated (pre-specified §3.2 grid)

The grid is the **§11.3-lesson-1-compliant subset**: only cells
that clear the protocol §5.2 admission floor (`n_closed ≥ 50`)
on the train window per the attrition diagnostic.

| Axis | Values | Justification (post diagnostic) |
|---|---|---|
| `momentum_lookback` (days) | **{63, 126}** | 3-month and 6-month — the latter is the academic standard (Jegadeesh-Titman, Asness). 9-month and 12-month dropped: at rebalance ≤ 21 d they fall to n_closed 32–37 (borderline) on this train window. |
| `K` (basket size) | **{3, 4}** | K=3 is 20 % concentration on 15 assets / ≈ 50 % on 5–6 effective bets. K=4 raises sample size by ~10–25 % across cells. |
| `rebalance_frequency` (days) | **{10, 21}** | 10-day rebalance generates ~115 rebalances over 5 y → all cells viable. Monthly (21 d) generates ~55 rebalances → viable on momentum ≤ 126. Quarterly (63 d) dropped: only 1 / 8 cells viable on this train. |

**8 cells per run** (2 × 2 × 2). All 8 clear `n_closed ≥ 50` on the
train window per the diagnostic (range 50–137 closed trades).

Cells dropped from the candidate grid (24 → 8) and rationale:

| Drop | Reason |
|---|---|
| `lookback ∈ {189, 252}` × `rebalance ∈ {21, 63}` | n_closed 17–38 (borderline / non-measurable). Sample insufficient for an admissible verdict. |
| `rebalance = 63 d` | 1 / 8 cells viable across all momenta. Quarterly rebalance is structurally slow for a 5-year train. |

Selection criterion on the **train** set (§3.3): max `mean_r`
whose 95 % CI lower bound ≥ 0 AND `temporal_concentration < 0.4`
AND `n_closed ≥ 50`. Tie-break: max
`vs_buy_and_hold.strategy_minus_bh_pct`. Same trio as the two
prior archived strategies — same admission discipline.

### 3.3 Default operating point (gate 3 audit reference)

`momentum_lookback = 126 d`, `K = 3`, `rebalance = 10 d`.

- **n_closed = 69** on the train window per the diagnostic — 38 %
  margin above the floor.
- 6-month momentum is the academic anchor (Jegadeesh-Titman,
  Asness); K = 3 is the focused basket; rebalance = 10 d is
  active without being whipsaw-prone.
- Used by `calibration/audit_trend_rotation_d1.py` (gate 3) as
  the reference cell for the streaming-vs-full-history diff.

### 3.4 Train / holdout split

| Set | Window | Purpose |
|---|---|---|
| **Train** | 2019-12-22 → 2024-12-31 (≈ 5.0 y) | §3.2 grid selection |
| **Holdout** | 2025-01-01 → 2026-04-30 (≈ 1.4 y) | §4 hypothesis check |

Train start anchored at XAUUSD's first D1 bar (2019-12-22) — the
binding date in the 15-asset intersection. The protocol §3.3
minimum 5 y is met to the day.

If the holdout `mean_r` diverges by more than 0.3 R from the
selected cell's train `mean_r`, this is an overfit signal — the
verdict is not auto-archived but flagged in the report and held
for operator review (cohérent avec spec MR BB H4 §3.3).

---

## 4. Pre-specified hypotheses (anti-data-dredging)

**Recorded BEFORE any backtest.** These define what counts as
success. The sheet is closed once this commit lands; reopening it
post-hoc to "loosen the criteria" disqualifies the run and forces
archive.

| # | Hypothesis | Target | Source / rationale |
|---|---|---|---|
| H1 | Closed trades / month / portfolio | 0.7–2.3 | Attrition diagnostic on viable cells: 0.85 (rank 13 cell) – 2.27 (rank 1 cell). Range padded ±10 % to absorb sample variance train↔holdout. |
| H2 | Win rate (closed) | 50–60 % | Jegadeesh-Titman empirical 50–55 % on US equities; Asness 55–60 % on multi-asset. The strategy times entries via cross-sectional rank, not via signal-vs-noise discrimination, so the breakeven asymmetry of the §11.3 chop fingerprint does **not** apply. |
| H3 | Mean R (pre-cost) per closed trade | +0.2 to +0.6 | Sharpe 0.4–0.8 (academic, multi-asset CSM) × per-trade volatility scaling. |
| H4 | Mean R (post-cost) per closed trade | +0.1 to +0.5 | Subtract ~0.05–0.10 R for spread + commission per round-trip across the basket. |
| H5 | `projected_annual_return_pct` | **5–15 %** | Derived from H4 × H1 × 12 × 1 % across the 5–6 effective bets. **Below the protocol §3 viability threshold of 20 %** — see operator-decision note below. |
| H6 | `mean_r_ci_95.lower` (≥ 1 cell, holdout) | > 0 | Without it, no measurable edge (§5.2 protocol) |
| H7 | `outlier_robustness.trim_5_5.mean_r` (selected cells) | > 0 | Edge must survive trimming top/bottom 5 % |
| H8 | `temporal_concentration` (selected cells) | < 0.4 | Below the regime-fitting flag |
| H9 | `vs_buy_and_hold.strategy_minus_bh_pct` (≥ 1 cell) | > 0 | Strategy must beat passive buy-and-hold of an equally-weighted basket of the same 15 assets |
| H10 | Top-K agreement Duk vs MT5 (gate 7) | > 70 % of rebalances | Rotation-specific transferability metric (NOT setup-list mismatch — see §6) |

**Verdict rule on the HOLDOUT** (not train):

| Hypotheses satisfied | Decision |
|---|---|
| ≥ 6 / 10 | Edge probable → proceed to gate 5 cross-check |
| 3 / 10 ≤ x ≤ 5 / 10 | Mixed signal → operator review before continuing |
| < 3 / 10 | No edge → mandatory archive (`archived/strategies/trend_rotation_d1_v1/`) |

**Operator decision on H5 deferred to post-gate-4** — H5's 5–15 %
band is below the protocol §3 viability floor of 20 %. If the
holdout passes ≥ 6 / 10 hypotheses with a projected annual return
in [5, 15] %, the operator decides between three paths:

- **(a)** Continue gates 5–8 anyway as a methodological learning,
  no Sprint-7 deployment commitment.
- **(b)** Archive with the explicit note "edge measurable but
  below viability threshold" — adds a fourth case study to §11
  for the strategies that *do* clear admission but fall short of
  deployment economics.
- **(c)** Revise §3 of the protocol to set a per-class viability
  threshold (cross-sectional momentum has structurally lower
  Sharpe / annual return than retail-technical patterns by
  design — academic baseline 5–15 % net is documented).

This is a path-decision, not a hypothesis revision. The §4
verdict counting is not affected by the H5 outcome — H5 is one of
the 10 hypotheses, evaluated on its band as written.

---

## 5. Anticipated pitfalls

### 5.1 Regime fitting on 2019-2024

Train spans COVID 2020 (extreme volatility + extreme rotation),
recovery 2021, Fed-hike 2022 (NDX bear), 2023-2024 NDX recovery.
Multiple regimes — but NDX dominance in 2023-2024 means
cross-sectional momentum had an implicit beta toward US large-cap
indices in the late train window.

**Mitigation**: H8 (temporal_concentration < 0.4) catches this.
If the edge is concentrated in a single regime quarter (e.g. NDX
runaway rally), the verdict is ARCHIVE regardless of point-mean.
The §11.2 lesson #3 mitigation generalises here.

### 5.2 Whipsaw cost erosion at rebalance = 10 d

At 10-day rebalance, ~115 rebalances over 5 y → ~10 closed trades
× K opens per year on average. Per-trade spread + commission
stack. Gate 8 Phase C with realistic FundedNext costs is the test
— if Mean R post-cost collapses vs pre-cost, this is the
materialising pitfall. v1 mitigation: keep the 21-day rebalance
on the grid as a low-cost sensitivity axis.

### 5.3 US-indices cluster dominance

NDX / SPX / US30 / US2000 / GER30 are 0.85–0.93 correlated on this
train window. In persistent risk-on regimes the top-K can be
dominated by 3–4 assets from the same cluster — the diversification
on paper does not materialise in execution.

**Mitigation v1**: none. Documented and accepted; the v2
candidate filter is "no more than 2 assets from the same
correlation cluster in the top-K". Not added pre-emptively to
keep the spec minimal and avoid an adjustable knob (cluster
boundaries themselves are a calibrated parameter and would
require their own pre-spec).

### 5.4 BTCUSD volatility outliers

BTC has moved ±30 % in single-week windows multiple times in the
train. Risk-parity sizing reduces but does not eliminate the
impact. The §2.6 volatility-regime filter handles the worst case
(ATR > 5 × 90-d median → exclude this rebalance) but not the
broader high-volatility regime where BTC alternates between
momentum-leader and momentum-laggard within a quarter — which is
exactly the regime where rotation strategies struggle.

### 5.5 Sample-size attrition in real backtest

The diagnostic measured `n_closed` on a clean rebalance series.
The actual backtest applies the §2.6 volatility filter, which
will exclude some rebalances and lower the realised `n_closed`.
If a viable cell drops from `n_closed ≥ 50` (diagnostic) to
< 50 (run), per protocol §3.2 it is excluded from selection at
gate 4. The report logs the realised `n_closed` per cell so this
attrition is visible.

### 5.6 No chop fingerprint here — but a different failure mode

The §11.2 / §11.3 win-rate-≈-RR-breakeven signature does not
apply (no per-position RR target). The analogous failure mode
for cross-sectional momentum is **"top-K basket return ≈
equally-weighted basket return"** — i.e. ranking adds no value
over equal-weighting. H9 (`vs_buy_and_hold` strategy minus EW
basket > 0) is the explicit detector.

If on holdout `mean_r > 0` AND `vs_buy_and_hold ≤ 0`, the
cross-sectional ranking is **noise** — same fingerprint as chop
in the retail-technical archives, just measured differently.
Verdict per the §4 rule reads this off H9 directly.

---

## 6. Validation plan — mapping to protocol gates

| Gate | Action | Pass criterion |
|---|---|---|
| **3** Audit look-ahead | New `calibration/audit_trend_rotation_d1.py`. Streaming-vs-full-history diff at the rebalance level — same top-K and same closed/opened sets at every rebalance date | 100 % bit-identical |
| **4** Backtest Duk | Tick simulator on train → §3.2 grid (8 cells) → re-run on holdout per cell with selected params; emit `BacktestResult` per cell. **n_closed reported per cell** so the attrition-diagnostic projection can be cross-checked against the realised count. | All 10 hypotheses (§4) measured on the holdout |
| **5** Cross-check DBN | Same selected cell on Databento — but DBN coverage is **partial** for this universe (NDX / SPX / DJI futures only, no FX / metals / oil / crypto). Run the strategy on the DBN-covered subset and check the basket-overlap + per-trade Mean R is within ±50 % of Duk on the same cells. Wider band than the ±30 % single-asset criterion because the basket subset distorts cross-asset rank. **Documented limitation, not a strategy failure mode.** | Mean R within ±50 % on the DBN-subset basket |
| **6** Sanity MT5 | Same selected cell on MT5 (~1.4 y depth — overlaps the holdout). Direction agreement check: did the basket pick the same risk-on / risk-off asset class skew on both sources? | Same direction sign as Duk; no violent sign flip |
| **7** Transferability — rotation-specific | Rebalance-level top-K agreement: on the same dates, what fraction of the K-asset basket matches between Duk and MT5? This **replaces** the setup-list mismatch metric used in the previous archives — there are no setup timestamps to compare for a rotation strategy. | Top-K agreement > 70 % across all rebalances on the common 1.4 y window |
| **8** Phase C | FundedNext spread + commission per asset (per-trade cost based on entry price + lot size + spread, similar to MR BB H4 §8 model); recompute holdout `BacktestResult` post-cost | Mean R post-cost > 0; CI lower > -0.05; **projected_annual_return_pct ≥ 20 % OR explicit operator path-decision per §4 H5 note** |
| **9** Decision | Operator review of §4 verdict + §3 viability discussion + Phase C results | Promote / hold for v2 / archive |

---

## 7. Out-of-scope (v1)

Explicit list of what we will NOT build into v1, to keep scope
honest and prevent rescue-by-feature when results disappoint:

- **No cluster-based filter** (§5.3). v2 candidate.
- **No macro regime filter** (VIX, ADX D1, term structure). v2 candidate.
- **No multi-period momentum** (combined 3 / 6 / 12-month score).
  v2 candidate.
- **No mean-reversion overlay on top performers**. v2 candidate.
- **No long/short** (no bottom-K short). v2 candidate.
- **No fundamental filter** (P/E, dividend yield, etc.). Out
  of universe — most assets aren't fundamentally-anchored at all
  (FX, metals, crypto).
- **No dynamic basket weighting by score magnitude**.
  Equal-risk-weighted only; weighting by score is a calibrated
  parameter that would require its own pre-spec.
- **No basket re-balancing within the period** (e.g. daily
  re-equalisation of weights). Risk parity computed at entry,
  held until exit.
- **No skip-most-recent-month convention** (the academic 12-1
  twist). Adds a parameter, kept for v2 if v1 admits with the
  6-month default.

If v1 admits, every one of these is a candidate for v2 and gets
its own pre-spec. None is to be added during v1 to "save" a
marginal result.

---

## 8. Budget — per protocol §8

| Phase | Target |
|---|---|
| Specification (this doc) | 1–2 h |
| Implementation + unit tests | 1–2 d |
| Audit (gate 3) | 0.5–1 d |
| Backtests Duk + DBN-subset + MT5 (gates 4–6) | 1–2 d |
| Transferability (gate 7) — rotation-specific | 0.5 d |
| Phase C (gate 8) | 0.5 d |
| Decision (gate 9) | 2 h |
| **Total target** | **4–7 d** |

**Hard stop-loss: 10 days from this commit to admission.**
Reduced from 12 days vs the previous archives — three iterations
of the protocol have built reusable scaffold (audit harness,
grid driver, BacktestResult format), so a fresh strategy should
not need the full 12-day envelope.

Beyond 10 days: mandatory move to
`archived/strategies/trend_rotation_d1_v1/` with the post-mortem
README per protocol §8.

---

## 9. Lessons from §11 incorporated

The five distilled lessons from the three archived strategies
(§11.1 TJR, §11.2 breakout-retest, §11.3 mean-reversion BB H4 v1.1)
all materialise concretely in this spec:

1. **§11.2 lesson #1 — pre-measure cadence on raw triggers**.
   Done. Pre-measure report `2026-05-04T07-22-14Z` measured
   0.78 closed/month at the academic-default operating point
   over 6.4 y of common window. The H1 band in §4 is anchored
   on this number, not an intuitive guess.

2. **§11.3 lesson #1 — pre-spec attrition diagnostic (§1.5
   protocol step)**. Done. Diagnostic report
   `2026-05-04T08-13-11Z` measured `n_closed` for all 24
   candidate-grid cells on the train window. The §3.2 grid in
   this spec is the **§11.3-compliant subset** — only cells
   that clear the admission floor are included. The risk of
   "non-informative archive due to insufficient sample" that
   sank MR BB H4 v1.0 is structurally mitigated.

3. **§11.2 lesson #2 / §11.3 lesson #2 — chop fingerprint is
   direction-agnostic**. The fingerprint does not transfer to
   cross-sectional momentum (no per-position RR target) but
   the analogous failure mode does — "top-K basket return ≈
   equal-weighted basket return". §5.6 names it explicitly and
   H9 in §4 is the detector.

4. **§11.3 lesson #3 — modification pattern with explicit
   versioning is methodologically defensible**. If gate 4
   surfaces a structural issue (e.g. a viable cell falls below
   `n_closed ≥ 50` post-volatility-filter), this spec can be
   revised to v1.1 *before any backtest re-run* with the
   modification log, the why, and the §4 hypothesis revisions
   if applicable. Pattern documented and ready.

5. **§11.3 lesson #4 — the n_closed ≥ 50 floor protects against
   small-sample apparent edges**. Holds at gate 4 selection.
   The diagnostic-projected counts (50–137 closed per cell) are
   not certainties; the realised counts (volatility filter
   applied) are reported per cell so any cell that drifts under
   the floor is excluded from the §4 verdict tally.

---

*Spec frozen at this commit. Any change to fixed parameters
(§3.1), calibration grid (§3.2), train/holdout split (§3.4), or
pre-spec hypotheses (§4) requires either a new strategy (v2) or
an explicit operator-approved revision recorded in commit
history. Quietly adjusting a number to chase a result
disqualifies the run.*
