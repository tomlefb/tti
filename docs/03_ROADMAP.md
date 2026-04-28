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

- [x] Implement `mark_swing_levels()` with multi-TF confluence promotion
      (H4 ∩ H1) per operator's liquidity-hierarchy philosophy. See
      `calibration/runs/FINAL_swing_calibration.md`.
- [x] `src/detection/liquidity.py`: Asian range, PDH/PDL, swing levels, equal H/L
- [x] `src/detection/sweep.py`: sweep detection on M5 with per-instrument buffers
- [x] Unit tests (`tests/detection/test_liquidity.py`, `test_sweep.py`)
- [x] Integration tests on the 19 reference dates × 4 pairs × 2 killzones
      (`tests/detection/test_sweep_integration.py`) producing a markdown
      sweep report at `calibration/runs/{TIMESTAMP}_sweep_integration.md`
- [~] **Calibration**: deferred — buffers kept at default values;
      calibration to be triggered if Sprint 3+ shows excessive FP/FN on
      real sweeps. Spot-check of the integration report (1270 sweeps over
      18 dates × 4 pairs) was acceptable to the operator.
- [x] CLI script that prints liquidity + sweeps for one (date, pair)
      (`scripts/print_liquidity_and_sweeps.py`)

**Done when**: detected sweeps match operator's eyeball assessment on
historical data (≥ 80% agreement on a sample of marked days), buffers
calibrated.

---

## Sprint 3 — MSS, FVG, full setup orchestrator

**Goal**: complete detection pipeline producing `Setup` objects.

Deliverables:

- [x] Sweep deduplication — multiple M5 candles sweeping the same level
      within a short window must collapse to a single sweep event for
      downstream consumption. (`deduplicate_sweeps` in `sweep.py`,
      union-find clustering on direction + time window + symmetric
      relative price tolerance; integrated into `detect_sweeps(dedupe=True)`.)
- [x] `src/detection/mss.py` with calibrated displacement filter
      (defaults `MSS_DISPLACEMENT_MULTIPLIER=1.5`,
      `MSS_DISPLACEMENT_LOOKBACK=20` — kept until calibration data drives
      a tuning pass).
- [x] `src/detection/fvg.py` with ATR-based size filter
      (`FVG_MIN_SIZE_ATR_MULTIPLIER=0.3`, `FVG_ATR_PERIOD=14`).
- [x] `src/detection/order_block.py` (fallback POI; 20-candle bounded
      look-back).
- [x] `src/detection/setup.py` orchestrator (`build_setup_candidates`,
      bias re-locked per-killzone, `SetupSettings` Protocol, full
      pipeline with TP nearest-≥-MIN_RR selection).
- [x] `src/detection/grading.py` for A+/A/B (heuristic stack with
      defensive displacement re-check).
- [x] Unit + integration tests
      (`test_sweep_dedup`, `test_mss`, `test_fvg`, `test_order_block`,
      `test_setup`, `test_grading`, `test_setup_integration`).
- [x] **Calibration**: amplitude split per-TF (`MIN_SWING_AMPLITUDE_ATR_MULT_H4=1.3`
      from grid search; H1=1.0 unchanged from Sprint 1; M5=1.0). Bias became
      H4-only by default (`BIAS_REQUIRE_H1_CONFIRMATION=False`) after the
      diagnostic dive on 2025-10-15 XAUUSD identified H1 noise as the dominant
      bias-killer. `PARTIAL_TP_RR_TARGET=5.0` added for the TP1 partial-exit
      mechanism on high-RR runners. Calibration of `MSS_DISPLACEMENT_MULTIPLIER`
      and `FVG_MIN_SIZE_ATR_MULTIPLIER` deferred — defaults proved adequate
      on integration test (16 setups, balanced distribution, no bottleneck
      identified at MSS or FVG stage in cascade). See
      `calibration/runs/FINAL_sprint3_calibration.md`.
- [x] CLI script (`scripts/print_setups_for_day.py`).

**Done when**: running the orchestrator on a historical day produces a list
of detected setups that the operator can review; at least 70% are confirmed
as valid by operator manual review.

---

## Sprint 4 — Telegram notifications with charts

**Goal**: every detected setup turns into a useful Telegram message.

Deliverables:

- [x] **Notification format**: TP1 (5R partial) and TP_runner (full RR)
      distinct, with `high_rr_runner` confluence flagged for tradability
      (extended-leg setups → emphasise scaling out at TP1; 🚀 emoji on
      the TP_R line when this confluence is set).
- [x] **Killzone gating**: filter setups whose `timestamp_utc` (MSS confirm
      time) falls outside the killzone end. Implemented in
      `build_setup_candidates` (orchestrator) — the integration test post-gating
      drops 3 setups from the Sprint 3 baseline (16 → 13): 2025-07-03 NDX100
      11:40 UTC London, 2025-11-21 XAUUSD 17:35 UTC NY, 2026-01-14 NDX100
      18:20 UTC NY. Boundary policy: timestamp == kz_end_utc kept (inclusive).
- [x] `src/notification/chart_renderer.py` produces annotated PNGs
      (mplfinance + matplotlib, headless Agg backend).
- [x] `src/notification/message_formatter.py` builds the HTML caption
      with per-symbol price precision and Paris-time parenthetical.
- [x] `src/notification/telegram_bot.py` sends + handles inline button
      callbacks (Taken/Skipped) with injectable ``on_callback`` for Sprint 5.
- [x] Manual test: `scripts/test_notification.py` builds a real Setup
      from fixtures, renders chart, prints caption, sends to Telegram,
      polls 60 s for callback. `--no-send` mode for visual review on Mac.

**Done when**: clicking `Taken`/`Skipped` on a notification persists the
decision (Sprint 5 will hook this into the journal).

---

## Sprint 5 — Journal & outcome tracking

**Goal**: persist every setup, decision, and outcome.

Deliverables:

- [ ] Hook the Telegram callback (Taken/Skipped) into SQLite persistence
      (currently stubs in `src/notification/telegram_bot.py` via the
      `on_callback` parameter).
- [ ] Persist ALL detected setups, including rejected ones (useful for
      tuning).
- [ ] Outcome tracker reconciles MT5 trade history with journal setups
      by symbol + timestamp proximity.
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

- **Active sprint**: 5
- **Last updated**: Sprint 4 closed 2026-04-28; Telegram notifications +
  chart rendering shipped. Killzone gating filter added (16 → 13 setups).
  Sample chart visually validated. Manual E2E test pending on Windows host
  (`scripts/test_notification.py`). Sprint 5 ready: journal & outcome
  tracking.

Each sprint completion: update this section with `Active sprint`, key
findings from the previous sprint, and any roadmap revisions.
