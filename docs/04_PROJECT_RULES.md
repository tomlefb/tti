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

## Configuration reference

Authoritative cross-reference of every key in `config/settings.py` (and
`config/secrets.py`). Names below are the **canonical names**; any code,
test, or doc that mentions a config key must use these names verbatim.
If a key is renamed, update this table first, then code, then docs.

### Timezones (`zoneinfo.ZoneInfo`)

| Key | Type | Meaning |
|---|---|---|
| `TZ_PARIS` | `ZoneInfo` | `Europe/Paris` — operator's local time, used for session boundaries and display. |
| `TZ_UTC` | `ZoneInfo` | `UTC` — internal storage timezone. All datetimes inside the system are UTC. |
| `TZ_NY` | `ZoneInfo` | `America/New_York` — convenience for NY-session displays. |

### Watched instruments

| Key | Type | Meaning |
|---|---|---|
| `WATCHED_PAIRS` | `list[str]` | Symbols scanned each cycle. Names must match those exposed by the MT5 terminal. |

### Sessions (Paris time tuples `(start_hour, start_min, end_hour, end_min)`)

| Key | Type | Meaning |
|---|---|---|
| `SESSION_ASIA` | `tuple[int, int, int, int]` | Asian range used for liquidity marking (accumulation). |
| `KILLZONE_LONDON` | `tuple[int, int, int, int]` | London killzone — first scan window. |
| `KILLZONE_NY` | `tuple[int, int, int, int]` | NY killzone — second scan window. |

### Swings (calibrated)

