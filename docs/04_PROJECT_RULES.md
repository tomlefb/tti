# 04 — Project Rules

Conventions, do's and don'ts. These rules apply to every code change. If
Claude Code is uncertain about how to do something, default to the rules
here, then to the strategy doc, then to the detection philosophy doc.

---

## Architectural rules (non-negotiable)

1. **Detection is rule-based.** No LLM call inside the detection pipeline
   (Sprints 0–6). LLM is only allowed as a **post-detection qualifier**
   (Sprint 7+) on structured input (never raw OHLC, never images).
   See `07_DETECTION_PHILOSOPHY.md`.
2. **Calibrated detectors require empirical calibration**, not arbitrary
   defaults. Any sprint shipping a calibrated detector is not "done"
   without a calibration session report.
3. **No auto-trading code.** This codebase will never contain `mt5.order_send()`
   or equivalent. If a feature request implies it, push back and refer to
   `CLAUDE.md` rule #1.
4. **Single source of truth for config.** All thresholds, buffers, session
   times, watched pairs live in `config/settings.py`. No magic numbers in
   detection code.
5. **Pure functions where possible.** Detectors take data, return data, no
   side effects. I/O at the edges only.
6. **Every detector has unit tests** before being wired into the pipeline.
7. **All times are UTC internally.** Convert to Paris/EST only at display
   time. Use `zoneinfo`, not naive datetimes.
8. **Broker time is not UTC.** Always go through `mt5_client` time conversion
   helpers when reading MT5 data.

---

## Code style

- **Language**: English everywhere — code, comments, docstrings, commit messages, docs.
- **Formatter**: `black` with default settings.
- **Linter**: `ruff` with sensible defaults.
- **Type hints**: required on all public functions; encouraged elsewhere.
- **Docstrings**: Google style for public functions and classes.
- **Imports**: stdlib → third-party → local, separated by blank lines.
- **Naming**:
  - `snake_case` for functions, variables, modules
  - `PascalCase` for classes and dataclasses
  - `SCREAMING_SNAKE_CASE` for module-level constants
  - Private internals prefixed with `_`
- **No wildcard imports** (`from foo import *`).
- **No bare `except:` clauses.** Always catch specific exceptions.

---

## File / module organization

- One concept per module. `swings.py` does swings. `fvg.py` does FVGs.
  Don't bundle.
- Each `src/<area>/` package has an `__init__.py` exporting the public API
  of that area. Internals stay internal.
- Tests mirror source layout: `src/detection/swings.py` ↔ `tests/detection/test_swings.py`.

---

## Testing

- `pytest` is the runner.
- Test files: `tests/<area>/test_<module>.py`.
- Fixtures for OHLC in `tests/fixtures/` as parquet or CSV. Document the
  scenario each fixture represents.
- Aim for: every detector has tests for (1) the happy path, (2) the
  empty/edge case, (3) at least one tricky case from real data.
- A failing test on `main` is a stop-the-line event. Fix or revert.
- Run `pytest` before pushing any meaningful change.
- Calibration tests (in `calibration/`) are separate from unit tests and
  not run in CI.

---

## Logging

- Use stdlib `logging`, configured once at startup in `src/scheduler/runner.py`.
- Loggers per module: `logger = logging.getLogger(__name__)`.
- Levels:
  - `DEBUG`: per-candle decisions inside detectors
  - `INFO`: detection cycle start/end, setups found, notifications sent
  - `WARNING`: data anomalies, missing fixtures, retried operations
  - `ERROR`: exceptions caught with context — also pinged to Telegram
  - `CRITICAL`: system cannot continue (DB unreachable, MT5 dead 5+ minutes)
- Log to `logs/system.log` with rotation (10 MB × 5 files).
- Never log secrets or full account balances. PII-light.

---

## Error handling

- The detection pipeline must NEVER crash the scheduler. Wrap each cycle
  in a try/except, log the error, ping Telegram, continue to the next cycle.
- MT5 disconnects: retry with exponential backoff (1s, 2s, 4s, 8s, 16s, give up).
- Telegram send failures: log, but don't retry forever — drop the
  notification after 3 attempts and continue.
- Database errors: stop the system; data integrity matters more than uptime.

---

## Performance

- Not a HFT system. Latency is irrelevant; correctness is everything.
- A detection cycle should complete in < 30 seconds for all 4 pairs.
- Memory: keep a rolling window of recent candles; don't hoard history
  beyond what's needed (e.g., 500 M5 candles, 200 H1, 200 H4, 100 D1 per pair).

---

## Things Claude Code should NOT do without asking

- Add a new third-party dependency. (Discuss in chat first; update
  `requirements.txt` only after approval.)
- Change a value in `config/settings.py.example`. (Tuning is the operator's
  call, not the assistant's. Calibration runs may *suggest* values, but
  committing them is the operator's decision.)
- Modify `docs/01_STRATEGY_TJR.md` or `docs/07_DETECTION_PHILOSOPHY.md`.
  (Strategy and methodology changes go through human review.)
- Touch `config/secrets.py` or `.env`.
- Implement order placement, position modification, or any code that calls
  `mt5.order_*` functions. **This is a hard prohibition.**
- "Improve" the architecture by reorganizing modules without an explicit
  request to do so.
- Replace a calibrated rule with an LLM call "to make it better". The
  separation rule-based/LLM is in `07_DETECTION_PHILOSOPHY.md` and is not
  to be relaxed casually.

---

## Things Claude Code should ALWAYS do

- Read the relevant doc(s) listed in `CLAUDE.md` before starting a task.
- For detection tasks, read both `01_STRATEGY_TJR.md` and `07_DETECTION_PHILOSOPHY.md`.
- Write tests alongside any new detector.
- Update `docs/03_ROADMAP.md` when a sprint deliverable ships.
- Use type hints and docstrings.
- Push back if a request conflicts with the rules in this file or the
  strategy spec.

---

## Git conventions

- Branch per sprint or per feature: `sprint-1-bias`, `feat/fvg-detector`.
- Commit messages: imperative mood, scoped. Examples:
  - `detection: add 3-bar fractal swing detector`
  - `journal: persist setup decisions on button callback`
  - `docs: clarify FVG size filter`
- No commits to `main` directly during active development; PR or
  fast-forward merge after the sprint.
- `.gitignore` is the law: never bypass it for "just this once".

---

## Secrets management

- Anything sensitive lives in `config/secrets.py` (gitignored).
- Loaded into the rest of the code via `config/settings.py` which imports
  from `secrets`. Never hardcode tokens, account IDs, or passwords.
- The `config/secrets.py.example` template documents what variables are
  needed and how to obtain them.
