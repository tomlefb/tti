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
       ├── SQLite journal ── all detected setups, decisions, outcomes
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

### 6. `src/qualifier/` (Sprint 7+, optional)

```
qualifier/
└── llm.py               # post-detection LLM qualification
```

Only ever called **after** the rule-based pipeline has produced a `Setup`.
Receives structured fields (never raw OHLC or images). Returns a JSON
score. See `07_DETECTION_PHILOSOPHY.md` for what the LLM is and is not
allowed to do.

### 7. `config/`

```
config/
├── settings.py          # paires, sessions, buffers, thresholds (gitignored)
├── settings.py.example  # template
└── secrets.py           # tokens, account IDs (gitignored)
```

`settings.py` is the single place to tune thresholds. No magic numbers
elsewhere in the code.

### 8. `calibration/`

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
- Order execution. The human places trades manually in MT5.
