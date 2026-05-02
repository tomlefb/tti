# Dukascopy coverage probe — 2026-05-02T14-28-29Z

## Setup

- Library: `dukascopy_python==4.0.1` (PyPI, official-style fork)
- Interval: M5 (`INTERVAL_MIN_5`)
- Side: BID
- Window per anchor: 5 calendar days
- Per-fetch retries: 2 (lib-internal); wall-clock not externally capped per fetch

## Result matrix

| Target | Dukascopy code | Recent | 5y | 10y | 15y | Verdict |
|---|---|---|---|---|---|---|
| XAUUSD | `XAU/USD` | OK 1380 | OK 1104 | OK 829 | OK 957 | depth >=15y |
| NDX100 | `E_NQ-100` | OK 1335 | OK 1068 | OK 507 | FAIL (empty dataframe) | depth >=10y |
| SPX500 | `E_SandP-500` | OK 1335 | OK 1067 | OK 507 | FAIL (empty dataframe) | depth >=10y |
| EURUSD | `EUR/USD` | OK 1428 | OK 1136 | OK 865 | FAIL (empty dataframe) | depth >=10y |
| GBPUSD | `GBP/USD` | OK 1428 | OK 1140 | OK 865 | FAIL (empty dataframe) | depth >=10y |
| US30 | `E_D&J-Ind` | OK 1335 | OK 1068 | OK 507 | FAIL (empty dataframe) | depth >=10y |
| BTCUSD | `BTC/USD` | OK 1441 | OK 1441 | FAIL (empty dataframe) | FAIL (empty dataframe) | depth >=5y |

## Detail per fetch

### XAUUSD (`XAU/USD`)

| Window | OK | Bars | Elapsed (s) | First | Last | Error |
|---|---|---|---|---|---|---|
| recent | yes | 1380 | 0.5 | 2026-04-19 22:00:00+00:00 | 2026-04-24 20:55:00+00:00 |  |
| 5y | yes | 1104 | 0.8 | 2021-04-19 22:00:00+00:00 | 2021-04-23 20:55:00+00:00 |  |
| 10y | yes | 829 | 0.6 | 2016-04-19 22:00:00+00:00 | 2016-04-24 22:00:00+00:00 |  |
| 15y | yes | 957 | 0.6 | 2011-04-19 22:00:00+00:00 | 2011-04-24 22:00:00+00:00 |  |

### NDX100 (`E_NQ-100`)

| Window | OK | Bars | Elapsed (s) | First | Last | Error |
|---|---|---|---|---|---|---|
| recent | yes | 1335 | 0.4 | 2026-04-19 22:00:00+00:00 | 2026-04-24 20:10:00+00:00 |  |
| 5y | yes | 1068 | 0.5 | 2021-04-19 22:00:00+00:00 | 2021-04-23 20:10:00+00:00 |  |
| 10y | yes | 507 | 0.7 | 2016-04-20 06:00:00+00:00 | 2016-04-22 20:00:00+00:00 |  |
| 15y | no | 0 | 0.6 |  |  | empty dataframe |

### SPX500 (`E_SandP-500`)

| Window | OK | Bars | Elapsed (s) | First | Last | Error |
|---|---|---|---|---|---|---|
| recent | yes | 1335 | 0.4 | 2026-04-19 22:00:00+00:00 | 2026-04-24 20:10:00+00:00 |  |
| 5y | yes | 1067 | 0.6 | 2021-04-19 22:00:00+00:00 | 2021-04-23 20:10:00+00:00 |  |
| 10y | yes | 507 | 0.7 | 2016-04-20 06:00:00+00:00 | 2016-04-22 20:00:00+00:00 |  |
| 15y | no | 0 | 0.8 |  |  | empty dataframe |

### EURUSD (`EUR/USD`)

| Window | OK | Bars | Elapsed (s) | First | Last | Error |
|---|---|---|---|---|---|---|
| recent | yes | 1428 | 0.4 | 2026-04-19 22:00:00+00:00 | 2026-04-24 20:55:00+00:00 |  |
| 5y | yes | 1136 | 0.6 | 2021-04-19 22:00:00+00:00 | 2021-04-23 20:55:00+00:00 |  |
| 10y | yes | 865 | 0.7 | 2016-04-19 22:00:00+00:00 | 2016-04-24 22:00:00+00:00 |  |
| 15y | no | 0 | 0.6 |  |  | empty dataframe |

### GBPUSD (`GBP/USD`)

| Window | OK | Bars | Elapsed (s) | First | Last | Error |
|---|---|---|---|---|---|---|
| recent | yes | 1428 | 0.4 | 2026-04-19 22:00:00+00:00 | 2026-04-24 20:55:00+00:00 |  |
| 5y | yes | 1140 | 0.5 | 2021-04-19 22:00:00+00:00 | 2021-04-23 20:55:00+00:00 |  |
| 10y | yes | 865 | 0.6 | 2016-04-19 22:00:00+00:00 | 2016-04-24 22:00:00+00:00 |  |
| 15y | no | 0 | 0.5 |  |  | empty dataframe |

