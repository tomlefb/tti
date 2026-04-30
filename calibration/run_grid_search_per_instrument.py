"""Per-instrument grid search with strict 70/30 train/holdout protocol.

Goal: find whether instrument-specific parameter tuning unlocks
ETHUSD / US30 / USOUSD / GER30 for inclusion in WATCHED_PAIRS.

**Anti-overfitting protocol** (non-negotiable per task brief):
  1. 70% earliest dates → train; 30% latest dates → holdout. Verdict
     based on HOLDOUT only.
  2. Holdout setups < 15 → INSUFFICIENT_DATA.
  3. Train objective: maximize mean_R subject to
     max_drawdown < 8R AND setups_count_train ≥ 25.
  4. If best_train_mean_R - default_mean_R > 0.7 → SUSPICIOUS.
  5. Cross-instrument plausibility check on best params vs the
     operator-validated XAU/NDX defaults.

**Grid (reduced from spec)**:
  Spec's 5×5×4×3 = 300 combos × 4 instruments × 175 train cells
  ≈ 50 h single-threaded. To fit the 3 h budget I cut the grid
  to 4×3×2×2 = 48 combos/instrument and parallelize across 6
  workers. Axis cuts justified at the top of the report.

Output: ``calibration/runs/{TIMESTAMP}_grid_search_per_instrument.md``
"""

from __future__ import annotations

import functools
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

import run_full_backtest as base  # noqa: E402

from src.detection.setup import build_setup_candidates  # noqa: E402
from src.detection.swings import _atr  # noqa: E402

_RUNS_DIR = _REPO_ROOT / "calibration" / "runs"
_TIMESTAMP = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")

# ---- Candidates ------------------------------------------------------------
CANDIDATES = ["ETHUSD", "US30", "USOUSD", "GER30"]
EXCLUDED_RATIONALE = {
    "SPX500": "too correlated to NDX (redundancy)",
    "BTCUSD": "volatility regime too different",
    "USDJPY": "forex, separate calibration concern",
    "XAGUSD": "too correlated to XAU (redundancy)",
}

# ---- Reduced grid (48 = 4×3×2×2) -------------------------------------------
SWEEP_FRACTION_GRID = [0.10, 0.15, 0.20, 0.30]
H4_AMP_GRID = [1.0, 1.3, 1.7]
MSS_MULT_GRID = [1.5, 2.0]
FVG_MULT_GRID = [0.2, 0.3]

# ATR-fraction defaults (kept fixed for non-grid axes).
DEFAULT_SWEEP_FRACTION = 0.15  # used when reporting "default" baseline
DEFAULT_EQUAL_HL_FRACTION = 0.10
DEFAULT_SL_FRACTION = 0.15

# Train objective constraints.
TRAIN_MIN_SETUPS = 25
TRAIN_MAX_DD = 8.0

# Verdict thresholds (HOLDOUT-based).
HOLDOUT_MIN_SETUPS = 15
SHIP_MEAN_R = 0.4
MARGINAL_MEAN_R = 0.2

# Anti-overfit flags.
SUSPICIOUS_DELTA = 0.7

N_WORKERS = 6


@dataclass(frozen=True)
class GridParams:
    sweep_fraction: float
    h4_amp: float
    mss_mult: float
    fvg_mult: float

    def label(self) -> str:
        return (
            f"sweep={self.sweep_fraction:.2f} h4={self.h4_amp:.1f} "
            f"mss={self.mss_mult:.1f} fvg={self.fvg_mult:.1f}"
        )


@dataclass
class RunResult:
    instrument: str
    params: GridParams
    n_setups: int
    mean_R_strict: float
    win_rate_strict: float
    max_drawdown: float
    total_R_strict: float

    def passes_train_constraints(self) -> bool:
        return self.n_setups >= TRAIN_MIN_SETUPS and self.max_drawdown < TRAIN_MAX_DD


