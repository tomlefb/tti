"""Look-ahead audit harness for the mean-reversion BB H4 strategy.

Gate 3 of ``docs/STRATEGY_RESEARCH_PROTOCOL.md``. Validates that the
streaming pipeline does not read bars past ``now_utc``.

Modes (per the gate-3 brief)
----------------------------
- **Mode A — streaming truncated (canonical leak test, PASS criterion)**:
  cycle-by-cycle drive of ``build_setup_candidates``, with the H4
  frame *physically truncated* to ``[: i + 1]`` at cycle ``i``. If a
  detector reads past ``now_utc`` somewhere in the pipeline, the
  truncation removes the data and Mode A diverges from Mode B.
- **Mode B — streaming full-frame**: same cycle pattern, ``now_utc``
  filter active inside detectors, but the H4 frame is NEVER cut.
  This is exactly how the production scheduler runs.

PASS criterion: ``setups_a == setups_b`` (multiset, by canonical
key — chronological order is allowed to differ when both modes
queue the same excess but resolve it on different cycles, though
in practice for MR BB H4 they should match cycle-for-cycle).

Why no Mode C (iterative end-of-fixture)?
-----------------------------------------
The breakout-retest audit reports Mode C as a *design-divergence*
diagnostic: spec §2.3's "most recent unlocked swing" rule preempts
older swings, so a single ``now_utc=end`` pass cannot replicate
streaming's cycle order. MR BB H4 has no equivalent rule — every
excess is independent, queued in the order it fires, and resolved
strictly within ``max_return_bars``. Mode C would not surface a
useful design effect, so we skip it.

Run
---
    python -m calibration.audit_mean_reversion_bb_h4 [--smoke-only]

Outputs
-------
- Console log per audit cell.
- Markdown report under
  ``calibration/runs/audit_mean_reversion_bb_h4_<TS>.md``.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.strategies.mean_reversion_bb_h4 import (  # noqa: E402
    Setup,
    StrategyParams,
    StrategyState,
    build_setup_candidates,
)

DUK_ROOT = REPO_ROOT / "tests" / "fixtures" / "dukascopy"
RUNS_DIR = REPO_ROOT / "calibration" / "runs"


# Per-instrument params: medians of the spec §3.2 grid + permissive
# max_risk_distance so this filter never hides an upstream divergence.
# min_rr at 1.0 is the spec default; the audit doesn't care about the
# floor's value provided it is the SAME between modes A and B.
INSTRUMENT_PARAMS: dict[str, StrategyParams] = {
    "XAUUSD": StrategyParams(
        min_penetration_atr_mult=0.3,
        sl_buffer=1.0,
        max_risk_distance=1e9,
    ),
    "NDX100": StrategyParams(
        min_penetration_atr_mult=0.3,
        sl_buffer=5.0,
        max_risk_distance=1e9,
    ),
    "SPX500": StrategyParams(
        min_penetration_atr_mult=0.3,
        sl_buffer=2.0,
        max_risk_distance=1e9,
    ),
}

# Spec §3.3 train/holdout split (same as breakout-retest for
# cross-strategy comparability).
TRAIN_START = pd.Timestamp("2020-01-01", tz="UTC")
TRAIN_END = pd.Timestamp("2024-12-31 23:59:59", tz="UTC")
HOLDOUT_START = pd.Timestamp("2025-01-01", tz="UTC")
HOLDOUT_END = pd.Timestamp("2026-04-29 23:59:59", tz="UTC")


# ---------------------------------------------------------------------------
# Data loading (verbatim mirror of audit_breakout_retest_h4)
# ---------------------------------------------------------------------------


def load_duk_m5(
    instrument: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    """Concatenate every Dukascopy monthly M5 parquet that overlaps the window."""
    instrument_dir = DUK_ROOT / instrument
    files = sorted(instrument_dir.glob("*_bid.parquet"))
    frames = []
    for f in files:
        ym = f.stem.split("_")[0]
        try:
            month_start = pd.Timestamp(f"{ym}-01", tz="UTC")
        except Exception:
            continue
        if month_start > end + pd.Timedelta(days=31):
            continue
        if month_start + pd.Timedelta(days=31) < start:
            continue
        frames.append(pd.read_parquet(f))
    if not frames:
        raise FileNotFoundError(f"No Duk M5 parquets for {instrument} in window")
    df = pd.concat(frames).sort_index()
    df = df[~df.index.duplicated(keep="first")]
    df = df.loc[(df.index >= start) & (df.index <= end)]
    return df[["open", "high", "low", "close"]]


def resample_m5_to_h4(m5: pd.DataFrame) -> pd.DataFrame:
    """M5 → H4 anchored at UTC midnight (closes at 04, 08, 12, 16, 20, 00)."""
    h4 = (
        m5.resample("4h", origin="epoch", label="left", closed="left")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
        .dropna(subset=["close"])
    )
    return h4


def to_pipeline_h4(h4: pd.DataFrame) -> pd.DataFrame:
    """DatetimeIndexed H4 → column-shape with ``time`` column + RangeIndex."""
    df = h4.reset_index().rename(columns={"timestamp": "time"})
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df[["time", "open", "high", "low", "close"]]


# ---------------------------------------------------------------------------
# Mode B — streaming full-frame (production-like)
# ---------------------------------------------------------------------------


def run_streaming(
    df_h4: pd.DataFrame,
    instrument: str,
    params: StrategyParams,
    *,
    cycle_step: int = 1,
) -> list[Setup]:
    """Drive the pipeline cycle-by-cycle on the **complete** H4 frame."""
    state = StrategyState()
    setups: list[Setup] = []
    times = pd.to_datetime(df_h4["time"], utc=True)
    n = len(df_h4)
    for i in range(0, n, cycle_step):
        bar_open = pd.Timestamp(times.iloc[i]).to_pydatetime()
        now_utc = bar_open + timedelta(hours=4)
        new_setups = build_setup_candidates(
            df_h4, instrument, params, state, now_utc=now_utc
        )
        setups.extend(new_setups)
    return setups


# ---------------------------------------------------------------------------
# Mode A — streaming truncated (canonical leak test)
# ---------------------------------------------------------------------------


def run_streaming_truncated(
    df_h4: pd.DataFrame,
    instrument: str,
    params: StrategyParams,
    *,
    cycle_step: int = 1,
) -> list[Setup]:
    """Same cycle pattern as ``run_streaming`` but the frame handed to
    each call is sliced to ``[: i + 1]``. Bit-identical to Mode B iff
    the pipeline honours ``now_utc`` correctly — any post-cutoff read
    becomes an out-of-bounds index access here, surfacing the leak.
    """
    state = StrategyState()
    setups: list[Setup] = []
    times = pd.to_datetime(df_h4["time"], utc=True)
    n = len(df_h4)
    for i in range(0, n, cycle_step):
        bar_open = pd.Timestamp(times.iloc[i]).to_pydatetime()
        now_utc = bar_open + timedelta(hours=4)
        df_truncated = df_h4.iloc[: i + 1].reset_index(drop=True)
        new_setups = build_setup_candidates(
            df_truncated, instrument, params, state, now_utc=now_utc
        )
        setups.extend(new_setups)
    return setups


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def _setup_key(s: Setup) -> tuple:
    """Stable canonical key for sorted-list comparison."""
    return (
        s.timestamp_utc.isoformat(),
        s.instrument,
        s.direction,
        round(s.entry_price, 6),
    )


def diff_setups(a: list[Setup], b: list[Setup]) -> dict:
    """Compare two setup lists; return a structured diff.

    Lists are sorted by canonical key so order-only differences (rare
    in MR BB H4 but possible if both modes happen to flush
    same-cycle multiple pendings in different order) don't trigger a
    false-positive leak signal. The audit's PASS criterion is the
    *multiset* of setups.
    """
    a_sorted = sorted(a, key=_setup_key)
    b_sorted = sorted(b, key=_setup_key)
    a_map = {_setup_key(s): s for s in a_sorted}
    b_map = {_setup_key(s): s for s in b_sorted}

    a_only_keys = sorted(a_map.keys() - b_map.keys())
    b_only_keys = sorted(b_map.keys() - a_map.keys())
    shared_keys = sorted(a_map.keys() & b_map.keys())

    field_diffs: list[dict] = []
    for k in shared_keys:
        sa = a_map[k]
        sb = b_map[k]
        if sa != sb:
            diffs = {}
            for field in (
                "entry_price",
                "stop_loss",
                "take_profit",
                "risk_reward",
            ):
                va = getattr(sa, field)
                vb = getattr(sb, field)
                if va != vb:
                    diffs[field] = (va, vb)
            field_diffs.append({"key": k, "fields": diffs})

    identical = (
        len(a_only_keys) == 0 and len(b_only_keys) == 0 and len(field_diffs) == 0
    )
    return {
        "n_a": len(a),
        "n_b": len(b),
        "n_shared": len(shared_keys),
        "a_only": [a_map[k] for k in a_only_keys],
        "b_only": [b_map[k] for k in b_only_keys],
        "field_diffs": field_diffs,
        "identical": identical,
    }


# ---------------------------------------------------------------------------
# Cell driver
# ---------------------------------------------------------------------------


def audit_cell(
    instrument: str,
    window_label: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    cycle_step: int = 1,
    log: bool = True,
) -> dict:
    """Audit one (instrument, window) cell.

    PASS criterion: Mode A (truncated) == Mode B (full-frame).
    """
    if log:
        print(
            f"\n--- {instrument} / {window_label} "
            f"({start.date()} → {end.date()}) ---"
        )

    t0 = time.perf_counter()
    m5 = load_duk_m5(instrument, start, end)
    h4 = resample_m5_to_h4(m5)
    df_h4 = to_pipeline_h4(h4)
    if log:
        print(
            f"  loaded: M5 rows={len(m5)}, H4 rows={len(df_h4)}",
            flush=True,
        )

    params = INSTRUMENT_PARAMS[instrument]

    t_b0 = time.perf_counter()
    setups_b = run_streaming(df_h4, instrument, params, cycle_step=cycle_step)
    t_b1 = time.perf_counter()
    if log:
        print(
            f"  B streaming full-frame : {len(setups_b)} setups in {t_b1 - t_b0:.1f}s",
            flush=True,
        )

    t_a0 = time.perf_counter()
    setups_a = run_streaming_truncated(
        df_h4, instrument, params, cycle_step=cycle_step
    )
    t_a1 = time.perf_counter()
    if log:
        print(
            f"  A streaming truncated  : {len(setups_a)} setups in {t_a1 - t_a0:.1f}s",
            flush=True,
        )

    diff_ab = diff_setups(setups_a, setups_b)

    cycles = (len(df_h4) + cycle_step - 1) // cycle_step
    if log:
        verdict = "PASS" if diff_ab["identical"] else "FAIL"
        print(
            f"  A-vs-B leak diff: A-only={len(diff_ab['a_only'])} "
            f"B-only={len(diff_ab['b_only'])} field={len(diff_ab['field_diffs'])} "
            f"→ {verdict}"
        )

    return {
        "instrument": instrument,
        "window": window_label,
        "cycles": cycles,
        "n_a": diff_ab["n_a"],
        "n_b": diff_ab["n_b"],
        "n_identical_ab": diff_ab["n_shared"] - len(diff_ab["field_diffs"]),
        "n_diff_ab": (
            len(diff_ab["a_only"])
            + len(diff_ab["b_only"])
            + len(diff_ab["field_diffs"])
        ),
        "a_only": diff_ab["a_only"],
        "b_only": diff_ab["b_only"],
        "field_diffs": diff_ab["field_diffs"],
        "identical": diff_ab["identical"],
        "wallclock_truncated_s": round(t_a1 - t_a0, 2),
        "wallclock_streaming_s": round(t_b1 - t_b0, 2),
        "wallclock_total_s": round(time.perf_counter() - t0, 2),
        "cycle_step": cycle_step,
    }


# ---------------------------------------------------------------------------
# Smoke test (gate 2 fixtures)
# ---------------------------------------------------------------------------


def smoke_test() -> bool:
    """Smoke-audit the gate-2 hand-built fixtures.

    Compares Mode A (truncated) with Mode B (full-frame) on each of
    the 6 fixtures shipped with
    ``tests/strategies/mean_reversion_bb_h4/test_pipeline_integration.py``.
    Identity = no leak; the per-fixture expected setup count is also
    asserted as a regression guard.
    """
    from tests.strategies.mean_reversion_bb_h4.test_pipeline_integration import (
        _fixture_long,
        _fixture_no_exhaustion,
        _fixture_no_return,
        _fixture_off_killzone,
        _fixture_shallow_penetration,
        _fixture_short,
        _params,
    )

    cases = [
        ("long_fixture", _fixture_long(), 1),
        ("short_fixture", _fixture_short(), 1),
        ("no_return", _fixture_no_return(), 0),
        ("off_killzone", _fixture_off_killzone(), 0),
        ("shallow_penetration", _fixture_shallow_penetration(), 0),
        # v1.1 (commit ae61f70): the §2.4 exhaustion filter is removed,
        # so the marubozu fixture now emits 1 setup. v1.0 expected 0.
        ("marubozu_emits_post_v1_1", _fixture_no_exhaustion(), 1),
    ]

    # Use the same loosened-min_rr parametrization as the integration
    # tests so the long/short fixtures emit their 1 setup each. The
    # audit only cares about A == B, so any consistent param set is
    # fine — but matching the integration tests keeps the smoke test
    # diagnostic-equivalent to the gate-2 verdict.
    params = _params()

    print("\n=== Gate 3 smoke test (gate-2 hand-built fixtures) ===")
    all_ok = True
    for name, df_h4, expected in cases:
        a = run_streaming_truncated(df_h4, "XAUUSD", params)
        b = run_streaming(df_h4, "XAUUSD", params)
        diff = diff_setups(a, b)
        ok = diff["identical"] and len(a) == expected
        verdict = "OK" if ok else "FAIL"
        print(
            f"  [{verdict}] {name}: A={len(a)} B={len(b)} expected={expected} "
            f"identical={diff['identical']}"
        )
        if not ok:
            all_ok = False
            if diff["a_only"]:
                print(f"    A only: {[_setup_key(s) for s in diff['a_only']]}")
            if diff["b_only"]:
                print(f"    B only: {[_setup_key(s) for s in diff['b_only']]}")
            if diff["field_diffs"]:
                print(f"    field diffs: {diff['field_diffs']}")
    return all_ok


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def write_report(
    smoke_ok: bool,
    cells: list[dict],
    *,
    runs_dir: Path = RUNS_DIR,
) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    path = runs_dir / f"audit_mean_reversion_bb_h4_{ts}.md"

    overall_pass = smoke_ok and all(c["identical"] for c in cells)

    lines: list[str] = []
    lines.append(f"# Look-ahead audit — mean_reversion_bb_h4 ({ts})")
    lines.append("")
    lines.append(f"- **Verdict global**: {'PASS' if overall_pass else 'FAIL'}")
    lines.append(f"- **Smoke test**: {'PASS' if smoke_ok else 'FAIL'}")
    lines.append(
        "- Spec: `docs/strategies/mean_reversion_bb_h4.md`. "
        "Gate 3 of `docs/STRATEGY_RESEARCH_PROTOCOL.md`."
    )
    lines.append("")
    lines.append("## Audit modes")
    lines.append("")
    lines.append(
        "- **Mode A — streaming truncated (PASS criterion)**: cycle-by-cycle "
        "drive of the pipeline with the H4 frame physically sliced to "
        "``[: i + 1]`` at cycle ``i``. If any detector reads past "
        "``now_utc`` in production, the truncation removes the data and "
        "Mode A diverges from Mode B."
    )
    lines.append(
        "- **Mode B — streaming full-frame**: same cycle pattern, ``now_utc`` "
        "active inside detectors, but the H4 frame is never cut. This is "
        "exactly how the production scheduler runs."
    )
    lines.append(
        "- No Mode C: MR BB H4 has no spec rule analogous to breakout-retest's "
        '"most recent unlocked swing" §2.3 preemption, so an iterative '
        "end-of-fixture pass would not surface a useful design effect."
    )
    lines.append("")
    lines.append("## Per-cell summary (A vs B — leak test)")
    lines.append("")
    lines.append(
        "| Instrument | Window | Cycles | Setups A | Setups B | Identical | "
        "Diff | Wallclock A (s) | Wallclock B (s) |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
    for c in cells:
        lines.append(
            f"| {c['instrument']} | {c['window']} | {c['cycles']} | "
            f"{c['n_a']} | {c['n_b']} | {c['n_identical_ab']} | {c['n_diff_ab']} | "
            f"{c['wallclock_truncated_s']} | {c['wallclock_streaming_s']} |"
        )
    lines.append("")

    failures = [c for c in cells if not c["identical"]]
    if failures:
        lines.append("## Divergences")
        lines.append("")
        for c in failures:
            lines.append(f"### {c['instrument']} / {c['window']}")
            lines.append("")
            if c["a_only"]:
                lines.append(
                    f"- {len(c['a_only'])} setups present only in **truncated** (Mode A):"
                )
                for s in c["a_only"][:10]:
                    lines.append(
                        f"  - `{s.timestamp_utc.isoformat()}` {s.direction} "
                        f"entry={s.entry_price:.4f} sl={s.stop_loss:.4f} "
                        f"tp={s.take_profit:.4f}"
                    )
            if c["b_only"]:
                lines.append(
                    f"- {len(c['b_only'])} setups present only in **full-frame** (Mode B):"
                )
                for s in c["b_only"][:10]:
                    lines.append(
                        f"  - `{s.timestamp_utc.isoformat()}` {s.direction} "
                        f"entry={s.entry_price:.4f} sl={s.stop_loss:.4f} "
                        f"tp={s.take_profit:.4f}"
                    )
            if c["field_diffs"]:
                lines.append(
                    f"- {len(c['field_diffs'])} setups present in both with field divergence:"
                )
                for fd in c["field_diffs"][:10]:
                    lines.append(f"  - `{fd['key']}`: {fd['fields']}")
            lines.append("")

    if overall_pass:
        lines.append("## Verdict")
        lines.append("")
        lines.append(
            "All audited cells produced bit-identical setup lists between "
            "truncated and full-frame streaming. **No look-ahead leak detected.**"
        )
        lines.append("")
        lines.append(
            "Suggested next: gate 4 — backtest principal Duk on train then "
            "holdout with the spec §4 pre-specified hypotheses."
        )
    else:
        lines.append("## Verdict")
        lines.append("")
        lines.append(
            "**At least one cell diverges.** Do not proceed to subsequent "
            "gates. Investigate the listed setups: which detector emits a "
            "setup in truncated mode but not in full-frame (or vice versa) "
            "narrows the look-ahead source."
        )

    path.write_text("\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--smoke-only",
        action="store_true",
        help="Skip the real-data audits; run only the gate-2 fixture smoke test.",
    )
    parser.add_argument(
        "--instruments",
        nargs="+",
        default=list(INSTRUMENT_PARAMS.keys()),
        help="Instruments to audit (default: %(default)s).",
    )
    parser.add_argument(
        "--cycle-step",
        type=int,
        default=1,
        help=(
            "H4-cycle stride for streaming mode. 1 = every close (canonical). "
            "Bigger values speed the audit but only stay valid if the larger "
            "stride still observes every return within ``max_return_bars``."
        ),
    )
    parser.add_argument(
        "--no-train",
        action="store_true",
        help="Skip the train (2020-01 → 2024-12) window.",
    )
    parser.add_argument(
        "--no-holdout",
        action="store_true",
        help="Skip the holdout (2025-01 → 2026-04) window.",
    )
    args = parser.parse_args()

    t_start = time.perf_counter()
    smoke_ok = smoke_test()

    if args.smoke_only:
        path = write_report(smoke_ok, [])
        print(f"\nReport: {path}")
        print(f"Total wallclock: {time.perf_counter() - t_start:.1f}s")
        return 0 if smoke_ok else 1

    if not smoke_ok:
        print("\nSmoke test failed — aborting before real-data audits.")
        return 1

    cells: list[dict] = []
    windows = []
    if not args.no_train:
        windows.append(("train", TRAIN_START, TRAIN_END))
    if not args.no_holdout:
        windows.append(("holdout", HOLDOUT_START, HOLDOUT_END))

    for instrument in args.instruments:
        for window_label, start, end in windows:
            cells.append(
                audit_cell(
                    instrument,
                    window_label,
                    start,
                    end,
                    cycle_step=args.cycle_step,
                )
            )

    path = write_report(smoke_ok, cells)
    print(f"\nReport: {path}")
    print(f"Total wallclock: {time.perf_counter() - t_start:.1f}s")

    overall_pass = smoke_ok and all(c["identical"] for c in cells)
    return 0 if overall_pass else 2


if __name__ == "__main__":
    sys.exit(main())
