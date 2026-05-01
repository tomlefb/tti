# 02 — Architecture

## High-level data flow

```
[Windows host machine]
  │
  ├── MetaTrader 5 terminal (running, logged in to FundedNext account)
  │
  └── Python process (this project)
       │
       ├── APScheduler ── triggers every 5 min during killzones
       │
       ├── MT5 client ─── fetches OHLC for D1, H4, H1, M15, M5 on watched pairs
       │
       ├── Detection pipeline (rule-based — see 07_DETECTION_PHILOSOPHY)
       │     1. Daily bias  (H4 + H1 swing structure, calibrated)
       │     2. Liquidity levels (Asian H/L, PDH/PDL, swings, equal highs/lows)
       │     3. Sweep detection (M5, calibrated buffers)
       │     4. MSS confirmation (M5, calibrated displacement)
       │     5. POI identification (FVG / OB)
       │     6. SL/TP/RR computation (pure logic)
       │     7. Setup quality grading (heuristic A+/A/B)
       │
       ├── (Optional, Sprint 7+) LLM qualifier
       │     Receives structured setup context, returns JSON quality score.
       │     NEVER receives raw OHLC or screenshots.
       │
       ├── Chart renderer (mplfinance) ── annotated PNG screenshot
       │
       ├── Telegram bot (python-telegram-bot) ── sends notification
       │     │
       │     └── inline buttons: [Taken] [Skipped]
       │
       ├── (Sprint 7+) Auto-execution layer (src/execution/)
       │     1. safe_guards.check_pre_trade  — kill switch + auto-disabled
       │        + delegates to hard_stops for daily-loss / SL / count gates
       │     2. order_manager.place_order    — position sizing, mt5.order_send,
       │        spread anomaly logging, journal persistence
       │     3. position_lifecycle.check_open_positions — every 30s:
       │        pending → filled, TP1 partial close + SL → BE, TP_runner / SL exit
       │     4. position_lifecycle.end_of_killzone_cleanup — 12:00 / 18:00 Paris
       │     5. recovery.reconcile_orphan_positions — runs once at startup
       │
       ├── SQLite journal ── setups, decisions, outcomes,
       │                     orders, spread_anomalies (Sprint 7+)
       │
       └── Outcome tracker ── periodically queries MT5 trade history
                              to attach actual trade results to journal entries
```

## Components

### 1. `src/mt5_client/`

Thin wrapper around the `MetaTrader5` package.

Responsibilities:

- `connect()` / `shutdown()` lifecycle
- `fetch_ohlc(symbol, timeframe, n_candles)` returning a pandas DataFrame
- Time conversion: broker time ↔ UTC ↔ Paris (broker time is typically
  UTC+2 winter / UTC+3 summer for many FX brokers — must detect at runtime)
- `get_account_info()` for risk checks
- `get_recent_trades(since)` for outcome tracking
- Connection health check + retry logic

### 2. `src/detection/`

The core. One module per concept. Each module is **pure**: takes data in,
returns structured output, no side effects.

```
detection/
├── swings.py        # find_swing_highs(), find_swing_lows() — calibrated
├── bias.py          # compute_daily_bias() using H4 + H1 swings
├── liquidity.py     # mark_asian_range(), mark_pdh_pdl(),
│                    # mark_swing_levels(), find_equal_highs_lows()
├── sweep.py         # detect_sweep() on M5 vs marked levels — calibrated
├── mss.py           # detect_mss() on M5 after sweep — calibrated displacement
├── fvg.py           # detect_fvg() in displacement window
├── order_block.py   # detect_order_block() as fallback POI
├── setup.py         # orchestrator — combines the above into a Setup object
└── grading.py       # A+/A/B classification (heuristic)
```

A `Setup` object is the final detection output:

```python
@dataclass
class Setup:
    timestamp_utc: datetime
    symbol: str
    direction: Literal["long", "short"]
    daily_bias: Literal["bullish", "bearish"]
    swept_level: LiquidityLevel
    mss_candle_time: datetime
    poi: POI                     # FVG or OrderBlock
    entry_price: float
    stop_loss: float
    take_profit: float
    risk_reward: float
    quality: Literal["A+", "A", "B"]
    confluences: list[str]       # ["OTE_overlap", "FVG+OB", ...]
```

### 3. `src/notification/`

```
notification/
├── chart_renderer.py    # mplfinance → annotated PNG of M5 with all levels
├── message_formatter.py # text summary of the Setup
└── telegram_bot.py      # send_setup_notification(), button callbacks
```

The notification message contains:

- Symbol, direction, quality (A+/A/B)
- Entry / SL / TP / RR
- Daily bias, swept level, killzone
- Inline buttons: `Taken` / `Skipped`
- Attached PNG: M5 chart with HTF levels, sweep, MSS, FVG/OB highlighted

### 4. `src/journal/`

```
journal/
├── schema.sql           # SQLite schema
├── db.py                # connection, migrations
├── repository.py        # CRUD for setups, decisions, outcomes
└── outcome_tracker.py   # background task: matches MT5 trades to journal
```

Journal tables (sketch):