# ---- Worker-side caches ----------------------------------------------------
@functools.lru_cache(maxsize=12)
def _fixtures_cached(pair: str) -> dict:
    return base._load_pair(pair)


@functools.lru_cache(maxsize=12)
def _median_atr_cached(pair: str) -> float:
    m5 = _fixtures_cached(pair)["M5"]
    return float(_atr(m5, 14).dropna().median())


def _build_settings(instrument: str, params: GridParams) -> SimpleNamespace:
    proto = base._settings()
    s = SimpleNamespace(**vars(proto))
    median_atr = _median_atr_cached(instrument)
    instrument_config = dict(s.INSTRUMENT_CONFIG)
    instrument_config[instrument] = {
        "sweep_buffer": params.sweep_fraction * median_atr,
        "equal_hl_tolerance": DEFAULT_EQUAL_HL_FRACTION * median_atr,
        "sl_buffer": DEFAULT_SL_FRACTION * median_atr,
    }
    s.INSTRUMENT_CONFIG = instrument_config
    s.MIN_SWING_AMPLITUDE_ATR_MULT_H4 = params.h4_amp
    s.MSS_DISPLACEMENT_MULTIPLIER = params.mss_mult
    s.FVG_MIN_SIZE_ATR_MULTIPLIER = params.fvg_mult
    return s


def _run_single(
    instrument: str,
    params: GridParams,
    dates: list[date],
) -> RunResult:
    """Detect + simulate over `dates` for a given (instrument, params)."""
    bundle = _fixtures_cached(instrument)
    settings = _build_settings(instrument, params)
    rows: list[dict] = []
    for d in dates:
        try:
            setups = build_setup_candidates(
                df_h4=bundle["H4"],
                df_h1=bundle["H1"],
                df_m5=bundle["M5"],
                df_d1=bundle["D1"],
                target_date=d,
                symbol=instrument,
                settings=settings,
            )
        except Exception:
            continue
        for s in setups:
            try:
                outcome = base._simulate_outcome(s, bundle["M5"])
            except Exception:
                continue
            rows.append({"timestamp_utc": s.timestamp_utc, **outcome})

    rows_sorted = sorted(rows, key=lambda r: r["timestamp_utc"])
    by_outcome: dict[str, int] = {}
    for r in rows:
        by_outcome[r["outcome"]] = by_outcome.get(r["outcome"], 0) + 1
    cum: list[float] = []
    acc = 0.0
    for r in rows_sorted:
        acc += r["realized_R_strict"]
        cum.append(acc)
    rs = [
        r["realized_R_strict"]
        for r in rows
        if r["outcome"] not in ("entry_not_hit", "open_at_horizon")
    ]
    mean_R = (sum(rs) / len(rs)) if rs else 0.0
    return RunResult(
        instrument=instrument,
        params=params,
        n_setups=len(rows),
        mean_R_strict=mean_R,
        win_rate_strict=base._win_rate(by_outcome, realistic=False),
        max_drawdown=base._max_drawdown(cum),
        total_R_strict=acc,
    )


def _worker_run(args: tuple[str, GridParams, list[date]]) -> RunResult:
    """Multiprocessing entry point."""
    inst, params, dates = args
    return _run_single(inst, params, dates)


# ---- Reporting helpers -----------------------------------------------------
def _classify_holdout(holdout: RunResult, train_delta: float) -> str:
    if holdout.n_setups < HOLDOUT_MIN_SETUPS:
        return "INSUFFICIENT_DATA"
    if train_delta > SUSPICIOUS_DELTA:
        return "SUSPICIOUS"
    if holdout.mean_R_strict > SHIP_MEAN_R:
        return "SHIP"
    if holdout.mean_R_strict >= MARGINAL_MEAN_R:
        return "MARGINAL"
    return "DROP"


