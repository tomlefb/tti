"""Reproducible TJR baseline on the 10y Databento fixture, using the
leak-free tick simulator.

For each instrument in ``--instruments``, this script iterates the
weekday Paris dates in ``[--start, --end]`` (excluding ±2h rollover
windows), runs ``simulate_target_date`` per cell, attaches a 24h M5
outcome simulation to every emitted setup, gates by
NOTIFY_QUALITIES, and packs the result into a
``src.backtest.result.BacktestResult`` JSON.

Output: one JSON per instrument under ``--output-dir`` named
``baseline_<variant>_<INSTRUMENT>.json``. Two runs with the same
arguments produce byte-identical files modulo the ``run_timestamp``
field (the bootstrap CI is seeded; the simulator is deterministic
given identical fixtures and parameters).

Variants are param overrides applied on top of the operator-validated
defaults from ``config/settings.py.example``. The full variant
registry is defined in ``calibration.baseline_tjr_variants``; this
script accepts a ``--variant`` name and looks it up there. The
default ``baseline`` variant runs the live-deployment settings
unchanged.

Usage::

    python calibration/baseline_tjr_databento.py \\
        --instruments XAUUSD,NDX100,SPX500 \\
        --start 2016-01-03 \\
        --end 2026-04-29 \\
        --variant baseline \\
        --output-dir calibration/runs/baseline_<TS>/

The output filename pattern is fixed so the variants runner can
discover all per-instrument JSONs under a shared output dir.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.backtest.result import BacktestResult, SetupRecord  # noqa: E402
from src.backtest.tick_simulator import simulate_target_date  # noqa: E402
from src.detection.setup import Setup  # noqa: E402

_DEFAULT_FIXTURE_DIR = (
    _REPO_ROOT / "tests" / "fixtures" / "historical_extended" / "processed_adjusted"
)
_FIXTURE_DIR = Path(os.environ.get("TTI_FIXTURE_DIR", str(_DEFAULT_FIXTURE_DIR)))
_TZ_PARIS = ZoneInfo("Europe/Paris")
_HORIZON_MINUTES = 24 * 60
_LOOKBACK_DAYS = 60
_ROLLOVER_HALF_WINDOW = timedelta(hours=2)


# ---------------------------------------------------------------------------
# Settings + variant overrides.
# ---------------------------------------------------------------------------
def _base_settings() -> dict:
    """Operator-validated defaults from ``config/settings.py.example``.

    Returned as a plain dict so variants can override individual keys
    cleanly. ``_finalise_settings`` converts it to the
    ``SimpleNamespace`` shape the detector expects.
    """
    ndx_cfg = {"sweep_buffer": 5.0, "equal_hl_tolerance": 3.0, "sl_buffer": 5.0}
    return {
        "SESSION_ASIA": (2, 0, 6, 0),
        "KILLZONE_LONDON": (9, 0, 12, 0),
        "KILLZONE_NY": (15, 30, 18, 0),
        "SWING_LOOKBACK_H4": 2,
        "SWING_LOOKBACK_H1": 2,
        "SWING_LOOKBACK_M5": 2,
        "MIN_SWING_AMPLITUDE_ATR_MULT_H4": 1.3,
        "MIN_SWING_AMPLITUDE_ATR_MULT_H1": 1.0,
        "MIN_SWING_AMPLITUDE_ATR_MULT_M5": 1.0,
        "BIAS_SWING_COUNT": 4,
        "BIAS_REQUIRE_H1_CONFIRMATION": False,
        "H4_H1_TIME_TOLERANCE_CANDLES_H4": 2,
        "H4_H1_PRICE_TOLERANCE_FRACTION": 0.001,
        "SWING_LEVELS_LOOKBACK_COUNT": 5,
        "SWEEP_RETURN_WINDOW_CANDLES": 2,
        "SWEEP_DEDUP_TIME_WINDOW_MINUTES": 30,
        "SWEEP_DEDUP_PRICE_TOLERANCE_FRACTION": 0.001,
        "MSS_DISPLACEMENT_MULTIPLIER": 1.5,
        "MSS_DISPLACEMENT_LOOKBACK": 20,
        "FVG_ATR_PERIOD": 14,
        "FVG_MIN_SIZE_ATR_MULTIPLIER": 0.3,
        "MIN_RR": 3.0,
        "A_PLUS_RR_THRESHOLD": 4.0,
        "PARTIAL_TP_RR_TARGET": 5.0,
        "INSTRUMENT_CONFIG": {
            "XAUUSD": {"sweep_buffer": 1.0, "equal_hl_tolerance": 0.5, "sl_buffer": 1.0},
            "NDX100": ndx_cfg,
            "SPX500": ndx_cfg,
        },
        # Notify-quality gate (post-detection). Variants can relax this
        # to admit B-grade setups for evaluation.
        "NOTIFY_QUALITIES": ("A+", "A"),
    }


def _apply_overrides(base: dict, overrides: Mapping[str, object]) -> dict:
    """Deep-merge ``overrides`` into a copy of ``base``. Mapping values
    merge key-by-key; scalars overwrite."""
    out = deepcopy(base)
    for k, v in overrides.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, Mapping):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


def _finalise_settings(d: dict) -> SimpleNamespace:
    """Convert the parameter dict to the ``SetupSettings``-shaped
    object the detector accepts. ``NOTIFY_QUALITIES`` is kept on the
    returned namespace so the runner can read it without a separate
    parameter."""
    return SimpleNamespace(**d)


# ---------------------------------------------------------------------------
# Fixture loading + rollover exclusion.
# ---------------------------------------------------------------------------
def _load_instrument(symbol: str) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for tf in ("D1", "H4", "H1", "M5"):
        df = pd.read_parquet(_FIXTURE_DIR / f"{symbol}_{tf}.parquet")
        if df["time"].dt.tz is None:
            df["time"] = df["time"].dt.tz_localize("UTC")
        out[tf] = df.sort_values("time").reset_index(drop=True)
    return out


def _rollovers_utc(symbol: str) -> list[datetime]:
    """Read rollover dates from the active fixture's metadata, falling
    back to processed/ if processed_adjusted/ doesn't carry them."""
    meta_path = _FIXTURE_DIR / f"{symbol}_metadata.json"
    with open(meta_path) as f:
        meta = json.load(f)
    if "rollover_dates" not in meta:
        fallback = _REPO_ROOT / "tests" / "fixtures" / "historical_extended" / "processed"
        with open(fallback / f"{symbol}_metadata.json") as f:
            meta = json.load(f)
    return [datetime.fromisoformat(s) for s in meta["rollover_dates"]]


