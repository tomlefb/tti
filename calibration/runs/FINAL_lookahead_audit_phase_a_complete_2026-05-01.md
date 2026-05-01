# Phase A — complete: detector is leak-free

**Date**: 2026-05-01
**Branch**: `feat/strategy-research`
**Audit script**: `calibration/audit_lookahead.py`
**Detector fix**: now_utc parameter on `build_setup_candidates`,
`mark_swing_levels`, `find_swings`, `find_raw_swings`,
`detect_fvgs_in_window`, `detect_sweeps`. Legacy path
(`now_utc=None`) is unchanged. pytest 330/330 still passes.

## Status

All three look-ahead leaks identified across the audit chain are
closed. The audit reaches:

- **30/30 clean** on the original 30-setup sample (seed=42,
  `--n-samples 30 --n-dates 400 --start 2016-01-03 --end 2026-04-29`).
- **53/53 clean** on the full post-fix pool (seed=42,
  `--n-samples 100` → harness samples min(100, 53) = 53 setups).

The pre-fix legacy scan produced 57 setups; the post-fix
production-truthful pool contains 53. The 4-setup gap is the count
of phantom setups that the legacy leaks created — they don't exist
in real-time semantic, and the now_utc bound correctly prevents the
detector from emitting them.

## Leaks closed

| # | Site                                  | Symptom                              | Closed in commit  |
|---|---------------------------------------|--------------------------------------|-------------------|
| 1 | FVG forward window (`setup.py:486-489`) | Tighter FVG picked using post-MSS data; quality / RR inflated | `d3caecc` |
| 2 | Sweep dedup pool (`setup.py:337-346`) | Different sweep picked via post-T candles in dedup cluster | `53cb7c6` |
| 3 | Swing confirmation (`swings.find_raw_swings`) | Trailing-N pivots shift as more pivots get confirmed by future data, changing the opposing-liquidity target | `9751f28` |

## What `now_utc` means now

`now_utc` is the production scheduler tick — the wall-clock instant
at which the detector is invoked. A candle at open time `t` on a
timeframe of `Δ` is **observable at `now_utc`** iff `t + Δ <= now_utc`
(its close has happened). Every forward-looking sub-search inside
`build_setup_candidates` enforces this rule when `now_utc` is set:

- `find_raw_swings` drops a pivot at index `i` if the candle at
  index `i + lookback` has not yet closed.
- `detect_fvgs_in_window` drops an FVG whose c3 candle has not yet
  closed.
- `detect_sweeps` drops a sweep whose return candle has not yet
  closed (and the orchestrator caps the killzone window at
  `min(kz_end, now_utc)` so sweep candle iteration is bounded too).

Two `find_swings` call sites deliberately do NOT take `now_utc`:

- `bias.compute_daily_bias`: the orchestrator pre-slices df_h4 /
  df_h1 to `time < kz_start_utc`. Since `kz_start_utc <= now_utc`,
  the slice already enforces the swing-confirmation bound.
- `mss.detect_mss`: the MSS iteration caps each candidate's pivot
  index at `i - swing_confirmation_offset`, so any swing data past
  the candidate MSS candle is already ignored.

Both are documented inline in their respective modules.

## Audit harness invariant

The audit script is now a **consistency test** rather than a
side-by-side comparison of leaky vs leak-free runs. Concretely, for
each sampled setup at MSS-confirm time T:

- **Phase A truthful run**: full df + `now_utc = next_5min_tick_after(T)`.
- **Phase B verification**: same wide df **truncated to `time <= T`**
  + same `now_utc`.

If the now_utc bound is implemented correctly, both runs see the
same observable candles and produce bit-identical setups. Any
divergence means there's another forward-leak path the now_utc
bound failed to catch — i.e., the audit alarms iff a fourth (or
beyond) leak emerges.

The Phase A two-pass discovery is unchanged from the last session:
the legacy scan finds candidate (target_date, T) pairs; for each T
the truthful run with `now_utc = next_5min_tick_after(T)` is
performed and only setups with `mss_confirm == T` are added to the
pool.

## Production deployment notes

`now_utc=None` is the legacy unconstrained mode. To use the leak-
free path in production, the scheduler must call
`build_setup_candidates(..., now_utc=tick)` where `tick` is the
APScheduler firing time. The existing scheduler integration in
`src/orchestrator/` does NOT pass `now_utc` yet — production calls
remain on the legacy path.

This is a **deliberate non-change** for Phase A: the detector now
supports the leak-free path, but production semantics are unchanged
until the orchestrator is updated. That's the natural seam between
Phase A (detector) and Phase B (backtest harness + production
wiring).

The CLAUDE.md rule 11 ("Lifecycle parity with backtest") will need
the orchestrator to pass `now_utc` once Phase B's tick-by-tick
harness is in place; production will follow.

## Recommended next step

Phase A is complete. Phase B per the original session plan is the
**tick-by-tick backtest harness**:

- Iterate the scheduler model (every 5 min within both killzones)
  and call `build_setup_candidates(..., now_utc=tick)` at each
  tick.
- Accumulate any new setups (those not seen at the previous tick).
- The result is a backtest that matches the production scheduler
  exactly; outcome simulation can then proceed on the leak-free
  setups.

Phase B should be authorised before re-running the Sprint 6.5 / 6.6
WATCHED_PAIRS validation; the existing `extended_10y_backtest`
script is on the legacy path and its results carry the leak.
