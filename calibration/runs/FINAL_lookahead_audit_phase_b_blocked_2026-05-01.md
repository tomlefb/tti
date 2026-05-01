# Phase B — blocked: fourth leak in `detect_mss`

**Date**: 2026-05-01
**Branch**: `feat/strategy-research`
**Status**: simulator + acceptance test built; acceptance test fails;
no Phase-B commits made beyond this findings doc.

## TL;DR

Building the Phase B tick simulator surfaced a fourth leak that the
Phase A audit did not catch: `detect_mss` iterates M5 candles forward
from the sweep-return time without bounding by `now_utc`. With a
production scheduler at tick `T`, the detector can therefore commit a
setup whose `mss_confirm_candle_time_utc` lies in the future — up to
120 minutes past `T` (the `_MSS_LOOKFORWARD_MINUTES` cap). The
simulator's identity-locked, first-emission semantic then anchors the
setup at the wrong (early) tick, propagating the leak into every
backtest that uses the simulator.

The audit (`audit_lookahead.py`) did not surface this because it only
verifies at one specific tick: `next_5min_tick_after(mss_confirm)`.
At that tick, the MSS-detection iteration's "first qualifying candle"
is the MSS itself, so the future-data access is invisible. The
simulator iterates earlier ticks too, where the leak becomes visible.

## Concrete reproduction

`NDX100 london short` on `2025-10-22`. London killzone: 07:00 → 10:00 UTC.
Tracing the simulator tick by tick (using the committed test fixture
under `tests/fixtures/historical/NDX100_*.parquet`):

```
tick=09:35  emit: mss=10:00 sweep=09:25 level=25149.36
tick=09:40  emit: mss=10:00 sweep=09:25 level=25149.36
tick=09:45  emit: mss=10:00 sweep=09:25 level=25149.36
tick=09:50  emit: mss=10:00 sweep=09:25 level=25149.36
tick=09:55  emit: mss=10:00 sweep=09:25 level=25149.36
tick=10:00  emit: mss=10:00 sweep=09:25 level=25149.36
```

At `tick=09:35`, the M5 candle whose open is `10:00` (and whose close
is `10:05`) does not yet exist in the production world — it's 25
minutes in the future. Yet `detect_mss` reads it because it has access
to the full `df_m5` frame the simulator passed in.

