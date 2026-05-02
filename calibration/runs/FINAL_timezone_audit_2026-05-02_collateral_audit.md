# Collateral audit — MT5 timezone bug

This is the read-only follow-up to the audit. The fix to
`scripts/export_historical_ohlc.py` lands the canonical correction;
this document enumerates everything else that touched MT5
timestamps and classifies whether each site needs a code change,
just a fixture refresh, or nothing.

## Code sites

### Fixed in this branch

| File:line | Status | Note |
|---|---|---|
| `scripts/export_historical_ohlc.py:216` | **fixed** (commit e871b6d) | `_fetch_and_save_max_history` paginated branch. |
| `scripts/export_historical_ohlc.py:262` | **fixed** (commit e871b6d) | `_fetch_and_save` single-call branch. |

### Has the same `unit="s", utc=True` pattern but is not a fixture writer

| File:line | Status | Note |
|---|---|---|
| `scripts/test_mt5.py:111` | **flag, low priority** | A smoke-test script that prints the last `n` MT5 candles to stdout. Same broken cast on `mt5.copy_rates_*` output. Not a fixture writer, so no on-disk artefact carries the bug, but the printed dataframe shows broker time mislabelled UTC. Worth fixing for consistency; out of scope here because no calibration depends on it. |

### Live-runtime path — already correct

| File:line | Status | Note |
|---|---|---|
| `src/mt5_client/client.py:285-286` | **OK** | Calls `broker_naive_seconds_to_utc(s, offset)` per-row before the `pd.to_datetime(..., utc=True)` normalisation. The `utc=True` here is a dtype no-op on already-aware values. |
| `src/mt5_client/time_conversion.py` | **OK** | Helper module is correct. `detect_broker_offset_hours` is used by the live client at connect time. |

A latent edge case: `broker_naive_seconds_to_utc` takes a single integer
offset, so if a live session were to *cross* a DST transition mid-run
the conversion would be wrong by 1h on the post-transition side. The
realistic scheduler cycle is a few minutes per killzone and the
operator restarts daily, so the window of exposure is narrow, but it's
worth a follow-up to make the live client DST-aware too.

### Operate on already-stored `df["time"]` — not a bug class

These call `pd.to_datetime(df["time"], utc=True)` (without `unit="s"`)
on a column that's already a tz-aware datetime. The call is idempotent
on correct data and a no-op on incorrect data; the values flow through
unchanged. None of these introduces or hides the bug — they are
**downstream propagators** that will read true UTC once the fixtures
are regenerated.

| File:line | Class |
|---|---|
| `src/notification/chart_renderer.py:208` | downstream |
| `src/detection/swings.py:149` | downstream |
| `src/detection/fvg.py:113` | downstream |
| `src/detection/liquidity.py:183, 227, 257` | downstream |
| `src/detection/mss.py:132` | downstream |
| `src/detection/order_block.py:75` | downstream |
| `src/detection/sweep.py:118` | downstream |
| `src/detection/setup.py:430` | downstream |
| `scripts/print_setups_for_day.py:84` | downstream |
| `scripts/test_scheduler_dry_run.py:100` | downstream |
| `calibration/run_extended_backtest.py:81` | downstream |
| `calibration/run_final_portfolio_validation.py:50` | downstream |
| `scripts/process_databento_extended.py:75, 205` | unrelated (Databento ts already UTC) |

## Calibration scripts that consume `tests/fixtures/historical/`

After Part B (operator re-runs the exporter), all of these will be
served correct UTC and should be re-run to refresh their published
reports:

| Script | Consumes MT5 fixture? | Re-run priority |
|---|---|---|
| `calibration/run_3way_alignment.py` | yes — direct read of `tests/fixtures/historical/{INST}_M5.parquet` | **high** (verdict in commit 379fc70 needs refresh) |
| `calibration/run_ground_truth_check.py` | yes — single-timestamp 3-source spot check | medium |
| `calibration/run_swing_calibration.py` | yes | medium (swing thresholds calibrated against shifted bars) |
| `calibration/build_panama_fixtures.py` | yes — pulls MT5 spot reference for Panama anchor calibration | medium |
| `calibration/run_mt5_vs_databento_tick.py` | yes — runs the leak-free tick simulator on both sources | **high** (Préalable 1 verdict, see below) |
| `calibration/run_mt5_vs_databento_deep_diagnosis.py` | yes | high |
| `calibration/run_mt5_vs_databento_preflight.py` | yes — explicit MT5-vs-DBN timestamp alignment check | **critical** (its conclusion of "no bug" was the false positive that let the bug survive) |
| `calibration/run_delta_distribution.py` | likely — needs an MT5 vs Databento spread comparison | medium |
| `calibration/baseline_tjr_databento.py` | only via `TTI_FIXTURE_DIR` env override, defaults to Databento | low (only if past run set TTI_FIXTURE_DIR=tests/fixtures/historical) |

