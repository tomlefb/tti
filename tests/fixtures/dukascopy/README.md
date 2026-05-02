# Dukascopy local cache

On-disk cache for OHLCV data downloaded via
:class:`src.data.dukascopy.DukascopyClient`.

The cache is consulted automatically when ``use_cache=True`` (the default).
It is also a regular `tests/fixtures/` subdirectory, so anything the test
suite needs to pin can sit alongside the cache without ceremony.

## Layout

One parquet file per `(instrument, year-month, side)`:

```
tests/fixtures/dukascopy/
├── XAUUSD/
│   ├── 2024-12_bid.parquet
│   ├── 2025-01_bid.parquet
│   └── ...
├── NDX100/
├── SPX500/
├── EURUSD/
├── GBPUSD/
├── US30/
└── BTCUSD/
```

Schema of each file matches the `DataFrame` returned by `fetch_m5`:

- Index: tz-aware UTC `DatetimeIndex` named `timestamp` (M5 cadence,
  weekends/holidays naturally absent).
- Columns: `open`, `high`, `low`, `close`, `volume` (lowercase, `float64`).

A `_bid` / `_ask` suffix lets both order sides coexist for the same
month if both are ever requested.

## How to populate

The cache is filled lazily by `DukascopyClient.fetch_m5` — first call
for a given month hits the network and writes the parquet, subsequent
calls read from disk.

A bulk download script for the seven canonical instruments across their
full available depth is planned as Phase 2 of the Dukascopy work — see
the `feat/strategy-research` branch history.

## Regeneration

The cache is reproducible: deleting any subdirectory and re-running the
relevant calibration / test will refetch the missing months. To start
clean:

```bash
rm -rf tests/fixtures/dukascopy/<INSTRUMENT>/   # one instrument
# or
rm -rf tests/fixtures/dukascopy/                 # everything
```

Be deliberate about regenerating: the underlying Dukascopy data does
revise occasionally (mostly outside of trading hours), and any test
baselines that pin specific bar values would silently shift. Treat a
full regeneration as a reviewable commit.

## Available depth

From the coverage probe in
`calibration/dukascopy_coverage_check_2026-05-02T14-28-29Z.md` — first
M5 bar served by `dukascopy_python==4.0.1`:

| Instrument | First bar served       | Effective depth (as of 2026-05) |
|------------|------------------------|----------------------------------|
| XAUUSD     | 2008-06-14 (or earlier)| ~18 yr                           |
| EURUSD     | 2012-01-11             | ~14 yr                           |
| GBPUSD     | 2012-06                | ~14 yr                           |
| NDX100     | 2012-06                | ~14 yr                           |
| SPX500     | 2012-06                | ~14 yr                           |
| US30       | 2012-06                | ~14 yr                           |
| BTCUSD     | 2017-06                | ~9 yr                            |

Pre-cutoff windows return an empty DataFrame; they are not an error.

## Source

- Data: <https://www.dukascopy.com/swiss/english/marketwatch/historical/>
- Library: [`dukascopy_python`](https://pypi.org/project/dukascopy-python/)
  v4.0.1, MIT-licensed PyPI package wrapping Dukascopy's public
  historical-tick endpoints.
- Wrapper: `src/data/dukascopy/client.py` (canonical-name mapping +
  cache layer + canonical schema).
