"""Run the TJR baseline under multiple parameter variants in
parallel and produce a comparison report.

Variants (config snapshots, not ``settings.py`` edits) are defined in
``calibration.baseline_tjr_databento.VARIANTS``:

- ``baseline``           — current operator-validated settings.
- ``swing_lookback_3``   — N-bar fractal lookback bumped from 2 → 3 on every TF.
- ``swing_lookback_5``   — same to 5.
- ``amp_atr_low``        — min_swing_amplitude_atr_mult dropped from 1.0/1.3 → 0.3.
- ``amp_atr_high``       — bumped to 0.8.
- ``quality_relaxed``    — NOTIFY_QUALITIES = (A+, A, B); detection unchanged.
- ``mss_disp_low``       — MSS_DISPLACEMENT_MULTIPLIER dropped 1.5 → 1.2.
- ``mss_disp_high``      — bumped to 1.8.

The runner spawns one subprocess per variant (each subprocess runs
all instruments sequentially) and caps concurrency at
``--max-parallel`` (default 4). The 8-variant default sweep with 50
dates per instrument fits in ~6-7 h on a 4-way machine; reduce
``--n-dates`` or the variant list if a shorter window is required.

After every variant has produced its per-instrument
``BacktestResult`` JSON, the runner reads them all and renders
``comparison.md`` under the same output dir.

Usage::

    python calibration/baseline_tjr_variants.py \\
        --instruments XAUUSD,NDX100,SPX500 \\
        --start 2016-01-03 \\
        --end 2026-04-29 \\
        --output-dir calibration/runs/variants_<TS>/ \\
        --n-dates 50 \\
        --max-parallel 4
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Re-export the variant registry for convenience.
from calibration.baseline_tjr_databento import VARIANTS  # noqa: E402
from src.backtest.result import BacktestResult  # noqa: E402

_BASELINE_SCRIPT = _REPO_ROOT / "calibration" / "baseline_tjr_databento.py"


# ---------------------------------------------------------------------------
# Subprocess driver — one variant per call.
# ---------------------------------------------------------------------------
def _run_variant(args_tuple: tuple) -> dict:
    """Spawn a baseline_tjr_databento.py invocation for one variant.

    Returns ``{variant, returncode, elapsed_seconds, stdout_tail, stderr_tail}``.
    """
    variant, instruments, start, end, output_dir, n_dates, seed = args_tuple
    variant_dir = Path(output_dir) / f"variant_{variant}"
    variant_dir.mkdir(parents=True, exist_ok=True)
    log_path = variant_dir / f"{variant}.log"

    cmd = [
        sys.executable,
        str(_BASELINE_SCRIPT),
        "--instruments",
        instruments,
        "--start",
        start,
        "--end",
        end,
        "--variant",
        variant,
        "--output-dir",
        str(variant_dir),
        "--seed",
        str(seed),
    ]
    if n_dates is not None:
        cmd.extend(["--n-dates", str(n_dates)])

    t0 = time.time()
    with open(log_path, "w") as logf:
        logf.write(f"# {variant} started at {datetime.now(UTC).isoformat()}\n")
        logf.write(f"# cmd: {' '.join(cmd)}\n")
        logf.flush()
        proc = subprocess.run(
            cmd,
            stdout=logf,
            stderr=subprocess.STDOUT,
            cwd=_REPO_ROOT,
        )
    elapsed = time.time() - t0
    tail = ""
    try:
        with open(log_path) as f:
            lines = f.readlines()
        tail = "".join(lines[-20:])
    except Exception:  # pragma: no cover
        pass
    return {
        "variant": variant,
        "returncode": proc.returncode,
        "elapsed_seconds": elapsed,
        "log_path": str(log_path),
        "stdout_tail": tail,
    }


# ---------------------------------------------------------------------------
# Comparison report.
# ---------------------------------------------------------------------------
def _load_results(output_dir: Path) -> dict[str, dict[str, BacktestResult]]:
    """Return ``{variant_name: {instrument: BacktestResult}}``."""
    out: dict[str, dict[str, BacktestResult]] = {}
    for variant_path in sorted(output_dir.glob("variant_*")):
        variant = variant_path.name.removeprefix("variant_")
        per_inst: dict[str, BacktestResult] = {}
        for json_path in sorted(variant_path.glob(f"baseline_{variant}_*.json")):
            instrument = json_path.stem.removeprefix(f"baseline_{variant}_")
            per_inst[instrument] = BacktestResult.from_json(json_path)
        if per_inst:
            out[variant] = per_inst
    return out


def _render_comparison(
    output_dir: Path,
    results: dict[str, dict[str, BacktestResult]],
    sweep_args: argparse.Namespace,
    variant_runs: list[dict],
) -> Path:
    lines: list[str] = []
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    lines.append(f"# TJR variants sweep — comparison — {ts}")
    lines.append("")
    lines.append(
        f"Sample: instruments {sweep_args.instruments}, period "
        f"{sweep_args.start} → {sweep_args.end}, "
        f"n_dates={sweep_args.n_dates if sweep_args.n_dates is not None else 'all'} "
        f"per instrument, seed={sweep_args.seed}, max_parallel={sweep_args.max_parallel}, "
        f"detection backend = leak-free tick simulator (Phase B)."
    )
    lines.append("")

    lines.append("## Wall-clock per variant")
    lines.append("")
    lines.append("| Variant | Returncode | Elapsed (min) |")
    lines.append("|---|---:|---:|")
    for run in sorted(variant_runs, key=lambda r: r["variant"]):
        lines.append(
            f"| {run['variant']} | {run['returncode']} | {run['elapsed_seconds']/60:.1f} |"
        )
    lines.append("")

    if not results:
        lines.append("No per-instrument JSONs found — every variant subprocess failed.")
        path = output_dir / "comparison.md"
        path.write_text("\n".join(lines) + "\n")
        return path

    instruments = sorted({inst for per in results.values() for inst in per})

    lines.append("## Headline (per variant per instrument)")
    lines.append("")
    lines.append(
        "| Variant | Instrument | n setups | n closed | mean R | "
        "CI 95% | win rate | setups/mo | frac pos sem | max DD |"
    )
    lines.append("|---|---|---:|---:|---:|---|---:|---:|---:|---:|")
    for variant in sorted(results.keys()):
        for inst in instruments:
            r = results[variant].get(inst)
            if r is None:
                lines.append(f"| {variant} | {inst} | — | — | — | — | — | — | — | — |")
                continue
            ci = f"[{r.mean_r_ci_95[0]:+.3f}, {r.mean_r_ci_95[1]:+.3f}]"
            n_closed = sum(
                1 for s in r.setups if s.outcome not in ("entry_not_hit", "open_at_horizon")
            )
            lines.append(
                f"| {variant} | {inst} | {r.n_setups} | {n_closed} | "
                f"{r.mean_r:+.3f} | {ci} | {r.win_rate:.1%} | "
                f"{r.setups_per_month:.2f} | {r.fraction_positive_semesters:.2f} | "
                f"{r.max_dd_r:.2f} |"
            )
    lines.append("")

    lines.append("## Edge candidates — CI lower bound > 0")
    lines.append("")
    edge_rows = []
    for variant in sorted(results.keys()):
        for inst in instruments:
            r = results[variant].get(inst)
            if r is None or r.n_setups == 0:
                continue
            if r.mean_r_ci_95[0] > 0:
                edge_rows.append((variant, inst, r))
    if edge_rows:
        lines.append(
            "These (variant, instrument) combos produce a 95% bootstrap CI on "
            "mean R that is strictly above zero — the strongest evidence of a "
            "real edge in this sweep."
        )
        lines.append("")
        lines.append("| Variant | Instrument | mean R | CI 95% | n closed |")
        lines.append("|---|---|---:|---|---:|")
        for variant, inst, r in edge_rows:
            n_closed = sum(
                1 for s in r.setups if s.outcome not in ("entry_not_hit", "open_at_horizon")
            )
            ci = f"[{r.mean_r_ci_95[0]:+.3f}, {r.mean_r_ci_95[1]:+.3f}]"
            lines.append(f"| {variant} | {inst} | {r.mean_r:+.3f} | {ci} | {n_closed} |")
    else:
        lines.append(
            "No (variant, instrument) combo produced a 95% CI strictly above zero "
            "on this sample — the bootstrap lower bound is at or below zero in "
            "every cell. Either the sample is too small to resolve a positive "
            "edge or the strategy as parameterised does not have one in the "
            "covered window."
        )
    lines.append("")

    if "baseline" in results:
        lines.append("## Welch test vs baseline (per instrument)")
        lines.append("")
        lines.append(
            "Each non-baseline variant's R distribution is contrasted with the "
            "baseline's via Welch's t-test (closed trades only). The bootstrap "
            "delta CI is the 95% percentile-method CI on the difference of "
            "mean R."
        )
        lines.append("")
        lines.append(
            "| Variant | Instrument | delta mean R | delta CI 95% | "
            "p value | n variant | n baseline |"
        )
        lines.append("|---|---|---:|---|---:|---:|---:|")
        for variant in sorted(results.keys()):
            if variant == "baseline":
                continue
            for inst in instruments:
                v = results[variant].get(inst)
                b = results["baseline"].get(inst)
                if v is None or b is None:
                    continue
                d = v.compare(b)
                ci = (
                    f"[{d['delta_ci_95'][0]:+.3f}, {d['delta_ci_95'][1]:+.3f}]"
                    if not _is_nan(d["delta_ci_95"][0])
                    else "—"
                )
                pv = f"{d['p_value']:.3f}" if not _is_nan(d["p_value"]) else "—"
                lines.append(
                    f"| {variant} | {inst} | {d['delta_mean_r']:+.3f} | {ci} | "
                    f"{pv} | {d['n_self']} | {d['n_other']} |"
                )
        lines.append("")

    lines.append("## Files")
    lines.append("")
    for variant in sorted(results.keys()):
        for inst in instruments:
            if inst in results[variant]:
                lines.append(
                    f"- `variant_{variant}/baseline_{variant}_{inst}.json` "
                    f"({results[variant][inst].n_setups} setups)"
                )
    lines.append("")

    path = output_dir / "comparison.md"
    path.write_text("\n".join(lines) + "\n")
    return path


def _is_nan(x: float) -> bool:
    return x != x  # NaN is the only float != itself


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instruments", default="XAUUSD,NDX100,SPX500")
    parser.add_argument("--start", default="2016-01-03")
    parser.add_argument("--end", default="2026-04-29")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--n-dates",
        type=int,
        default=None,
        help="Sample size per instrument (default: all dates in period).",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-parallel", type=int, default=4)
    parser.add_argument(
        "--variants",
        default=",".join(sorted(VARIANTS.keys())),
        help="Comma-separated list of variant names; default = all.",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    selected = [v.strip() for v in args.variants.split(",") if v.strip()]
    for v in selected:
        if v not in VARIANTS:
            raise SystemExit(f"unknown variant {v!r}; expected one of {sorted(VARIANTS.keys())}")

    started_at = datetime.now(UTC).isoformat()
    print(f"=== Variants sweep started at {started_at} ===", flush=True)
    print(f"  output dir : {output_dir}", flush=True)
    print(f"  variants   : {selected}", flush=True)
    print(f"  instruments: {args.instruments}", flush=True)
    print(f"  period     : {args.start} → {args.end}", flush=True)
    print(f"  n_dates    : {args.n_dates}", flush=True)
    print(f"  parallel   : {args.max_parallel}", flush=True)
    sys.stdout.flush()

    job_args = [
        (
            v,
            args.instruments,
            args.start,
            args.end,
            str(output_dir),
            args.n_dates,
            args.seed,
        )
        for v in selected
    ]

    runs: list[dict] = []
    with ProcessPoolExecutor(max_workers=args.max_parallel) as pool:
        future_to_variant = {pool.submit(_run_variant, ja): ja[0] for ja in job_args}
        for future in as_completed(future_to_variant):
            variant = future_to_variant[future]
            try:
                run = future.result()
            except Exception as exc:  # pragma: no cover
                print(f"  variant {variant} raised: {exc!r}", flush=True)
                run = {
                    "variant": variant,
                    "returncode": -1,
                    "elapsed_seconds": 0.0,
                    "log_path": "",
                    "stdout_tail": str(exc),
                }
            runs.append(run)
            print(
                f"  variant {run['variant']:20s} done rc={run['returncode']} "
                f"elapsed={run['elapsed_seconds']/60:.1f}min",
                flush=True,
            )

    summary = {
        "started_at": started_at,
        "ended_at": datetime.now(UTC).isoformat(),
        "args": vars(args),
        "runs": runs,
    }
    (output_dir / "sweep_summary.json").write_text(json.dumps(summary, indent=2, default=str))

    print("Loading per-variant results and rendering comparison ...", flush=True)
    results = _load_results(output_dir)
    comparison_path = _render_comparison(output_dir, results, args, runs)
    print(f"Comparison → {comparison_path}", flush=True)
    print(comparison_path.read_text(), flush=True)
    return 0 if all(r["returncode"] == 0 for r in runs) else 2


if __name__ == "__main__":
    raise SystemExit(main())