def _excluded_paris_dates(rollovers: list[datetime]) -> set[date]:
    excluded: set[date] = set()
    for r in rollovers:
        win_start = r - _ROLLOVER_HALF_WINDOW
        win_end = r + _ROLLOVER_HALF_WINDOW
        d_start = win_start.astimezone(_TZ_PARIS).date()
        d_end = win_end.astimezone(_TZ_PARIS).date()
        cur = d_start
        while cur <= d_end:
            excluded.add(cur)
            cur += timedelta(days=1)
    return excluded


def _trading_dates_for(df_m5: pd.DataFrame) -> list[date]:
    times = pd.to_datetime(df_m5["time"], utc=True)
    paris_dates = sorted(set(times.dt.tz_convert(_TZ_PARIS).dt.date))
    return [d for d in paris_dates if d.weekday() < 5]


# ---------------------------------------------------------------------------
# Fixture cache for fast slicing.
# ---------------------------------------------------------------------------
class FixtureCache:
    def __init__(self, bundle: dict[str, pd.DataFrame]) -> None:
        self.bundle = bundle
        self.times_ns: dict[str, np.ndarray] = {}
        for tf, df in bundle.items():
            ts = df["time"]
            self.times_ns[tf] = (
                ts.dt.tz_convert("UTC").dt.tz_localize(None).values.astype("datetime64[ns]")
            )

    def slice_window(self, end_utc: datetime, days_lookback: int) -> dict[str, pd.DataFrame]:
        end_np = np.datetime64(end_utc.astimezone(UTC).replace(tzinfo=None), "ns")
        start_np = end_np - np.timedelta64(days_lookback, "D")
        out: dict[str, pd.DataFrame] = {}
        for tf, df in self.bundle.items():
            ta = self.times_ns[tf]
            si = int(np.searchsorted(ta, start_np, side="left"))
            ei = int(np.searchsorted(ta, end_np, side="right"))
            out[tf] = df.iloc[si:ei]
        return out


class M5Cache:
    def __init__(self, df_m5: pd.DataFrame) -> None:
        ts = df_m5["time"]
        self.times_ns: np.ndarray = (
            ts.dt.tz_convert("UTC").dt.tz_localize(None).values.astype("datetime64[ns]")
        )
        self.lows: np.ndarray = df_m5["low"].to_numpy(dtype="float64")
        self.highs: np.ndarray = df_m5["high"].to_numpy(dtype="float64")
        self.n: int = len(df_m5)


def _eod_paris_utc(d: date) -> datetime:
    eod = datetime.combine(d, time(23, 59))
    return eod.replace(tzinfo=_TZ_PARIS).astimezone(UTC)


