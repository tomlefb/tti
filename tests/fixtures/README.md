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

## Catalog

(Populated as fixtures are added in Sprints 1+.)
