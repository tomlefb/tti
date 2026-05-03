"""MT5 vs Databento — TJR detector under the leak-free tick simulator.

Runs the operator-validated TJR baseline on both fixture sources
(MT5 broker series + Databento Panama-adjusted continuous front-month)
restricted to their temporal overlap window for XAUUSD, NDX100,
SPX500. Produces, per (source, instrument):

- ``<source>_<INSTRUMENT>.json`` — a ``BacktestResult`` (aggregate
  metrics + bootstrap CI on the closed-trade R sample).
- ``<source>_<INSTRUMENT>_setups.jsonl`` — one row per emitted setup
  with the full set of fields needed by the downstream diff
  (timestamp_utc, killzone, direction, quality, swept_level_price,
  entry/SL/TP, realized_R, outcome).

Source identifiers are ``mt5`` (``tests/fixtures/historical``) and
``dbn`` (``tests/fixtures/historical_extended/processed_adjusted``).
The fixture-dir override is applied per-process via the
``TTI_FIXTURE_DIR`` env var that ``baseline_tjr_databento.py`` already
honours, so we get isolated fixture loading without touching settings.

Overlap windows (computed from fixture min/max times, post timezone-fix and
extended-depth MT5 fixtures, commit ``f868793``):

- XAUUSD : 2019-12-23 → 2026-04-29 (~6.4 years; was 2025-06-20 → 2026-04-27)
- NDX100 : 2022-10-20 → 2026-04-29 (~3.5 years; was 2025-06-20 → 2026-04-27)
- SPX500 : 2022-10-20 → 2026-04-29 (~3.5 years; was 2024-11-26 → 2026-04-27)

Six (source, instrument) jobs are dispatched via
``ProcessPoolExecutor``; each child resolves its own fixture dir and
runs all dates sequentially. With ``--max-parallel 4`` the wall time
is dominated by the longest cell.

Pytest-checkable: this module imports cleanly (no side effects at
import time) and exposes ``OVERLAP_WINDOWS`` for tests.

Usage::

    nohup python calibration/run_mt5_vs_databento_tick.py \\
        --output-dir calibration/runs/mt5_vs_databento_tick_<TS>/ \\
        --max-parallel 4 \\
        > nohup.out 2>&1 &
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Overlap windows post timezone-fix MT5 fixtures (commit f868793,
# 1500-day depth across instruments). MT5 is the limiting source on
# the lower end (XAU 2019-12, NDX/SPX 2022-10); DBN reaches back to
# 2016 on all three. Upper end is 2026-04-29 (DBN bound).
OVERLAP_WINDOWS: dict[str, tuple[str, str]] = {
    "XAUUSD": ("2019-12-23", "2026-04-29"),
    "NDX100": ("2022-10-20", "2026-04-29"),
    "SPX500": ("2022-10-20", "2026-04-29"),
}

SOURCES: dict[str, Path] = {
    "mt5": _REPO_ROOT / "tests" / "fixtures" / "historical",
    "dbn": _REPO_ROOT / "tests" / "fixtures" / "historical_extended" / "processed_adjusted",
}

_WORKER_SCRIPT = _REPO_ROOT / "calibration" / "_run_mt5_vs_databento_tick_worker.py"


def _run_cell(args_tuple: tuple) -> dict:
    """One (source, instrument) job. Spawns a worker subprocess so the
    fixture-dir env var is genuinely isolated per cell, and so a crash
    in one cell does not poison the parent process.
    """
    source, instrument, start, end, output_dir, n_dates, seed = args_tuple
    out_dir = Path(output_dir)
    log_path = out_dir / f"{source}_{instrument}.log"
    fixture_dir = SOURCES[source]
    cmd = [
        sys.executable,
        str(_WORKER_SCRIPT),
        "--source",
        source,
        "--instrument",
        instrument,
        "--start",
        start,
        "--end",
        end,
        "--output-dir",
        str(out_dir),
        "--seed",
        str(seed),
    ]
    if n_dates is not None:
        cmd.extend(["--n-dates", str(n_dates)])
    env = {**os.environ, "TTI_FIXTURE_DIR": str(fixture_dir)}
    t0 = time.time()
    with open(log_path, "w") as logf:
        logf.write(f"# {source} × {instrument} started {datetime.now(UTC).isoformat()}\n")
        logf.write(f"# fixture_dir: {fixture_dir}\n")
        logf.write(f"# cmd: {' '.join(cmd)}\n")
        logf.flush()
        proc = subprocess.run(
            cmd, stdout=logf, stderr=subprocess.STDOUT, cwd=_REPO_ROOT, env=env
        )
    elapsed = time.time() - t0
    tail = ""
    try:
        with open(log_path) as f:
            tail = "".join(f.readlines()[-20:])
    except Exception:
        pass
    return {
        "source": source,
        "instrument": instrument,
        "returncode": proc.returncode,
        "elapsed_seconds": elapsed,
        "log_path": str(log_path),
        "stdout_tail": tail,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--instruments",
        default="XAUUSD,NDX100,SPX500",
        help="Comma-separated; only listed instruments are run.",
    )
    parser.add_argument(
        "--sources",
        default="mt5,dbn",
        help="Comma-separated; subset of {mt5,dbn}.",
    )
    parser.add_argument(
        "--n-dates",
        type=int,
        default=None,
        help="If set, sample N dates per cell (seed=42). Default = full window.",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-parallel", type=int, default=4)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    instruments = [s.strip() for s in args.instruments.split(",") if s.strip()]
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    for s in sources:
        if s not in SOURCES:
            raise SystemExit(f"unknown source {s!r}; expected one of {sorted(SOURCES.keys())}")
    for inst in instruments:
        if inst not in OVERLAP_WINDOWS:
            raise SystemExit(
                f"no overlap window known for {inst!r}; expected one of {sorted(OVERLAP_WINDOWS)}"
            )

    started = datetime.now(UTC).isoformat()
    print(f"=== mt5_vs_databento_tick started {started} ===", flush=True)
    print(f"  output_dir : {output_dir}", flush=True)
    print(f"  sources    : {sources}", flush=True)
    print(f"  instruments: {instruments}", flush=True)
    print(f"  n_dates    : {args.n_dates}", flush=True)
    print(f"  parallel   : {args.max_parallel}", flush=True)
    job_args = [
        (
            src,
            inst,
            *OVERLAP_WINDOWS[inst],
            str(output_dir),
            args.n_dates,
            args.seed,
        )
        for src in sources
        for inst in instruments
    ]
    print(f"  total jobs : {len(job_args)}", flush=True)

    runs: list[dict] = []
    with ProcessPoolExecutor(max_workers=args.max_parallel) as pool:
        future_to_label = {pool.submit(_run_cell, ja): f"{ja[0]}×{ja[1]}" for ja in job_args}
        for future in as_completed(future_to_label):
            label = future_to_label[future]
            try:
                run = future.result()
            except Exception as exc:
                src_, inst_ = label.split("×", 1)
                run = {
                    "source": src_,
                    "instrument": inst_,
                    "returncode": -1,
                    "elapsed_seconds": 0.0,
                    "log_path": "",
                    "stdout_tail": str(exc),
                }
            runs.append(run)
            print(
                f"  {run['source']:4s} × {run['instrument']:7s} done "
                f"rc={run['returncode']} elapsed={run['elapsed_seconds']/60:.1f}min",
                flush=True,
            )

    summary = {
        "started_at": started,
        "ended_at": datetime.now(UTC).isoformat(),
        "args": vars(args),
        "overlap_windows": OVERLAP_WINDOWS,
        "runs": runs,
    }
    (output_dir / "run_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(f"=== summary → {output_dir / 'run_summary.json'} ===", flush=True)
    return 0 if all(r["returncode"] == 0 for r in runs) else 2


if __name__ == "__main__":
    raise SystemExit(main())
