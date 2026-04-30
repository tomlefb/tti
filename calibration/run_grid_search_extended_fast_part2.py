"""Overnight grid search on 4 additional instruments — part 2.

Same protocol as ``run_grid_search_extended_fast.py`` (70/30 chrono
split, train constraints, holdout-only verdict, decoupled
detection/simulation, real-time progress) but on:

  - US30   (17.0 mo)
  - GER30  (25.3 mo)
  - SPX500 (17.1 mo)
  - BTCUSD (16.1 mo)

**ETH lesson** — default params can outperform best-train holdout
when best-train is overfit. This run reports BOTH default and
best-train holdout for every instrument and adds a
``DEFAULT_SHIPS`` flag when the default holdout already passes the
SHIP threshold.

Final suggested ``WATCHED_PAIRS`` combines:
  - XAUUSD, NDX100 (operator-validated baseline).
  - Any of these 4 with verdict SHIP or DEFAULT_SHIPS.
  - ETHUSD (DEFAULT_SHIPS from the previous run — default holdout
    +0.526 / 35 setups passes SHIP threshold).
  - USOUSD (DROP from previous run — default +0.160 / 24 fails).

Output: ``calibration/runs/{TIMESTAMP}_grid_search_extended_fast_part2.md``.
"""

from __future__ import annotations

import functools
import os
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
_PROGRESS_LOG = _RUNS_DIR / "grid_progress.log"

# ---- Scope -----------------------------------------------------------------
INSTRUMENTS = ["US30", "GER30", "SPX500", "BTCUSD"]

# ---- Grid (identical) -------------------------------------------------------
SWEEP_FRACTION_GRID = [0.10, 0.15, 0.20, 0.30]
H4_AMP_GRID = [1.0, 1.3, 1.7]
MSS_MULT_GRID = [1.5, 2.0]
FVG_MULT_GRID = [0.2, 0.3]

DEFAULT_SWEEP_FRACTION = 0.15
DEFAULT_EQUAL_HL_FRACTION = 0.10
DEFAULT_SL_FRACTION = 0.15

TRAIN_MIN_SETUPS = 25
TRAIN_MAX_DD = 8.0
HOLDOUT_MIN_SETUPS = 15
SHIP_MEAN_R = 0.4
MARGINAL_MEAN_R = 0.2
SUSPICIOUS_DELTA = 0.7

N_WORKERS = max(1, (os.cpu_count() or 2) - 1)


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


# ---- Worker side ------------------------------------------------------------
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
    cfg = dict(s.INSTRUMENT_CONFIG)
    cfg[instrument] = {
        "sweep_buffer": params.sweep_fraction * median_atr,
        "equal_hl_tolerance": DEFAULT_EQUAL_HL_FRACTION * median_atr,
        "sl_buffer": DEFAULT_SL_FRACTION * median_atr,
    }
    s.INSTRUMENT_CONFIG = cfg
    s.MIN_SWING_AMPLITUDE_ATR_MULT_H4 = params.h4_amp
    s.MSS_DISPLACEMENT_MULTIPLIER = params.mss_mult
    s.FVG_MIN_SIZE_ATR_MULTIPLIER = params.fvg_mult
    return s


def _setup_to_dict(s) -> dict:
    return {
        "timestamp_utc": s.timestamp_utc,
        "direction": s.direction,
        "entry_price": s.entry_price,
        "stop_loss": s.stop_loss,
        "tp1_price": s.tp1_price,
        "tp_runner_price": s.tp_runner_price,
        "tp1_rr": s.tp1_rr,
        "tp_runner_rr": s.tp_runner_rr,
        "quality": s.quality,
        "killzone": s.killzone,
    }


def _worker_detect(args: tuple[str, GridParams, list[date]]) -> dict:
    inst, params, dates = args
    bundle = _fixtures_cached(inst)
    settings = _build_settings(inst, params)
    setup_dicts: list[dict] = []
    for d in dates:
        try:
            setups = build_setup_candidates(
                df_h4=bundle["H4"],
                df_h1=bundle["H1"],
                df_m5=bundle["M5"],
                df_d1=bundle["D1"],
                target_date=d,
                symbol=inst,
                settings=settings,
            )
        except Exception:
            continue
        for s in setups:
            setup_dicts.append(_setup_to_dict(s))
    return {"instrument": inst, "params": params, "setups": setup_dicts}