| Key | Type | Meaning |
|---|---|---|
| `SWING_LOOKBACK_H4` | `int` | Candles each side that must be lower (high) / higher (low) on H4. |
| `SWING_LOOKBACK_H1` | `int` | Same on H1. |
| `SWING_LOOKBACK_M5` | `int` | Same on M5. |
| `MIN_SWING_AMPLITUDE_ATR_MULT_H4` | `float` | Min distance on H4 to prev opposite-type H4 swing as multiple of ATR(14) on H4. |
| `MIN_SWING_AMPLITUDE_ATR_MULT_H1` | `float` | Same on H1. |
| `MIN_SWING_AMPLITUDE_ATR_MULT_M5` | `float` | Same on M5 (used by `detect_mss`'s significant-swing pass). |
| `BIAS_SWING_COUNT` | `int` | Number of significant swings considered when computing daily bias. |
| `BIAS_REQUIRE_H1_CONFIRMATION` | `bool` | `False` (Sprint 3 default): H4 alone determines bias. `True` (legacy): H4 ∧ H1 must agree. |

### Multi-TF confluence (Sprint 2 — calibrated heuristic)

| Key | Type | Meaning |
|---|---|---|
| `H4_H1_TIME_TOLERANCE_CANDLES_H4` | `int` | Time tolerance for promoting an H1 swing to "major" via H4 confluence, expressed in H4 candles (±). |
| `H4_H1_PRICE_TOLERANCE_FRACTION` | `float` | Price tolerance for the same match, as fraction of swing price (e.g. `0.001` = 0.1%). |
| `SWING_LEVELS_LOOKBACK_COUNT` | `int` | Number of most recent H4 swings considered as promotion candidates. Minor H1 swings come from the last `2 × this` swings. |

### Sweep (calibrated)

| Key | Type | Meaning |
|---|---|---|
| `SWEEP_RETURN_WINDOW_CANDLES` | `int` | Candles after the wick during which the close must return back across the swept level. |
| `SWEEP_DEDUP_TIME_WINDOW_MINUTES` | `int` | Time window (minutes) within which same-direction, same-level sweeps are collapsed to the deepest one. Sprint 3 heuristic. |
| `SWEEP_DEDUP_PRICE_TOLERANCE_FRACTION` | `float` | Relative price tolerance for the same dedup; symmetric `\|p1 − p2\| ≤ tol × (\|p1\| + \|p2\|) / 2`. |

### MSS (calibrated)

| Key | Type | Meaning |
|---|---|---|
| `MSS_DISPLACEMENT_MULTIPLIER` | `float` | MSS body must be ≥ multiplier × mean body of the previous N candles. |
| `MSS_DISPLACEMENT_LOOKBACK` | `int` | The N above. |

### FVG (calibrated for size; pure logic for geometry)

| Key | Type | Meaning |
|---|---|---|
| `FVG_ATR_PERIOD` | `int` | ATR period used to size-filter FVGs. |
| `FVG_MIN_SIZE_ATR_MULTIPLIER` | `float` | Min FVG size as multiple of ATR(`FVG_ATR_PERIOD`). |

### Setup thresholds

| Key | Type | Meaning |
|---|---|---|
| `MIN_RR` | `float` | Minimum risk-reward ratio for a setup to qualify. |
| `A_PLUS_RR_THRESHOLD` | `float` | RR at or above which grading may upgrade to `A+`. |
| `PARTIAL_TP_RR_TARGET` | `float` | RR cap for the partial-exit TP1. When `tp_runner_rr` exceeds this, `tp1_*` is clamped to entry ± `PARTIAL_TP_RR_TARGET` × risk. |

### Per-instrument (`INSTRUMENT_CONFIG[symbol]` dict)

| Key | Type | Unit | Meaning |
|---|---|---|---|
| `sweep_buffer` | `float` | price units | Min wick excursion beyond a level to count as a sweep. |
| `equal_hl_tolerance` | `float` | price units | Max distance between two levels to consider them "equal". |
| `sl_buffer` | `float` | price units | Extra distance beyond sweep extreme for stop loss. |

Units are USD for `XAUUSD`, points for indices (`NDX100`), decimal price
for FX (`EURUSD`, `GBPUSD`).

### Risk management — hard stops

| Key | Type | Unit | Meaning |
|---|---|---|---|
| `ACCOUNT_BALANCE_BASE` | `float` | USD | Base account size; reference for sanity checks. |
| `DAILY_LOSS_LIMIT` | `float` | USD (absolute) | Broker daily loss cap. |
| `MAX_LOSS_LIMIT` | `float` | USD (absolute) | Broker max loss cap (challenge bust). |
| `PROFIT_TARGET` | `float` | USD (absolute) | Phase profit target. |
| `DAILY_LOSS_STOP_FRACTION` | `float` | fraction (0–1) | System suppresses notifications at this fraction of `DAILY_LOSS_LIMIT`. |
| `MAX_LOSS_STOP_FRACTION` | `float` | fraction (0–1) | Same vs `MAX_LOSS_LIMIT`; triggers permanent suspension + critical alert. |
| `RISK_PER_TRADE_FRACTION` | `float` | fraction (0–1) | Fraction of account balance risked per trade (e.g. 0.01 = 1%). |
| `MAX_TRADES_PER_DAY` | `int` | count | Daily total trade cap across all pairs. |
| `MAX_TRADES_PER_PAIR_PER_DAY` | `int` | count | Per-pair daily trade cap. |
| `MAX_CONSECUTIVE_SL_PER_DAY` | `int` | count | Stops the day after this many consecutive SLs. |
| `NEWS_BLACKOUT_TODAY` | `bool` | — | Manual switch suppressing notifications around scheduled news. |

### Scheduler

| Key | Type | Meaning |
|---|---|---|
| `DETECTION_INTERVAL_MINUTES` | `int` | Period of the detection cycle inside killzones. |
| `HEARTBEAT_AT_KILLZONE_START` | `bool` | Whether to send a Telegram heartbeat at killzone start. |

### Logging

| Key | Type | Meaning |
|---|---|---|
| `LOG_FILE` | `str` (path) | Rotating log file path. |
| `LOG_MAX_BYTES` | `int` | Per-file rotation threshold in bytes. |
| `LOG_BACKUP_COUNT` | `int` | Number of rotated files to retain. |
| `LOG_LEVEL` | `str` | Root logger level (`DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL`). |

### Database

| Key | Type | Meaning |
|---|---|---|
| `DB_PATH` | `str` (path) | SQLite journal file path. |

### Notification (Sprint 4)

| Key | Type | Meaning |
|---|---|---|
| `CHART_OUTPUT_DIR` | `str` (path) | Directory for runtime chart screenshots (gitignored). |
| `CHART_LOOKBACK_CANDLES_M5` | `int` | M5 candles before MSS confirm shown in the chart. |
| `CHART_LOOKFORWARD_CANDLES_M5` | `int` | M5 candles after MSS confirm shown in the chart. |
| `TELEGRAM_CALLBACK_TIMEOUT_SECONDS` | `int` | Polling window for `scripts/test_notification.py` after sending the test setup. |

### Secrets (from `config/secrets.py`, gitignored)

| Key | Type | Meaning |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | `str` | Token from @BotFather. |
| `TELEGRAM_CHAT_ID` | `int` | Operator's Telegram chat ID. |
| `MT5_LOGIN` | `int` | MT5 account number. |
| `MT5_PASSWORD` | `str` | MT5 password. |
| `MT5_SERVER` | `str` | MT5 server name (exact, from broker). |

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