# ---------------------------------------------------------------------------
# Outcome simulation (mirrors run_extended_10y_backtest._simulate_outcome).
# ---------------------------------------------------------------------------
def _simulate_outcome(setup: Setup, m5: M5Cache) -> dict:
    setup_ts = np.datetime64(setup.timestamp_utc.astimezone(UTC).replace(tzinfo=None), "ns")
    start = int(np.searchsorted(m5.times_ns, setup_ts, side="left"))
    if start >= m5.n:
        return {"outcome": "open_at_horizon", "realized_R": 0.0}
    horizon_end_ts = setup_ts + np.timedelta64(_HORIZON_MINUTES, "m")
    end = int(np.searchsorted(m5.times_ns, horizon_end_ts, side="right"))
    if end <= start:
        return {"outcome": "open_at_horizon", "realized_R": 0.0}
    lows = m5.lows[start:end]
    highs = m5.highs[start:end]
    n = end - start
    direction = setup.direction
    entry = setup.entry_price
    sl = setup.stop_loss
    tp1 = setup.tp1_price
    tpr = setup.tp_runner_price
    same_tps = abs(tp1 - tpr) < 1e-9
    entry_idx: int | None = None
    sl_before_entry_flag = False
    for i in range(n):
        if direction == "long":
            entry_now = lows[i] <= entry
            sl_now = lows[i] <= sl
        else:
            entry_now = highs[i] >= entry
            sl_now = highs[i] >= sl
        if entry_now:
            if sl_now:
                sl_before_entry_flag = True
            entry_idx = i
            break
    if entry_idx is None:
        return {"outcome": "entry_not_hit", "realized_R": 0.0}
    if sl_before_entry_flag:
        return {"outcome": "sl_before_entry", "realized_R": -1.0}
    tp1_idx: int | None = None
    for i in range(entry_idx, n):
        if direction == "long":
            sl_now = lows[i] <= sl
            tp1_now = highs[i] >= tp1
        else:
            sl_now = highs[i] >= sl
            tp1_now = lows[i] <= tp1
        if sl_now:
            return {"outcome": "sl_hit", "realized_R": -1.0}
        if tp1_now:
            tp1_idx = i
            break
    if tp1_idx is None:
        return {"outcome": "open_at_horizon", "realized_R": 0.0}
    if same_tps:
        r = 0.5 * setup.tp1_rr + 0.5 * setup.tp_runner_rr
        return {"outcome": "tp_runner_hit", "realized_R": r}
    for j in range(tp1_idx + 1, n):
        if direction == "long":
            sl_now = lows[j] <= sl
            tpr_now = highs[j] >= tpr
        else:
            sl_now = highs[j] >= sl
            tpr_now = lows[j] <= tpr
        if sl_now:
            r = (setup.tp1_rr - 1.0) / 2.0
            return {"outcome": "tp1_hit_only", "realized_R": r}
        if tpr_now:
            r = 0.5 * setup.tp1_rr + 0.5 * setup.tp_runner_rr
            return {"outcome": "tp_runner_hit", "realized_R": r}
    r = (setup.tp1_rr - 1.0) / 2.0
    return {"outcome": "tp1_hit_only", "realized_R": r}


# ---------------------------------------------------------------------------
# Variant registry (kept here so the variants script can reuse it).
# ---------------------------------------------------------------------------
VARIANTS: dict[str, dict] = {
    "baseline": {},
    "swing_lookback_3": {
        "SWING_LOOKBACK_H4": 3,
        "SWING_LOOKBACK_H1": 3,
        "SWING_LOOKBACK_M5": 3,
    },
    "swing_lookback_5": {
        "SWING_LOOKBACK_H4": 5,
        "SWING_LOOKBACK_H1": 5,
        "SWING_LOOKBACK_M5": 5,
    },
    "amp_atr_low": {
        "MIN_SWING_AMPLITUDE_ATR_MULT_H4": 0.3,
        "MIN_SWING_AMPLITUDE_ATR_MULT_H1": 0.3,
        "MIN_SWING_AMPLITUDE_ATR_MULT_M5": 0.3,
    },
    "amp_atr_high": {
        "MIN_SWING_AMPLITUDE_ATR_MULT_H4": 0.8,
        "MIN_SWING_AMPLITUDE_ATR_MULT_H1": 0.8,
        "MIN_SWING_AMPLITUDE_ATR_MULT_M5": 0.8,
    },
    "quality_relaxed": {
        # Relax the post-detection quality gate; detection params unchanged.
        "NOTIFY_QUALITIES": ("A+", "A", "B"),
    },
    "mss_disp_low": {"MSS_DISPLACEMENT_MULTIPLIER": 1.2},
    "mss_disp_high": {"MSS_DISPLACEMENT_MULTIPLIER": 1.8},
}


