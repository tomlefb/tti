# Phase A — partial fix and third leak findings

**Date**: 2026-05-01
**Branch**: `feat/strategy-research`
**Audit script**: `calibration/audit_lookahead.py` (revised)
**Detector fix**: FVG forward-window + sweep dedup pool now bounded by
optional `now_utc` parameter on `build_setup_candidates`. Legacy path
(`now_utc=None`) is unchanged; pytest 330/330 still passes.

## Status

The look-ahead audit at `calibration/runs/FINAL_lookahead_audit_2026-05-01.md`
identified two leak sites: the FVG forward window
(`setup.py:486-489`) and the sweep dedup pool
(`setup.py:337-346`). Phase A of the user's plan instructed me to fix
**only those two sites** and re-run the audit, expecting 30/30 clean.
"If at any point the fix touches more than the FVG and sweep-dedup
sites, STOP — that means there's a third leak we didn't identify.
Report it before continuing."

The two fixes are implemented (see Section 1 below). After re-running,
the audit still shows divergence on a fraction of sampled setups — but
in a *different* failure mode than before. The remaining divergence is
attributable to a **third leak** in the swing-level marking
(`liquidity.mark_swing_levels` via `swings.find_raw_swings`). I am
stopping per the constraint and reporting.

## 1. What was fixed (committed in this branch)

### 1.1 FVG forward window — `setup.py:486-491`

```python
fvg_window_start = mss.displacement_candle_time_utc
fvg_window_end = mss.mss_confirm_candle_time_utc + pd.Timedelta(
    minutes=_FVG_LOOKFORWARD_FROM_MSS_MINUTES
)
if now_utc is not None:
    fvg_window_end = min(fvg_window_end, pd.Timestamp(now_utc))
```

### 1.2 FVG c3-closure rule — `fvg.detect_fvgs_in_window`

