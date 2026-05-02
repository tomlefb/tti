# Regenerating MT5 fixtures after the timezone fix

The MT5 historical export script (`scripts/export_historical_ohlc.py`)
was carrying a timezone bug from Sprint 0: the broker's local
wallclock (Athens / Cyprus, EET in winter, EEST in summer) was being
labelled as UTC, shifting every bar by 2 hours (EET) or 3 hours
(EEST). The bug is fixed in commit `e871b6d` on the
`feat/strategy-research` branch.

This document is the operator runbook for regenerating
`tests/fixtures/historical/*.parquet` so consumer code
(calibration scripts, detection unit tests) reads true UTC
timestamps.

## When to run

After landing the fix branch and before any new calibration run that
depends on MT5 fixtures. Existing `tests/fixtures/historical/*` are
shifted; every report that consumed them is biased by 2-3 hours.

## Pre-requisites

1. **Windows host with MT5 installed and logged in.** The
   `MetaTrader5` Python package is Windows-only and the export
   talks to a running terminal.
2. **MT5 terminal pointed at the correct broker.** The broker
   timezone hard-coded in the helper is `Europe/Athens` (EET / EEST).
   FundedNext and most Cyprus-based prop firms match this convention.
   If the operator's broker reports a different timezone, see the
   "Different broker timezone" section below.
3. **Broker symbols available.** All seven canonical instruments
   should resolve via the alias table baked into the script
   (XAUUSD, NDX100, SPX500, EURUSD, GBPUSD, US30, BTCUSD).
4. **Cache primed.** In MT5 right-click on each chart and pick
   "All bars / Auto-scroll" then scroll to the leftmost edge. MT5
   only serves history that's already been cached locally — if the
   chart hasn't been scrolled back, the export will get a short
   window only.
5. **Dependencies up to date** on the Windows host:
   `pip install -r requirements.txt`. The fix imports `zoneinfo`
   (stdlib in Python 3.9+) and `pandas`. Both are in
   `requirements.txt`.

## Commands

The script reads `settings.WATCHED_PAIRS` by default. The 7-instrument
list can be passed explicitly to be safe:

```bash
# From the repo root, on the Windows host, in an activated venv.
python scripts/export_historical_ohlc.py --symbols XAUUSD NDX100 SPX500 EURUSD GBPUSD US30 BTCUSD
```

For a deeper history (e.g. 3 years rather than the default ~6 months),
use the paginated mode:

```bash
python scripts/export_historical_ohlc.py --symbols XAUUSD NDX100 SPX500 EURUSD GBPUSD US30 BTCUSD --max-history-days 1100
```

The script prints a per-symbol-per-timeframe progress line and ends
with a summary block listing the row counts and date ranges of every
output file. With the fix in place, the date ranges shown should be
true UTC (e.g., a 24/5 FX bar that opens at "broker 22:00 EEST"
will now appear as "19:00 UTC" rather than "22:00 UTC").

## Validation after the run

Two cheap checks the operator can run from Mac after pulling the
new fixtures:

1. **First-bar UTC alignment vs Dukascopy.** Run a small interactive
   spot-check (no need to re-run the full 3-way report yet):

   ```python
   import pandas as pd
   import sys
   sys.path.insert(0, '/path/to/tti')
   from src.data.dukascopy import DukascopyClient

   for instrument in ['XAUUSD', 'NDX100', 'SPX500']:
       mt5 = pd.read_parquet(f'tests/fixtures/historical/{instrument}_M5.parquet').set_index('time').sort_index()
       sample_ts = mt5.index[len(mt5) // 2]   # mid-fixture
       window = mt5.loc[sample_ts:sample_ts + pd.Timedelta(minutes=15)]
       duk = DukascopyClient().fetch_m5(
           instrument, sample_ts.to_pydatetime(),
           (sample_ts + pd.Timedelta(minutes=15)).to_pydatetime(),
       )
       print(f'{instrument} @ {sample_ts}')
       print('  MT5  close:', window['close'].iloc[0])
       print('  Duk  close:', duk['close'].iloc[0] if len(duk) else 'n/a')
   ```

   The MT5 and Dukascopy closes at the same UTC timestamp must match
   to within the bid/ask spread (a few pips for FX, a few index
   points for indices). If they differ by hundreds of points, the
   timezone is still wrong.

2. **Re-run the 3-way alignment.** Once the spot-check is green:

   ```bash
   python calibration/run_3way_alignment.py
   ```

   Expected after the fix: bar-body sign agreement Duk vs MT5 jumps
   from 0.50 to 0.92+ on every instrument, return correlation Duk
   vs MT5 jumps from ~0.95 to 0.99+. Generate a fresh
   `FINAL_3way_alignment_<TS>_verdict.md` and supersede the
   previous one (commit 379fc70 verdict was correct in direction
   but distorted in magnitude by the bug).

## Different broker timezone

If the operator's broker is **not** on Athens / Cyprus time (some
US-based brokers run on EST/EDT, some Asian shops on Hong Kong / GMT+8):

1. Probe the offset on the Windows host before exporting:

   ```python
   import MetaTrader5 as mt5
   from datetime import datetime, UTC
   mt5.initialize()
   tick = mt5.symbol_info_tick('XAUUSD')  # any heavily-traded symbol
   broker_now = datetime.fromtimestamp(tick.time, tz=UTC).replace(tzinfo=None)
   utc_now = datetime.utcnow()
   print('apparent offset:', (broker_now - utc_now).total_seconds() / 3600, 'hours')
   ```

2. If the apparent offset is ±3h or ±2h with seasonal flip → Athens.
   No change required.
3. If a different IANA zone applies (`America/New_York`, `Asia/Hong_Kong`, ...),
   open `scripts/export_historical_ohlc.py` and change the
   `_BROKER_TZ = zoneinfo.ZoneInfo("Europe/Athens")` constant. Add a
   note documenting which broker the new value targets.

## Regeneration is a stop-the-line event

Per `tests/fixtures/README.md`, regenerating these fixtures shifts
every test baseline that pinned absolute timestamps. After this
run:

- Re-run the full pytest suite. Tests that pinned MT5 candles by
  exact timestamp will fail with shifts of 2-3h. Update those
  pinned values to the new (correct) UTC values.
- Re-run any calibration script whose published report depended on
  MT5 timestamps. The companion audit
  (`calibration/runs/timezone_audit_2026-05-02T16-04-57Z/collateral_audit.md`)
  enumerates the candidates.
- Commit the regenerated fixtures **as a single deliberate change**
  on `feat/strategy-research` with a clear "regen after timezone
  fix" message, so reviewers can see the size of the diff and
  confirm it's expected.