# ---------------------------------------------------------------------------
# Per-instrument backtest runner.
# ---------------------------------------------------------------------------
def _run_instrument(
    symbol: str,
    period_start: date,
    period_end: date,
    settings_obj: SimpleNamespace,
    notify_qualities: tuple[str, ...],
    variant_name: str,
    n_dates: int | None,
    seed: int,
) -> BacktestResult:
    bundle = _load_instrument(symbol)
    fixture_cache = FixtureCache(bundle)
    m5_cache = M5Cache(bundle["M5"])
    excluded = _excluded_paris_dates(_rollovers_utc(symbol))
    all_dates = _trading_dates_for(bundle["M5"])
    in_range = [d for d in all_dates if period_start <= d <= period_end and d not in excluded]
    if n_dates is not None and n_dates < len(in_range):
        import random  # noqa: PLC0415

        in_range = sorted(random.Random(seed).sample(in_range, k=n_dates))

    print(f"  [{symbol}] {len(in_range)} cells (variant={variant_name}) ...", flush=True)
    setups: list[SetupRecord] = []
    progress_every = max(len(in_range) // 20, 1)
    for i, d in enumerate(in_range, 1):
        sliced = fixture_cache.slice_window(_eod_paris_utc(d) + timedelta(days=1), _LOOKBACK_DAYS)
        try:
            day_setups = simulate_target_date(
                df_h4=sliced["H4"],
                df_h1=sliced["H1"],
                df_m5=sliced["M5"],
                df_d1=sliced["D1"],
                target_date=d,
                symbol=symbol,
                settings=settings_obj,
            )
        except Exception as exc:
            print(f"    skip {symbol} {d}: detection — {type(exc).__name__}: {exc}", flush=True)
            continue
        for s in day_setups:
            if s.quality not in notify_qualities:
                continue
            outcome = _simulate_outcome(s, m5_cache)
            setups.append(
                SetupRecord(
                    timestamp_utc=s.timestamp_utc.isoformat(),
                    instrument=s.symbol,
                    direction=s.direction,
                    quality=str(s.quality),
                    realized_r=float(outcome["realized_R"]),
                    outcome=outcome["outcome"],
                )
            )
        if i % progress_every == 0:
            print(
                f"    {symbol} {i}/{len(in_range)} (cumulative={len(setups)})",
                flush=True,
            )

    return BacktestResult.from_setups(
        strategy_name=f"tjr_{variant_name}",
        instrument=symbol,
        period_start=period_start,
        period_end=period_end,
        setups=setups,
        params_used={
            "variant": variant_name,
            "n_dates_sample": n_dates,
            "seed": seed,
            "notify_qualities": list(notify_qualities),
            "fixture_dir": str(_FIXTURE_DIR),
            "lookback_days": _LOOKBACK_DAYS,
            "horizon_minutes": _HORIZON_MINUTES,
            "rollover_excluded_days": len(excluded),
            "settings": _serialise_settings(settings_obj),
        },
    )


def _serialise_settings(settings_obj: SimpleNamespace) -> dict:
    """Return a JSON-friendly view of the parameter dict."""
    out = {}
    for k, v in vars(settings_obj).items():
        if isinstance(v, tuple):
            out[k] = list(v)
        elif isinstance(v, dict):
            out[k] = {kk: dict(vv) if isinstance(vv, dict) else vv for kk, vv in v.items()}
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instruments", default="XAUUSD,NDX100,SPX500")
    parser.add_argument("--start", default="2016-01-03")
    parser.add_argument("--end", default="2026-04-29")
    parser.add_argument("--variant", default="baseline", choices=sorted(VARIANTS.keys()))
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--n-dates",
        type=int,
        default=None,
        help="If set, randomly sample this many dates per instrument "
        "(seed-controlled). Default = run all dates in the period.",
    )
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    instruments = [s.strip() for s in args.instruments.split(",") if s.strip()]
    period_start = date.fromisoformat(args.start)
    period_end = date.fromisoformat(args.end)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    overrides = VARIANTS[args.variant]
    base = _base_settings()
    final_dict = _apply_overrides(base, overrides)
    notify = tuple(final_dict.pop("NOTIFY_QUALITIES"))
    settings_obj = _finalise_settings(final_dict)

    print(
        f"=== TJR baseline — variant={args.variant} — "
        f"period {period_start} → {period_end} — "
        f"instruments {instruments} ===",
        flush=True,
    )
    for sym in instruments:
        result = _run_instrument(
            sym,
            period_start,
            period_end,
            settings_obj,
            notify,
            args.variant,
            args.n_dates,
            args.seed,
        )
        path = output_dir / f"baseline_{args.variant}_{sym}.json"
        result.to_json(path)
        print(
            f"  [{sym}] n={result.n_setups} mean_r={result.mean_r:+.3f} "
            f"CI=[{result.mean_r_ci_95[0]:+.3f},{result.mean_r_ci_95[1]:+.3f}] "
            f"win_rate={result.win_rate:.1%} "
            f"frac_pos_sem={result.fraction_positive_semesters:.2f} "
            f"→ {path}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