## Past reports affected

### Critical to re-run after Part B

- **`FINAL_3way_alignment_2026-05-02T15-55-27Z_*`** (commit 379fc70):
  the verdict B (Duk ≈ DBN) is correct in direction but the magnitude
  of MT5's distance is inflated. Body sign agreement is currently
  reported at 0.50 chance level vs an expected 0.92+ after fix
  (already validated empirically in `lag_scan.md`).
- **`FINAL_mt5_vs_databento_preflight_2026-05-02T13-16-05Z`**: this was
  the report that *should* have caught the bug — its "10 random NDX100
  timestamps from the common window" check was scoped to ±60 min lag
  and concluded "no bug". The true offset is ±120-180 min, outside the
  window. The report's conclusion is wrong and should be marked as
  superseded.
- **`FINAL_mt5_vs_databento_tick_2026-05-02T11-43-04Z`**: ran the
  TJR tick simulator on both MT5 and Databento fixtures and produced
  setup-level diff numbers. Every MT5 setup was generated on data
  shifted 2-3h, so the resulting setups themselves are at the wrong
  UTC times. The structural finding (large divergence between the two
  sources) is partially explained by the timezone bug; need a fresh
  run on corrected fixtures to see what divergence remains.

### Likely affected, lower priority

- **Sprint 6.5 / 6.6 portfolio validation**: per `CLAUDE.md`,
  ETHUSD was dropped at Sprint 6.5 due to "A-grade filter inversion
  on crypto microstructure". If that grading was computed against
  MT5 fixtures (worth confirming in the report under
  `calibration/runs/*sprint_6_6_portfolio_validation.md`, untracked
  here), the conclusion may be sensitive to the timezone shift on
  the killzone boundaries. Re-running after Part B will tell.
- **`FINAL_swing_calibration.md`**: thresholds calibrated against
  shifted-by-2-3h candles. The relative shape of swings is invariant
  to a constant time shift, so the calibrated *values* are likely
  unchanged, but the bin assignments to "London" / "NY" killzones
  could be slightly off. Re-run to confirm.

### Not affected

- **All `FINAL_lookahead_audit_*`** reports — they test detector
  determinism on fixed inputs. Relative bar order is invariant under
  a constant time shift, so the leak conclusions hold regardless of
  the fixture's labelling.
- **`FINAL_legacy_vs_tick_diff_2026-05-01T21-43-38Z`** — runs on
  Databento `historical_extended` (10-year window, no MT5 input).
- **`FINAL_dukascopy_bulk_*`** — pure Dukascopy.
- **`FINAL_sprint3_calibration.md`** — pre-MT5-fixture work.

## Recommended order of operations after this fix lands

1. Operator pulls `feat/strategy-research`, lands the fix branch on
   the Windows host, runs `python scripts/export_historical_ohlc.py
   --symbols XAUUSD NDX100 SPX500 EURUSD GBPUSD US30 BTCUSD` per the
   regen instructions.
2. Spot-check one bar of each instrument against Dukascopy on Mac to
   confirm the fix is applied (see
   `calibration/broker_calibration/REGEN_FIXTURES_INSTRUCTIONS.md`).
3. Commit the regenerated fixtures as a single deliberate diff.
4. Re-run **`run_3way_alignment.py`** first — it's the most direct
   confirmation that the bug is gone (sign agreement 0.50 → 0.92+).
   Replace the previous FINAL with the new one.
5. Re-run **`run_mt5_vs_databento_preflight.py`** with a wider lag
   window (`±360 min` minimum) and supersede the old "no bug"
   verdict.
6. Re-run **`run_mt5_vs_databento_tick.py`** and the deep-diagnosis
   to refresh the setup-level divergence numbers.
7. Re-run **`run_swing_calibration.py`** to confirm thresholds are
   stable (likely no functional change).
8. Open a follow-up issue for the live-client DST edge case
   (single-offset captured at connect, used across a session that
   could span a DST transition).
