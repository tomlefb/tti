# MT5 timestamp handling — code inspection

## The bug location (and a pre-existing TODO)

`scripts/export_historical_ohlc.py:254-262` — the historical
fixture exporter casts MT5's broker-time POSIX seconds to UTC without
the timezone conversion. The author flagged the issue at write-time
in the surrounding comment but the follow-up never landed:

```python
df = pd.DataFrame(rates)

# MT5 returns 'time' as Unix seconds in BROKER timezone. The seconds
# value is the broker-local wall-clock interpreted as if it were UTC,
# so this is technically a broker-time conversion masquerading as UTC.
# TODO: refactor to use src/mt5_client time-conversion helpers once
#       Sprint 1 implements them; until then, broker-server offset
#       must be normalized at consumption time, not here.
df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
```

The same code path exists in the max-history pagination branch at
`scripts/export_historical_ohlc.py:215-216`:

```python
df = pd.DataFrame(rates)
df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
```

with no comment.

The TODO says the consumer should normalise — but no consumer in the
calibration tree actually does. `calibration/run_ground_truth_check.py`,
`calibration/run_3way_alignment.py`, and the `calibration/baseline_*`
runners all read `tests/fixtures/historical/*.parquet` as if `time`
were UTC. The Sprint 1 normalisation step the TODO depends on never
materialised in the offline / fixture path.

## The conversion helper that already exists

`src/mt5_client/time_conversion.py` was written precisely for this
problem. From its module docstring:

> Many FX brokers run their MT5 server in Athens time (UTC+2 winter,
> UTC+3 summer) so that the daily candle aligns with the New York
> close. The MT5 Python API returns POSIX timestamps in **broker**
> time, not UTC. This module owns the offset detection and the
> conversion.

It exposes:

- `detect_broker_offset_hours(broker_now_seconds, ...)` —
  `src/mt5_client/time_conversion.py:39`. Returns the integer hour
  offset by comparing a live tick against `datetime.now(UTC)`.
- `broker_naive_seconds_to_utc(seconds, offset_hours)` —
  `src/mt5_client/time_conversion.py:96`. Converts a POSIX-seconds
  broker timestamp to an aware UTC `datetime` using the offset.
- `_athens_default_offset_hours(now_utc)` —
  `src/mt5_client/time_conversion.py:33`. Coarse Apr–Oct fallback
  if the runtime probe fails (the case for an offline export).

The helper is fully tested for runtime use; it has not been wired
into the historical exporter. That is the gap.

## Live-runtime path

`src/mt5_client/client.py` (the live client, used by the scheduler)
does call `detect_broker_offset_hours` at connect time and stores
the result. The live ingestion converts properly. The bug is
restricted to the `scripts/export_historical_ohlc.py` path, but
**all calibration runs depend on that path's output**, so every
backtest published to date that consumed `tests/fixtures/historical/`
has used broker-time-as-UTC timestamps.

## Why the test in the previous Phase missed it

Phase 1 of the strategy-research preparation included a
cross-correlation lag check (Question 1, Préalable 1) but with a
window of `±60 min` by 5-min steps. The true offset is `+120 min`
or `+180 min` depending on DST regime — both **outside** the prior
test's range. The peak at lag 0 inside that window was an artefact
of being far from the real maximum on a noisy correlation surface,
not a confirmation that the timestamps aligned.