The simulator's first-emission lock then anchors the setup at
`tick=09:35`. The audit's truncated re-run at `tick=10:05` (the
correct production-tick for an MSS at `10:00`) cannot reproduce that
setup because the dedupe pool at `tick=10:05` picks a different sweep
(`09:50`, deeper, beating `09:25`'s cluster member at the same level).
The acceptance test
`tests/backtest/test_tick_simulator_matches_audit.py` therefore fails:

```
FAILED test_simulator_setups_match_audit_truncation
- AssertionError: simulator emitted mss=10:00, sweep=09:25 but the
  truncated re-run found no matching setup. re-run identities:
  [(..., mss=08:35, sweep=08:15, level=25149.36)]
```

## Why this is the only remaining forward-iteration leak

Tracing every detector path that walks candles past `now_utc`:

| Component                       | Forward bound? | Status         |
|---------------------------------|----------------|----------------|
| `find_raw_swings`               | ✅ Phase A     | Bounded by `now_utc` (commit `9751f28`). |
| `detect_fvgs_in_window`         | ✅ Phase A     | c3 close ≤ `now_utc` (commit `d3caecc`). |
| `detect_sweeps`                 | ✅ Phase A     | return-candle close ≤ `now_utc` (commit `53cb7c6`). |
| `detect_order_block`            | n/a            | Looks **backward** from MSS displacement; no forward dep. |
| `compute_daily_bias`            | n/a            | Orchestrator pre-slices df to `< kz_start_utc`; bounded by slice. |
| `mark_asian_range`              | n/a            | Closed window ends ≤ kz_start; no forward dep. |
| `mark_pdh_pdl`                  | n/a            | Yesterday's D1; no forward dep. |
| **`detect_mss`**                | ❌             | **Iterates `(sweep_return, sweep_return+120min]` against the full `df_m5`. No `now_utc` filter.** |

`mss.detect_mss` is the only remaining forward-iteration that does not
respect `now_utc`. Once it does, the simulator's setups will match
the audit's truncated re-run bit-identically and the Phase B
acceptance test will pass.

## Fix sketch (NOT implemented)

Same shape as the FVG / sweep fixes:

```python
def detect_mss(
    df_m5, sweep, *,
    swing_lookback_m5, min_swing_amplitude_atr_mult,
    displacement_multiplier, displacement_lookback,
    max_lookforward_minutes=120, atr_period=14,
    now_utc: datetime | None = None,         # NEW
) -> MSS | None:
    ...
    # Existing iteration:
    for i in range(n):
        candle_time = times_py[i]
        if candle_time <= search_start:
            continue
        if candle_time > search_end:
            break
        # NEW: candle must have closed by now_utc.
        if now_utc is not None and m5_timeframe is not None:
            if candle_time + m5_timeframe > now_utc:
                break
        ...
```

Then `setup._try_build_setup` forwards `now_utc=now_utc` to the
`detect_mss` call (currently passes nothing). The orchestrator
already has `now_utc` in scope from the audit-fix work; no
signature change at the build_setup_candidates level.

`tests/backtest/test_tick_simulator_matches_audit.py` was added in
this session and is currently failing on the suspect setup above; it
will pass once `detect_mss` is bounded.

The bias / Asian / PDH-PDL exemptions in `bias.py` and the existing
swing-confirmation lock in `mss.py` continue to apply — neither needs
forwarding.

## Working-tree state

Built and ready to commit pending the user's call:

- `src/backtest/__init__.py`
- `src/backtest/tick_simulator.py` (uncommitted)
- `tests/backtest/__init__.py`
- `tests/backtest/test_tick_simulator_matches_audit.py` (uncommitted; fails currently)

Committed in this session:

- `calibration/runs/FINAL_lookahead_audit_phase_b_blocked_2026-05-01.md` (this file)

Steps not started:

- Step 2 of the plan (refactor `run_extended_10y_backtest.py` with
  `--mode tick|legacy`) — pointless until the simulator is leak-free.
- Step 4 (legacy vs tick diff on the 10y fixture) — same.

## Recommended next step

Authorise the `detect_mss` fix (mirror of FVG / sweep, ~15 lines plus
docstring + comment). After that:

1. Re-run the simulator on `NDX100 2025-10-22`, confirm
   `tests/backtest/test_tick_simulator_matches_audit.py` passes.
2. Re-run the look-ahead audit (must remain 30/30 / 53/53 — the audit
   rule is a special case of the simulator's invariant).
3. Resume Phase B Steps 2-4 (CLI flag, diff report) on a leak-free
   simulator.

If the operator prefers to STOP completely and re-baseline first, the
findings here are sufficient to make that decision; the working tree
state is recoverable from git status.

## Why the audit didn't catch this

For completeness — the audit's structure is:

1. Discover candidate `(target_date, T=mss_confirm)` pairs via a
   legacy scan.
2. For each, run `build_setup_candidates(now_utc = next_5min_tick_after(T))`
   on the wide slice; keep setups whose own `mss_confirm == T`.
3. Verify on a `time <= T` truncated slice with the same `now_utc`.

In step 2, the call site passes `now_utc = T + 5min`. `detect_mss`
iterates from `sweep_return` forward and returns the **first**
qualifying candle. By construction, that first qualifying candle is
the MSS at `T` — there cannot be an earlier qualifying candle, else
the original setup wouldn't have had `mss_confirm = T`. So
`detect_mss` returns the right answer at this tick even though it
*could* read past `T+5min` — it never has reason to.

The simulator iterates **earlier ticks** too. At a tick before
`T+5min`, `detect_mss`'s "first qualifying candle" can be a future
candle (relative to that tick) that doesn't exist yet in production.
That's the leak the audit missed.

A natural follow-up to the fix: extend the audit harness to also
verify at intermediate ticks (not just `next_5min_tick_after(T)`),
which would catch this class of leak in any future regression. Worth
a small line item in Phase C.
