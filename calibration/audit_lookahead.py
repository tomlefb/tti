"""Look-ahead audit for the TJR setup detector.

Goal
----
Verify that a setup observed in a historical backtest (full data
available to the detector) would have been produced identically in real
time, when only data up to the moment of MSS confirmation is available.
Any field that differs between the two runs implies the detector used
future data to commit the historical setup — a look-ahead bug that
would silently invalidate every backtest.

Method
------
1. Phase A — build the production-truthful setup pool. Across a sample
   of trading dates (10y XAUUSD/NDX100 Databento fixtures, 60-day
   lookback), run a legacy scan (``now_utc=None``) to find candidate
   MSS-confirm times, then for each unique scheduler tick those imply,
   re-run the detector with ``now_utc=tick`` on the same full window.
   The setups returned by these now_utc-bounded runs are exactly what
   the production scheduler would have emitted at the corresponding
   ticks — phantom setups produced solely by the legacy leak are
   correctly absent.
2. Phase B — randomly sample N setups from the pool. For each setup
   at MSS confirm time T:
     - Slice the four OHLC frames to keep rows whose ``time <= T``
       (the MSS candle is the latest visible — one M5 candle past it
       has not yet closed at scheduler tick ``T+5min``).
     - Re-run ``build_setup_candidates`` for the same target date,
       passing ``now_utc = next_5min_tick_after(T)``.
     - Locate the matching setup by strict key (symbol, killzone,
       direction, mss_confirm, sweep_candle, swept_level_price).
     - Compare entry/SL/TP/swept-level/POI/quality fields.
3. Write a report listing clean vs suspect setups. With the now_utc
   fix in place we expect 30/30 clean: the detector is deterministic
   under the now_utc bound regardless of whether df_m5 carries unused
   future data.

Output: ``calibration/runs/lookahead_audit_<UTC-timestamp>.txt``.

This script is read-only on detector code — its job is to tell us
whether the detector is clean, not to fix anything.
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

from src.detection.setup import Setup, build_setup_candidates  # noqa: E402

_DEFAULT_FIXTURE_DIR = (
    _REPO_ROOT / "tests" / "fixtures" / "historical_extended" / "processed_adjusted"
)
_FIXTURE_DIR = Path(os.environ.get("TTI_FIXTURE_DIR", str(_DEFAULT_FIXTURE_DIR)))
_RUNS_DIR = _REPO_ROOT / "calibration" / "runs"
_TZ_PARIS = ZoneInfo("Europe/Paris")
_PRICE_TOL = 1e-6


# ---------------------------------------------------------------------------
# Settings — operator-validated values from config/settings.py.example.
# Identical to run_extended_10y_backtest._settings() so the audit runs
# on the same parameterisation as the production backtest.
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Fixture loading + windowing — same pattern as the extended-10y backtest.
# ---------------------------------------------------------------------------
def _load_instrument(symbol: str) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for tf in ("D1", "H4", "H1", "M5"):
        df = pd.read_parquet(_FIXTURE_DIR / f"{symbol}_{tf}.parquet")
        if df["time"].dt.tz is None:
            df["time"] = df["time"].dt.tz_localize("UTC")
        out[tf] = df.sort_values("time").reset_index(drop=True)
    return out


class FixtureCache:
    """Pre-built tz-naive datetime64 arrays for fast searchsorted slicing."""

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
    """Smallest 5-minute-aligned UTC instant strictly greater than ``t``.

    Models the production scheduler's APScheduler 5-minute cron that
    fires at :00, :05, :10, ..., :55. For an ``mss_confirm`` candle
    open time aligned to a 5-min boundary, the candle closes at
    ``mss_confirm + 5min`` (also aligned), so this returns
    ``mss_confirm + 5min`` — the soonest scheduler tick at which the
    just-closed MSS candle is observable.
    """
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    minute_floor = t.replace(second=0, microsecond=0, minute=(t.minute // 5) * 5)
    if minute_floor <= t:
        minute_floor = minute_floor + timedelta(minutes=5)
    return minute_floor


# ---------------------------------------------------------------------------
# Setup signature — every field whose value should be identical between
# the full-data run and the truncated re-run.
# ---------------------------------------------------------------------------
def _setup_key(s: Setup) -> tuple:
    """Stable identity for a setup.

    The orchestrator iterates over every bias-aligned sweep in the killzone
    and emits one setup per successful sweep, so two distinct sweeps can
    both produce an MSS at the same candle (different swept levels →
    different sweeps → independent setups with the same
    ``mss_confirm_candle_time_utc``). The sweep candle time + the swept
    level price together pin down the originating sweep and therefore the
    setup itself.
    """
    return (
        s.symbol,
        s.killzone,
        s.direction,
        s.mss.mss_confirm_candle_time_utc,
        s.sweep.sweep_candle_time_utc,
        round(float(s.swept_level_price), 6),
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


# ---------------------------------------------------------------------------
# Detection wrappers.
# ---------------------------------------------------------------------------
def _detect(
    symbol: str,
    frames: dict[str, pd.DataFrame],
    target_date: date,
    settings,
    *,
    now_utc: datetime | None = None,
) -> list[Setup]:
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


# ---------------------------------------------------------------------------
# Main audit loop.
# ---------------------------------------------------------------------------
def run_audit(
    instruments: list[str],
    n_samples: int,
    seed: int,
    start_d: date,
    end_d: date,
    n_dates: int,
    days_lookback: int = 60,
) -> dict:
    rng = random.Random(seed)
    settings = _settings()

    print(f"Loading fixtures from {_FIXTURE_DIR}")
    fixtures = {sym: FixtureCache(_load_instrument(sym)) for sym in instruments}

    # Step A — build the production-truthful setup pool. Two-pass per
    # date: a legacy scan (now_utc=None) discovers candidate
    # ``mss_confirm`` times T; for EACH such T we then re-run the
    # detector with ``now_utc = next_5min_tick_after(T)`` on the full
    # window and keep only setups whose own ``mss_confirm`` equals T.
    # Coupling the discovery now_utc to T (instead of running once per
    # unique tick and accepting whatever surfaces) is essential: a
    # setup at T_a discovered under tick(T_b≠T_a) may not reproduce
    # under tick(T_a) — different now_utc, different FVG/sweep bounds
    # → different setup. Phase B will use ``now_utc = next_5min_tick_after(T)``,
    # so the pool must too.
    all_setups: list[Setup] = []
    seen_keys: set[tuple] = set()
    for sym in instruments:
        cache = fixtures[sym]
        all_dates = _trading_dates_for(cache.bundle["M5"])
        in_range = [d for d in all_dates if start_d <= d <= end_d]
        date_sample = sorted(rng.sample(in_range, k=min(n_dates, len(in_range))))
        print(f"  [{sym}] discovering candidate ticks across {len(date_sample)} sampled dates ...")
        for d in date_sample:
            end_utc = _eod_paris_utc(d)
            sliced = cache.slice_window(end_utc, days_lookback)
            try:
                legacy = _detect(sym, sliced, d, settings, now_utc=None)
            except Exception as exc:  # pragma: no cover — edge data only
                print(f"    skip {sym} {d}: legacy scan raised {exc!r}")
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
                except Exception as exc:  # pragma: no cover — edge data only
                    print(f"    skip {sym} {d} truthful@{tick}: {exc!r}")
                    continue
                for s in truthful:
                    if s.mss.mss_confirm_candle_time_utc != T_leg:
                        continue
                    k = _setup_key(s)
                    if k in seen_keys:
                        continue
                    seen_keys.add(k)
                    all_setups.append(s)
        print(
            f"  [{sym}] cumulative truthful setups: "
            f"{sum(1 for s in all_setups if s.symbol == sym)}"
        )

    print(f"Total truthful setups collected: {len(all_setups)}")
    if len(all_setups) == 0:
        raise SystemExit("No setups collected — cannot run the audit.")

    # Step B — sample setups.
    sample = rng.sample(all_setups, k=min(n_samples, len(all_setups)))

    # Step C — verify each on a slice that holds NO data past the MSS
    # confirm candle. The detector with the same now_utc must reproduce
    # the truthful setup bit-identically; any divergence implies a
    # remaining leak path that the now_utc bound failed to catch.
    clean: list[Setup] = []
    suspect: list[dict] = []
    skipped: list[dict] = []

    for i, original in enumerate(sample, 1):
        sym = original.symbol
        T = original.mss.mss_confirm_candle_time_utc
        tick = _next_5min_tick_after(T)
        target_d = _paris_date(T)
        # Phase A's truthful run sliced the fixture from
        # eod_paris_utc(target_d) - days_lookback to eod_paris_utc.
        # We MUST start from the same earliest candle here; otherwise
        # the ATR Wilder seed differs and FVG ``size_atr_ratio`` /
        # swing-amplitude tests can disagree on candles that should be
        # identical. Achieve this by reusing the same wide slice and
        # then truncating the M5/H1/H4/D1 frames to ``time <= T`` in
        # memory — the detector then sees no candles past T but
        # computes ATR on exactly the same underlying data.
        sliced_full = fixtures[sym].slice_window(_eod_paris_utc(target_d), days_lookback)
        sliced = {tf: df.loc[df["time"] <= T] for tf, df in sliced_full.items()}
        try:
            re_setups = _detect(sym, sliced, target_d, settings, now_utc=tick)
        except Exception as exc:
            skipped.append(
                {
                    "original": original,
                    "reason": f"re-run raised: {exc!r}",
                }
            )
            continue

        key = _setup_key(original)
        match: Setup | None = None
        for s in re_setups:
            if _setup_key(s) == key:
                match = s
                break

        if match is None:
            suspect.append(
                {
                    "original": original,
                    "issue": "no matching setup in re-run",
                    "re_run_count": len(re_setups),
                    "re_run_keys": [_setup_key(s) for s in re_setups],
                    "diffs": [],
                }
            )
            continue

        a = _setup_signature(original)
        b = _setup_signature(match)
        diffs = _diff(a, b)
        if not diffs:
            clean.append(original)
        else:
            suspect.append(
                {
                    "original": original,
                    "issue": "field divergence",
                    "diffs": diffs,
                    "re_run_count": len(re_setups),
                    "re_run_keys": [_setup_key(s) for s in re_setups],
                }
            )
        if i % 5 == 0 or i == len(sample):
            print(f"  audited {i}/{len(sample)} ; clean={len(clean)} suspect={len(suspect)}")

    return {
        "clean": clean,
        "suspect": suspect,
        "skipped": skipped,
        "total_setups_pool": len(all_setups),
        "by_symbol_pool": dict(_count_by_symbol(all_setups)),
    }


def _count_by_symbol(setups: list[Setup]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for s in setups:
        out[s.symbol] += 1
    return out


# ---------------------------------------------------------------------------
# Reporting.
# ---------------------------------------------------------------------------
def _write_report(args: argparse.Namespace, result: dict) -> Path:
    _RUNS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    path = _RUNS_DIR / f"lookahead_audit_{ts}.txt"

    clean = result["clean"]
    suspect = result["suspect"]
    skipped = result["skipped"]

    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("TJR detector — look-ahead audit")
    lines.append(f"  generated   : {datetime.now(UTC).isoformat()}")
    lines.append(f"  instruments : {','.join(args.instruments)}")
    lines.append(f"  date range  : {args.start} -> {args.end}")
    lines.append(f"  date sample : {args.n_dates} per instrument")
    lines.append(f"  setup pool  : {result['total_setups_pool']} ({result['by_symbol_pool']})")
    lines.append(f"  audited     : {args.n_samples} (seed={args.seed})")
    lines.append(
        "  slice rule  : Phase B keeps rows with time <= mss_confirm; "
        "now_utc = next_5min_tick_after(mss_confirm)"
    )
    lines.append(f"  lookback    : {args.days_lookback} days")
    lines.append("=" * 78)
    lines.append("")
    lines.append(f"Clean    : {len(clean)}")
    lines.append(f"Suspect  : {len(suspect)}")
    lines.append(f"Skipped  : {len(skipped)}")
    total = len(clean) + len(suspect)
    lines.append(f"Compared : {total}")
    lines.append("")

    if suspect:
        lines.append("SUSPECT SETUPS")
        lines.append("-" * 78)
        for entry in suspect:
            o: Setup = entry["original"]
            lines.append(
                f"[{o.symbol}] {o.killzone} {o.direction} "
                f"mss_confirm={o.mss.mss_confirm_candle_time_utc.isoformat()} "
                f"quality={o.quality}"
            )
            lines.append(f"  issue: {entry['issue']}")
            lines.append(f"  re-run setup count at slice end: {entry.get('re_run_count')}")
            if entry["issue"] == "no matching setup in re-run":
                for k in entry.get("re_run_keys", []):
                    lines.append(f"  re-run key: {k}")
            for d in entry.get("diffs", []):
                lines.append(d)
            lines.append("")

    if skipped:
        lines.append("SKIPPED")
        lines.append("-" * 78)
        for entry in skipped:
            o = entry["original"]
            lines.append(
                f"[{o.symbol}] {o.mss.mss_confirm_candle_time_utc.isoformat()} "
                f"reason={entry['reason']}"
            )
        lines.append("")

    lines.append("VERDICT")
    lines.append("-" * 78)
    if not suspect and not skipped:
        lines.append(
            "ALL CLEAN — every audited setup is reproduced bit-identically by the "
            "detector when df_m5 holds no candles past mss_confirm. The "
            "now_utc bound is consistent on this sample: full-data and truncated "
            "runs agree, so no future data leaks into historical detections."
        )
    elif suspect:
        lines.append(
            "SUSPECT — at least one setup diverged. Inspect the divergences "
            "above. A look-ahead bias may be present."
        )
    else:
        lines.append("INCONCLUSIVE — re-runs raised on some setups; manual review needed.")

    path.write_text("\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instruments", default="XAUUSD,NDX100")
    parser.add_argument("--n-samples", type=int, default=30)
    parser.add_argument(
        "--n-dates",
        type=int,
        default=180,
        help="trading dates per instrument to sample for the setup pool",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--start", default="2016-01-03")
    parser.add_argument("--end", default="2026-04-29")
    parser.add_argument("--days-lookback", type=int, default=60)
    args = parser.parse_args()

    args.instruments = [s.strip() for s in args.instruments.split(",") if s.strip()]
    start_d = date.fromisoformat(args.start)
    end_d = date.fromisoformat(args.end)

    result = run_audit(
        instruments=args.instruments,
        n_samples=args.n_samples,
        seed=args.seed,
        start_d=start_d,
        end_d=end_d,
        n_dates=args.n_dates,
        days_lookback=args.days_lookback,
    )
    path = _write_report(args, result)
    print()
    print(path.read_text())
    print(f"Report written to: {path}")
    return 0 if not result["suspect"] and not result["skipped"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