- `setups`: every detection (including those rejected — useful for tuning)
- `decisions`: operator's response (taken / skipped / no_response)
- `outcomes`: actual P&L from MT5 history, attached after the fact
- `daily_state`: bias of the day, trades count, daily PnL, hard-stop flag

### 5. `src/scheduler/`

```
scheduler/
├── jobs.py              # the killzone-bound detection cycle
└── runner.py            # APScheduler setup + main loop
```

Cron schedule:

- Every 5 minutes during 09:00–12:00 Paris (London killzone)
- Every 5 minutes during 15:30–18:00 Paris (NY killzone)
- Once at 08:55 Paris and 15:25 Paris: pre-killzone bias computation
- Daily at 23:00 Paris: outcome tracker reconciliation

### 6. `src/execution/` (Sprint 7+)

```
execution/
├── safe_guards.py        # kill switch + auto_trading_disabled + hard_stops delegation
├── order_manager.py      # OrderResult, compute_volume, place_order, cancel_order, modify_position_sl
├── position_lifecycle.py # check_open_positions, end_of_killzone_cleanup, _reconcile_closed_position
└── recovery.py           # reconcile_orphan_positions (orphan close + lost-order detection)
```

Auto-execution module. Owns every `mt5.order_*` call in the codebase.

- `safe_guards.check_pre_trade(setup, ...)` runs:
  1. Kill switch (file-based, hard-disable).
  2. `daily_state.auto_trading_disabled` flag (set by the layer itself
     when a critical fault is observed mid-cycle).
  3. Delegates to `src/scheduler/hard_stops.is_blocked(...)` for the
     financial gates (account info, daily loss, max loss, news,
     daily count, consecutive SL, per-pair count).
  No financial-gate logic is duplicated.

- `order_manager.place_order(...)` is the place-order pipeline:
  pre-flight → account+symbol info → position-size calc → spread
  anomaly logging → MT5 `order_send` → retcode check → persist
  to `orders` → Telegram pre+post notification. Returns
  `OrderResult(success, ticket, error_code, error_message)`.

- `position_lifecycle.check_open_positions(...)` runs every
  `LIFECYCLE_CHECK_INTERVAL_SEC` (default 30s) and walks status
  transitions: `pending → filled → tp1_hit → (tp_runner_hit | sl_hit)`.
  TP1 detection: `bid ≥ TP1` (long) | `ask ≤ TP1` (short).
  Idempotent — once `position.volume < order.volume`, no further
  partial fires. Realised R uses `profit_usd / initial_risk_usd` when
  available (handles BLENDED TP1-partial + runner outcomes correctly).

- `recovery.reconcile_orphan_positions(...)` runs once at scheduler
  startup. Orphan positions (MT5 has it, journal does not — or knows
  about it with a terminal status) are closed at market with a
  CRITICAL Telegram alert. Lost orders (journal has it, MT5 does
  not) are marked `lost`.

### 7. `src/qualifier/` (Sprint 7+, optional)

```
qualifier/
└── llm.py               # post-detection LLM qualification
```

Only ever called **after** the rule-based pipeline has produced a `Setup`.
Receives structured fields (never raw OHLC or images). Returns a JSON
score. See `07_DETECTION_PHILOSOPHY.md` for what the LLM is and is not
allowed to do.

### 8. `config/`

```
config/
├── settings.py          # paires, sessions, buffers, thresholds (gitignored)
├── settings.py.example  # template
└── secrets.py           # tokens, account IDs (gitignored)
```

`settings.py` is the single place to tune thresholds. No magic numbers
elsewhere in the code.

### 9. `calibration/`

```
calibration/
├── README.md            # how to run a calibration session
├── reference_charts/    # operator-marked reference data (committed)
└── runs/                # output of calibration runs (gitignored)
```

See `07_DETECTION_PHILOSOPHY.md` for the calibration protocol.

## Process model

A single Python process runs continuously on the Windows host. It must:

- Survive MT5 disconnects (retry with backoff)
- Log everything to a rotating file (`logs/system.log`)
- Send a Telegram heartbeat once per killzone start ("✅ London killzone
  started, watching XAUUSD, NDX100, EURUSD, GBPUSD")
- Send a Telegram alert on critical errors

## Testing strategy

- **Unit tests** (pytest) for every detector, with hand-crafted OHLC
  fixtures producing known outputs.
- **Integration tests** for the orchestrator on recorded historical OHLC
  (a few real days of data saved as parquet).
- **Calibration tests** (separate from unit tests): comparison of
  detector output to operator-marked reference charts. See
  `07_DETECTION_PHILOSOPHY.md`.
- **No live tests against MT5** in CI — only smoke-test scripts run manually
  on the Windows host (`scripts/test_*.py`).

## What we explicitly do NOT build

- A web UI. Telegram is the only interface.
- A REST API. The system is a single-process internal tool.
- Cloud deployment. Runs on the operator's home Windows machine.
- Live-account auto-execution by default. Sprint 7 enables auto-execution
  on the **demo** account; promoting to live requires a separate config
  change AND a careful review pass.
