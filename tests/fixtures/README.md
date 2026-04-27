# Test fixtures

Hand-crafted or recorded OHLC data used by unit tests.

## Conventions

- Prefer `parquet` for multi-column OHLC data; CSV is acceptable for tiny
  hand-built scenarios.
- One file per scenario. Filename describes what the scenario tests, e.g.
  `xauusd_m5_clean_sweep_then_mss_long.parquet`.
- For each fixture, add a short comment block (in the test that loads it,
  or in this README under `Catalog` below) describing:
    - source (synthetic / recorded from MT5 on date X / etc.)
    - timeframe and symbol
    - the situation it represents
    - the expected detector output

## What goes here vs `calibration/`

- `tests/fixtures/`: small, deterministic, version-controlled. Used by
  unit tests with **known expected outputs**.
- `calibration/reference_charts/`: operator-marked real charts used to
  tune calibrated thresholds. NOT consumed by unit tests.

## `historical/` — real OHLC exports from MT5

The `historical/` subdirectory contains real OHLC parquet exports for the
four watched pairs (`XAUUSD`, `NDX100`, `EURUSD`, `GBPUSD`) across four
timeframes (`D1`, `H4`, `H1`, `M5`), generated on the Windows host by
`scripts/export_historical_ohlc.py`. They serve as Sprint 1+ development
fixtures so the Mac developer can iterate on detectors without an MT5
connection. Files are named `{SYMBOL}_{TF}.parquet`, timestamps are stored
as UTC `datetime64[ns]` in the `time` column, and columns mirror what
`mt5.copy_rates_from_pos` returns (`time`, `open`, `high`, `low`, `close`,
`tick_volume`, `real_volume`, `spread`). **Do not regenerate these files
casually**: a regeneration shifts the underlying data and silently invalidates
any test baselines computed against them. If a regeneration is genuinely
needed, do it as an explicit, reviewed commit and update affected tests in
the same change.

## Catalog

(Populated as fixtures are added in Sprints 1+.)