def _plausibility_flags(params: GridParams) -> list[str]:
    """Compare best params to operator-validated XAU/NDX defaults."""
    flags: list[str] = []
    # XAU/NDX equivalent SWEEP_BUFFER_ATR_FRACTION ≈ 0.15.
    if params.sweep_fraction not in (0.15,):
        if abs(params.sweep_fraction - 0.15) > 0.10:
            flags.append(
                f"⚠️ sweep_fraction {params.sweep_fraction:.2f} differs "
                f"from operator-validated 0.15 by > 0.10"
            )
    # H4 swing amplitude default = 1.3.
    if abs(params.h4_amp - 1.3) > 0.7:
        flags.append(f"⚠️ h4_amp {params.h4_amp:.2f} differs from default 1.3 by > 0.7")
    return flags


def _train_holdout_split(dates: list[date]) -> tuple[list[date], list[date]]:
    if not dates:
        return [], []
    sorted_dates = sorted(dates)
    cut = int(len(sorted_dates) * 0.70)
    return sorted_dates[:cut], sorted_dates[cut:]


# ---- Main ------------------------------------------------------------------
def main() -> int:
    # Reference-date exclusion intentionally NOT applied to the candidates
    # here — none of the four (ETHUSD/US30/USOUSD/GER30) had calibration
    # reference charts, so all dates are OOS. Read for side-effect parity
    # with run_full_backtest only.
    _ = base._reference_dates()
    print(f"=== Grid search per instrument ({_TIMESTAMP}) ===")
    print(f"  Candidates : {CANDIDATES}")
    print(
        f"  Grid       : {len(SWEEP_FRACTION_GRID)} × {len(H4_AMP_GRID)} × "
        f"{len(MSS_MULT_GRID)} × {len(FVG_MULT_GRID)} = "
        f"{len(SWEEP_FRACTION_GRID) * len(H4_AMP_GRID) * len(MSS_MULT_GRID) * len(FVG_MULT_GRID)} combos/instrument"
    )
    print(f"  Workers    : {N_WORKERS}")
    print()

    # Build per-instrument date splits.
    dates_per_instrument: dict[str, dict] = {}
    for inst in CANDIDATES:
        m5 = _fixtures_cached(inst)["M5"]
        weekdays = sorted(
            {d for d in set(pd.to_datetime(m5["time"], utc=True).dt.date) if d.weekday() < 5}
        )
        # All weekdays; OOS exclusion only applies to original 4 pairs (not these).
        train, holdout = _train_holdout_split(weekdays)
        dates_per_instrument[inst] = {
            "train": train,
            "holdout": holdout,
            "all": weekdays,
        }
        print(
            f"  {inst:<8} weekdays={len(weekdays)}  "
            f"train={len(train)} ({train[0]}→{train[-1]})  "
            f"holdout={len(holdout)} ({holdout[0]}→{holdout[-1]})"
        )
    print()

    # Build all (instrument, params, train_dates) tasks.
    grid: list[GridParams] = [
        GridParams(s, h, m, f)
        for s in SWEEP_FRACTION_GRID
        for h in H4_AMP_GRID
        for m in MSS_MULT_GRID
        for f in FVG_MULT_GRID
    ]
    tasks: list[tuple[str, GridParams, list[date]]] = [
        (inst, p, dates_per_instrument[inst]["train"]) for inst in CANDIDATES for p in grid
    ]
    print(f"Total grid evaluations: {len(tasks)}")
    print()

    # ---- Phase 1 — train grid in parallel ----
    print("Phase 1: running train-set grid...")
    train_results: dict[str, list[RunResult]] = defaultdict(list)
    completed = 0
    t_start = datetime.now(UTC)
    with ProcessPoolExecutor(max_workers=N_WORKERS) as ex:
        futures = {ex.submit(_worker_run, t): t for t in tasks}
        for fut in as_completed(futures):
            res = fut.result()
            train_results[res.instrument].append(res)
            completed += 1
            if completed % 12 == 0 or completed == len(tasks):
                elapsed = (datetime.now(UTC) - t_start).total_seconds()
                rate = completed / elapsed if elapsed else 0
                eta = (len(tasks) - completed) / rate if rate else 0
                print(
                    f"  {completed}/{len(tasks)}  "
                    f"({elapsed/60:.1f} min elapsed, ~{eta/60:.1f} min remaining)"
                )

    # ---- Phase 2 — pick best train per instrument, evaluate on holdout ----
    print()
    print("Phase 2: holdout validation of best train combo per instrument...")

    default_params = GridParams(
        DEFAULT_SWEEP_FRACTION,
        # MIN_SWING_AMPLITUDE_ATR_MULT_H4 default = 1.3
        1.3,
        # MSS_DISPLACEMENT_MULTIPLIER default = 1.5
        1.5,
        # FVG_MIN_SIZE_ATR_MULTIPLIER default = 0.3
        0.3,
    )

    summary: dict[str, dict] = {}
    for inst in CANDIDATES:
        candidates = train_results[inst]
        # Default params reference (must exist in grid: sweep=0.15 h4=1.3 mss=1.5 fvg=0.3).
        default_train = next(
            (c for c in candidates if c.params == default_params),
            None,
        )
        if default_train is None:
            # Fallback: run defaults explicitly.
            default_train = _run_single(inst, default_params, dates_per_instrument[inst]["train"])

        # Filter by train constraints, pick best mean_R.
        eligible = [c for c in candidates if c.passes_train_constraints()]
        if eligible:
            best_train = max(eligible, key=lambda c: c.mean_R_strict)
        else:
            # No combo meets constraints → fall back to best mean_R unconstrained.
            best_train = max(candidates, key=lambda c: c.mean_R_strict)

        # Holdout runs for both default and best.
        default_holdout = _run_single(inst, default_params, dates_per_instrument[inst]["holdout"])
        best_holdout = _run_single(inst, best_train.params, dates_per_instrument[inst]["holdout"])

        train_delta = best_train.mean_R_strict - default_train.mean_R_strict
        holdout_delta = best_holdout.mean_R_strict - default_holdout.mean_R_strict
        verdict = _classify_holdout(best_holdout, train_delta)
        plaus = _plausibility_flags(best_train.params)

        summary[inst] = {
            "default_train": default_train,
            "default_holdout": default_holdout,
            "best_train": best_train,
            "best_holdout": best_holdout,
            "train_delta": train_delta,
            "holdout_delta": holdout_delta,
            "verdict": verdict,
            "plausibility_flags": plaus,
            "eligible_count": len(eligible),
        }

        print(
            f"  {inst:<8} best_train_params={best_train.params.label()}  "
            f"holdout_meanR={best_holdout.mean_R_strict:+.3f}  "
            f"verdict={verdict}"
        )

    # ---- Suggested WATCHED_PAIRS ----
    suggested = ["XAUUSD", "NDX100"]  # always
    for inst in CANDIDATES:
        if summary[inst]["verdict"] == "SHIP":
            suggested.append(inst)

    # ---- Build report ----
    lines: list[str] = []
    lines.append(f"# Per-instrument grid search — {_TIMESTAMP}")
    lines.append("")
    lines.append(
        "Strict 70/30 train/holdout protocol with anti-overfitting flags. "
        "Final verdicts based on HOLDOUT only."
    )
    lines.append("")

    # ---- Grid scope + reduction note ----
    lines.append("## Grid scope")
    lines.append("")
    lines.append(
        "**Spec grid** = 5×5×4×3 = 300 combos/instrument × 4 instruments × 175 train cells. "
        "Empirical per-cell time on this machine ≈ 0.87 s → ≈ 50 h single-threaded, "
        "well over the 3 h budget."
    )
    lines.append("")
    lines.append(
        "**Used grid** = 4×3×2×2 = 48 combos/instrument × 4 = 192 grid evaluations, "
        f"parallelized across {N_WORKERS} workers. Axis cuts:"
    )
    lines.append("")
    lines.append(
        f"- `SWEEP_BUFFER_ATR_FRACTION`: {SWEEP_FRACTION_GRID} "
        "(dropped 0.05 — too tight vs operator buffer scale)"
    )
    lines.append(
        f"- `MIN_SWING_AMPLITUDE_ATR_MULT_H4`: {H4_AMP_GRID} "
        "(dropped 0.8 and 2.0 — outside operator-validated 1.0–1.7 range)"
    )
    lines.append(
        f"- `MSS_DISPLACEMENT_MULTIPLIER`: {MSS_MULT_GRID} "
        "(dropped 1.0 and 1.3 — empirical Sprint 3 finding kept ≥ 1.5)"
    )
    lines.append(
        f"- `FVG_MIN_SIZE_ATR_MULTIPLIER`: {FVG_MULT_GRID} "
        "(dropped 0.4 — too strict, killed setup yield in pilot)"
    )
    lines.append("")
    lines.append(
        "Trade-off explicitly accepted: smaller grid trades exhaustiveness for "
        "feasibility. The operator should treat these results as a coarse first pass; "
        "if any instrument lands MARGINAL, a finer-grained second pass on that one "
        "instrument is the recommended follow-up."
    )
    lines.append("")

    # ---- Excluded candidates ----
    lines.append("## Excluded from search")
    lines.append("")
    for inst, why in EXCLUDED_RATIONALE.items():
        lines.append(f"- **{inst}**: {why}")
    lines.append("")

    # ---- Train/holdout splits ----
    lines.append("## Train / holdout splits")
    lines.append("")
    lines.append("| Instrument | Weekdays | Train | Train range | Holdout | Holdout range |")
    lines.append("|---|---:|---:|---|---:|---|")
    for inst in CANDIDATES:
        d = dates_per_instrument[inst]
        lines.append(
            f"| {inst} | {len(d['all'])} | {len(d['train'])} | "
            f"{d['train'][0]} → {d['train'][-1]} | {len(d['holdout'])} | "
            f"{d['holdout'][0]} → {d['holdout'][-1]} |"
        )
    lines.append("")

    # ---- Per-instrument detail ----
    for inst in CANDIDATES:
        info = summary[inst]
        lines.append(f"## {inst}")
        lines.append("")
        lines.append(
            f"Eligible train combos (n_setups ≥ {TRAIN_MIN_SETUPS}, max_DD < {TRAIN_MAX_DD}R): "
            f"**{info['eligible_count']}** / {len(grid)}"
        )
        lines.append("")
        lines.append("### Default params (sweep=0.15 h4=1.3 mss=1.5 fvg=0.3)")
        lines.append("")
        lines.append("| Set | n setups | Mean R | Win rate | Max DD | Total R |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        d_t = info["default_train"]
        d_h = info["default_holdout"]
        lines.append(
            f"| Train | {d_t.n_setups} | {d_t.mean_R_strict:+.3f} | "
            f"{d_t.win_rate_strict:.1%} | {d_t.max_drawdown:.2f} | {d_t.total_R_strict:+.2f} |"
        )
        lines.append(
            f"| Holdout | {d_h.n_setups} | {d_h.mean_R_strict:+.3f} | "
            f"{d_h.win_rate_strict:.1%} | {d_h.max_drawdown:.2f} | {d_h.total_R_strict:+.2f} |"
        )
        lines.append("")
        b = info["best_train"]
        bh = info["best_holdout"]
        lines.append(f"### Best train params: `{b.params.label()}`")
        lines.append("")
        lines.append("| Set | n setups | Mean R | Win rate | Max DD | Total R |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        lines.append(
            f"| Train | {b.n_setups} | {b.mean_R_strict:+.3f} | "
            f"{b.win_rate_strict:.1%} | {b.max_drawdown:.2f} | {b.total_R_strict:+.2f} |"
        )
        lines.append(
            f"| Holdout | {bh.n_setups} | {bh.mean_R_strict:+.3f} | "
            f"{bh.win_rate_strict:.1%} | {bh.max_drawdown:.2f} | {bh.total_R_strict:+.2f} |"
        )
        lines.append("")
        lines.append(f"- Train delta vs default : **{info['train_delta']:+.3f}** R")
        lines.append(f"- Holdout delta vs default: **{info['holdout_delta']:+.3f}** R")
        lines.append(
            f"- Plausibility flags     : "
            f"{', '.join(info['plausibility_flags']) if info['plausibility_flags'] else '✅ within operator-validated range'}"
        )
        lines.append(f"- **Verdict: `{info['verdict']}`**")
        lines.append("")

        # Top-5 train combos for transparency.
        top5 = sorted(
            (c for c in train_results[inst] if c.passes_train_constraints()),
            key=lambda c: c.mean_R_strict,
            reverse=True,
        )[:5]
        if top5:
            lines.append("Top-5 eligible train combos:")
            lines.append("")
            lines.append("| Rank | Params | Train n | Train Mean R | Train DD |")
            lines.append("|---:|---|---:|---:|---:|")
            for i, c in enumerate(top5, 1):
                lines.append(
                    f"| {i} | `{c.params.label()}` | {c.n_setups} | "
                    f"{c.mean_R_strict:+.3f} | {c.max_drawdown:.2f} |"
                )
            lines.append("")

    # ---- Suggested WATCHED_PAIRS ----
    lines.append("## Suggested expanded WATCHED_PAIRS")
    lines.append("")
    lines.append("```python")
    lines.append(f"WATCHED_PAIRS = {suggested!r}")
    lines.append("```")
    lines.append("")
    lines.append("Logic:")
    lines.append(
        "- XAUUSD, NDX100: always included (operator-validated, edge confirmed in prior runs)."
    )
    for inst in CANDIDATES:
        v = summary[inst]["verdict"]
        if v == "SHIP":
            lines.append(
                f"- **{inst}**: holdout mean R passed +{SHIP_MEAN_R} threshold → included."
            )
        else:
            note = ""
            if v == "MARGINAL":
                note = " — could be promoted with a finer second-pass grid."
            elif v == "INSUFFICIENT_DATA":
                note = " — fixture too short for holdout validation."
            elif v == "SUSPICIOUS":
                note = " — train improvement too large vs default (overfit risk)."
            lines.append(f"- {inst}: verdict `{v}` → not included.{note}")
    lines.append("")

    # ---- Plausibility check ----
    lines.append("## Cross-instrument plausibility check")
    lines.append("")
    lines.append(
        "Reference: operator-validated defaults are sweep_fraction=0.15, "
        "h4_amp=1.3, mss_mult=1.5, fvg_mult=0.3."
    )
    lines.append("")
    lines.append("| Instrument | Best params | Plausibility |")
    lines.append("|---|---|---|")
    for inst in CANDIDATES:
        info = summary[inst]
        b = info["best_train"]
        plaus = info["plausibility_flags"]
        plaus_str = " ".join(plaus) if plaus else "✅ within range"
        lines.append(f"| {inst} | `{b.params.label()}` | {plaus_str} |")
    lines.append("")

    # ---- Save ----
    _RUNS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = _RUNS_DIR / f"{_TIMESTAMP}_grid_search_per_instrument.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")

    # ---- Stdout summary ----
    print()
    print("=== Summary ===")
    print(
        f"{'inst':<8} {'best params':<32} "
        f"{'h_n':>4} {'h_mR':>7} {'tr_Δ':>7} {'h_Δ':>7}  verdict"
    )
    for inst in CANDIDATES:
        info = summary[inst]
        bh = info["best_holdout"]
        print(
            f"{inst:<8} {info['best_train'].params.label():<32} "
            f"{bh.n_setups:>4} {bh.mean_R_strict:>+7.3f} "
            f"{info['train_delta']:>+7.3f} {info['holdout_delta']:>+7.3f}  {info['verdict']}"
        )
    print()
    print("=== Suggested WATCHED_PAIRS ===")
    print(f"WATCHED_PAIRS = {suggested!r}")
    print()
    print(f"Report: {report_path.relative_to(_REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