# ---- Main side --------------------------------------------------------------
def _train_holdout_split(dates: list[date]) -> tuple[list[date], list[date]]:
    if not dates:
        return [], []
    sd = sorted(dates)
    cut = int(len(sd) * 0.70)
    return sd[:cut], sd[cut:]


def _outcome_cache_key(instrument: str, sd: dict) -> tuple:
    return (
        instrument,
        sd["timestamp_utc"],
        sd["direction"],
        round(sd["entry_price"], 6),
        round(sd["stop_loss"], 6),
        round(sd["tp1_price"], 6),
        round(sd["tp_runner_price"], 6),
    )


def _simulate(sd: dict, df_m5: pd.DataFrame) -> dict:
    fake = SimpleNamespace(
        timestamp_utc=sd["timestamp_utc"],
        direction=sd["direction"],
        entry_price=sd["entry_price"],
        stop_loss=sd["stop_loss"],
        tp1_price=sd["tp1_price"],
        tp_runner_price=sd["tp_runner_price"],
        tp1_rr=sd["tp1_rr"],
        tp_runner_rr=sd["tp_runner_rr"],
    )
    return base._simulate_outcome(fake, df_m5)


def _aggregate_combo(
    instrument: str,
    setup_dicts: list[dict],
    outcome_cache: dict[tuple, dict],
    df_m5: pd.DataFrame,
) -> dict:
    rows: list[dict] = []
    for sd in setup_dicts:
        key = _outcome_cache_key(instrument, sd)
        if key not in outcome_cache:
            outcome_cache[key] = _simulate(sd, df_m5)
        out = outcome_cache[key]
        rows.append({"timestamp_utc": sd["timestamp_utc"], **out})
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
    return {
        "n_setups": len(rows),
        "mean_R_strict": mean_R,
        "win_rate_strict": base._win_rate(by_outcome, realistic=False),
        "max_drawdown": base._max_drawdown(cum),
        "total_R_strict": acc,
    }


def _passes_train(agg: dict) -> bool:
    return agg["n_setups"] >= TRAIN_MIN_SETUPS and agg["max_drawdown"] < TRAIN_MAX_DD


def _passes_ship(agg: dict) -> bool:
    return agg["n_setups"] >= HOLDOUT_MIN_SETUPS and agg["mean_R_strict"] > SHIP_MEAN_R


def _classify_holdout(holdout: dict, train_delta: float, default_holdout: dict) -> str:
    """Verdict combining the standard rules with the ETH lesson.

    DEFAULT_SHIPS takes precedence over SUSPICIOUS / MARGINAL when the
    default-params holdout already qualifies — the operator can ship
    the instrument with operator-validated defaults regardless of what
    the grid found.
    """
    if _passes_ship(default_holdout):
        # Default already ships → safest path.
        return "DEFAULT_SHIPS"
    if holdout["n_setups"] < HOLDOUT_MIN_SETUPS:
        return "INSUFFICIENT_DATA"
    if train_delta > SUSPICIOUS_DELTA:
        return "SUSPICIOUS"
    if holdout["mean_R_strict"] > SHIP_MEAN_R:
        return "SHIP"
    if holdout["mean_R_strict"] >= MARGINAL_MEAN_R:
        return "MARGINAL"
    return "DROP"


def _plausibility_flags(params: GridParams) -> list[str]:
    flags: list[str] = []
    if abs(params.sweep_fraction - 0.15) > 0.10:
        flags.append(
            f"⚠️ sweep_fraction {params.sweep_fraction:.2f} differs "
            f"from operator-validated 0.15 by > 0.10"
        )
    if abs(params.h4_amp - 1.3) > 0.7:
        flags.append(f"⚠️ h4_amp {params.h4_amp:.2f} differs from default 1.3 by > 0.7")
    return flags


def _log_progress(msg: str) -> None:
    print(msg, flush=True)
    with open(_PROGRESS_LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now(UTC).isoformat()} {msg}\n")


