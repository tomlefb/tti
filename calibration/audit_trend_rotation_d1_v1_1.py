"""Look-ahead audit (gate 3) for trend_rotation_d1 v1.1.

Reuses the audit machinery from
``calibration/audit_trend_rotation_d1`` (Mode A truncated vs Mode B
full-frame, bit-identical TradeExit + final-state diff). The only
v1.1-specific aspect is the cell list — the v1.1 grid §3.2 is
{63,126} × {3,4,5} × {3,5,7}; this script audits 4 representative
cells covering the corners and centre of the v1.1 grid.

Pipeline code is unchanged from v1; v1.1 only redefines parameter
values. The audit is rerun by methodological discipline to confirm
the pipeline behaves bit-identically at the new (shorter)
rebalance frequencies that v1 never exercised.

Cells audited
-------------
- mom=63,  K=3, rebal=3  — fastest cadence, smallest basket
- mom=63,  K=5, rebal=3  — fastest cadence, largest basket
- mom=126, K=4, rebal=5  — v1.1 default operating point
- mom=126, K=5, rebal=7  — slowest v1.1 cadence, largest basket

Each cell is audited on train (2019-12-22 → 2024-12-31) and
holdout (2025-01-01 → 2026-04-30) windows = 8 audits.

Run
---
    python -m calibration.audit_trend_rotation_d1_v1_1
"""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from calibration.audit_trend_rotation_d1 import (  # noqa: E402
    HOLDOUT_END,
    HOLDOUT_START,
    RUNS_DIR,
    TRAIN_END,
    TRAIN_START,
    audit_cell,
    load_panel,
    smoke_test,
)

V1_1_KEY_CELLS: list[dict] = [
    {"momentum": 63,  "K": 3, "rebalance": 3},
    {"momentum": 63,  "K": 5, "rebalance": 3},
    {"momentum": 126, "K": 4, "rebalance": 5},
    {"momentum": 126, "K": 5, "rebalance": 7},
]


def write_report(
    smoke_ok: bool,
    cells: list[dict],
    *,
    runs_dir: Path = RUNS_DIR,
    wallclock_s: float = 0.0,
) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    path = runs_dir / f"audit_trend_rotation_d1_v1_1_{ts}.md"

    overall_pass = smoke_ok and all(c["identical"] for c in cells)

    lines: list[str] = []
    lines.append(f"# Look-ahead audit — trend_rotation_d1 v1.1 ({ts})")
    lines.append("")
    lines.append(f"- **Verdict global**: {'PASS' if overall_pass else 'FAIL'}")
    lines.append(f"- **Smoke test**: {'PASS' if smoke_ok else 'FAIL'}")
    lines.append(
        "- Spec: `docs/strategies/trend_rotation_d1_v1_1.md` "
        "(commit `bb12a95`). Gate 3 of "
        "`docs/STRATEGY_RESEARCH_PROTOCOL.md`."
    )
    lines.append(
        "- Pipeline code unchanged from v1 (`889f18c`); v1.1 redefines "
        "only `StrategyParams` values. Re-audit by discipline."
    )
    lines.append(f"- Wallclock: {wallclock_s:.1f} s")
    lines.append("")
    lines.append("## Per-cell summary (4 cells × 2 windows = 8 audits)")
    lines.append("")
    lines.append(
        "| Cell (mom/K/rebal) | Window | Cycles | Exits A | Exits B | "
        "Identical exits | Identical final state | Verdict |"
    )
    lines.append("|---|---|---:|---:|---:|:---:|:---:|:---:|")
    for c in cells:
        cell_str = (
            f"{c['cell']['momentum']}d/{c['cell']['K']}/"
            f"{c['cell']['rebalance']}d"
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
                lines.append(f"- {len(ed['a_only'])} exits only in **truncated** (Mode A)")
            if ed["b_only"]:
                lines.append(f"- {len(ed['b_only'])} exits only in **full-frame** (Mode B)")
            if ed["field_diffs"]:
                lines.append(f"- {len(ed['field_diffs'])} exits with field divergence")
            lines.append("")

    lines.append("## Verdict")
    lines.append("")
    if overall_pass:
        lines.append(
            "All 4 v1.1 cells produced bit-identical TradeExit lists "
            "AND final StrategyState across train + holdout windows. "
            "**No look-ahead leak detected at v1.1 rebalance frequencies "
            "(3, 5, 7 days).** Pipeline gate-3-clean for v1.1."
        )
        lines.append("")
        lines.append(
            "Suggested next: gate 4 §3.6 pre-measure on the 18-cell grid, "
            "then full gate 4 on the §3.6-viable subgrid."
        )
    else:
        lines.append(
            "**At least one cell diverges.** Do not proceed to gate 4. "
            "v1.1 introduces new rebalance frequencies (3, 5, 7 d) that "
            "v1 never exercised; investigate the divergence list above."
        )
    lines.append("")

    path.write_text("\n".join(lines) + "\n")
    return path


def main() -> int:
    t_start = time.perf_counter()

    print("=== Smoke test ===", flush=True)
    smoke_ok = smoke_test()
    if not smoke_ok:
        print("\nSmoke test failed — aborting before real-data audits.")
        return 1

    print("\nLoading panel...", flush=True)
    panel = load_panel()
    print(f"  {len(panel)} assets loaded", flush=True)

    cells: list[dict] = []
    windows = [
        ("train", TRAIN_START, TRAIN_END),
        ("holdout", HOLDOUT_START, HOLDOUT_END),
    ]
    for window_label, start, end in windows:
        for cell in V1_1_KEY_CELLS:
            cells.append(audit_cell(panel, cell, window_label, start, end))

    wallclock = time.perf_counter() - t_start
    path = write_report(smoke_ok, cells, wallclock_s=wallclock)
    print(f"\nReport: {path}")
    print(f"Total wallclock: {wallclock:.1f}s")

    overall_pass = smoke_ok and all(c["identical"] for c in cells)
    return 0 if overall_pass else 2


if __name__ == "__main__":
    sys.exit(main())
