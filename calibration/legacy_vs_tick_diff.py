"""Quantify the inflation Sprint 6.5 / 6.6 backtests carried.

For the same date sample the look-ahead audit uses (seed=42, 400
trading dates per instrument over 10y XAUUSD + NDX100), this script
runs both the **legacy** detector path
(``build_setup_candidates(now_utc=None)`` — one call per day, the
pre-Phase-B path that drove every prior backtest) and the
**tick-faithful** path (the production scheduler simulated 5 min at a
time, identity-locked at first emission). It then attaches an outcome
simulation to every emitted setup and reports the per-mode aggregates
side by side.

The audit already proved the tick path is leak-free: 53/53 truthful
setups reproduce bit-identically under the truncated re-run, and
53/53 emit at exactly the expected scheduler tick. This script
extends that work in two directions the audit did not cover:

1. **Legacy as a peer**: instead of being the leaky baseline whose
   output the audit only used to *discover* candidate ticks, the
   legacy path's setups are run end-to-end (outcome simulation
   included) and contrasted with the tick output, so the operator
   can see the magnitude of the inflation in mean-R / win-rate
   terms — not just in setup count.
2. **Field-level deltas on overlapping identities**: setups present
   in both modes get a per-field comparison (POI, entry, SL, TP, RR,
   quality). The tick-faithful version is the production reality;
   any divergence visible here is the per-trade delta a real
   notification would have shown vs the inflated number a leaky
   backtest reported.

Runtime: ~45 min on the full 400-date sample (legacy: ~12 min for
the per-day calls; tick reuses the audit's truthful pool which costs
~30 min if re-run from scratch, or is essentially free if the audit
has already populated its computation; outcome sim is negligible).
The full ~10y date space (~4800 trading dates) would extrapolate to
~4.5 days for the tick path alone, so we sample by design.

Output: ``calibration/runs/legacy_vs_tick_diff_<UTC-timestamp>.md``.

Usage::

    python calibration/legacy_vs_tick_diff.py \\
        --instruments XAUUSD,NDX100 \\
        --n-dates 400 \\
        --start 2016-01-03 \\
        --end 2026-04-29

Defaults match ``audit_lookahead.py`` so the truthful pool is
identical (seeded RNG, same date sample).
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from datetime import UTC, date, datetime, time
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.backtest.tick_simulator import _identity, simulate_target_date  # noqa: E402
from src.detection.setup import Setup, build_setup_candidates  # noqa: E402

_DEFAULT_FIXTURE_DIR = (
    _REPO_ROOT / "tests" / "fixtures" / "historical_extended" / "processed_adjusted"
)
_FIXTURE_DIR = Path(os.environ.get("TTI_FIXTURE_DIR", str(_DEFAULT_FIXTURE_DIR)))
_RUNS_DIR = _REPO_ROOT / "calibration" / "runs"
_TZ_PARIS = ZoneInfo("Europe/Paris")
_NOTIFY_QUALITIES = ("A+", "A")
_HORIZON_MINUTES = 24 * 60
_LOOKBACK_DAYS = 60


def _settings() -> SimpleNamespace:
    ndx_cfg = {"sweep_buffer": 5.0, "equal_hl_tolerance": 3.0, "sl_buffer": 5.0}
    return SimpleNamespace(
        SESSION_ASIA=(2, 0, 6, 0),
        KILLZONE_LONDON=(9, 0, 12, 0),
        KILLZONE_NY=(15, 30, 18, 0),
        SWING_LOOKBACK_H4=2,
        SWING_LOOKBACK_H1=2,
        SWING_LOOKBACK_M5=2,
        MIN_SWING_AMPLITUDE_ATR_MULT_H4=1.3,
        MIN_SWING_AMPLITUDE_ATR_MULT_H1=1.0,
        MIN_SWING_AMPLITUDE_ATR_MULT_M5=1.0,
        BIAS_SWING_COUNT=4,
        BIAS_REQUIRE_H1_CONFIRMATION=False,
        H4_H1_TIME_TOLERANCE_CANDLES_H4=2,
        H4_H1_PRICE_TOLERANCE_FRACTION=0.001,
        SWING_LEVELS_LOOKBACK_COUNT=5,
        SWEEP_RETURN_WINDOW_CANDLES=2,
        SWEEP_DEDUP_TIME_WINDOW_MINUTES=30,
        SWEEP_DEDUP_PRICE_TOLERANCE_FRACTION=0.001,
        MSS_DISPLACEMENT_MULTIPLIER=1.5,
        MSS_DISPLACEMENT_LOOKBACK=20,
        FVG_ATR_PERIOD=14,
        FVG_MIN_SIZE_ATR_MULTIPLIER=0.3,
        MIN_RR=3.0,
        A_PLUS_RR_THRESHOLD=4.0,
        PARTIAL_TP_RR_TARGET=5.0,
        INSTRUMENT_CONFIG={
            "XAUUSD": {"sweep_buffer": 1.0, "equal_hl_tolerance": 0.5, "sl_buffer": 1.0},
            "NDX100": ndx_cfg,
        },
    )


def _load_instrument(symbol: str) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for tf in ("D1", "H4", "H1", "M5"):
        df = pd.read_parquet(_FIXTURE_DIR / f"{symbol}_{tf}.parquet")
        if df["time"].dt.tz is None:
            df["time"] = df["time"].dt.tz_localize("UTC")
        out[tf] = df.sort_values("time").reset_index(drop=True)
    return out


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
    """Numpy arrays for fast outcome simulation — same layout as in
    ``calibration/run_extended_10y_backtest.py`` so the outcome
    semantics here match the existing harness exactly."""

    def __init__(self, df_m5: pd.DataFrame) -> None:
        ts = df_m5["time"]
        self.times_ns: np.ndarray = (
            ts.dt.tz_convert("UTC").dt.tz_localize(None).values.astype("datetime64[ns]")
        )
        self.lows: np.ndarray = df_m5["low"].to_numpy(dtype="float64")
        self.highs: np.ndarray = df_m5["high"].to_numpy(dtype="float64")
        self.n: int = len(df_m5)


def _trading_dates_for(df_m5: pd.DataFrame) -> list[date]:
    times = pd.to_datetime(df_m5["time"], utc=True)
    paris_dates = sorted(set(times.dt.tz_convert(_TZ_PARIS).dt.date))
    return [d for d in paris_dates if d.weekday() < 5]


def _eod_paris_utc(d: date) -> datetime:
    eod = datetime.combine(d, time(23, 59))
    return eod.replace(tzinfo=_TZ_PARIS).astimezone(UTC)


def _simulate_outcome(setup: Setup, m5: M5Cache) -> dict:
    """Mirrors ``run_extended_10y_backtest._simulate_outcome``: 24h
    horizon from setup.timestamp_utc on M5, partial-exit convention
    (50% TP1 + 50% TP_runner). ``realized_R_strict`` charges -1 for
    sl_before_entry; ``realized_R_realistic`` charges 0."""
    setup_ts = np.datetime64(setup.timestamp_utc.astimezone(UTC).replace(tzinfo=None), "ns")
    start = int(np.searchsorted(m5.times_ns, setup_ts, side="left"))
    if start >= m5.n:
        return {"outcome": "open_at_horizon", "realized_R_strict": 0.0, "realized_R_realistic": 0.0}
    horizon_end_ts = setup_ts + np.timedelta64(_HORIZON_MINUTES, "m")
    end = int(np.searchsorted(m5.times_ns, horizon_end_ts, side="right"))
    if end <= start:
        return {"outcome": "open_at_horizon", "realized_R_strict": 0.0, "realized_R_realistic": 0.0}
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
        return {"outcome": "entry_not_hit", "realized_R_strict": 0.0, "realized_R_realistic": 0.0}
    if sl_before_entry_flag:
        return {
            "outcome": "sl_before_entry",
            "realized_R_strict": -1.0,
            "realized_R_realistic": 0.0,
        }
    tp1_idx: int | None = None
    for i in range(entry_idx, n):
        if direction == "long":
            sl_now = lows[i] <= sl
            tp1_now = highs[i] >= tp1
        else:
            sl_now = highs[i] >= sl
            tp1_now = lows[i] <= tp1
        if sl_now:
            return {"outcome": "sl_hit", "realized_R_strict": -1.0, "realized_R_realistic": -1.0}
        if tp1_now:
            tp1_idx = i
            break
    if tp1_idx is None:
        return {"outcome": "open_at_horizon", "realized_R_strict": 0.0, "realized_R_realistic": 0.0}
    if same_tps:
        r = 0.5 * setup.tp1_rr + 0.5 * setup.tp_runner_rr
        return {"outcome": "tp_runner_hit", "realized_R_strict": r, "realized_R_realistic": r}
    for j in range(tp1_idx + 1, n):
        if direction == "long":
            sl_now = lows[j] <= sl
            tpr_now = highs[j] >= tpr
        else:
            sl_now = highs[j] >= sl
            tpr_now = lows[j] <= tpr
        if sl_now:
            r = (setup.tp1_rr - 1.0) / 2.0
            return {"outcome": "tp1_hit_only", "realized_R_strict": r, "realized_R_realistic": r}
        if tpr_now:
            r = 0.5 * setup.tp1_rr + 0.5 * setup.tp_runner_rr
            return {"outcome": "tp_runner_hit", "realized_R_strict": r, "realized_R_realistic": r}
    r = (setup.tp1_rr - 1.0) / 2.0
    return {"outcome": "tp1_hit_only", "realized_R_strict": r, "realized_R_realistic": r}


def _aggregate(rows: list[dict]) -> dict:
    notify_rows = [r for r in rows if r["quality"] in _NOTIFY_QUALITIES]
    closed = [r for r in notify_rows if r["outcome"] not in ("entry_not_hit", "open_at_horizon")]
    wins = sum(1 for r in closed if r["outcome"] in ("tp1_hit_only", "tp_runner_hit"))
    losses = sum(1 for r in closed if r["outcome"] in ("sl_hit", "sl_before_entry"))
    mean_r = sum(r["realized_R_strict"] for r in closed) / len(closed) if closed else 0.0
    win_rate = wins / (wins + losses) if (wins + losses) else 0.0
    return {
        "n_total": len(rows),
        "n_notify": len(notify_rows),
        "n_closed": len(closed),
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "mean_r": mean_r,
        "total_r": sum(r["realized_R_strict"] for r in closed),
        "by_quality": {q: sum(1 for r in rows if r["quality"] == q) for q in ("A+", "A", "B")},
    }


def _setup_to_row(s: Setup, outcome: dict) -> dict:
    return {
        "instrument": s.symbol,
        "timestamp_utc": s.timestamp_utc,
        "killzone": s.killzone,
        "direction": s.direction,
        "quality": s.quality,
        "entry": s.entry_price,
        "stop_loss": s.stop_loss,
        "tp1_rr": s.tp1_rr,
        "tp_runner_rr": s.tp_runner_rr,
        "swept_level_type": s.swept_level_type,
        "target_level_type": s.target_level_type,
        "poi_type": s.poi_type,
        **outcome,
    }


def _diff_overlapping(legacy_by_id: dict, tick_by_id: dict) -> dict:
    """For each identity present in both modes, compare key fields."""
    common = set(legacy_by_id) & set(tick_by_id)
    out = {
        "n_common": len(common),
        "poi_changed": 0,
        "entry_changed": 0,
        "sl_changed": 0,
        "tp_runner_rr_changed": 0,
        "quality_changed": 0,
        "quality_demotions_to_b": 0,  # A/A+ → B (would not have been notified)
    }
    for key in common:
        a = legacy_by_id[key]
        b = tick_by_id[key]
        if a.poi_type != b.poi_type:
            out["poi_changed"] += 1
        if abs(a.entry_price - b.entry_price) > 1e-6:
            out["entry_changed"] += 1
        if abs(a.stop_loss - b.stop_loss) > 1e-6:
            out["sl_changed"] += 1
        if abs(a.tp_runner_rr - b.tp_runner_rr) > 1e-6:
            out["tp_runner_rr_changed"] += 1
        if a.quality != b.quality:
            out["quality_changed"] += 1
            if a.quality in _NOTIFY_QUALITIES and b.quality == "B":
                out["quality_demotions_to_b"] += 1
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instruments", default="XAUUSD,NDX100")
    parser.add_argument("--n-dates", type=int, default=400)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--start", default="2016-01-03")
    parser.add_argument("--end", default="2026-04-29")
    args = parser.parse_args()
    args.instruments = [s.strip() for s in args.instruments.split(",") if s.strip()]
    start_d = date.fromisoformat(args.start)
    end_d = date.fromisoformat(args.end)
    settings = _settings()

    print(f"Loading fixtures from {_FIXTURE_DIR}")
    fixtures = {sym: FixtureCache(_load_instrument(sym)) for sym in args.instruments}
    m5_caches = {sym: M5Cache(fixtures[sym].bundle["M5"]) for sym in args.instruments}
    rng = random.Random(args.seed)

    legacy_setups: list[Setup] = []
    tick_setups: list[Setup] = []
    cells = 0
    for sym in args.instruments:
        cache = fixtures[sym]
        all_dates = _trading_dates_for(cache.bundle["M5"])
        in_range = [d for d in all_dates if start_d <= d <= end_d]
        date_sample = sorted(rng.sample(in_range, k=min(args.n_dates, len(in_range))))
        print(f"  [{sym}] running legacy + tick across {len(date_sample)} dates ...")
        for i, d in enumerate(date_sample, 1):
            cells += 1
            sliced = cache.slice_window(_eod_paris_utc(d), _LOOKBACK_DAYS)
            try:
                legacy_setups.extend(
                    build_setup_candidates(
                        df_h4=sliced["H4"],
                        df_h1=sliced["H1"],
                        df_m5=sliced["M5"],
                        df_d1=sliced["D1"],
                        target_date=d,
                        symbol=sym,
                        settings=settings,
                    )
                )
            except Exception as exc:  # pragma: no cover
                print(f"    legacy skip {sym} {d}: {exc!r}")
                continue
            try:
                tick_setups.extend(
                    simulate_target_date(
                        df_h4=sliced["H4"],
                        df_h1=sliced["H1"],
                        df_m5=sliced["M5"],
                        df_d1=sliced["D1"],
                        target_date=d,
                        symbol=sym,
                        settings=settings,
                    )
                )
            except Exception as exc:  # pragma: no cover
                print(f"    tick skip {sym} {d}: {exc!r}")
                continue
            if i % 25 == 0:
                print(
                    f"    {sym} {i}/{len(date_sample)} "
                    f"(legacy={sum(1 for s in legacy_setups if s.symbol == sym)}, "
                    f"tick={sum(1 for s in tick_setups if s.symbol == sym)})"
                )

    print(f"Total: legacy={len(legacy_setups)}, tick={len(tick_setups)}")

    print("Outcome simulation ...")
    legacy_rows: list[dict] = []
    tick_rows: list[dict] = []
    for s in legacy_setups:
        outcome = _simulate_outcome(s, m5_caches[s.symbol])
        legacy_rows.append(_setup_to_row(s, outcome))
    for s in tick_setups:
        outcome = _simulate_outcome(s, m5_caches[s.symbol])
        tick_rows.append(_setup_to_row(s, outcome))

    legacy_by_id = {_identity(s): s for s in legacy_setups}
    tick_by_id = {_identity(s): s for s in tick_setups}
    overlap_diff = _diff_overlapping(legacy_by_id, tick_by_id)
    legacy_only = set(legacy_by_id) - set(tick_by_id)
    tick_only = set(tick_by_id) - set(legacy_by_id)

    legacy_agg_all = _aggregate(legacy_rows)
    tick_agg_all = _aggregate(tick_rows)
    legacy_agg_per: dict = {}
    tick_agg_per: dict = {}
    for sym in args.instruments:
        legacy_agg_per[sym] = _aggregate([r for r in legacy_rows if r["instrument"] == sym])
        tick_agg_per[sym] = _aggregate([r for r in tick_rows if r["instrument"] == sym])

    _RUNS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    path = _RUNS_DIR / f"FINAL_legacy_vs_tick_diff_{ts}.md"

    lines = []
    lines.append(f"# Legacy vs tick-faithful — extended 10y backtest diff — {ts}")
    lines.append("")
    lines.append(
        f"Sample: {args.n_dates} trading dates per instrument "
        f"(seed={args.seed}, range {args.start} → {args.end}, instruments "
        f"{','.join(args.instruments)}, total cells={cells}, lookback={_LOOKBACK_DAYS}d)."
    )
    lines.append("")
    lines.append(
        "**Mode legacy** = ``build_setup_candidates(now_utc=None)``, the "
        "pre-Phase-B path that produced every backtest before this branch. "
        "**Mode tick** = ``simulate_target_date(...)``, which iterates the "
        "5-min APScheduler firings inside both killzones with ``now_utc=tick`` "
        "set, locking each setup identity at its first emission. The Phase A "
        "+ Phase-B-core leak fixes (FVG forward window, sweep dedupe pool, "
        "swing confirmation, detect_mss forward iteration) make the tick path "
        "leak-free; the audit at "
        "``calibration/runs/FINAL_lookahead_audit_phase_a_complete_2026-05-01.md`` "
        "and the tick-by-tick audit at ``calibration/audit_tick_simulator.py`` "
        "verify this end-to-end."
    )
    lines.append("")

    lines.append("## Setup count")
    lines.append("")
    lines.append("| Mode | Total | A+ | A | B | A/A+ (notify) |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    lines.append(
        f"| legacy | {legacy_agg_all['n_total']} | "
        f"{legacy_agg_all['by_quality']['A+']} | {legacy_agg_all['by_quality']['A']} | "
        f"{legacy_agg_all['by_quality']['B']} | {legacy_agg_all['n_notify']} |"
    )
    lines.append(
        f"| tick | {tick_agg_all['n_total']} | "
        f"{tick_agg_all['by_quality']['A+']} | {tick_agg_all['by_quality']['A']} | "
        f"{tick_agg_all['by_quality']['B']} | {tick_agg_all['n_notify']} |"
    )
    lines.append("")
    inflate = legacy_agg_all["n_total"] - tick_agg_all["n_total"]
    lines.append(
        f"**Setup-count inflation in legacy: {inflate:+d} "
        f"({inflate / max(tick_agg_all['n_total'], 1) * 100:+.1f}% vs tick).** "
        f"Notify-quality inflation: "
        f"{legacy_agg_all['n_notify'] - tick_agg_all['n_notify']:+d}."
    )
    lines.append("")

    lines.append("## Per-instrument outcome (A/A+ only, NOTIFY_QUALITIES gated)")
    lines.append("")
    lines.append("| Instrument | Mode | n | Closed | Win rate | Mean R | Total R |")
    lines.append("|---|---|---:|---:|---:|---:|---:|")
    for sym in args.instruments:
        for label, agg in (("legacy", legacy_agg_per[sym]), ("tick", tick_agg_per[sym])):
            lines.append(
                f"| {sym} | {label} | {agg['n_notify']} | {agg['n_closed']} | "
                f"{agg['win_rate']:.1%} | {agg['mean_r']:+.3f} | {agg['total_r']:+.2f} |"
            )
    lines.append("")
    lines.append("Combined:")
    lines.append("")
    lines.append("| Mode | n | Closed | Win rate | Mean R | Total R |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    lines.append(
        f"| legacy | {legacy_agg_all['n_notify']} | {legacy_agg_all['n_closed']} | "
        f"{legacy_agg_all['win_rate']:.1%} | {legacy_agg_all['mean_r']:+.3f} | "
        f"{legacy_agg_all['total_r']:+.2f} |"
    )
    lines.append(
        f"| tick | {tick_agg_all['n_notify']} | {tick_agg_all['n_closed']} | "
        f"{tick_agg_all['win_rate']:.1%} | {tick_agg_all['mean_r']:+.3f} | "
        f"{tick_agg_all['total_r']:+.2f} |"
    )
    lines.append("")
    delta_mr = legacy_agg_all["mean_r"] - tick_agg_all["mean_r"]
    lines.append(
        f"**Mean-R inflation in legacy: {delta_mr:+.3f}** (legacy "
        f"{legacy_agg_all['mean_r']:+.3f} vs tick {tick_agg_all['mean_r']:+.3f}). "
        "This is the bias the Sprint 6.5 / 6.6 numbers carried."
    )
    lines.append("")

    lines.append("## Identity-level diff")
    lines.append("")
    lines.append(f"- Identities in both modes: **{overlap_diff['n_common']}**")
    lines.append(
        f"- Legacy-only (phantoms — would never have been emitted in real time): "
        f"**{len(legacy_only)}**"
    )
    lines.append(
        f"- Tick-only (transient-cluster winners legacy's dedupe collapses): "
        f"**{len(tick_only)}**"
    )
    lines.append("")
    lines.append(
        "Tick-only is **not** a leak signal. The legacy run is a single "
        "post-killzone call: its sweep dedupe operates on the full "
        "killzone window and only the deepest representative of each "
        "price-time cluster survives. The tick simulator emits a setup "
        "at the moment each cluster's *current* deepest representative "
        "qualifies — and locks that identity at first emission. If a "
        "deeper sweep appears later in the same cluster it is a "
        "**different** identity (``sweep_candle_time_utc`` and "
        "``swept_level_price`` change), so the simulator emits a new "
        "setup and locks that one independently. Both events would "
        "have triggered separate notifications in the production "
        "scheduler — legacy collapses them into one. The audit at "
        "``calibration/audit_tick_simulator.py`` proves the tick path "
        "is leak-free: 53/53 setups in its pool emit at exactly "
        "``next_5min_tick_after(mss_confirm)`` with bit-identical fields."
    )
    lines.append("")
    lines.append(
        "Among the identities present in both, the per-field deltas (counts of "
        "setups that differ on each axis):"
    )
    lines.append("")
    lines.append("| Field | # changed |")
    lines.append("|---|---:|")
    lines.append(f"| poi_type (FVG ↔ OrderBlock) | {overlap_diff['poi_changed']} |")
    lines.append(f"| entry_price | {overlap_diff['entry_changed']} |")
    lines.append(f"| stop_loss | {overlap_diff['sl_changed']} |")
    lines.append(f"| tp_runner_rr | {overlap_diff['tp_runner_rr_changed']} |")
    lines.append(f"| quality | {overlap_diff['quality_changed']} |")
    lines.append(
        f"| **quality demotions A/A+ → B (would NOT have notified in real time)** "
        f"| **{overlap_diff['quality_demotions_to_b']}** |"
    )
    lines.append("")

    lines.append("## Interpretation")
    lines.append("")
    lines.append(
        "Three distinct effects separate the legacy and tick paths. They "
        "do **not** all push in the same direction:"
    )
    lines.append("")
    lines.append(
        f"1. **Phantom setups (legacy-only)** — {len(legacy_only)} identities "
        "the legacy scan emits that production would never have produced. "
        "Each one's outcome is pure noise added to the legacy aggregates."
    )
    lines.append(
        f"2. **In-flight quality / RR inflation on overlapping identities** — "
        f"{overlap_diff['quality_changed']} setups exist in both modes but with "
        f"different fields. Of those, {overlap_diff['quality_demotions_to_b']} "
        "would not have been notified (legacy inflated to A/A+, tick at B); "
        "the remainder share a quality tier but typically have a tighter "
        "FVG / smaller SL / larger RR in legacy because the detector picked "
        "a POI that hadn't yet formed at the production scheduler tick."
    )
    lines.append(
        f"3. **Cluster-collapse undercounting (tick-only)** — {len(tick_only)} "
        "identities the tick path emits as separate notifications that "
        "legacy's sweep dedupe folded into a single setup. These are real "
        "production events the legacy backtest never reports; their "
        "outcomes (and risk consumption) are missing from the legacy "
        "aggregates entirely."
    )
    lines.append("")
    lines.append(
        "Effects (1) and (2) bias legacy mean-R **upward** vs the tick "
        "ground truth; effect (3) biases legacy mean-R **away** from the "
        "production reality in whichever direction the cluster-collapsed "
        "trades fall on average. The aggregate sign of the bias depends on "
        "the per-instrument outcome distribution; it is not a one-line "
        "answer. The numbers in this sample do not extrapolate cleanly to "
        f"the full 10y space (the sample is {args.n_dates} dates per "
        "instrument vs ~2400 trading dates per instrument total) but the "
        "**per-trade** delta on overlapping identities is structural and "
        "applies to any backtest using the legacy path."
    )

    path.write_text("\n".join(lines) + "\n")
    print()
    print(path.read_text())
    print(f"Report written to: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
