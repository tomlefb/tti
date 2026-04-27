# 07 — Detection Philosophy: Rule-based vs LLM

This document is the **methodology doc** that governs how every detector
in this project is designed. Read it together with `01_STRATEGY_TJR.md`
for any task that touches detection logic.

It exists because the lazy framing "rule-based vs LLM" is a false binary.
The real question is: *what kind of cognitive task is this detector trying
to do, and is a deterministic rule a faithful encoding of it?*

---

## 1. The four categories

Every detector in this project belongs to exactly one of these:

### 1.1 Pure logic

A detector whose definition is **mathematically exact** with **no tunable
parameters**. The output is deterministic and unambiguous given any OHLC
input.

Examples in this project:

- **FVG geometric detection** — gap between candle 1's high/low and candle
  3's low/high; either it exists or it doesn't.
- **Asian range** — `min(low)` and `max(high)` over a fixed time window.
- **PDH / PDL** — high/low of yesterday's D1 candle.
- **Killzone check** — `start <= now < end`.
- **SL / TP / RR computation** — arithmetic.
- **Hard stops (daily loss, max trades count, etc.)** — numeric comparisons.
- **OTE zone** — Fibonacci 0.62–0.79 of a fixed leg, mathematical.

**Rule for these**: implement, unit test, ship. No calibration needed.
Tests are the only validation.

### 1.2 Calibrated rules

A detector whose **definition is deterministic** but whose **parameters
must be empirically tuned** to match the operator's expert visual reading
of charts.

These exist because the trading concept they encode (a "swing", a "sweep",
a "displacement", a "significant FVG") is fundamentally about how the
human eye distinguishes signal from noise. There is a correct mathematical
definition; there is no universally correct *parameterization* of that
definition.

Examples in this project:

- **Swing points** — definition (3-bar fractal) is exact, but `lookback`
  and `min_amplitude_atr_multiplier` must match what the operator
  considers a meaningful swing.
- **Sweep** — definition (wick crosses + close returns) is exact, but
  per-instrument `sweep_buffer` (how much excursion counts as a sweep, vs
  noise) requires tuning.
- **Sweep return window** — "close returns within 1–2 candles" is the
  definition; the "1–2" itself is calibrated.
- **MSS displacement** — definition (body close beyond swing) is exact;
  `displacement_multiplier` (how much body size counts as impulsive)
  requires tuning.
- **Significant FVG** — gap detection is pure logic; `min_size_atr_multiplier`
  is a calibrated noise filter.
- **Equal Highs / Lows clustering** — definition exact; `tolerance` per
  instrument requires tuning.

**Rule for these**: implement with parameters from `config/settings.py.example`,
unit test the logic, then run the **calibration protocol** (see section 3
below) before shipping. Parameter values committed to settings only after
calibration.

### 1.3 Heuristic rules

A detector whose underlying rule is **deterministic but somewhat arbitrary**.
There exist multiple reasonable alternative rules, and we have picked one
based on intuition or convention. Documented as such; revisit if data
suggests a different choice would perform better.

Examples in this project:

- **Bias on broken structure** — when the latest swing breaks the prior
  HH/HL or LH/LL pattern, we declare bias `neutral`. Alternative: trust
  the most recent swing as the new bias direction. Alternative: weight by
  number of swings on each side. We picked "neutral" because it's
  conservative; other choices are valid.
- **POI priority** — FVG > Order Block. Could equally be: take both,
  prefer overlap, prefer the one closer to OTE, etc.
- **Sweep selection when multiple occur** — most recent qualifying sweep
  wins. Could be: largest excursion, most significant level, etc.
- **A+ / A / B grading rules** — the precise booleans defining each tier
  are an opinionated stack; could be replaced by a numeric scoring system,
  or by an LLM in Sprint 7.

**Rule for these**: implement, unit test, ship. **Document the heuristic
choice** in code comments and in the strategy doc. When reviewing
performance, check whether alternative heuristics would have produced
better outcomes; revisit if so.

### 1.4 Judgment layer

A task that genuinely requires **contextual reasoning** which a
deterministic rule cannot capture without significant loss of signal.

Examples in this project:

- **"Is this sweep clean or messy?"** — A sweep within a tight choppy
  range that bounces off the level several times before extending — is
  this a real sweep or an extended consolidation? Rules can approximate
  but the human eye does this better.
- **"What's the macro context?"** — D1 trend, position within weekly
  range, proximity to major HTF unmitigated POIs. Each is detectable
  individually; the *synthesis* into "is this a good spot for the setup"
  is contextual.
- **Market regime** — trending vs ranging vs dead vs volatile. ADX/ATR
  give approximations; a genuine assessment is contextual.
- **Final A+/A/B/reject qualification** — could be a heuristic rule (and
  is, in v1) but is a natural fit for an LLM qualifier in Sprint 7.
- **Retrospective journal analysis** — "Why am I losing on Tuesdays?",
  "Is there a pattern in my skipped-but-would-have-won setups?". LLM is
  excellent at this.

**Rule for these**: in v1, deferred to either:

(a) the **operator's own judgment** when they receive a notification, OR
(b) deliberately **out of scope** (e.g., we don't try to detect market regime).

In Sprint 7+, an **LLM qualifier** may be introduced for tasks in this
category, with strict constraints (see section 4).

---

## 2. Decision flow when implementing a new detector

When asked to build any detection logic, ask in order:

1. **Is the concept exactly definable mathematically with no tunable knobs?**
   → Pure logic. Implement and test.

2. **Is the concept exactly definable but with parameters that depend on
   what the operator considers "significant"?**
   → Calibrated rule. Implement with config-driven parameters; calibrate
   before shipping.

3. **Is there a deterministic rule, but my choice of rule is one among
   several reasonable alternatives?**
   → Heuristic rule. Implement, document the choice, plan to revisit.

4. **Does the task require synthesizing context in a way no fixed rule
   captures faithfully?**
   → Judgment layer. Either defer to the operator (default), declare
   out-of-scope, or queue for the LLM qualifier (Sprint 7+).

If you find yourself wanting to "just call an LLM" for a category 1, 2,
or 3 task because it would be "easier" or "more flexible": **stop**.
That path leads to a system that is slow, expensive, non-reproducible,
and impossible to debug. The whole point of separating these layers is
that calibrated rules are auditable and cheap; LLM judgment is reserved
for where it earns its place.

Conversely, if you find yourself implementing increasingly baroque rules
to capture something that an LLM would handle in one prompt: **stop and
escalate**. The judgment layer exists to absorb exactly this pressure.

---

## 3. Calibration protocol (for category 1.2 detectors)

A calibrated detector is **not done** until it has been calibrated against
operator-marked reference data. The protocol:

### Step 1 — Reference set

The operator selects 5–10 reference charts representing diverse market
conditions (one trending day, one ranging day, one volatile day, one slow
day, etc.) per timeframe being calibrated. For the swing detector, that
means 5–10 H1 charts and 5–10 H4 charts.

These charts are saved as PNG screenshots with the operator's manual
swing markings drawn on top, in `calibration/reference_charts/`.

### Step 2 — Automated detection

The detector is run on the same OHLC ranges (saved as parquet alongside
the screenshots). The output is a list of detected swings with timestamps.

### Step 3 — Comparison report

A small script (per detector, lives in `calibration/`) overlays the
detector's output on the same charts and produces a side-by-side
comparison report. Metrics:

- **Recall**: fraction of operator-marked swings that the detector found.
- **Precision**: fraction of detector-found swings that the operator
  considers valid.
- **Visual delta**: marked images showing extra/missing swings.

### Step 4 — Parameter tuning

The operator reviews the report. If recall < 80% or precision < 80%,
parameters are adjusted (lookback, ATR multiplier, etc.) and Step 2 + 3
re-run. Iterate until both ≥ 80% on the reference set.

### Step 5 — Out-of-sample sanity check

Before committing, run the detector with the tuned parameters on **fresh
charts the operator has not used for calibration**. Eyeball check: does
it look right? If not, iterate; the calibration set was too narrow.

### Step 6 — Commit

Tuned parameters committed to `config/settings.py.example`. Calibration
report committed to `calibration/reference_charts/`. Sprint deliverable
marked done in `03_ROADMAP.md`.

This protocol is mandatory for every category 1.2 detector. Cutting it
out is the single most likely way the project fails: shipping a detector
with default parameters that don't match what the operator actually
trades.

---

## 4. Constraints on LLM use (Sprint 7+ only)

When (and only when) Sprint 7 is reached:

### 4.1 What the LLM may receive

- **Structured fields only**: bias, swept level type and price, distances
  to other levels in the operator's units, displacement body ratio,
  ATR-normalized FVG size, OTE overlap boolean, RR, recent structure
  summary, news flags, daily trade count, etc.
- A **fixed JSON schema** for the response.

### 4.2 What the LLM may NEVER receive

- **Raw OHLC data**. LLMs hallucinate prices and swing points; they cannot
  reliably "read" a candle series. Any output based on raw OHLC is
  untrustworthy regardless of how confident it sounds.
- **Chart screenshots**. Same problem — multimodal LLMs hallucinate
  levels, misread axes, and produce confidently wrong output.
- **Authority over the entry, SL, or TP**. These come from the rule-based
  pipeline. The LLM only qualifies; it does not modify.

### 4.3 What the LLM may output

- A **JSON object** matching a fixed schema, e.g.:

  ```json
  {
    "quality": "A+|A|B|reject",
    "concerns": ["short text", "..."],
    "confluence_strengths": ["short text", "..."],
    "confidence": 0-10
  }
  ```

- Free text in `concerns` / `confluence_strengths` is allowed (it's read
  by the operator), but `quality` and `confidence` are the only fields
  the system acts on.

### 4.4 Calibration of the LLM layer

Before LLM scores influence anything user-visible:

- Score at least 50 setups (live or replayed).
- Compute correlation between LLM `confidence` and trade outcomes.
- If correlation is weak (e.g., Spearman ρ < 0.2), **do not ship the LLM
  layer**. Ship the system without it and document the negative result
  in `docs/03_ROADMAP.md`.

### 4.5 Other valid LLM uses (no constraint, low risk)

- **Weekly journal analysis**: feed the LLM the journal SQLite contents
  (or a JSON export) and ask for patterns. Read by operator only; doesn't
  affect any live decision.
- **Notification post-write quality check**: an LLM proofreads the text
  summary for clarity. Cosmetic, low-stakes.

These are fine to add anytime without the strict calibration of section 4.4.

---

## 5. Mental models for choosing wisely

Two mental models help avoid mistakes:

**The "simulator" test**: if a junior trader could perfectly follow this
detector by hand using a calculator and a chart, it's a rule (categories
1, 2, or 3). If they'd need to "feel" the market or "consider the
context", it's judgment (category 4).

**The "10,000 reps" test**: a calibrated rule, once calibrated, behaves
identically on the 1st and the 10,000th invocation. An LLM does not — its
output drifts based on prompt phrasing, model version, sampling, and
prior context. Anything that needs to be exactly the same every time
must be a rule.

Apply both tests when in doubt.