New `now_utc: datetime | None` keyword. When set, an FVG whose c3
candle has not yet closed by `now_utc` is dropped. This is the
**stricter** of the two options the plan offered ("c3 closed" vs "c2
formed"); see the docstring in `src/detection/fvg.py`.

### 1.3 Sweep dedup pool — `setup.py:354-365`

```python
sweep_kz_end = min(kz_end_utc, now_utc) if now_utc is not None else kz_end_utc
sweeps = detect_sweeps(
    df_m5, levels,
    killzone_window_utc=(kz_start_utc, sweep_kz_end),
    ...
    now_utc=now_utc,
)
```

### 1.4 Sweep return-candle closure — `sweep.detect_sweeps`

New `now_utc: datetime | None` keyword. Sweeps whose return candle
has not yet closed by `now_utc` are dropped before they reach the
dedup pool.

### 1.5 `build_setup_candidates(now_utc=None)` plumbing

Optional parameter forwarded to all forward-looking subcalls. Legacy
calls (no `now_utc` argument) are unchanged.

### 1.6 Audit harness rework — `calibration/audit_lookahead.py`

- Added `_next_5min_tick_after(t)` modeling the production
  APScheduler 5-min cron.
- Phase A is now a two-pass: a legacy scan discovers candidate
  `mss_confirm` times T; for each T we re-run the detector with
  `now_utc = next_5min_tick_after(T)` on the same wide slice and
  keep only setups whose own `mss_confirm` equals T. This couples
  the discovery `now_utc` to the verification `now_utc` Phase B will
  use.
- Phase B uses `df.loc[df["time"] <= T]` on the **same wide slice**
  that Phase A used, so the ATR Wilder seed is identical between
  phases (otherwise `size_atr_ratio` and swing-amplitude tests can
  disagree on candles that are otherwise identical).
- Phase B passes `now_utc = next_5min_tick_after(T)` to the detector.

After these changes pytest is **330/330**.

## 2. Third leak — `liquidity.mark_swing_levels`

### 2.1 Mechanism

`mark_swing_levels` calls `find_swings(df_h4, lookback_h4=2, ...)`
which calls `find_raw_swings`. A pivot at H4 index `i` requires
**`lookback` candles before AND after** to confirm:

```python
# src/detection/swings.py:126
for i in range(lookback, n - lookback):
    ...
```

So a pivot at H4 candle `i` is **never detected** unless the H4 frame
contains candles `i+1, ..., i+lookback`. The pivot's *time* may be
well before `as_of_utc` (e.g. a pivot at 12:00 UTC with `as_of_utc`
at 13:30 UTC), but its **confirmation** requires data at
`pivot_time + lookback × H4 = pivot_time + 8h`. That data may be
**after `as_of_utc` and after `now_utc`**.

The downstream `_significant_swings_with_time` filters by pivot time
≤ `as_of_utc`, but it has nothing to filter — the pivot is either in
`find_raw_swings`'s output or it isn't. So the *as-of-time* filter
catches the pivot-time leak but not the confirmation-time leak.

### 2.2 Why this changes setup outputs

`mark_swing_levels` truncates the result to the most recent
`n_swings` H4 swings (`h4_recent = h4_sigs[-n_swings:]`, default 5).
If the full-data run confirms more H4 swings than the truncated run
does (because the full run has the post-`as_of_utc` data needed to
confirm pivots at `as_of_utc - lookback × H4`), the **last-5 set
shifts**: older swings that the truncated run keeps in the last-5
get pushed out of the full-data run's last-5 by newly-confirmable
later swings.

`_select_take_profit` then picks the closest opposing-liquidity
level by entry distance from this list. With different last-5 sets,
the two phases pick **different target levels**, yielding different
`tp_runner_price`, `tp_runner_rr`, `tp1_*`, and `target_level_type`.

### 2.3 Concrete reproduction

Setup `NDX100 ny long mss_confirm=2024-03-11T14:55Z`. Same NY
killzone start `13:30 UTC`. After Phase A's two-pass with the FVG /
sweep fixes in place:

| field             | full-df run                  | truncated-df run             |
|-------------------|------------------------------|------------------------------|
| POI               | FVG @19850.5 ↔ 19823.75      | FVG @19850.5 ↔ 19823.75 ✓    |
| entry_price       | 19850.5                      | 19850.5 ✓                    |
| stop_loss         | (matches)                    | (matches) ✓                  |
| target_level_type | `swing_h1_high` @20204.25    | `swing_h4_high` @20068.50    |
| tp_runner_rr      | 5.24                         | 3.23                         |
| tp1_rr            | 5.0                          | 3.23                         |
| quality           | A                            | A                            |

Probing `mark_swing_levels` directly with the same pre-/post-fix
inputs confirms the cause:

```
H4 full df: 261 candles (last=2024-03-11 20:00 UTC)
H4 trunc:   259 candles (last=2024-03-11 12:00 UTC)

FULL DF highs:    19912.50, 20204.25, 20248.75, 20254.25, 20517.25, 20585.50
TRUNC DF highs:   19912.50, 20068.50, 20204.25, 20248.75, 20254.25, 20517.25, 20585.50
                            ^^^^^^^^
                            present only in the truncated run
```

The truncated run keeps the H4 high at `20068.50` (pivot
`2024-03-06 16:00 UTC`, strength `major`); the full-data run's
last-5 H4 swings push it out because the post-`as_of_utc` data
confirms additional H4 swings.

### 2.4 Fix sketch (NOT implemented per the user's STOP rule)

The structural fix is to bound `find_raw_swings` (and / or
`mark_swing_levels`) by `now_utc` such that **a pivot at index `i`
is only considered confirmed when `times[i] + lookback × timeframe
<= now_utc`**, in addition to the existing `i+lookback < n` index
guard. Equivalently: in `find_raw_swings`, after computing the
pivot, also check `times[i + lookback] + timeframe <= now_utc`.

Concretely the parameter would propagate the same way the FVG /
sweep `now_utc` does: `mark_swing_levels(..., now_utc=...)` →
`find_swings(..., now_utc=...)` → `find_raw_swings(..., now_utc=...)`.
The `_significant_swings_with_time` filter is already correct (it
filters on pivot time, not confirmation time, but with the
upstream `find_raw_swings` change the unconfirmed pivots no longer
appear).

This third site mirrors the FVG-c3 and sweep-return-candle fixes:
it makes the detector's *observability* contract uniform — a
candle/swing/event is observable iff its formation has completed
by `now_utc`.

A fourth site likely needs the same treatment: H1 swing detection
(`mark_swing_levels` → `find_swings` on H1) has the same lookback
contract. Same fix, applied to the H1 path.

## 3. Recommended next step

Per the user's plan:

> If at any point the fix touches more than the FVG and sweep-dedup
> sites, STOP — that means there's a third leak we didn't identify.
> Report it before continuing.

I have stopped. The code on `feat/strategy-research` contains the
two-site fix (FVG + sweep) and the audit-harness rework. The third
fix (swing confirmation by `now_utc`) is **not implemented**. The
audit on this branch will not currently reach 30/30 clean.

Three options for the operator to choose from:

1. **Extend Phase A**: add the swing-confirmation fix (and likely
   the parallel H1 fix) to the same branch, re-run the audit,
   target 30/30. Two more focused edits, both small. This is the
   cleanest path; the leak shape mirrors the first two.

2. **Compromise on coverage**: ship the two-site fix as-is and
   accept that the audit will continue to flag a small fraction of
   setups whose target_level / tp_runner_* depend on the swing
   last-5 leak. The financial impact of this leak is smaller than
   the FVG leak (target affects tp_runner_rr and which RR-tier the
   setup falls into; entry/SL/POI are unaffected).

3. **Pause and re-baseline**: acknowledge the audit is not yet
   conclusive and run the audit at lower n-samples on the existing
   branch to scope the residual divergence rate empirically before
   deciding whether to extend.

Path 1 is the right answer in absolute terms; it's a ~30-line
change distributed across `swings.py` and `liquidity.py` and the
audit will most likely come back clean. I recommend the operator
authorise it explicitly so I can proceed.
