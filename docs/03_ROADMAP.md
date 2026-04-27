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

- [ ] Python 3.11+ env on Windows host with all `requirements.txt` deps installed
- [ ] `scripts/test_mt5.py` successfully fetches M5 candles from MT5 and prints them
- [ ] Telegram bot created via BotFather; `scripts/test_telegram.py` sends a message
- [ ] Git repo set up, Mac↔Windows sync working
- [ ] `config/secrets.py` and `config/settings.py` populated locally (never committed)
- [ ] First Telegram heartbeat test from Windows host

**Done when**: a manual run of both test scripts succeeds.

---

## Sprint 1 — Daily bias detection (calibrated)

**Goal**: detect the daily bias on H4 + H1 reliably, with the swing
detector calibrated against the operator's eye.

Deliverables:

- [ ] `src/detection/swings.py` with parameterized lookback + ATR amplitude filter
- [ ] `src/detection/bias.py` returning `bullish`/`bearish`/`no_trade`
- [ ] Unit tests with hand-crafted fixtures
- [ ] **Calibration session**: 5 reference charts marked manually by operator;
      detector run on same periods; visual comparison report produced.
- [ ] Tuned values committed to `config/settings.py.example`
- [ ] CLI script that prints the current bias for all 4 watched pairs

**Done when**: bias output matches operator's manual reading on at least
20 historical days across the 4 pairs (≥ 80% agreement), AND the
calibration report is checked into `calibration/reference_charts/`.

---

## Sprint 2 — Liquidity marking & sweep detection

**Goal**: identify the liquidity levels and detect when they get swept.

Deliverables:

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

- **Active sprint**: 0
- **Last updated**: project kickoff

Each sprint completion: update this section with `Active sprint`, key
findings from the previous sprint, and any roadmap revisions.
