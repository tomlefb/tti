"""Look-ahead audit harness for the trend_rotation_d1 strategy.

Gate 3 of ``docs/STRATEGY_RESEARCH_PROTOCOL.md``. Validates that
the multi-asset rotation pipeline does not read panel data past
``now_utc``.

Modes
-----
- **Mode A — streaming truncated (canonical leak test, PASS
  criterion)**: at each rebalance cycle, the per-asset OHLC frame
  is **physically sliced** to ``df.index <= now_utc`` *before*
  the pipeline call. If a detector reads beyond ``now_utc`` in
  production, the truncation removes the data and Mode A diverges
  from Mode B.
- **Mode B — streaming full-frame**: the full panel is passed
  every cycle; the pipeline filters internally via the
  ``< now_utc`` slice in ``_score_one_asset``. This is exactly
  how the production scheduler runs.

PASS criterion: ``trade_exits_a == trade_exits_b`` (bit-identical,
in order — pipeline emits exits in alphabetical asset order at
each rebalance, so streams are deterministic). Final-state
``current_basket`` and ``open_positions`` are also compared as a
secondary check (catches divergences on the last rebalance that
do not produce a close).

Run
---
    python -m calibration.audit_trend_rotation_d1 [--smoke-only]
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.strategies.trend_rotation_d1 import (  # noqa: E402
    StrategyParams,
    StrategyState,
    TradeEntry,
    TradeExit,
    build_rebalance_candidates,
)

HIST_ROOT = REPO_ROOT / "tests" / "fixtures" / "historical"
RUNS_DIR = REPO_ROOT / "calibration" / "runs"

UNIVERSE = (
    "NDX100", "SPX500", "US30", "US2000", "GER30", "UK100", "JP225",
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD",
    "XAUUSD", "XAGUSD",
    "USOUSD",
    "BTCUSD",
)

TRAIN_START = pd.Timestamp("2019-12-22", tz="UTC")
TRAIN_END = pd.Timestamp("2024-12-31", tz="UTC")
HOLDOUT_START = pd.Timestamp("2025-01-01", tz="UTC")
HOLDOUT_END = pd.Timestamp("2026-04-30", tz="UTC")

# Spec §3.2 grid — the 4 most-active cells per the diagnostic.
KEY_CELLS: list[dict] = [
    {"momentum": 63,  "K": 3, "rebalance": 10},
    {"momentum": 63,  "K": 4, "rebalance": 10},
    {"momentum": 126, "K": 3, "rebalance": 10},
    {"momentum": 126, "K": 4, "rebalance": 10},
]


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load_panel() -> dict[str, pd.DataFrame]:
    """Load the 15-asset D1 panel as a dict of frames indexed by
    tz-aware UTC date."""
    panel: dict[str, pd.DataFrame] = {}
    for asset in UNIVERSE:
        p = HIST_ROOT / f"{asset}_D1.parquet"
        if not p.exists():
            raise FileNotFoundError(f"Missing fixture: {p}")
        df = pd.read_parquet(p)
        if "time" in df.columns:
            df["time"] = pd.to_datetime(df["time"], utc=True)
            df = df.set_index("time")
        # Normalise to calendar-day 00:00 UTC for cross-asset alignment
        # (existing fixtures mix 21:00 / 22:00 / 00:00 conventions).
        df.index = df.index.normalize()
        df = df[~df.index.duplicated(keep="first")].sort_index()
        panel[asset] = df
    return panel


def cycle_dates(
    panel: dict[str, pd.DataFrame],
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> list[pd.Timestamp]:
    """Union of trading dates across the panel within ``[start, end]``."""
    all_dates: set[pd.Timestamp] = set()
    for df in panel.values():
        all_dates |= set(df.index)
    return sorted(d for d in all_dates if start <= d <= end)


# ---------------------------------------------------------------------------
# Mode A — streaming truncated
# ---------------------------------------------------------------------------


def run_streaming_truncated(
    panel: dict[str, pd.DataFrame],
    params: StrategyParams,
    dates: list[pd.Timestamp],
) -> tuple[list[TradeExit], StrategyState]:
    state = StrategyState()
    exits: list[TradeExit] = []
    for now in dates:
        # Physically slice each asset frame to the visible prefix.
        truncated = {
            a: df.loc[df.index <= now]
            for a, df in panel.items()
        }
        new_exits = build_rebalance_candidates(
            truncated, params, state, now_utc=now.to_pydatetime()
        )
        exits.extend(new_exits)
    return exits, state


# ---------------------------------------------------------------------------
# Mode B — streaming full-frame
# ---------------------------------------------------------------------------


def run_streaming(
    panel: dict[str, pd.DataFrame],
    params: StrategyParams,
    dates: list[pd.Timestamp],
) -> tuple[list[TradeExit], StrategyState]:
    state = StrategyState()
    exits: list[TradeExit] = []
    for now in dates:
        new_exits = build_rebalance_candidates(
            panel, params, state, now_utc=now.to_pydatetime()
        )
        exits.extend(new_exits)
    return exits, state


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def _exit_key(e: TradeExit) -> tuple:
    return (
        e.asset,
        e.entry_timestamp_utc.isoformat(),
        e.exit_timestamp_utc.isoformat(),
    )


def diff_exits(a: list[TradeExit], b: list[TradeExit]) -> dict:
    """Compare two TradeExit lists. The lists are emitted in the same
    deterministic order (alphabetical asset within each rebalance) so
    plain ``a == b`` would also work, but we use a key-based diff to
    surface specific divergences in the report."""
    a_map = {_exit_key(e): e for e in a}
    b_map = {_exit_key(e): e for e in b}
    a_only = sorted(a_map.keys() - b_map.keys())
    b_only = sorted(b_map.keys() - a_map.keys())
    shared = sorted(a_map.keys() & b_map.keys())

    field_diffs: list[dict] = []
    for k in shared:
        if a_map[k] != b_map[k]:
            sa, sb = a_map[k], b_map[k]
            diffs: dict = {}
            for field_name in (
                "entry_price",
                "exit_price",
                "position_size",
                "atr_at_entry",
                "return_r",
            ):
                va = getattr(sa, field_name)
                vb = getattr(sb, field_name)
                if va != vb:
                    diffs[field_name] = (va, vb)
            if diffs:
                field_diffs.append({"key": k, "fields": diffs})

    return {
        "n_a": len(a),
        "n_b": len(b),
        "n_shared": len(shared),
        "a_only": [a_map[k] for k in a_only],
        "b_only": [b_map[k] for k in b_only],
        "field_diffs": field_diffs,
        "identical": (
            len(a_only) == 0
            and len(b_only) == 0
            and len(field_diffs) == 0
        ),
    }


def diff_final_state(
    state_a: StrategyState, state_b: StrategyState
) -> dict:
    """Final-state diff: current_basket + open_positions."""
    basket_diff = state_a.current_basket.symmetric_difference(
        state_b.current_basket
    )
    open_diff: dict = {}
    for asset in (
        set(state_a.open_positions) | set(state_b.open_positions)
    ):
        a = state_a.open_positions.get(asset)
        b = state_b.open_positions.get(asset)
        if a != b:
            open_diff[asset] = {"a": a, "b": b}
    return {
        "basket_symmetric_diff": sorted(basket_diff),
        "open_positions_diff": open_diff,
        "identical": not basket_diff and not open_diff,
    }


# ---------------------------------------------------------------------------
# Cell driver
# ---------------------------------------------------------------------------


def _params_from_cell(cell: dict) -> StrategyParams:
    return StrategyParams(
        universe=UNIVERSE,
        momentum_lookback_days=cell["momentum"],
        K=cell["K"],
        rebalance_frequency_days=cell["rebalance"],
        risk_per_trade_pct=1.0,
        atr_period=20,
        atr_explosive_threshold=5.0,
        atr_regime_lookback=90,
    )


def audit_cell(
    panel: dict[str, pd.DataFrame],
    cell: dict,
    window_label: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    log: bool = True,
) -> dict:
    """Audit one (cell, window) tuple. PASS = Mode A == Mode B on
    TradeExit list AND final state."""
    if log:
        print(
            f"\n--- cell mom={cell['momentum']} K={cell['K']} "
            f"rebal={cell['rebalance']} / {window_label} "
            f"({start.date()} → {end.date()}) ---"
        )
    params = _params_from_cell(cell)
    dates = cycle_dates(panel, start, end)
    if log:
        print(f"  dates: {len(dates)} cycles")

    t0 = time.perf_counter()
    exits_b, state_b = run_streaming(panel, params, dates)
    t1 = time.perf_counter()
    if log:
        print(
            f"  B streaming full-frame : {len(exits_b)} exits in {t1 - t0:.1f}s",
            flush=True,
        )

    t0 = time.perf_counter()
    exits_a, state_a = run_streaming_truncated(panel, params, dates)
    t1 = time.perf_counter()
    if log:
        print(
            f"  A streaming truncated  : {len(exits_a)} exits in {t1 - t0:.1f}s",
            flush=True,
        )

    diff_e = diff_exits(exits_a, exits_b)
    diff_s = diff_final_state(state_a, state_b)
    overall_ok = diff_e["identical"] and diff_s["identical"]

    if log:
        print(
            f"  A-vs-B exits: A-only={len(diff_e['a_only'])} "
            f"B-only={len(diff_e['b_only'])} "
            f"field={len(diff_e['field_diffs'])}; "
            f"final state identical={diff_s['identical']} "
            f"→ {'PASS' if overall_ok else 'FAIL'}"
        )

    return {
        "cell": cell,
        "window": window_label,
        "n_dates": len(dates),
        "n_exits_a": diff_e["n_a"],
        "n_exits_b": diff_e["n_b"],
        "exits_diff": diff_e,
        "state_diff": diff_s,
        "identical": overall_ok,
    }


# ---------------------------------------------------------------------------
# Smoke test (gate 2 fixtures)
# ---------------------------------------------------------------------------


def smoke_test() -> bool:
    from tests.strategies.trend_rotation_d1.test_pipeline_integration import (
        _fixture_basket_transition,
        _fixture_explosive_asset,
        _fixture_short_history,
        _fixture_stable_ranking,
        _short_params,
    )

    cases = [
        (
            "fixture_a_basket_transition",
            _fixture_basket_transition(),
            _short_params(lookback=5, K=2, rebal=5),
        ),
        (
            "fixture_b_stable_ranking",
            _fixture_stable_ranking(),
            StrategyParams(
                universe=("A", "B", "C"),
                momentum_lookback_days=5,
                K=2,
                rebalance_frequency_days=5,
                atr_period=5,
                atr_regime_lookback=10,
            ),
        ),
        (
            "fixture_c_explosive_asset",
            _fixture_explosive_asset(),
            StrategyParams(
                universe=("A", "B", "C", "D"),
                momentum_lookback_days=5,
                K=2,
                rebalance_frequency_days=5,
                atr_period=5,
                atr_explosive_threshold=5.0,
                atr_regime_lookback=10,
            ),
        ),
        (
            "fixture_d_short_history",
            _fixture_short_history(),
            StrategyParams(
                universe=("A", "B", "C", "D"),
                momentum_lookback_days=5,
                K=2,
                rebalance_frequency_days=5,
                atr_period=5,
                atr_regime_lookback=10,
            ),
        ),
    ]

    print("\n=== Gate 3 smoke test (gate-2 hand-built fixtures) ===")
    all_ok = True
    for name, panel, params in cases:
        # Use union of asset dates as cycle dates (panel is small).
        dates = sorted(
            set().union(*(df.index for df in panel.values()))
        )
        exits_b, state_b = run_streaming(panel, params, dates)
        exits_a, state_a = run_streaming_truncated(panel, params, dates)
        diff_e = diff_exits(exits_a, exits_b)
        diff_s = diff_final_state(state_a, state_b)
        ok = diff_e["identical"] and diff_s["identical"]
        verdict = "OK" if ok else "FAIL"
        print(
            f"  [{verdict}] {name}: "
            f"A={len(exits_a)} B={len(exits_b)} "
            f"identical_exits={diff_e['identical']} "
            f"identical_state={diff_s['identical']}"
        )
        if not ok:
            all_ok = False
            if diff_e["a_only"]:
                print(f"    A only: {[_exit_key(e) for e in diff_e['a_only']]}")
            if diff_e["b_only"]:
                print(f"    B only: {[_exit_key(e) for e in diff_e['b_only']]}")
            if diff_e["field_diffs"]:
                print(f"    field diffs: {diff_e['field_diffs']}")
            if not diff_s["identical"]:
                print(
                    f"    state diff: basket={diff_s['basket_symmetric_diff']} "
                    f"open={list(diff_s['open_positions_diff'].keys())}"
                )
    return all_ok


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def write_report(
    smoke_ok: bool,
    cells: list[dict],
    *,
    runs_dir: Path = RUNS_DIR,
    wallclock_s: float = 0.0,
) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    path = runs_dir / f"audit_trend_rotation_d1_{ts}.md"

    overall_pass = smoke_ok and all(c["identical"] for c in cells)

    lines: list[str] = []
    lines.append(f"# Look-ahead audit — trend_rotation_d1 ({ts})")
    lines.append("")
    lines.append(f"- **Verdict global**: {'PASS' if overall_pass else 'FAIL'}")
    lines.append(f"- **Smoke test**: {'PASS' if smoke_ok else 'FAIL'}")
    lines.append(
        "- Spec: `docs/strategies/trend_rotation_d1.md` (commit "
        "`889f18c`). Gate 3 of `docs/STRATEGY_RESEARCH_PROTOCOL.md`."
    )
    lines.append(f"- Wallclock: {wallclock_s:.1f} s")
    lines.append("")
    lines.append("## Audit modes")
    lines.append("")
    lines.append(
        "- **Mode A — streaming truncated (PASS criterion)**: per-asset "
        "panel frames are physically sliced to ``df.index <= now_utc`` "
        "at every cycle. If any detector reads past ``now_utc`` in "
        "production, the truncation removes the data and Mode A "
        "diverges from Mode B."
    )
    lines.append(
        "- **Mode B — streaming full-frame**: full panel passed every "
        "cycle; the pipeline filters internally via "
        "``df.loc[df.index < now_utc]`` in ``_score_one_asset``."
    )
    lines.append("")
    lines.append(
        "PASS criterion: bit-identical ``TradeExit`` list AND final "
        "``StrategyState`` (current_basket + open_positions)."
    )
    lines.append("")

    lines.append("## Per-cell summary")
    lines.append("")
    lines.append(
        "| Cell (mom/K/rebal) | Window | Cycles | Exits A | Exits B | "
        "Identical exits | Identical final state | Verdict |"
    )
    lines.append(
        "|---|---|---:|---:|---:|:---:|:---:|:---:|"
    )
    for c in cells:
        cell_str = (
            f"{c['cell']['momentum']}d/{c['cell']['K']}/{c['cell']['rebalance']}d"
        )
        ex_id = c["exits_diff"]["identical"]
        st_id = c["state_diff"]["identical"]
        verdict = "PASS" if c["identical"] else "FAIL"
        lines.append(
            f"| {cell_str} | {c['window']} | {c['n_dates']} | "
            f"{c['n_exits_a']} | {c['n_exits_b']} | "
            f"{'✅' if ex_id else '❌'} | "
            f"{'✅' if st_id else '❌'} | "
            f"{verdict} |"
        )
    lines.append("")

    failures = [c for c in cells if not c["identical"]]
    if failures:
        lines.append("## Divergences")
        lines.append("")
        for c in failures:
            cell_str = (
                f"{c['cell']['momentum']}d/{c['cell']['K']}/"
                f"{c['cell']['rebalance']}d"
            )
            lines.append(f"### {cell_str} / {c['window']}")
            lines.append("")
            ed = c["exits_diff"]
            if ed["a_only"]:
                lines.append(f"- {len(ed['a_only'])} exits only in **truncated** (Mode A):")
                for e in ed["a_only"][:10]:
                    lines.append(
                        f"  - asset={e.asset} entry={e.entry_timestamp_utc} "
                        f"exit={e.exit_timestamp_utc} return_r={e.return_r:+.4f}"
                    )
            if ed["b_only"]:
                lines.append(f"- {len(ed['b_only'])} exits only in **full-frame** (Mode B):")
                for e in ed["b_only"][:10]:
                    lines.append(
                        f"  - asset={e.asset} entry={e.entry_timestamp_utc} "
                        f"exit={e.exit_timestamp_utc} return_r={e.return_r:+.4f}"
                    )
            if ed["field_diffs"]:
                lines.append(f"- {len(ed['field_diffs'])} exits in both with field divergence:")
                for fd in ed["field_diffs"][:10]:
                    lines.append(f"  - `{fd['key']}`: {fd['fields']}")
            sd = c["state_diff"]
            if not sd["identical"]:
                lines.append("- Final state divergence:")
                if sd["basket_symmetric_diff"]:
                    lines.append(
                        f"  - basket symmetric diff: {sd['basket_symmetric_diff']}"
                    )
                if sd["open_positions_diff"]:
                    lines.append(
                        f"  - open positions diff: "
                        f"{list(sd['open_positions_diff'].keys())}"
                    )
            lines.append("")

    if overall_pass:
        lines.append("## Verdict")
        lines.append("")
        lines.append(
            "All audited cells produced bit-identical TradeExit lists "
            "AND final StrategyState between truncated and full-frame "
            "streaming. **No look-ahead leak detected.**"
        )
        lines.append("")
        lines.append(
            "Suggested next: gate 4 — backtest principal Duk on train "
            "then holdout with the spec §4 pre-specified hypotheses."
        )
    else:
        lines.append("## Verdict")
        lines.append("")
        lines.append(
            "**At least one cell diverges.** Do not proceed to gate 4. "
            "The divergence list above narrows the leak source — typically "
            "a detector reading panel data past ``now_utc``."
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
        "--no-train",
        action="store_true",
        help="Skip the train (2019-12 → 2024-12) window.",
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
        path = write_report(smoke_ok, [], wallclock_s=time.perf_counter() - t_start)
        print(f"\nReport: {path}")
        return 0 if smoke_ok else 1

    if not smoke_ok:
        print("\nSmoke test failed — aborting before real-data audits.")
        return 1

    print("\nLoading panel...", flush=True)
    panel = load_panel()
    print(f"  {len(panel)} assets loaded", flush=True)

    cells: list[dict] = []
    windows = []
    if not args.no_train:
        windows.append(("train", TRAIN_START, TRAIN_END))
    if not args.no_holdout:
        windows.append(("holdout", HOLDOUT_START, HOLDOUT_END))

    for window_label, start, end in windows:
        for cell in KEY_CELLS:
            cells.append(audit_cell(panel, cell, window_label, start, end))

    wallclock = time.perf_counter() - t_start
    path = write_report(smoke_ok, cells, wallclock_s=wallclock)
    print(f"\nReport: {path}")
    print(f"Total wallclock: {wallclock:.1f}s ({wallclock / 60:.1f} min)")

    overall_pass = smoke_ok and all(c["identical"] for c in cells)
    return 0 if overall_pass else 2


if __name__ == "__main__":
    sys.exit(main())
