# TJR Trading System — Claude Code Context

> This file is auto-loaded at the start of every Claude Code session.
> It is the **entry point**: read it first, then navigate to the relevant doc.

---

## What this project is

An automated SMC/ICT trade setup **detector + executor** based on TJR Trades'
methodology.

It scans the validated portfolio (XAUUSD + NDX100, see rule 9) on the M5
timeframe during London and NY killzones, detects valid setups using
deterministic Python logic, and **auto-executes** the A/A+ setups on a
FundedNext demo account (Sprint 7+). Telegram notifications fire in parallel
at every lifecycle event so the operator retains full visibility.

The goal is to remove the operator from continuous chart monitoring AND from
the click-to-execute step that suffers under work-hour constraints, while
keeping every safety net (daily loss circuit breaker, kill switch, hard
stops, journal of every event) under operator control.

---

## Critical rules — read before any task

1. **Auto-execution is enabled on the demo account (Sprint 7+).** Order
   placement code lives in `src/execution/`. The `AUTO_TRADING_ENABLED`
   flag in `config/settings.py` controls activation; the `KILL_SWITCH`
   file at the project root, when present, hard-disables order placement
   immediately. The operator can move to a live account later by
   updating settings + a careful review. Telegram notifications run in
   parallel for visibility and post-trade review. The detection
   pipeline is unchanged — `src/execution/` only consumes `Setup`
   objects from it.
2. **Detection is rule-based; LLMs are only for the judgment layer.**
   See `docs/07_DETECTION_PHILOSOPHY.md` for the precise taxonomy. The short
   version: pure logic and calibrated rules in Python; LLMs only as a
   post-detection qualifier (Sprint 7+).
3. **Always write unit tests** for any detector before integrating it into the
   live pipeline. Use fixtures with known OHLC data and known expected output.
4. **Calibrated rules require empirical calibration**, not arbitrary defaults.
   See `07_DETECTION_PHILOSOPHY.md` for the calibration protocol.
5. **Be skeptical of backtests.** A strategy that works on 6 months may fail
   on 2 years. Out-of-sample validation required for any tuning.
6. **All times stored UTC internally.** Convert to Paris/EST only at display
   time. Broker time ≠ UTC — always use the `mt5_client` time conversion helpers.
7. **No silent failures.** Every detector logs what it sees and what it
   decided. Missed setups must be debuggable after the fact.
8. **Funded Next account is at risk.** Hard-code drawdown limits as a
   safety layer (system stops sending notifications if daily loss exceeded).
9. **Validated WATCHED_PAIRS.** Validated portfolio for live deployment is
   XAUUSD + NDX100 (A/A+ qualities only). ETHUSD was tested in Sprint 6.5
   but dropped in Sprint 6.6 due to A-grade filter inversion on crypto
   microstructure (mean R = -0.42 on 26 A-grade setups, vs +0.27 on
   all-quality). ETHUSD config preserved in `config/settings.py.example`
   for future re-calibration but is not actively watched. Other instruments
   tested (EURUSD, GBPUSD, USOUSD, US30, GER30, SPX500, BTCUSD, USDJPY,
   XAGUSD) all dropped at Sprint 6.5 due to insufficient or negative edge.
   See `calibration/runs/2026-04-29T*_grid_search_extended_fast*.md` and
   `calibration/runs/*_sprint_6_6_portfolio_validation.md` for the
   validation reports. Do not add pairs without re-running the validation.
10. **Notification gating by quality.** `NOTIFY_QUALITIES = ["A+", "A"]`
    is the live-deployment default — B-grade detections are still produced
    by the orchestrator and journaled (`was_notified=False`) so the
    operator can audit false negatives, but they do not push to Telegram.
    Auto-execution honours the same gate (B-grade detections never reach
    `place_order`).
11. **Lifecycle parity with backtest.** Position lifecycle (TP1 partial →
    BE → TP runner / SL) must mirror the backtest behaviour exactly. Any
    change to the execution logic must be reflected in the backtest engine
    (`calibration/`) so live results stay comparable to backtest
    expectations. Diverging the two is a stop-the-line event.

