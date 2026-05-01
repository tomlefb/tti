"""Tick-by-tick audit — strengthens ``audit_lookahead.py``.

The single-tick audit at ``calibration/audit_lookahead.py`` verifies
that, at the production-truthful tick ``next_5min_tick_after(mss_confirm)``,
the detector reproduces the historical setup bit-identically when
``df_m5`` is truncated to ``time <= mss_confirm``. That check passes
on 53/53 setups after the four leak fixes, but it has a known
blindspot: at the truthful tick, ``detect_mss``'s "first qualifying
candle" iteration coincides with ``mss_confirm``, so any forward
data the function *could* read is irrelevant. The simulator iterates
**earlier ticks** too, where the same forward access becomes a leak.

This script closes the blindspot. For each truthful setup at MSS
confirm time T:

1. Identify the *correct* emission tick — ``next_5min_tick_after(T)``.
2. Run the production-faithful tick simulator across the full
   killzone that contains T, tracking the first tick at which each
   setup identity surfaces.
3. Verify the truthful setup is emitted at exactly the correct tick
   (no earlier, no later) and that every downstream field matches.

Failure modes:

- Setup emitted *earlier* than the correct tick: a forward leak —
  the detector saw the MSS candle before its close.
- Setup emitted *later*: scheduler-cadence mismatch or the simulator
  is locking on a sibling identity.
- Field divergence at the correct tick: a residual leak in a
  field-determining sub-component (POI choice, sweep dedupe, etc.).

A 53/53 pass means the leak-free contract holds end-to-end:
production scheduler at tick T emits the same Setup the historical
backtest sees, regardless of which path produced the truthful pool.

Output: ``calibration/runs/audit_tick_simulator_<UTC-timestamp>.txt``.

Usage::

    python calibration/audit_tick_simulator.py \\
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
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.backtest.tick_simulator import _identity  # noqa: E402
from src.detection.liquidity import paris_session_to_utc  # noqa: E402
from src.detection.setup import Setup, build_setup_candidates  # noqa: E402

_DEFAULT_FIXTURE_DIR = (
    _REPO_ROOT / "tests" / "fixtures" / "historical_extended" / "processed_adjusted"
)
_FIXTURE_DIR = Path(os.environ.get("TTI_FIXTURE_DIR", str(_DEFAULT_FIXTURE_DIR)))
_RUNS_DIR = _REPO_ROOT / "calibration" / "runs"
_TZ_PARIS = ZoneInfo("Europe/Paris")
_PRICE_TOL = 1e-6


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


def _trading_dates_for(df_m5: pd.DataFrame) -> list[date]:
    times = pd.to_datetime(df_m5["time"], utc=True)
    paris_dates = sorted(set(times.dt.tz_convert(_TZ_PARIS).dt.date))
    return [d for d in paris_dates if d.weekday() < 5]


def _eod_paris_utc(d: date) -> datetime:
    eod = datetime.combine(d, time(23, 59))
    return eod.replace(tzinfo=_TZ_PARIS).astimezone(UTC)


def _paris_date(utc_dt: datetime) -> date:
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=UTC)
    return utc_dt.astimezone(_TZ_PARIS).date()


def _next_5min_tick_after(t: datetime) -> datetime:
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    floored = t.replace(second=0, microsecond=0, minute=(t.minute // 5) * 5)
    if floored <= t:
        floored = floored + timedelta(minutes=5)
    return floored


def _detect(symbol, frames, target_date, settings, *, now_utc=None) -> list[Setup]:
    return build_setup_candidates(
        df_h4=frames["H4"],
        df_h1=frames["H1"],
        df_m5=frames["M5"],
        df_d1=frames["D1"],
        target_date=target_date,
        symbol=symbol,
        settings=settings,
        now_utc=now_utc,
    )


def _setup_signature(s: Setup) -> dict:
    poi_kind = type(s.poi).__name__
    if poi_kind == "FVG":
        poi = {
            "kind": "FVG",
            "proximal": float(s.poi.proximal),
            "distal": float(s.poi.distal),
            "size": float(s.poi.size),
            "size_atr_ratio": float(s.poi.size_atr_ratio),
        }
    else:
        poi = {
            "kind": poi_kind,
            "proximal": float(s.poi.proximal),
            "distal": float(s.poi.distal),
            "candle_time_utc": s.poi.candle_time_utc.isoformat(),
        }
    return {
        "timestamp_utc": s.timestamp_utc.isoformat(),
        "direction": s.direction,
        "daily_bias": s.daily_bias,
        "killzone": s.killzone,
        "swept_level_price": float(s.swept_level_price),
        "swept_level_type": s.swept_level_type,
        "swept_level_strength": s.swept_level_strength,
        "sweep_candle_time_utc": s.sweep.sweep_candle_time_utc.isoformat(),
        "sweep_extreme_price": float(s.sweep.sweep_extreme_price),
        "sweep_return_candle_time_utc": s.sweep.return_candle_time_utc.isoformat(),
        "mss_confirm_candle_time_utc": s.mss.mss_confirm_candle_time_utc.isoformat(),
        "mss_broken_swing_time_utc": s.mss.broken_swing_time_utc.isoformat(),
        "mss_broken_swing_price": float(s.mss.broken_swing_price),
        "mss_displacement_body_ratio": float(s.mss.displacement_body_ratio),
        "poi_type": s.poi_type,
        "poi": poi,
        "entry_price": float(s.entry_price),
        "stop_loss": float(s.stop_loss),
        "target_level_type": s.target_level_type,
        "tp_runner_price": float(s.tp_runner_price),
        "tp_runner_rr": float(s.tp_runner_rr),
        "tp1_price": float(s.tp1_price),
        "tp1_rr": float(s.tp1_rr),
        "quality": s.quality,
        "confluences": list(s.confluences),
    }


def _diff(a: dict, b: dict, prefix: str = "") -> list[str]:
    diffs: list[str] = []
    keys = set(a.keys()) | set(b.keys())
    for k in sorted(keys):
        path = f"{prefix}.{k}" if prefix else k
        va, vb = a.get(k), b.get(k)
        if isinstance(va, dict) and isinstance(vb, dict):
            diffs.extend(_diff(va, vb, prefix=path))
            continue
        if isinstance(va, float) and isinstance(vb, float):
            if abs(va - vb) > _PRICE_TOL:
                diffs.append(f"  - {path}: {va!r} vs {vb!r} (delta {vb - va:+g})")
            continue
        if va != vb:
            diffs.append(f"  - {path}: {va!r} vs {vb!r}")
    return diffs


def _build_truthful_pool(
    instruments, n_dates, seed, start_d, end_d, days_lookback, settings, fixtures
):
    """Reproduces ``audit_lookahead.py`` Phase A. Returns (pool, sliced_cache).

    ``sliced_cache`` is keyed by (symbol, target_date) and holds the
    same wide-window slice the truthful run used. We re-use it during
    the tick-by-tick verification so the simulator works against the
    very same df Phase A produced the truthful setup with — otherwise
    ATR-seeding differences could produce false-positive divergences.
    """
    rng = random.Random(seed)
    pool: list[Setup] = []
    seen_keys: set[tuple] = set()
    sliced_cache: dict[tuple, dict[str, pd.DataFrame]] = {}
    for sym in instruments:
        cache = fixtures[sym]
        all_dates = _trading_dates_for(cache.bundle["M5"])
        in_range = [d for d in all_dates if start_d <= d <= end_d]
        date_sample = sorted(rng.sample(in_range, k=min(n_dates, len(in_range))))
        print(f"  [{sym}] discovering candidate ticks across {len(date_sample)} dates ...")
        for d in date_sample:
            sliced = cache.slice_window(_eod_paris_utc(d), days_lookback)
            try:
                legacy = _detect(sym, sliced, d, settings, now_utc=None)
            except Exception as exc:  # pragma: no cover — edge data
                print(f"    skip {sym} {d}: legacy raised {exc!r}")
                continue
            seen_T: set[datetime] = set()
            for leg in legacy:
                T_leg = leg.mss.mss_confirm_candle_time_utc
                if T_leg in seen_T:
                    continue
                seen_T.add(T_leg)
                tick = _next_5min_tick_after(T_leg)
                try:
                    truthful = _detect(sym, sliced, d, settings, now_utc=tick)
                except Exception as exc:  # pragma: no cover
                    print(f"    skip {sym} {d} truthful@{tick}: {exc!r}")
                    continue
                for s in truthful:
                    if s.mss.mss_confirm_candle_time_utc != T_leg:
                        continue
                    k = _identity(s)
                    if k in seen_keys:
                        continue
                    seen_keys.add(k)
                    pool.append(s)
                    sliced_cache.setdefault((sym, d), sliced)
        print(f"  [{sym}] truthful setups: " f"{sum(1 for s in pool if s.symbol == sym)}")
    return pool, sliced_cache


def _simulate_killzone_with_first_tick(
    sliced: dict[str, pd.DataFrame],
    target_date: date,
    kz_start_utc: datetime,
    kz_end_utc: datetime,
    symbol: str,
    settings,
    *,
    tick_interval_minutes: int = 5,
) -> dict[tuple, tuple[datetime, Setup]]:
    """Run the production-faithful tick simulator over a single
    killzone, recording for each setup identity the *first tick* at
    which it surfaced. Identity here matches
    ``backtest.tick_simulator._identity``. Used by the tick-by-tick
    audit to verify that a truthful setup is emitted at exactly its
    expected scheduler tick.
    """
    interval = timedelta(minutes=tick_interval_minutes)
    tick = kz_start_utc + interval
    last_tick = kz_end_utc + interval
    seen: dict[tuple, tuple[datetime, Setup]] = {}
    while tick <= last_tick:
        setups = _detect(symbol, sliced, target_date, settings, now_utc=tick)
        for s in setups:
            if not (kz_start_utc <= s.mss.mss_confirm_candle_time_utc <= kz_end_utc):
                continue
            key = _identity(s)
            if key in seen:
                continue
            seen[key] = (tick, s)
        tick += interval
    return seen


def run_audit(
    instruments,
    n_dates,
    seed,
    start_d,
    end_d,
    days_lookback,
    tick_interval_minutes,
):
    settings = _settings()
    print(f"Loading fixtures from {_FIXTURE_DIR}")
    fixtures = {sym: FixtureCache(_load_instrument(sym)) for sym in instruments}

    print("Phase A — building truthful pool ...")
    pool, sliced_cache = _build_truthful_pool(
        instruments, n_dates, seed, start_d, end_d, days_lookback, settings, fixtures
    )
    print(f"Truthful pool size: {len(pool)}")

    print("Phase B — tick-by-tick verification ...")
    by_killzone: dict[tuple, list[Setup]] = defaultdict(list)
    for s in pool:
        target_d = _paris_date(s.mss.mss_confirm_candle_time_utc)
        kz_session = settings.KILLZONE_LONDON if s.killzone == "london" else settings.KILLZONE_NY
        kz_start_utc, kz_end_utc = paris_session_to_utc(target_d, kz_session)
        by_killzone[(s.symbol, target_d, kz_start_utc, kz_end_utc)].append(s)

    print(f"  {len(by_killzone)} distinct killzones to simulate")
    clean = []
    suspect = []
    for i, ((sym, target_d, kz_start_utc, kz_end_utc), setups_in_kz) in enumerate(
        sorted(by_killzone.items()), 1
    ):
        sliced = sliced_cache.get((sym, target_d))
        if sliced is None:
            sliced = fixtures[sym].slice_window(_eod_paris_utc(target_d), days_lookback)
        first_tick_per_id = _simulate_killzone_with_first_tick(
            sliced=sliced,
            target_date=target_d,
            kz_start_utc=kz_start_utc,
            kz_end_utc=kz_end_utc,
            symbol=sym,
            settings=settings,
            tick_interval_minutes=tick_interval_minutes,
        )
        for original in setups_in_kz:
            T = original.mss.mss_confirm_candle_time_utc
            expected_tick = _next_5min_tick_after(T)
            key = _identity(original)
            entry = first_tick_per_id.get(key)
            if entry is None:
                suspect.append(
                    {
                        "original": original,
                        "issue": "simulator did not emit this identity",
                        "expected_tick": expected_tick,
                        "diffs": [],
                    }
                )
                continue
            actual_tick, actual_setup = entry
            if actual_tick != expected_tick:
                suspect.append(
                    {
                        "original": original,
                        "issue": (
                            f"emission tick mismatch: expected {expected_tick.isoformat()} "
                            f"got {actual_tick.isoformat()}"
                        ),
                        "expected_tick": expected_tick,
                        "actual_tick": actual_tick,
                        "diffs": [],
                    }
                )
                continue
            sig_a = _setup_signature(original)
            sig_b = _setup_signature(actual_setup)
            diffs = _diff(sig_a, sig_b)
            if not diffs:
                clean.append(original)
            else:
                suspect.append(
                    {
                        "original": original,
                        "issue": "field divergence at expected tick",
                        "expected_tick": expected_tick,
                        "diffs": diffs,
                    }
                )
        if i % 5 == 0 or i == len(by_killzone):
            print(
                f"  killzone {i}/{len(by_killzone)} : " f"clean={len(clean)} suspect={len(suspect)}"
            )

    return {
        "pool_size": len(pool),
        "clean": clean,
        "suspect": suspect,
        "by_symbol_pool": {sym: sum(1 for s in pool if s.symbol == sym) for sym in instruments},
    }


def _write_report(args, result) -> Path:
    _RUNS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    path = _RUNS_DIR / f"audit_tick_simulator_{ts}.txt"

    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("TJR detector — tick-by-tick audit (production-faithful simulator)")
    lines.append(f"  generated   : {datetime.now(UTC).isoformat()}")
    lines.append(f"  instruments : {','.join(args.instruments)}")
    lines.append(f"  date range  : {args.start} -> {args.end}")
    lines.append(f"  date sample : {args.n_dates} per instrument (seed={args.seed})")
    lines.append(f"  pool size   : {result['pool_size']} ({result['by_symbol_pool']})")
    lines.append(f"  tick cadence: {args.tick_interval_minutes} min")
    lines.append(f"  lookback    : {args.days_lookback} days")
    lines.append("=" * 78)
    lines.append("")
    n_clean = len(result["clean"])
    n_suspect = len(result["suspect"])
    lines.append(f"Clean    : {n_clean}")
    lines.append(f"Suspect  : {n_suspect}")
    lines.append(f"Compared : {n_clean + n_suspect}")
    lines.append("")
    if result["suspect"]:
        lines.append("SUSPECT")
        lines.append("-" * 78)
        for entry in result["suspect"]:
            o = entry["original"]
            lines.append(
                f"[{o.symbol}] {o.killzone} {o.direction} "
                f"mss_confirm={o.mss.mss_confirm_candle_time_utc.isoformat()} "
                f"quality={o.quality}"
            )
            lines.append(f"  issue: {entry['issue']}")
            if "actual_tick" in entry:
                lines.append(
                    f"  expected_tick={entry['expected_tick'].isoformat()}  actual={entry['actual_tick'].isoformat()}"
                )
            for d in entry.get("diffs", []):
                lines.append(d)
            lines.append("")

    lines.append("VERDICT")
    lines.append("-" * 78)
    if not result["suspect"]:
        lines.append(
            "ALL CLEAN — every truthful setup is emitted by the production-faithful "
            "tick simulator at exactly the scheduler tick "
            "next_5min_tick_after(mss_confirm), with bit-identical fields. The "
            "leak-free contract holds end-to-end across the full audited pool."
        )
    else:
        lines.append(
            "SUSPECT — at least one setup did not emit at the expected tick or "
            "with the expected fields. See details above; the divergence likely "
            "indicates a 5th leak that the single-tick audit missed."
        )

    path.write_text("\n".join(lines) + "\n")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instruments", default="XAUUSD,NDX100")
    parser.add_argument("--n-dates", type=int, default=400)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--start", default="2016-01-03")
    parser.add_argument("--end", default="2026-04-29")
    parser.add_argument("--days-lookback", type=int, default=60)
    parser.add_argument("--tick-interval-minutes", type=int, default=5)
    args = parser.parse_args()
    args.instruments = [s.strip() for s in args.instruments.split(",") if s.strip()]
    start_d = date.fromisoformat(args.start)
    end_d = date.fromisoformat(args.end)

    result = run_audit(
        instruments=args.instruments,
        n_dates=args.n_dates,
        seed=args.seed,
        start_d=start_d,
        end_d=end_d,
        days_lookback=args.days_lookback,
        tick_interval_minutes=args.tick_interval_minutes,
    )
    path = _write_report(args, result)
    print()
    print(path.read_text())
    print(f"Report written to: {path}")
    return 0 if not result["suspect"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