# ---- Previous-run carry-forward (ETH/USOUSD already validated) -------------
PREVIOUS_RESULTS = {
    "ETHUSD": {
        "default_holdout_n": 35,
        "default_holdout_mean_R": 0.526,
        "best_train_label": "sweep=0.30 h4=1.7 mss=2.0 fvg=0.3",
        "best_holdout_n": 20,
        "best_holdout_mean_R": 0.657,
        "best_train_delta": 0.842,
        "verdict_strict": "SUSPICIOUS",
        "verdict_with_default_lens": "DEFAULT_SHIPS",
    },
    "USOUSD": {
        "default_holdout_n": 24,
        "default_holdout_mean_R": 0.160,
        "best_train_label": "sweep=0.15 h4=1.7 mss=2.0 fvg=0.2",
        "best_holdout_n": 15,
        "best_holdout_mean_R": 0.187,
        "best_train_delta": 0.127,
        "verdict_strict": "DROP",
        "verdict_with_default_lens": "DROP",
    },
}
PREVIOUS_REPORT = "calibration/runs/2026-04-29T13-59-44Z_grid_search_extended_fast.md"


def main() -> int:
    _RUNS_DIR.mkdir(parents=True, exist_ok=True)
    with open(_PROGRESS_LOG, "a", encoding="utf-8") as f:
        f.write(
            f"\n=== START {_TIMESTAMP} — fast grid (extended) part 2 " f"on {INSTRUMENTS} ===\n"
        )

    _log_progress(
        f"Fast grid search (part 2) on {INSTRUMENTS} | workers={N_WORKERS} | "
        f"grid={len(SWEEP_FRACTION_GRID)}×{len(H4_AMP_GRID)}×"
        f"{len(MSS_MULT_GRID)}×{len(FVG_MULT_GRID)}="
        f"{len(SWEEP_FRACTION_GRID) * len(H4_AMP_GRID) * len(MSS_MULT_GRID) * len(FVG_MULT_GRID)} combos/inst"
    )

    fixtures: dict[str, dict] = {p: _fixtures_cached(p) for p in INSTRUMENTS}
    dates_per_instrument: dict[str, dict[str, list[date]]] = {}
    for inst in INSTRUMENTS:
        m5 = fixtures[inst]["M5"]
        weekdays = sorted(
            {d for d in set(pd.to_datetime(m5["time"], utc=True).dt.date) if d.weekday() < 5}
        )
        train, holdout = _train_holdout_split(weekdays)
        dates_per_instrument[inst] = {"train": train, "holdout": holdout, "all": weekdays}
        _log_progress(
            f"  {inst}: weekdays={len(weekdays)} "
            f"train={len(train)} ({train[0]}→{train[-1]}) "
            f"holdout={len(holdout)} ({holdout[0]}→{holdout[-1]})"
        )

    grid: list[GridParams] = [
        GridParams(s, h, m, f)
        for s in SWEEP_FRACTION_GRID
        for h in H4_AMP_GRID
        for m in MSS_MULT_GRID
        for f in FVG_MULT_GRID
    ]
    tasks: list[tuple[str, GridParams, list[date]]] = [
        (inst, p, dates_per_instrument[inst]["train"]) for inst in INSTRUMENTS for p in grid
    ]
    _log_progress(f"Total grid evaluations (train phase): {len(tasks)}")

    # ---- Phase 1 ----
    train_results: dict[str, list[tuple[GridParams, dict]]] = defaultdict(list)
    outcome_cache: dict[tuple, dict] = {}
    completed_per_inst: dict[str, int] = defaultdict(int)
    t_start = datetime.now(UTC)

    with ProcessPoolExecutor(max_workers=N_WORKERS) as ex:
        futures = {ex.submit(_worker_detect, t): t for t in tasks}
        for fut in as_completed(futures):
            res = fut.result()
            inst = res["instrument"]
            params = res["params"]
            setup_dicts = res["setups"]
            agg = _aggregate_combo(inst, setup_dicts, outcome_cache, fixtures[inst]["M5"])
            train_results[inst].append((params, agg))
            completed_per_inst[inst] += 1
            elapsed = (datetime.now(UTC) - t_start).total_seconds()
            total_done = sum(completed_per_inst.values())
            rate = total_done / elapsed if elapsed else 0
            eta = (len(tasks) - total_done) / rate if rate else 0
            _log_progress(
                f"[{inst} {completed_per_inst[inst]}/{len(grid)}] "
                f"{params.label()} → train n={agg['n_setups']:>3} "
                f"mean_R={agg['mean_R_strict']:+.3f} dd={agg['max_drawdown']:.2f} "
                f"| total {total_done}/{len(tasks)} "
                f"({elapsed/60:.1f}m, ~{eta/60:.1f}m left)"
            )

    _log_progress(f"Phase 1 complete in {(datetime.now(UTC) - t_start).total_seconds()/60:.1f} min")

    # ---- Phase 2 ----
    default_params = GridParams(DEFAULT_SWEEP_FRACTION, 1.3, 1.5, 0.3)
    summary: dict[str, dict] = {}
    for inst in INSTRUMENTS:
        candidates = train_results[inst]

        default_train_pair = next((c for c in candidates if c[0] == default_params), None)
        if default_train_pair is None:
            default_train = _aggregate_combo(
                inst,
                _worker_detect((inst, default_params, dates_per_instrument[inst]["train"]))[
                    "setups"
                ],
                outcome_cache,
                fixtures[inst]["M5"],
            )
        else:
            default_train = default_train_pair[1]

        eligible = [(p, a) for (p, a) in candidates if _passes_train(a)]
        if eligible:
            best = max(eligible, key=lambda pa: pa[1]["mean_R_strict"])
        else:
            best = max(candidates, key=lambda pa: pa[1]["mean_R_strict"])
        best_params, best_train_agg = best

        default_holdout_setups = _worker_detect(
            (inst, default_params, dates_per_instrument[inst]["holdout"])
        )["setups"]
        best_holdout_setups = _worker_detect(
            (inst, best_params, dates_per_instrument[inst]["holdout"])
        )["setups"]
        default_holdout = _aggregate_combo(
            inst, default_holdout_setups, outcome_cache, fixtures[inst]["M5"]
        )
        best_holdout = _aggregate_combo(
            inst, best_holdout_setups, outcome_cache, fixtures[inst]["M5"]
        )

        train_delta = best_train_agg["mean_R_strict"] - default_train["mean_R_strict"]
        holdout_delta = best_holdout["mean_R_strict"] - default_holdout["mean_R_strict"]
        verdict = _classify_holdout(best_holdout, train_delta, default_holdout)
        plaus = _plausibility_flags(best_params)

        summary[inst] = {
            "default_params": default_params,
            "default_train": default_train,
            "default_holdout": default_holdout,
            "best_params": best_params,
            "best_train": best_train_agg,
            "best_holdout": best_holdout,
            "train_delta": train_delta,
            "holdout_delta": holdout_delta,
            "verdict": verdict,
            "plausibility_flags": plaus,
            "eligible_count": len(eligible),
            "all_train_results": candidates,
        }

        _log_progress(
            f"[{inst}] Done. "
            f"Default holdout: mean R={default_holdout['mean_R_strict']:+.3f} on {default_holdout['n_setups']}. "
            f"Best-train: mean R={best_holdout['mean_R_strict']:+.3f} on {best_holdout['n_setups']}. "
            f"Verdict: {verdict}."
        )

    # ---- Suggested WATCHED_PAIRS (combine with previous run) ----
    suggested = ["XAUUSD", "NDX100"]
    # Previous run carry-forward.
    if PREVIOUS_RESULTS["ETHUSD"]["verdict_with_default_lens"] in ("SHIP", "DEFAULT_SHIPS"):
        suggested.append("ETHUSD")
    if PREVIOUS_RESULTS["USOUSD"]["verdict_with_default_lens"] in ("SHIP", "DEFAULT_SHIPS"):
        suggested.append("USOUSD")
    # Current run.
    for inst in INSTRUMENTS:
        if summary[inst]["verdict"] in ("SHIP", "DEFAULT_SHIPS"):
            suggested.append(inst)

    # ---- Build report ----
    lines: list[str] = []
    lines.append(f"# Fast grid search on extended fixtures — part 2 — {_TIMESTAMP}")
    lines.append("")
    lines.append(
        f"Extension of the previous run (`{PREVIOUS_REPORT}`) covering "
        f"{INSTRUMENTS} on extended fixtures. Same protocol; "
        "DEFAULT_SHIPS verdict added per the ETH lesson — when default "
        "params already pass the SHIP threshold on holdout, the instrument "
        "ships with operator-validated defaults regardless of what the grid "
        "finds (this avoids overfit-driven recommendations)."
    )
    lines.append("")

    # Train/holdout splits.
    lines.append("## Train / holdout splits (extended fixtures)")
    lines.append("")
    lines.append("| Instrument | Weekdays | Train | Train range | Holdout | Holdout range |")
    lines.append("|---|---:|---:|---|---:|---|")
    for inst in INSTRUMENTS:
        d = dates_per_instrument[inst]
        lines.append(
            f"| {inst} | {len(d['all'])} | {len(d['train'])} | "
            f"{d['train'][0]} → {d['train'][-1]} | {len(d['holdout'])} | "
            f"{d['holdout'][0]} → {d['holdout'][-1]} |"
        )
    lines.append("")

    # ---- Per-instrument detail ----
    for inst in INSTRUMENTS:
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
            f"| Train | {d_t['n_setups']} | {d_t['mean_R_strict']:+.3f} | "
            f"{d_t['win_rate_strict']:.1%} | {d_t['max_drawdown']:.2f} | "
            f"{d_t['total_R_strict']:+.2f} |"
        )
        ships_marker = " ✅ SHIP threshold" if _passes_ship(d_h) else ""
        lines.append(
            f"| Holdout | {d_h['n_setups']} | {d_h['mean_R_strict']:+.3f}{ships_marker} | "
            f"{d_h['win_rate_strict']:.1%} | {d_h['max_drawdown']:.2f} | "
            f"{d_h['total_R_strict']:+.2f} |"
        )
        lines.append("")

        b = info["best_train"]
        bh = info["best_holdout"]
        lines.append(f"### Best train params: `{info['best_params'].label()}`")
        lines.append("")
        lines.append("| Set | n setups | Mean R | Win rate | Max DD | Total R |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        lines.append(
            f"| Train | {b['n_setups']} | {b['mean_R_strict']:+.3f} | "
            f"{b['win_rate_strict']:.1%} | {b['max_drawdown']:.2f} | "
            f"{b['total_R_strict']:+.2f} |"
        )
        lines.append(
            f"| Holdout | {bh['n_setups']} | {bh['mean_R_strict']:+.3f} | "
            f"{bh['win_rate_strict']:.1%} | {bh['max_drawdown']:.2f} | "
            f"{bh['total_R_strict']:+.2f} |"
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

        top5 = sorted(
            (c for c in info["all_train_results"] if _passes_train(c[1])),
            key=lambda pa: pa[1]["mean_R_strict"],
            reverse=True,
        )[:5]
        if top5:
            lines.append("Top-5 eligible train combos:")
            lines.append("")
            lines.append("| Rank | Params | Train n | Train Mean R | Train DD |")
            lines.append("|---:|---|---:|---:|---:|")
            for i, (p, a) in enumerate(top5, 1):
                lines.append(
                    f"| {i} | `{p.label()}` | {a['n_setups']} | "
                    f"{a['mean_R_strict']:+.3f} | {a['max_drawdown']:.2f} |"
                )
            lines.append("")

    # ---- Decision matrix ----
    lines.append("## Decision matrix (combined with previous run)")
    lines.append("")
    lines.append(
        "Verdict rules:\n"
        "- **DEFAULT_SHIPS**: default holdout n ≥ 15 AND mean R > 0.4 (ETH lesson — safest path).\n"
        f"- **SHIP**: best-train holdout passes threshold AND not SUSPICIOUS.\n"
        f"- **SUSPICIOUS**: train Δ > {SUSPICIOUS_DELTA} (overfit risk).\n"
        f"- **INSUFFICIENT_DATA**: holdout n < {HOLDOUT_MIN_SETUPS}.\n"
        f"- **MARGINAL**: holdout mean R in [{MARGINAL_MEAN_R}, {SHIP_MEAN_R}].\n"
        "- **DROP**: otherwise."
    )
    lines.append("")
    lines.append(
        "| Instrument | Default holdout (n, mR) | Best-train holdout (n, mR) | "
        "Train Δ | Verdict |"
    )
    lines.append("|---|---|---|---:|---|")
    # Previous run rows.
    for inst, prev in PREVIOUS_RESULTS.items():
        lines.append(
            f"| {inst} (prev) | {prev['default_holdout_n']}, "
            f"{prev['default_holdout_mean_R']:+.3f} | "
            f"{prev['best_holdout_n']}, {prev['best_holdout_mean_R']:+.3f} | "
            f"{prev['best_train_delta']:+.3f} | "
            f"**{prev['verdict_with_default_lens']}** |"
        )
    # Current run rows.
    for inst in INSTRUMENTS:
        info = summary[inst]
        d_h = info["default_holdout"]
        bh = info["best_holdout"]
        lines.append(
            f"| {inst} | {d_h['n_setups']}, {d_h['mean_R_strict']:+.3f} | "
            f"{bh['n_setups']}, {bh['mean_R_strict']:+.3f} | "
            f"{info['train_delta']:+.3f} | **{info['verdict']}** |"
        )
    lines.append("")

    # ---- Default-vs-best comparison highlight ----
    lines.append("## Default-vs-best holdout comparison")
    lines.append("")
    lines.append(
        "Per the ETH lesson, when default holdout already ships (mean R > 0.4 AND n ≥ 15) "
        "we prefer it over the grid's best-train (which may be overfit)."
    )
    lines.append("")
    lines.append(
        "| Instrument | Default ships? | Best-train better than default on holdout? | Δ holdout (best - default) |"
    )
    lines.append("|---|---|---|---:|")
    for inst in INSTRUMENTS:
        info = summary[inst]
        d_h = info["default_holdout"]
        bh = info["best_holdout"]
        d_ships = "✅ yes" if _passes_ship(d_h) else "❌ no"
        better = "✅ yes" if bh["mean_R_strict"] > d_h["mean_R_strict"] else "❌ no"
        lines.append(f"| {inst} | {d_ships} | {better} | {info['holdout_delta']:+.3f} |")
    lines.append("")

    # ---- Suggested WATCHED_PAIRS ----
    lines.append("## Suggested WATCHED_PAIRS (combined)")
    lines.append("")
    lines.append("```python")
    lines.append(f"WATCHED_PAIRS = {suggested!r}")
    lines.append("```")
    lines.append("")
    lines.append("Logic:")
    lines.append("- XAUUSD, NDX100: operator-validated baseline.")
    for inst in PREVIOUS_RESULTS:
        v = PREVIOUS_RESULTS[inst]["verdict_with_default_lens"]
        if v in ("SHIP", "DEFAULT_SHIPS"):
            lines.append(f"- **{inst}** (prev run): `{v}` → included.")
        else:
            lines.append(f"- {inst} (prev run): `{v}` → excluded.")
    for inst in INSTRUMENTS:
        v = summary[inst]["verdict"]
        if v in ("SHIP", "DEFAULT_SHIPS"):
            lines.append(f"- **{inst}**: `{v}` → included.")
        else:
            lines.append(f"- {inst}: `{v}` → excluded.")
    lines.append("")

    # ---- Plausibility check ----
    lines.append("## Plausibility check")
    lines.append("")
    lines.append("| Instrument | Best params | Plausibility |")
    lines.append("|---|---|---|")
    for inst in INSTRUMENTS:
        info = summary[inst]
        plaus = info["plausibility_flags"]
        plaus_str = " ".join(plaus) if plaus else "✅ within range"
        lines.append(f"| {inst} | `{info['best_params'].label()}` | {plaus_str} |")
    lines.append("")

    # ---- Save ----
    report_path = _RUNS_DIR / f"{_TIMESTAMP}_grid_search_extended_fast_part2.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")

    # ---- Stdout summary ----
    print(flush=True)
    print("=== SUMMARY (overnight grid search) ===", flush=True)
    for inst in INSTRUMENTS:
        info = summary[inst]
        d_h = info["default_holdout"]
        bh = info["best_holdout"]
        print(
            f"{inst}: {info['verdict']} "
            f"(default mR {d_h['mean_R_strict']:+.3f}/n={d_h['n_setups']} | "
            f"best-train mR {bh['mean_R_strict']:+.3f}/n={bh['n_setups']})",
            flush=True,
        )
    print(flush=True)
    print("=== EXPANDED WATCHED_PAIRS (combining all results) ===", flush=True)
    print(f"WATCHED_PAIRS = {suggested!r}", flush=True)
    print(flush=True)
    print(f"Path to report: {report_path.relative_to(_REPO_ROOT)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