For the full rule set, see `docs/04_PROJECT_RULES.md`.

---

## Where to find what

Before starting any task, read the relevant doc(s):

| Task involves... | Read first |
|---|---|
| Project vision and why decisions were made | `docs/00_PROJECT_CONTEXT.md` |
| Trading strategy logic (bias, sweep, MSS, FVG, entry, SL/TP) | `docs/01_STRATEGY_TJR.md` |
| Architecture, data flow, components, tech choices | `docs/02_ARCHITECTURE.md` |
| What sprint we are in, what to build next | `docs/03_ROADMAP.md` |
| Coding conventions, project rules, do/don't | `docs/04_PROJECT_RULES.md` |
| Funded Next constraints, risk limits, hard stops | `docs/05_TRADING_RULES.md` |
| SMC/ICT terminology cheatsheet | `docs/06_GLOSSARY.md` |
| **How to decide rule-based vs LLM, calibration protocol** | `docs/07_DETECTION_PHILOSOPHY.md` |

If a task touches detection logic in any way, **always** read both
`01_STRATEGY_TJR.md` and `07_DETECTION_PHILOSOPHY.md` together. The first
says *what* to detect; the second says *how to think about detecting it*.

---

## Tech stack (fixed)

- **OS for runtime**: Windows (MT5 Python lib is Windows-only)
- **OS for development**: macOS (developer's main machine, syncs via Git)
- **Python**: 3.11+
- **Market data**: `MetaTrader5` official Python package (reads from running MT5 terminal)
- **Data manipulation**: `pandas`, `numpy`
- **Indicators**: custom implementations preferred; `pandas-ta` allowed for ATR/standard stuff
- **Charting (for notifications)**: `mplfinance`
- **Notifications**: `python-telegram-bot` (async)
- **Persistence**: SQLite (via `sqlite3` stdlib or `sqlalchemy` if needed)
- **Scheduling**: `APScheduler`
- **Timezones**: `pytz` or `zoneinfo` (Python 3.9+)

---

## Current state

- **Sprint**: 7 (Auto-execution on demo) — complete pending operator review.
- **Status**: Detection + auto-execution + lifecycle + recovery + Telegram
  notifications wired and tested. Smoke-test passes on Mac (dry-run); MT5
  go-live pending operator review of the `sprint-7` branch.
- **Next milestone**: Operator merges `sprint-7` to `main`, pulls on the
  Windows host, runs the scheduler against the FundedNext demo account.

When a sprint completes, update `docs/03_ROADMAP.md` (mark items done, note
deviations, update "Current state" above).

---

## Key architectural invariants

- Detection pipeline runs on Windows host where MT5 terminal is open and connected.
- Detection cycle triggered by `APScheduler` every 5 minutes during killzones.
- Each cycle: fetch OHLC → compute bias → mark liquidity → scan for sweep+MSS+POI → if valid, notify + (Sprint 7+) auto-execute on A/A+.
- Position lifecycle polled every 30s (Sprint 7+): pending → filled → tp1_hit (50% close + SL→BE) → tp_runner_hit / sl_hit. Mirrors backtest behaviour exactly.
- End-of-killzone cleanup (Sprint 7+): pending limit orders cancelled at 12:00 Paris (London) and 18:00 Paris (NY).
- Recovery on startup (Sprint 7+): orphan positions closed at market with critical Telegram alert; lost orders marked.
- Telegram bot inline buttons (`Taken` / `Skipped`) write to SQLite journal.
- Trade outcomes are tracked post-hoc by querying MT5 trade history (no live PnL feed needed).

---

## Working with this project

- The developer codes from a Mac, often via Claude Code.
- Code runs on the Windows machine.
- Sync via private GitHub repo. Never commit secrets.
- `config/secrets.py` is gitignored. Use `config/secrets.py.example` as the template.
- All English in code, comments, docstrings, and docs.