### US30 (`E_D&J-Ind`)

| Window | OK | Bars | Elapsed (s) | First | Last | Error |
|---|---|---|---|---|---|---|
| recent | yes | 1335 | 0.3 | 2026-04-19 22:00:00+00:00 | 2026-04-24 20:10:00+00:00 |  |
| 5y | yes | 1068 | 0.7 | 2021-04-19 22:00:00+00:00 | 2021-04-23 20:10:00+00:00 |  |
| 10y | yes | 507 | 0.8 | 2016-04-20 06:00:00+00:00 | 2016-04-22 20:00:00+00:00 |  |
| 15y | no | 0 | 0.5 |  |  | empty dataframe |

### BTCUSD (`BTC/USD`)

| Window | OK | Bars | Elapsed (s) | First | Last | Error |
|---|---|---|---|---|---|---|
| recent | yes | 1441 | 0.7 | 2026-04-19 22:00:00+00:00 | 2026-04-24 22:00:00+00:00 |  |
| 5y | yes | 1441 | 0.6 | 2021-04-19 22:00:00+00:00 | 2021-04-24 22:00:00+00:00 |  |
| 10y | no | 0 | 0.6 |  |  | empty dataframe |
| 15y | no | 0 | 0.7 |  |  | empty dataframe |

## Data format sample

From `XAUUSD`:

- Columns: `['open', 'high', 'low', 'close', 'volume']`
- Index: pandas `DatetimeIndex`, tz-aware UTC, M5 cadence (gaps on weekends/holidays)
- First index: `2026-04-19 22:00:00+00:00`
- Last index:  `2026-04-24 20:55:00+00:00`

Sanity checks: tz-aware UTC ✅, M5 cadence ✅, OHLCV columns ✅. Format is
directly usable by the existing `pandas`-based pipeline; only column case /
index naming would need a thin adapter (compare to MT5 / Databento DataFrames).

## Note on the empty-15y windows

The 5-year baseline (2011-04-20 → 2011-04-25) was a holiday week (Good
Friday + Easter Monday), so I re-probed each failed instrument on
non-holiday weeks (March 2011, June 2011, June 2010) and then year-by-year
to find the actual data-start cutoff. Results:

| Target  | First M5 bar served by `dukascopy_python==4.0.1` |
|---|---|
| XAUUSD  | 2008-06-14 (probed; likely earlier) |
| EURUSD  | 2012-01-11 |
| GBPUSD  | 2012-06 (≤2011-06 returns empty) |
| NDX100  | 2012-06 |
| SPX500  | 2012-06 |
| US30    | 2012-06 |
| BTCUSD  | 2017-06 |

So the failures at 15y are not transient — this lib has a hard data-start
cutoff around 2012-Jan / 2012-Jun for FX-majors and US indices, and 2017
for BTCUSD. Dukascopy's website itself advertises FX history back to 2003,
so this is a lib-level limitation (the underlying historical files use a
different naming scheme pre-2012 that this lib does not map to).

## Summary

- Targets probed: **7**
- With M5 depth ≥ 5 years (cutoff ≤ 2021-05): **7 / 7**
- With M5 depth ≥ 10 years (cutoff ≤ 2016-05): **6 / 7** (BTCUSD only goes back to 2017)
- With M5 depth ≥ 15 years (cutoff ≤ 2011-05): **1 / 7** (XAUUSD only)
- Effective depth for backtests starting 2026-05:
  - XAUUSD: ~18 yr
  - EURUSD / GBPUSD / NDX100 / SPX500 / US30: ~14 yr
  - BTCUSD: ~9 yr

## Recommendation

**Full integration is viable.** All 7 instruments are addressable from a
single Python library with a clean DataFrame format compatible with the
existing pipeline, and depth comfortably exceeds the project's calibration
horizon (Sprint 6.5 grid-search uses windows of months-to-2-years, not
decades). Concrete next steps:

1. Build a `DukascopyClient` that wraps `dukascopy_python.fetch` and
   normalises the output to the project's canonical OHLCV schema (UTC index,
   `open/high/low/close/volume` lowercase — already matches).
2. Add a thin caching layer (parquet under `tests/fixtures/dukascopy/`)
   keyed by `(instrument, interval, side, start, end)`. Fetches are fast
   (≤1 s for 5 days of M5) but cache spares the network during repeated
   calibration runs.
3. For the cross-source verification work that motivated this probe
   (MT5 ↔ Databento divergence in `calibration/run_ground_truth_check.py`,
   see commit `f8ed52c`), Dukascopy is now the natural third source. Use
   it side-by-side with the existing two on the same windows.
4. **Pre-2012 history is out of reach with this lib.** If a deeper
   backtest is ever required, evaluate alternatives (the JS `dukascopy-node`
   client appears to handle the older naming) before promising it.
