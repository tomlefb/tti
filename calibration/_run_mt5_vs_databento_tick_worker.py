"""Worker for ``run_mt5_vs_databento_tick.py``. Runs the TJR baseline
under the leak-free tick simulator on one (source, instrument) cell
and emits both:

- ``<source>_<INSTRUMENT>.json`` — a ``BacktestResult``.
- ``<source>_<INSTRUMENT>_setups.jsonl`` — one JSON row per emitted
  A/A+ setup with the extended fields the diff script needs.

The fixture directory is read from the ``TTI_FIXTURE_DIR`` env var set
by the parent dispatcher, so this worker is fixture-source-agnostic.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from calibration.baseline_tjr_databento import (  # noqa: E402
    FixtureCache,
    M5Cache,
    VARIANTS,
    _apply_overrides,
    _base_settings,
    _eod_paris_utc,
    _excluded_paris_dates,
    _finalise_settings,
    _load_instrument,
    _rollovers_utc,
    _serialise_settings,
    _simulate_outcome,
    _trading_dates_for,
)
from src.backtest.result import BacktestResult, SetupRecord  # noqa: E402
from src.backtest.tick_simulator import simulate_target_date  # noqa: E402

# Lookback window mirrors baseline_tjr_databento (60 days of context
# loaded for each detection cycle so H4/D1 swing structure is stable).
_LOOKBACK_DAYS = 60


def _setup_to_full_row(setup, outcome: dict) -> dict:
    """Project a Setup + outcome onto the JSONL row schema used by the
    diff. Only the fields needed for matching, divergence analysis,
    and the 5-case detail printout are kept."""
    return {
        "timestamp_utc": setup.timestamp_utc.isoformat(),
        "instrument": setup.symbol,
        "direction": setup.direction,
        "killzone": setup.killzone,
        "quality": str(setup.quality),
        "swept_level_price": float(setup.swept_level_price),
        "swept_level_type": setup.swept_level_type,
        "swept_level_strength": setup.swept_level_strength,
        "entry_price": float(setup.entry_price),
        "stop_loss": float(setup.stop_loss),
        "tp1_price": float(setup.tp1_price),
        "tp1_rr": float(setup.tp1_rr),
        "tp_runner_price": float(setup.tp_runner_price),
        "tp_runner_rr": float(setup.tp_runner_rr),
        "poi_type": setup.poi_type,
        "daily_bias": setup.daily_bias,
        "realized_r": float(outcome["realized_R"]),
        "outcome": outcome["outcome"],
        "confluences": list(setup.confluences),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--instrument", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--n-dates", type=int, default=None)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    period_start = date.fromisoformat(args.start)
    period_end = date.fromisoformat(args.end)

    base = _base_settings()
    final = _apply_overrides(base, VARIANTS["baseline"])
    notify = tuple(final.pop("NOTIFY_QUALITIES"))
    settings_obj = _finalise_settings(final)

    print(
        f"[{args.source}×{args.instrument}] period {period_start} → {period_end} "
        f"notify={notify}",
        flush=True,
    )

    bundle = _load_instrument(args.instrument)
    fixture_cache = FixtureCache(bundle)
    m5_cache = M5Cache(bundle["M5"])
    # MT5 broker fixtures are post-rollover-adjusted continuous CFDs
    # without a rollover metadata file. Databento adjusted carries the
    # original Panama rollover dates we still want to exclude (±2h
    # windows mask the seam-bar artefacts).
    try:
        excluded = _excluded_paris_dates(_rollovers_utc(args.instrument))
    except FileNotFoundError:
        excluded = set()
    all_dates = _trading_dates_for(bundle["M5"])
    in_range = [d for d in all_dates if period_start <= d <= period_end and d not in excluded]
    if args.n_dates is not None and args.n_dates < len(in_range):
        import random

        in_range = sorted(random.Random(args.seed).sample(in_range, k=args.n_dates))
    print(
        f"[{args.source}×{args.instrument}] {len(in_range)} cells (excluded={len(excluded)})",
        flush=True,
    )

    setups_records: list[SetupRecord] = []
    full_rows: list[dict] = []
    progress_every = max(len(in_range) // 20, 1)
    skipped = 0
    from datetime import timedelta  # noqa: PLC0415

    for i, d in enumerate(in_range, 1):
        sliced = fixture_cache.slice_window(_eod_paris_utc(d) + timedelta(days=1), _LOOKBACK_DAYS)
        try:
            day_setups = simulate_target_date(
                df_h4=sliced["H4"],
                df_h1=sliced["H1"],
                df_m5=sliced["M5"],
                df_d1=sliced["D1"],
                target_date=d,
                symbol=args.instrument,
                settings=settings_obj,
            )
        except Exception as exc:
            skipped += 1
            print(
                f"  skip {args.instrument} {d}: detection — {type(exc).__name__}: {exc}",
                flush=True,
            )
            continue
        for s in day_setups:
            if s.quality not in notify:
                continue
            outcome = _simulate_outcome(s, m5_cache)
            setups_records.append(
                SetupRecord(
                    timestamp_utc=s.timestamp_utc.isoformat(),
                    instrument=s.symbol,
                    direction=s.direction,
                    quality=str(s.quality),
                    realized_r=float(outcome["realized_R"]),
                    outcome=outcome["outcome"],
                )
            )
            full_rows.append(_setup_to_full_row(s, outcome))
        if i % progress_every == 0:
            print(
                f"  [{args.source}×{args.instrument}] {i}/{len(in_range)} "
                f"(setups so far={len(setups_records)})",
                flush=True,
            )

    result = BacktestResult.from_setups(
        strategy_name=f"tjr_{args.source}",
        instrument=args.instrument,
        period_start=period_start,
        period_end=period_end,
        setups=setups_records,
        params_used={
            "source": args.source,
            "n_dates_sample": args.n_dates,
            "seed": args.seed,
            "notify_qualities": list(notify),
            "fixture_dir": str(Path(__import__("os").environ.get("TTI_FIXTURE_DIR", ""))),
            "lookback_days": _LOOKBACK_DAYS,
            "rollover_excluded_days": len(excluded),
            "skipped_dates": skipped,
            "settings": _serialise_settings(settings_obj),
        },
    )
    json_path = out_dir / f"{args.source}_{args.instrument}.json"
    result.to_json(json_path)
    jsonl_path = out_dir / f"{args.source}_{args.instrument}_setups.jsonl"
    with open(jsonl_path, "w") as f:
        for row in full_rows:
            f.write(json.dumps(row, default=str) + "\n")
    print(
        f"[{args.source}×{args.instrument}] DONE n={result.n_setups} "
        f"mean_r={result.mean_r:+.3f} CI=[{result.mean_r_ci_95[0]:+.3f},"
        f"{result.mean_r_ci_95[1]:+.3f}] win={result.win_rate:.1%} "
        f"→ {json_path.name}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
