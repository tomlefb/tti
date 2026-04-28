# 03 — Roadmap

High-level milestones. Each sprint is roughly 1 week of evening/weekend work.
**Update this file at the end of each sprint** with what shipped, what
deviated, and what the next sprint should focus on.

> Reminder: any sprint that ships a calibrated detector (see
> `07_DETECTION_PHILOSOPHY.md`) MUST include the calibration step before
> being considered "done".

---

## Sprint 0 — Setup & scaffolding

**Goal**: prove the plumbing works end-to-end before writing detection logic.

Deliverables:

- [x] Python 3.11+ env on Windows host with all `requirements.txt` deps installed
- [x] `scripts/test_mt5.py` successfully fetches M5 candles from MT5 and prints them
- [x] Telegram bot created via BotFather; `scripts/test_telegram.py` sends a message
- [x] Git repo set up, Mac↔Windows sync working
- [x] `config/secrets.py` and `config/settings.py` populated locally (never committed)
- [x] First Telegram heartbeat test from Windows host
- [x] Project skeleton complete and importable (`src/`, `tests/`, `scripts/`, `logs/`, `calibration/`)
- [x] `config/settings.py.example` and `config/secrets.py.example` in place; real config gitignored
- [x] `pytest` passes (trivial smoke test asserts `src` package imports)
- [x] `black` and `ruff` clean on the whole codebase
- [x] `pyproject.toml` configures black, ruff, and pytest

**Done when**: a manual run of both test scripts succeeds.

---

## Sprint 1 — Daily bias detection (calibrated)

**Goal**: detect the daily bias on H4 + H1 reliably, with the swing
detector calibrated against the operator's eye.

Deliverables:

- [x] `src/detection/swings.py` with parameterized lookback + ATR amplitude filter
- [x] `src/detection/bias.py` returning `bullish`/`bearish`/`no_trade`
- [x] Unit tests with hand-crafted fixtures
      (`tests/detection/test_swings.py`, `tests/detection/test_bias.py`)
- [x] Integration tests on the committed historical fixtures
      (`tests/detection/test_integration.py`)
- [x] Calibration harness consuming operator-marked reference annotations
      (`calibration/run_swing_calibration.py`)
- [x] **Calibration session**: 38 operator-annotated sessions (19 H4 + 19 H1).
      H4 detector calibrated (P=87.1%, R=77.1%, F1=81.8%). H1 plateaus at
      F1≈60% — identified as a design choice, deferred to Sprint 2's
      `mark_swing_levels()` (multi-TF confluence promotion). See
      `calibration/runs/FINAL_swing_calibration.md`.
- [x] Tuned values committed to `config/settings.py.example`
      (`MIN_SWING_AMPLITUDE_ATR_MULT` 0.5 → 1.0)
- [x] CLI script that prints the current bias for all 4 watched pairs
      (`scripts/print_current_bias.py`, fixture-based — not live MT5)

**Done when**: bias output matches operator's manual reading on at least
20 historical days across the 4 pairs (≥ 80% agreement), AND the
calibration report is checked into `calibration/reference_charts/`.

---

## Sprint 2 — Liquidity marking & sweep detection

**Goal**: identify the liquidity levels and detect when they get swept.

Deliverables:

- [ ] Implement `mark_swing_levels()` with multi-TF confluence promotion
      (H4 ∩ H1) per operator's liquidity-hierarchy philosophy. See
      `calibration/runs/FINAL_swing_calibration.md`.
- [ ] `src/detection/liquidity.py`: Asian range, PDH/PDL, swing levels, equal H/L
- [ ] `src/detection/sweep.py`: sweep detection on M5 with per-instrument buffers
- [ ] Unit tests
- [ ] **Calibration**: per-instrument sweep buffer tuned on a few weeks of historical M5
- [ ] CLI script that scans the last N days and prints all detected sweeps

**Done when**: detected sweeps match operator's eyeball assessment on
historical data (≥ 80% agreement on a sample of marked days), buffers
calibrated.

---

## Sprint 3 — MSS, FVG, full setup orchestrator

**Goal**: complete detection pipeline producing `Setup` objects.

Deliverables:

- [ ] `src/detection/mss.py` with calibrated displacement filter
- [ ] `src/detection/fvg.py` with ATR-based size filter
- [ ] `src/detection/order_block.py` (fallback POI)
- [ ] `src/detection/setup.py` orchestrator
- [ ] `src/detection/grading.py` for A+/A/B (heuristic)
- [ ] Unit + integration tests
- [ ] **Calibration**: displacement multiplier and FVG size threshold tuned

**Done when**: running the orchestrator on a historical day produces a list
of detected setups that the operator can review; at least 70% are confirmed
as valid by operator manual review.

---

## Sprint 4 — Telegram notifications with charts

**Goal**: every detected setup turns into a useful Telegram message.

Deliverables:

- [ ] `src/notification/chart_renderer.py` produces annotated PNGs
- [ ] `src/notification/message_formatter.py` builds the text summary
- [ ] `src/notification/telegram_bot.py` sends + handles inline button callbacks
- [ ] Manual test: trigger a fake `Setup` → receive notification → click button → confirm callback received

**Done when**: clicking `Taken`/`Skipped` on a notification persists the
decision (Sprint 5 will hook this into the journal).

---

## Sprint 5 — Journal & outcome tracking

**Goal**: persist every setup, decision, and outcome.

Deliverables:

- [ ] `src/journal/schema.sql` and migrations
- [ ] `src/journal/repository.py` for CRUD
- [ ] `src/journal/outcome_tracker.py` queries MT5 history daily
- [ ] Simple Streamlit dashboard (or jupyter notebook) showing stats:
      detection count, taken vs skipped, win rate, avg RR, by pair, by quality

**Done when**: operator can see, for any past day, what was detected, what
was taken, what won, what lost.

---

## Sprint 6 — Scheduler, hardening, paper trading

**Goal**: the system runs autonomously during killzones for 2–3 weeks
without intervention. Paper trading (no real money).

Deliverables:

- [ ] `src/scheduler/runner.py` with APScheduler running the cycle
- [ ] Robust logging (rotating file + Telegram errors)
- [ ] Heartbeat at killzone start
- [ ] Hard stops wired in (daily loss, max trades, news filter on/off)
- [ ] Run for 2–3 weeks; collect data; review weekly

**Done when**: ≥ 2 weeks of paper trading data with no system crashes,
≥ 80% precision (notifications operator confirms as valid setups), and
operator confidence high enough to consider real-money use.

---

## Sprint 7 (optional) — LLM qualifier layer

**Goal**: add an LLM as a post-detection setup qualifier (NOT detector).
See `07_DETECTION_PHILOSOPHY.md` for what is and is not allowed.

Deliverables:

- [ ] `src/qualifier/llm.py`: takes structured setup context (NOT screenshots,
      NOT raw OHLC) and returns a JSON score
      `{quality: A+|A|B|reject, concerns: [...], confluence_score: 0-10}`
- [ ] Calibration measurement: do LLM scores correlate with trade outcomes?
- [ ] If calibrated: integrate LLM score into notifications

**Done when**: ≥ 50 setups scored, statistical correlation between LLM
score and outcome measured. If correlation is weak/null, ship the project
without this layer and document the negative result.

---

## Current state

- **Active sprint**: 2
- **Last updated**: Sprint 1 closed 2026-04-28; H4 detector calibrated
  (F1=81.8%), H1 design choice deferred to Sprint 2 — see
  `calibration/runs/FINAL_swing_calibration.md`.

Each sprint completion: update this section with `Active sprint`, key
findings from the previous sprint, and any roadmap revisions.
