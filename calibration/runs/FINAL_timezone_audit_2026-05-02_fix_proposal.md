# Fix proposal — MT5 fixture timestamp conversion

**Status: investigation only, no implementation. Operator validates first.**

## What is broken

`tests/fixtures/historical/*.parquet` is the canonical MT5 OHLCV
fixture used by every offline calibration script. Its `time` column
contains the broker's local wallclock (Athens / Cyprus time:
EET = UTC+2 winter, EEST = UTC+3 summer) but is stored with a
`datetime64[ns, UTC]` dtype, so consumers treat it as UTC and end up
with bars shifted 2–3 hours from where they actually were in real
time.

Visible symptoms before this audit:

- 3-way alignment report (commit 379fc70): bar-body sign agreement
  between MT5 and Dukascopy/Databento at chance level (~0.50) for
  three instruments, while Dukascopy vs Databento sat at 0.65 / 0.93
  / 0.91. The 0.50 reading was the bug, not a real micro-structure
  difference.
- The "B unanimous" verdict in that same report (Duk ≈ DBN, MT5
  distinct) is correct in *direction* — Duk and DBN do share
  microstructure — but its *magnitude* against MT5 is inflated by
  the misalignment. Once the fix is applied, MT5 will look much
  closer to both sources too.

Empirically (`lag_scan.md`), shifting MT5 by `−180 min` (summer) or
`−120 min` (winter) raises sign-agreement from 0.50 to 0.92–0.98
and return correlation from ~0 to 0.99+ on every (instrument,
month) cell tested.

## Root cause

`scripts/export_historical_ohlc.py:262` (and the parallel call at
`:216`) writes `pd.to_datetime(rates['time'], unit='s', utc=True)`
without converting from broker time first.

`src/mt5_client/time_conversion.py` was built for this exact problem
but is only wired into the live runtime; the offline export bypasses
it. The author flagged this in a TODO comment at the bug site;
follow-up never landed.

## Proposed fix

Two parts. Part A is the source of truth (export script must produce
UTC). Part B mitigates the existing fixtures so consumers don't have
to wait for a re-export run to be unblocked.

### Part A — fix the exporter (1 file change)

In `scripts/export_historical_ohlc.py`, replace the broken cast in
**both** branches (`_fetch_and_save_max_history` and
`_fetch_and_save`) with a conversion through the existing helper.

The helper takes an integer hour offset; the exporter runs against
the live MT5 terminal at export time, so the runtime probe is
available:

```python
from src.mt5_client.time_conversion import (
    broker_naive_seconds_to_utc,
    detect_broker_offset_hours,
)
```

At the start of `main()` (after `mt5.initialize`) detect the offset
once and reuse it across all symbol/timeframe pairs:

```python
# Detect the broker timezone offset from a fresh tick. This must
# come from a heavily-traded symbol so the tick is recent.
probe_symbol = next(
    (s for s in resolution.values() if s is not None), None
)
broker_now = mt5.symbol_info_tick(probe_symbol).time if probe_symbol else None
broker_offset_hours = detect_broker_offset_hours(broker_now)
```

Then in both `_fetch_and_save*` functions, replace:

```python
df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
```

with:

```python
df["time"] = pd.Series(
    [broker_naive_seconds_to_utc(s, broker_offset_hours) for s in df["time"]],
    dtype="datetime64[ns, UTC]",
)
```

Note that **a single integer offset is wrong across DST boundaries**
inside a multi-month export. For M5 paginated fetches that cover
years, the conversion must be DST-aware. The cleanest version is:

```python
import zoneinfo
broker_tz = zoneinfo.ZoneInfo("Europe/Athens")  # or Europe/Nicosia

def _broker_seconds_to_utc(s):
    naive_wallclock = datetime.utcfromtimestamp(float(s))
    return naive_wallclock.replace(tzinfo=broker_tz).astimezone(UTC)

df["time"] = pd.to_datetime([_broker_seconds_to_utc(s) for s in df["time"]])
```

`zoneinfo` follows the IANA DST rules so a 5-year export converts
correctly across every spring-forward and fall-back. Confirm the
broker's tz with the operator before settling on `Europe/Athens` vs
`Europe/Nicosia` (FundedNext historically uses Athens; some
prop-firms use Cyprus).

### Part B — re-export the fixtures (one operator action)

After Part A lands, `scripts/export_historical_ohlc.py` must be
re-run on the Windows host to overwrite `tests/fixtures/historical/*`.

This is a deliberate, reviewed regeneration as warned by
`tests/fixtures/README.md`. Any test that pinned absolute timestamps
on the old fixtures will fail; those tests are wrong by 2–3h and need
their pinned values updated. Test baselines based on **bar relative
positions** (e.g. "the third sweep of the killzone") are unaffected.

### Optional Part C — short-term workaround for already-published runs

If a re-export blocks Sprint 7+ work, the calibration scripts can
apply the conversion at read time as a temporary measure. The same
two-line `_load_mt5` helper used in `run_3way_alignment.py:62-68`
becomes:

```python
def _load_mt5(instrument: str) -> pd.DataFrame:
    path = _MT5_DIR / f"{instrument}_M5.parquet"
    df = pd.read_parquet(path)
    # Treat the labelled-UTC value as Athens wallclock and convert.
    df["time"] = (
        df["time"].dt.tz_localize(None)
        .dt.tz_localize("Europe/Athens", ambiguous="NaT")
        .dt.tz_convert("UTC")
    )
    df = df.dropna(subset=["time"]).set_index("time").sort_index()
    return df[["open", "high", "low", "close"]].astype("float64")
```

`ambiguous="NaT"` discards the duplicated wallclock during the
fall-back hour rather than picking arbitrarily. The `NaT` rows
should be a handful per year per instrument and can be dropped.

This workaround is acceptable for calibration-only runs but not for
the live runtime — which already does the conversion correctly via
the `mt5_client` helpers.

## Implications for runs that already shipped

- 3-way alignment report (commit 379fc70): the **direction** of the
  verdict (Duk ≈ DBN structurally) survives; the **magnitude** of
  MT5's "distance" is overstated. After the fix, MT5 will correlate
  with both Duk and DBN at 0.99+ on returns and 0.92+ on sign,
  consistent with the empirical fix-validation already shown.
- Any 2-source robustness criterion that ranked MT5 against the
  others (or treated 0.50 sign agreement as a real microstructure
  signal) needs to be re-run.
- The 1162 Dukascopy parquets in `tests/fixtures/dukascopy/` and the
  Databento `historical_extended/` are unaffected — both already
  store true UTC.

## What to do, in order

1. Operator confirms the broker tz (Athens vs Cyprus) — most likely
   Athens for FundedNext / Cyprus-based brokers.
2. Land Part A (exporter fix) on `feat/strategy-research`. Add a
   small unit test that round-trips a known broker-naive timestamp
   through `_broker_seconds_to_utc` and asserts the UTC reconstruction.
3. Re-run the exporter on Windows to replace the fixtures.
4. Re-run `calibration/run_3way_alignment.py` to confirm sign
   agreement and correlation jump as expected, and update the verdict
   commentary in any downstream document that referenced the inflated
   MT5-vs-others gap.
5. Consider a `feat/strategy-research`-scoped guard test that loads
   `tests/fixtures/historical/XAUUSD_M5.parquet` and asserts that the
   first bar's hour aligns with a known UTC reference (e.g. via a
   Dukascopy intersection on the same minute), so the fixture can't
   silently regress to broker time again.
