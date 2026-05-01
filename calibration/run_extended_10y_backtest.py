"""Extended 10-year historical backtest on Databento continuous futures.

Validates the SMC/ICT edge over 2016-2026 instead of the 11-month MT5
window used in Sprint 6.5. Three instruments: XAUUSD (GC), NDX100 (NQ),
SPX500 (ES). For SPX500 we deliberately re-use the NDX100 instrument
config (same asset class, similar volatility profile) — explicit
assumption to test, not a calibration.

Detection settings = `config/settings.py.example` operator-validated
defaults (Sprint 3). NOTIFY_QUALITIES = ["A+", "A"] applied as in
Sprint 6.5. Rollover dates from each instrument's metadata are
excluded with a ±2h window (Paris dates whose UTC span touches the
window).

Two detection modes:

- ``--mode tick`` (default, post-Phase-B): the
  ``src.backtest.tick_simulator`` simulates a 5-min APScheduler firing
  across both killzones of each trading day, calling the detector
  with ``now_utc=tick`` so every forward-looking sub-search is bound
  to data the production scheduler would already have observed. Each
  setup identity locks at first emission. This matches the production
  scheduler exactly — the numbers it produces are what the live
  system would have generated in real time.
- ``--mode legacy``: the pre-Phase-B path — one call to
  ``build_setup_candidates`` per trading day with ``now_utc=None``.
  Documented as **leak-prone**; kept solely so we can A/B compare on
  the same fixtures and quantify the inflation legacy backtests
  carried (see
  ``calibration/runs/FINAL_lookahead_audit_phase_a_complete_2026-05-01.md``
  and the FVG / sweep / swing / MSS leak fixes in the
  ``feat/strategy-research`` history).

Output: calibration/runs/{TS}_extended_10y_backtest.md (gitignored).
Read-only on detector code; no parameter tuning.
"""

from __future__ import annotations

import json
import math
import os
import statistics
import sys
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.backtest.tick_simulator import simulate_target_date  # noqa: E402
from src.detection.setup import Setup, build_setup_candidates  # noqa: E402

# Fixture dir is overridable via env var so the same runner can be pointed
# at the raw stitched fixtures or the Panama-adjusted fixtures without
# duplicating code.
_DEFAULT_FIXTURE_DIR = _REPO_ROOT / "tests" / "fixtures" / "historical_extended" / "processed"
_FIXTURE_DIR = Path(os.environ.get("TTI_FIXTURE_DIR", str(_DEFAULT_FIXTURE_DIR)))
_RUNS_DIR = _REPO_ROOT / "calibration" / "runs"
_REPORT_TAG = os.environ.get("TTI_REPORT_TAG", "extended_10y_backtest")
_INSTRUMENTS = ["XAUUSD", "NDX100", "SPX500"]
_NOTIFY_QUALITIES = ("A+", "A")
_HORIZON_MINUTES = 24 * 60
_TZ_PARIS = ZoneInfo("Europe/Paris")
_ROLLOVER_HALF_WINDOW = timedelta(hours=2)
_TIMESTAMP = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")

# MT5 backtest reference (Sprint 6.5 filtered, 2025-07 to 2026-04).
# Source: calibration/runs/2026-04-29T07-12-33Z_backtest_filtered.md.
_MT5_REFERENCE = {
    "XAUUSD": {"setups": 14, "win_rate": 0.286, "mean_R": 0.576},
    "NDX100": {"setups": 15, "win_rate": 0.467, "mean_R": 1.381},
}
_MT5_OVERLAP_START = date(2025, 4, 1)
_MT5_OVERLAP_END = date(2026, 4, 30)


# ---------------------------------------------------------------------------
# Settings — operator-validated values from config/settings.py.example.
# SPX500 reuses NDX100 entry (explicit assumption — see module docstring).
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
            "SPX500": ndx_cfg,
        },
    )


# ---------------------------------------------------------------------------
# Fixture loading + rollover-window exclusion.
# ---------------------------------------------------------------------------
def _load_instrument(symbol: str) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for tf in ("D1", "H4", "H1", "M5"):
        df = pd.read_parquet(_FIXTURE_DIR / f"{symbol}_{tf}.parquet")
        # Detector & simulator use df["time"] as UTC tz-aware. Confirm.
        if df["time"].dt.tz is None:
            df["time"] = df["time"].dt.tz_localize("UTC")
        out[tf] = df
    return out


def _rollovers_utc(symbol: str) -> list[datetime]:
    """Read rollover dates from the active fixture's metadata, falling back
    to the raw `processed/` metadata if the active dir is an adjusted set
    (the adjusted metadata doesn't repeat rollover_dates)."""
    with open(_FIXTURE_DIR / f"{symbol}_metadata.json") as f:
        meta = json.load(f)
    if "rollover_dates" not in meta:
        with open(_DEFAULT_FIXTURE_DIR / f"{symbol}_metadata.json") as f:
            meta = json.load(f)
    return [datetime.fromisoformat(s) for s in meta["rollover_dates"]]


def _excluded_paris_dates(rollovers: list[datetime]) -> set[date]:
    """Paris dates whose UTC 0:00–24:00 span overlaps any rollover ±2h window.

    Rollovers always happen mid-session (typically 13:00–18:00 UTC), so this
    almost always excludes the rollover Paris date and occasionally an
    adjacent one if the rollover falls near midnight.
    """
    excluded: set[date] = set()
    for r in rollovers:
        win_start = r - _ROLLOVER_HALF_WINDOW
        win_end = r + _ROLLOVER_HALF_WINDOW
        # Walk the Paris dates that the [win_start, win_end] window touches.
        d_start = win_start.astimezone(_TZ_PARIS).date()
        d_end = win_end.astimezone(_TZ_PARIS).date()
        cur = d_start
        while cur <= d_end:
            excluded.add(cur)
            cur += timedelta(days=1)
    return excluded


def _trading_dates_for(df_m5: pd.DataFrame) -> list[date]:
    """Paris weekdays (Mon-Fri) present in the M5 fixture."""
    times = pd.to_datetime(df_m5["time"], utc=True)
    paris_dates = sorted(set(times.dt.tz_convert(_TZ_PARIS).dt.date))
    return [d for d in paris_dates if d.weekday() < 5]


# ---------------------------------------------------------------------------
# Per-cell windowing — the detector's internal _slice_frame_until is O(N)
# on the raw column, so we narrow each input frame to a 60-day window
# around the target date BEFORE calling the detector. Output is identical
# (older candles only matter via ATR/swing lookbacks of <30 days).
# ---------------------------------------------------------------------------
class FixtureCache:
    def __init__(self, bundle: dict[str, pd.DataFrame]):
        self.bundle = bundle
        self.times_ns: dict[str, np.ndarray] = {}
        for tf, df in bundle.items():
            ts = df["time"]
            self.times_ns[tf] = (
                ts.dt.tz_convert("UTC").dt.tz_localize(None).values.astype("datetime64[ns]")
            )

    def slice_until(self, end_utc: datetime, days_lookback: int) -> dict[str, pd.DataFrame]:
        end_np = np.datetime64(end_utc.astimezone(UTC).replace(tzinfo=None), "ns")
        start_np = end_np - np.timedelta64(days_lookback, "D")
        out: dict[str, pd.DataFrame] = {}
        for tf, df in self.bundle.items():
            ta = self.times_ns[tf]
            si = int(np.searchsorted(ta, start_np, side="left"))
            ei = int(np.searchsorted(ta, end_np, side="right"))
            out[tf] = df.iloc[si:ei]
        return out


# ---------------------------------------------------------------------------
# Outcome simulator — adapted from run_full_backtest._simulate_outcome with
# pre-built numpy arrays for performance (24h M5 horizon, partial-exit
# convention 50% TP1 / 50% TP_runner). Strict = sl_before_entry counts
# -1.0R, realistic = 0.0R (limit never filled).
# ---------------------------------------------------------------------------
class M5Cache:
    """Pre-built numpy arrays for fast outcome simulation.

    Avoids re-converting the full M5 datetime column on every setup. The
    timestamps are stored as tz-naive datetime64[ns] (UTC interpretation —
    pandas strips the tz on ``.values`` access).
    """

    def __init__(self, df_m5: pd.DataFrame):
        ts = df_m5["time"]
        self.times_ns: np.ndarray = (
            ts.dt.tz_convert("UTC").dt.tz_localize(None).values.astype("datetime64[ns]")
        )
        self.lows: np.ndarray = df_m5["low"].to_numpy(dtype="float64")
        self.highs: np.ndarray = df_m5["high"].to_numpy(dtype="float64")
        self.n: int = len(df_m5)


def _simulate_outcome(setup: Setup, m5: M5Cache) -> dict:
    setup_ts = np.datetime64(setup.timestamp_utc.astimezone(UTC).replace(tzinfo=None), "ns")
    start = int(np.searchsorted(m5.times_ns, setup_ts, side="left"))
    if start >= m5.n:
        return _no_data_outcome()

    horizon_end_ts = setup_ts + np.timedelta64(_HORIZON_MINUTES, "m")
    end = int(np.searchsorted(m5.times_ns, horizon_end_ts, side="right"))
    if end <= start:
        return _no_data_outcome()

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
        return {
            "outcome": "entry_not_hit",
            "realized_R_strict": 0.0,
            "realized_R_realistic": 0.0,
        }

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
        return {
            "outcome": "open_at_horizon",
            "realized_R_strict": 0.0,
            "realized_R_realistic": 0.0,
        }

    if same_tps:
        r = 0.5 * setup.tp1_rr + 0.5 * setup.tp_runner_rr
        return {
            "outcome": "tp_runner_hit",
            "realized_R_strict": r,
            "realized_R_realistic": r,
        }

    for j in range(tp1_idx + 1, n):
        if direction == "long":
            sl_now = lows[j] <= sl
            tpr_now = highs[j] >= tpr
        else:
            sl_now = highs[j] >= sl
            tpr_now = lows[j] <= tpr
        if sl_now:
            r = (setup.tp1_rr - 1.0) / 2.0
            return {
                "outcome": "tp1_hit_only",
                "realized_R_strict": r,
                "realized_R_realistic": r,
            }
        if tpr_now:
            r = 0.5 * setup.tp1_rr + 0.5 * setup.tp_runner_rr
            return {
                "outcome": "tp_runner_hit",
                "realized_R_strict": r,
                "realized_R_realistic": r,
            }

    r = (setup.tp1_rr - 1.0) / 2.0
    return {"outcome": "tp1_hit_only", "realized_R_strict": r, "realized_R_realistic": r}


def _no_data_outcome() -> dict:
    return {
        "outcome": "open_at_horizon",
        "realized_R_strict": 0.0,
        "realized_R_realistic": 0.0,
    }


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------
def _max_drawdown(cum: list[float]) -> float:
    if not cum:
        return 0.0
    peak = cum[0]
    worst = 0.0
    for v in cum:
        if v > peak:
            peak = v
        worst = min(worst, v - peak)
    return -worst


def _win_rate(rows: list[dict]) -> float:
    wins = sum(1 for r in rows if r["outcome"] in ("tp1_hit_only", "tp_runner_hit"))
    losses = sum(1 for r in rows if r["outcome"] in ("sl_hit", "sl_before_entry"))
    denom = wins + losses
    return wins / denom if denom else 0.0


def _mean_R(rows: list[dict], *, key: str = "realized_R_strict") -> float:
    vals = [r[key] for r in rows if r["outcome"] not in ("entry_not_hit", "open_at_horizon")]
    return sum(vals) / len(vals) if vals else 0.0


def _total_R(rows: list[dict], *, key: str = "realized_R_strict") -> float:
    return sum(r[key] for r in rows)


def _months_span(rows: list[dict], all_dates: list[date]) -> int:
    if not all_dates:
        return 0
    d0, d1 = min(all_dates), max(all_dates)
    return max((d1.year - d0.year) * 12 + (d1.month - d0.month) + 1, 1)


def _drawdown_R(rows: list[dict]) -> float:
    rs = sorted(rows, key=lambda r: r["timestamp_utc"])
    cum = []
    acc = 0.0
    for r in rs:
        acc += r["realized_R_strict"]
        cum.append(acc)
    return _max_drawdown(cum)


# ---------------------------------------------------------------------------
# Backtest core
# ---------------------------------------------------------------------------
_LOOKBACK_DAYS = 60


def _run_instrument(
    symbol: str,
    fixture_cache: FixtureCache,
    m5_cache: M5Cache,
    paris_dates: list[date],
    excluded: set[date],
    settings: SimpleNamespace,
    *,
    mode: str = "tick",
) -> tuple[list[dict], list[str], int, int]:
    rows: list[dict] = []
    errors: list[str] = []
    cells = 0
    skipped_rollover = 0
    progress_every = max(len(paris_dates) // 20, 1)
    for i, d in enumerate(paris_dates):
        if d in excluded:
            skipped_rollover += 1
            continue
        cells += 1
        if i % progress_every == 0:
            print(
                f"    {symbol} progress {i}/{len(paris_dates)} " f"(setups so far: {len(rows)})",
                flush=True,
            )
        # Slice each frame to a 60-day window ending at the day after `d`
        # (covers both killzones + 24h forward horizon for outcome eval).
        end_utc = datetime(d.year, d.month, d.day, tzinfo=UTC) + timedelta(days=2)
        window = fixture_cache.slice_until(end_utc, days_lookback=_LOOKBACK_DAYS)
        try:
            if mode == "tick":
                # Production-faithful path — simulate every 5-min
                # APScheduler firing inside both killzones, call the
                # detector with ``now_utc=tick`` and lock each
                # identity at first emission.
                setups = simulate_target_date(
                    df_h4=window["H4"],
                    df_h1=window["H1"],
                    df_m5=window["M5"],
                    df_d1=window["D1"],
                    target_date=d,
                    symbol=symbol,
                    settings=settings,
                )
            elif mode == "legacy":
                # Pre-Phase-B leak-prone path — one detector call per
                # day with ``now_utc=None``. Kept for A/B comparison
                # only; do NOT use these numbers for any decision.
                setups = build_setup_candidates(
                    df_h4=window["H4"],
                    df_h1=window["H1"],
                    df_m5=window["M5"],
                    df_d1=window["D1"],
                    target_date=d,
                    symbol=symbol,
                    settings=settings,
                )
            else:
                raise ValueError(f"unknown mode {mode!r}; expected 'tick' or 'legacy'")
        except Exception as exc:
            msg = f"{symbol} {d}: detection — {type(exc).__name__}: {exc}"
            errors.append(msg)
            sys.stderr.write(msg + "\n")
            continue
        for s in setups:
            if s.quality not in _NOTIFY_QUALITIES:
                continue
            try:
                outcome = _simulate_outcome(s, m5_cache)
            except Exception as exc:
                msg = f"{symbol} {d} {s.timestamp_utc}: simulate — {type(exc).__name__}: {exc}"
                errors.append(msg)
                sys.stderr.write(msg + "\n")
                continue
            rows.append(
                {
                    "instrument": symbol,
                    "date": d,
                    "timestamp_utc": s.timestamp_utc,
                    "killzone": s.killzone,
                    "direction": s.direction,
                    "quality": s.quality,
                    "tp1_rr": s.tp1_rr,
                    "tp_runner_rr": s.tp_runner_rr,
                    **outcome,
                }
            )
    return rows, errors, cells, skipped_rollover


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------
def _annual_return_at_1pct(setups_per_month: float, mean_R: float) -> float:
    """Approximate annual % return assuming 1% risk per trade."""
    return setups_per_month * 12.0 * mean_R * 0.01 * 100.0  # in %


def _section_per_instrument_summary(
    per_instrument: dict[str, list[dict]], cells_per: dict[str, int]
) -> list[str]:
    out = ["## Section 1 — Per-instrument summary (10 years)", ""]
    out.append(
        "| Instrument | Source | Cells | Months | Setups (A/A+) | Setups/month | "
        "Win rate | Mean R | Total R | Max DD |"
    )
    out.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    sources = {
        "XAUUSD": "GC continuous",
        "NDX100": "NQ continuous",
        "SPX500": "ES continuous",
    }
    for sym in _INSTRUMENTS:
        rows = per_instrument[sym]
        all_d = [r["date"] for r in rows]
        months = _months_span(rows, all_d) if all_d else 124
        setups = len(rows)
        spm = setups / months if months else 0.0
        wr = _win_rate(rows)
        mr = _mean_R(rows)
        tr = _total_R(rows)
        dd = _drawdown_R(rows)
        out.append(
            f"| {sym} | {sources[sym]} | {cells_per[sym]} | {months} | {setups} | "
            f"{spm:.2f} | {wr:.1%} | {mr:+.3f} | {tr:+.2f} | {dd:.2f} |"
        )
    out.append("")
    return out


def _section_yearly(per_instrument: dict[str, list[dict]]) -> list[str]:
    out = ["## Section 2 — Year-by-year breakdown", ""]
    out.append("| Year | Instrument | Setups | Win rate | Mean R | Total R |")
    out.append("|---|---|---:|---:|---:|---:|")
    years = set()
    by_year: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for sym, rows in per_instrument.items():
        for r in rows:
            y = r["date"].year
            years.add(y)
            by_year[(y, sym)].append(r)
    for y in sorted(years):
        for sym in _INSTRUMENTS:
            grp = by_year.get((y, sym), [])
            if not grp:
                out.append(f"| {y} | {sym} | 0 | — | — | — |")
                continue
            wr = _win_rate(grp)
            mr = _mean_R(grp)
            tr = _total_R(grp)
            out.append(f"| {y} | {sym} | {len(grp)} | {wr:.1%} | {mr:+.3f} | {tr:+.2f} |")
    out.append("")
    return out


def _section_mt5_overlap(per_instrument: dict[str, list[dict]]) -> list[str]:
    out = ["## Section 3 — Comparison with MT5 backtest (Sprint 6.5 overlap)", ""]
    out.append(
        "Databento metrics restricted to the date range that overlaps the "
        "Sprint 6.5 MT5 fixtures (2025-04-01 → 2026-04-30, A/A+ only). Sprint 6.5 "
        "numbers are reproduced from "
        "`calibration/runs/2026-04-29T07-12-33Z_backtest_filtered.md`."
    )
    out.append("")
    out.append("| Instrument | Metric | MT5 (Sprint 6.5) | Databento (overlap) | Δ |")
    out.append("|---|---|---:|---:|---:|")
    for sym in ("XAUUSD", "NDX100"):
        ref = _MT5_REFERENCE.get(sym)
        rows = [
            r for r in per_instrument[sym] if _MT5_OVERLAP_START <= r["date"] <= _MT5_OVERLAP_END
        ]
        if ref is None:
            continue
        mt5_n = ref["setups"]
        db_n = len(rows)
        out.append(f"| {sym} | Setups | {mt5_n} | {db_n} | {db_n - mt5_n:+d} |")
        mt5_wr = ref["win_rate"]
        db_wr = _win_rate(rows)
        out.append(
            f"| {sym} | Win rate | {mt5_wr:.1%} | {db_wr:.1%} | {(db_wr - mt5_wr) * 100:+.1f} pp |"
        )
        mt5_mr = ref["mean_R"]
        db_mr = _mean_R(rows)
        out.append(f"| {sym} | Mean R | {mt5_mr:+.3f} | {db_mr:+.3f} | {db_mr - mt5_mr:+.3f} |")
    out.append("")
    out.append(
        "Notes: MT5 fixtures are post-rollover-adjusted broker prices, Databento "
        "fixtures are raw stitched continuous front-month — small numerical "
        "divergences are expected. Large divergence (e.g., sign flip on mean R) "
        "would mean the data sources are not equivalent for SMC purposes."
    )
    out.append("")
    return out


def _spx_ndx_correlation(rows_spx: list[dict], rows_ndx: list[dict]) -> dict:
    spx_keys = {(r["date"], r["killzone"], r["direction"]) for r in rows_spx}
    ndx_keys = {(r["date"], r["killzone"], r["direction"]) for r in rows_ndx}
    spx_only_dates = {r["date"] for r in rows_spx}
    ndx_only_dates = {r["date"] for r in rows_ndx}
    same_date_same_dir = len(spx_keys & ndx_keys)
    same_date = len(spx_only_dates & ndx_only_dates)
    return {
        "spx_setups": len(rows_spx),
        "ndx_setups": len(rows_ndx),
        "same_date_count": same_date,
        "same_date_same_kz_dir": same_date_same_dir,
        "spx_redundancy_pct": (same_date_same_dir / len(rows_spx) * 100.0) if rows_spx else 0.0,
    }


def _section_spx_verdict(per_instrument: dict[str, list[dict]]) -> tuple[list[str], str]:
    rows_spx = per_instrument["SPX500"]
    rows_ndx = per_instrument["NDX100"]
    out = ["## Section 4 — SPX500 verdict", ""]
    if not rows_spx:
        out.append("No SPX500 setups detected — DROP (no edge to assess).")
        out.append("")
        return out, "DROP"

    mr = _mean_R(rows_spx)
    wr = _win_rate(rows_spx)
    spm = len(rows_spx) / max(_months_span(rows_spx, [r["date"] for r in rows_spx]), 1)
    corr = _spx_ndx_correlation(rows_spx, rows_ndx)

    out.append(f"- Mean R per setup (full 10y): **{mr:+.3f}**")
    out.append(f"- Win rate: **{wr:.1%}**")
    out.append(f"- Setups/month: **{spm:.2f}**")
    out.append(f"- SPX setups overlapping NDX (same date): {corr['same_date_count']}")
    out.append(
        f"- SPX setups overlapping NDX (same date+killzone+direction): "
        f"{corr['same_date_same_kz_dir']} "
        f"({corr['spx_redundancy_pct']:.1f}% of SPX setups)"
    )
    out.append("")

    if mr < 0.4:
        verdict = "DROP"
        rationale = (
            f"Mean R {mr:+.3f} < 0.4 threshold — insufficient edge over the "
            "operator-validated NDX baseline."
        )
    elif corr["spx_redundancy_pct"] >= 70.0:
        verdict = "DROP"
        rationale = (
            f"Mean R {mr:+.3f} ≥ 0.4 BUT redundancy with NDX "
            f"{corr['spx_redundancy_pct']:.1f}% ≥ 70% — adding SPX would mostly "
            "replay the same setups as NDX, inflating risk without adding "
            "independent edge."
        )
    elif mr >= 0.4 and corr["spx_redundancy_pct"] < 50.0:
        verdict = "SHIP"
        rationale = (
            f"Mean R {mr:+.3f} ≥ 0.4 AND redundancy "
            f"{corr['spx_redundancy_pct']:.1f}% < 50% — clearly profitable AND "
            "largely independent of NDX."
        )
    else:
        verdict = "MARGINAL"
        rationale = (
            f"Mean R {mr:+.3f} ≥ 0.4 but redundancy "
            f"{corr['spx_redundancy_pct']:.1f}% in 50–70% band — partial "
            "overlap with NDX. Operator call: ship for diversification or "
            "drop to keep portfolio concentrated."
        )

    out.append(f"**Verdict: `{verdict}`** — {rationale}")
    out.append("")
    return out, verdict


def _section_portfolio(per_instrument: dict[str, list[dict]]) -> tuple[list[str], dict]:
    rows_xau = per_instrument["XAUUSD"]
    rows_ndx = per_instrument["NDX100"]
    rows_spx = per_instrument["SPX500"]

    scenarios = {
        "XAU + NDX (current ship plan)": rows_xau + rows_ndx,
        "XAU + NDX + SPX (extended)": rows_xau + rows_ndx + rows_spx,
        "XAU only": rows_xau,
        "NDX only": rows_ndx,
        "SPX only": rows_spx,
    }
    out = ["## Section 5 — Combined portfolio scenarios", ""]
    out.append(
        "| Scenario | Setups | Months | Setups/month | Mean R | Win rate | "
        "Total R | Max DD | Annual % @1% risk |"
    )
    out.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    summary = {}
    for label, rows in scenarios.items():
        if not rows:
            out.append(f"| {label} | 0 | — | — | — | — | — | — | — |")
            summary[label] = None
            continue
        all_d = [r["date"] for r in rows]
        months = _months_span(rows, all_d)
        spm = len(rows) / months if months else 0.0
        mr = _mean_R(rows)
        wr = _win_rate(rows)
        tr = _total_R(rows)
        dd = _drawdown_R(rows)
        ann = _annual_return_at_1pct(spm, mr)
        out.append(
            f"| {label} | {len(rows)} | {months} | {spm:.2f} | {mr:+.3f} | "
            f"{wr:.1%} | {tr:+.2f} | {dd:.2f} | {ann:+.1f}% |"
        )
        summary[label] = {"mean_R": mr, "spm": spm, "wr": wr, "dd": dd, "ann": ann}
    out.append("")
    return out, summary


def _section_edge_stability(per_instrument: dict[str, list[dict]]) -> tuple[list[str], str]:
    out = ["## Section 6 — Edge stability", ""]
    all_rows = sum(per_instrument.values(), [])
    if not all_rows:
        out.append("No setups; cannot assess edge stability.")
        out.append("")
        return out, "UNKNOWN"

    by_month: dict[str, list[dict]] = defaultdict(list)
    for r in all_rows:
        by_month[r["timestamp_utc"].strftime("%Y-%m")].append(r)
    monthly_means = []
    for m in sorted(by_month):
        rs = by_month[m]
        mm = _mean_R(rs)
        if mm != 0.0 or any(r["outcome"] not in ("entry_not_hit", "open_at_horizon") for r in rs):
            monthly_means.append(mm)
    cv = (
        statistics.pstdev(monthly_means) / abs(statistics.mean(monthly_means))
        if monthly_means and statistics.mean(monthly_means) != 0
        else math.inf
    )

    by_quarter: dict[str, list[dict]] = defaultdict(list)
    for r in all_rows:
        q = (r["timestamp_utc"].month - 1) // 3 + 1
        key = f"{r['timestamp_utc'].year}-Q{q}"
        by_quarter[key].append(r)
    quarter_totals = {q: _total_R(rs) for q, rs in by_quarter.items()}
    if quarter_totals:
        worst_q = min(quarter_totals.items(), key=lambda kv: kv[1])
        best_q = max(quarter_totals.items(), key=lambda kv: kv[1])
    else:
        worst_q = best_q = ("n/a", 0.0)

    rolling6_wr: list[tuple[str, float]] = []
    months_sorted = sorted(by_month)
    for i in range(len(months_sorted)):
        window = []
        for j in range(max(0, i - 5), i + 1):
            window.extend(by_month[months_sorted[j]])
        rolling6_wr.append((months_sorted[i], _win_rate(window)))

    out.append(f"- Monthly mean R: N={len(monthly_means)} months with at least one resolved setup")
    if monthly_means:
        out.append(
            f"  - Mean of monthly mean R: {statistics.mean(monthly_means):+.3f}, "
            f"stdev: {statistics.pstdev(monthly_means):.3f}, CV: {cv:.2f}"
        )
    out.append(f"- Worst quarter: {worst_q[0]} = {worst_q[1]:+.2f}R")
    out.append(f"- Best quarter: {best_q[0]} = {best_q[1]:+.2f}R")
    out.append("")

    if cv < 1.0:
        verdict = "STABLE"
    elif cv < 2.0:
        verdict = "REGIME-DEPENDENT"
    else:
        verdict = "UNSTABLE"
    out.append(f"**Verdict: `{verdict}`** (CV-based heuristic).")
    out.append("")

    out.append("Rolling 6-month win rate (most recent 24 months):")
    out.append("")
    out.append("| Window end | Win rate |")
    out.append("|---|---:|")
    for m, wr in rolling6_wr[-24:]:
        out.append(f"| {m} | {wr:.1%} |")
    out.append("")

    return out, verdict


def _section_recommendation(
    spx_verdict: str,
    per_instrument: dict[str, list[dict]],
    portfolio: dict,
) -> list[str]:
    out = ["## Section 7 — Final WATCHED_PAIRS recommendation", ""]

    keep: list[str] = []
    reasons: list[str] = []

    for sym in ("XAUUSD", "NDX100"):
        rows = per_instrument[sym]
        mr = _mean_R(rows)
        spm = len(rows) / max(_months_span(rows, [r["date"] for r in rows]), 1) if rows else 0.0
        if mr >= 0.4 and spm >= 1.0:
            keep.append(sym)
            reasons.append(
                f"- **{sym}**: KEEP — Mean R {mr:+.3f} ≥ 0.4 and "
                f"{spm:.2f} setups/month over 10 years."
            )
        else:
            reasons.append(
                f"- **{sym}**: ⚠️ REVIEW — Mean R {mr:+.3f}, {spm:.2f} setups/month "
                "fails the SHIP threshold over 10 years (was validated on a 11-month "
                "MT5 sample)."
            )

    if spx_verdict == "SHIP":
        keep.append("SPX500")
        reasons.append("- **SPX500**: ADD — Section 4 verdict SHIP.")
    elif spx_verdict == "MARGINAL":
        reasons.append("- **SPX500**: HOLD — Section 4 verdict MARGINAL; operator call.")
    else:
        reasons.append(f"- **SPX500**: DO NOT ADD — Section 4 verdict {spx_verdict}.")

    reasons.append(
        "- **ETHUSD**: out of scope of this run (no 10y crypto futures fixture). "
        "Sprint 6.5 DEFAULT_SHIPS verdict on extended MT5 fixture stands until "
        "re-validated on a longer window."
    )

    out.append("```python")
    out.append("WATCHED_PAIRS = [")
    for s in keep:
        out.append(f'    "{s}",')
    out.append('    "ETHUSD",  # retained from Sprint 6.5 — re-validate when 10y data avail.')
    out.append("]")
    out.append("```")
    out.append("")
    out.append("Per-instrument reasoning:")
    out.append("")
    out.extend(reasons)
    out.append("")
    return out


def _section_sanity_flags(
    per_instrument: dict[str, list[dict]],
    yearly_means: dict[str, dict[int, float]],
    excluded_dates: dict[str, set[date]],
    rollover_in_setups: dict[str, int],
) -> list[str]:
    out = ["## Section 8 — Sanity flags", ""]
    flagged = False
    for sym in _INSTRUMENTS:
        rows = per_instrument[sym]
        n = len(rows)
        # Setup count anomalous: < 30 over 10y or > 600.
        if n < 30:
            out.append(f"- ⚠️ **{sym}**: only {n} A/A+ setups over 10 years — anomalously low.")
            flagged = True
        if n > 600:
            out.append(f"- ⚠️ **{sym}**: {n} A/A+ setups over 10 years — anomalously high.")
            flagged = True
        # Year-by-year variability: report range of yearly mean R.
        ym = yearly_means.get(sym, {})
        if ym:
            ms = list(ym.values())
            spread = max(ms) - min(ms)
            if spread > 3.0:
                out.append(
                    f"- ⚠️ **{sym}**: yearly mean R spread {spread:.2f}R "
                    f"(min {min(ms):+.3f}, max {max(ms):+.3f}) — edge highly regime-dependent."
                )
                flagged = True
        # Rollover leak check.
        leak = rollover_in_setups.get(sym, 0)
        if leak > 0:
            out.append(
                f"- ⚠️ **{sym}**: {leak} setups detected on dates that should have been "
                "rollover-excluded — exclusion logic bug."
            )
            flagged = True
    if not flagged:
        out.append("- ✅ All sanity checks clear.")
    out.append("")
    return out


def _yearly_means(rows: list[dict]) -> dict[int, float]:
    by_year: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        by_year[r["date"].year].append(r)
    return {y: _mean_R(rs) for y, rs in by_year.items()}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    import argparse  # noqa: PLC0415

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("tick", "legacy"),
        default="tick",
        help=(
            "Detection mode. 'tick' (default) is the production-faithful "
            "tick-by-tick simulator from src.backtest.tick_simulator — "
            "no look-ahead leak. 'legacy' is the pre-Phase-B path "
            "(one detector call per day, leak-prone); kept only for "
            "A/B comparison."
        ),
    )
    parser.add_argument(
        "--instruments",
        default=",".join(_INSTRUMENTS),
        help="Comma-separated subset of XAUUSD,NDX100,SPX500.",
    )
    args = parser.parse_args()
    instruments = [s.strip() for s in args.instruments.split(",") if s.strip()]
    for sym in instruments:
        if sym not in _INSTRUMENTS:
            raise SystemExit(f"unknown instrument {sym!r}; expected one of {_INSTRUMENTS}")

    settings = _settings()
    print(f"=== Extended 10y backtest — mode={args.mode} — {_TIMESTAMP} ===")
    print()

    fixtures: dict[str, dict[str, pd.DataFrame]] = {}
    rollovers: dict[str, list[datetime]] = {}
    excluded_per_sym: dict[str, set[date]] = {}
    paris_dates_per_sym: dict[str, list[date]] = {}

    print("Step 1 — load fixtures + rollover dates …")
    for sym in instruments:
        fixtures[sym] = _load_instrument(sym)
        rollovers[sym] = _rollovers_utc(sym)
        excluded_per_sym[sym] = _excluded_paris_dates(rollovers[sym])
        paris_dates_per_sym[sym] = _trading_dates_for(fixtures[sym]["M5"])
        print(
            f"  {sym}: weekday Paris dates={len(paris_dates_per_sym[sym])}, "
            f"rollovers={len(rollovers[sym])}, "
            f"rollover-excluded Paris dates={len(excluded_per_sym[sym])}, "
            f"M5 rows={len(fixtures[sym]['M5'])}"
        )
    print()

    print("Step 2 — build caches …", flush=True)
    m5_caches = {sym: M5Cache(fixtures[sym]["M5"]) for sym in instruments}
    fixture_caches = {sym: FixtureCache(fixtures[sym]) for sym in instruments}

    print(f"Step 3 — run detection cell-by-cell (mode={args.mode}) …", flush=True)
    per_instrument: dict[str, list[dict]] = {}
    cells_per: dict[str, int] = {}
    skipped_per: dict[str, int] = {}
    errors_all: list[str] = []
    for sym in instruments:
        print(f"  {sym} …", flush=True)
        rows, errs, cells, skipped = _run_instrument(
            sym,
            fixture_caches[sym],
            m5_caches[sym],
            paris_dates_per_sym[sym],
            excluded_per_sym[sym],
            settings,
            mode=args.mode,
        )
        per_instrument[sym] = rows
        cells_per[sym] = cells
        skipped_per[sym] = skipped
        errors_all.extend(errs)
        print(
            f"    cells processed={cells}, rollover skipped={skipped}, "
            f"A/A+ setups={len(rows)}, errors={len(errs)}"
        )
    print()

    print("Step 4 — render report …", flush=True)
    yearly = {sym: _yearly_means(per_instrument[sym]) for sym in _INSTRUMENTS}
    rollover_leak = {
        sym: sum(1 for r in per_instrument[sym] if r["date"] in excluded_per_sym[sym])
        for sym in _INSTRUMENTS
    }

    lines: list[str] = []
    lines.append(f"# Extended historical backtest — 10 years × 3 instruments — {_TIMESTAMP}")
    lines.append("")
    lines.append(
        "Out-of-sample validation of the SMC/ICT edge over Databento "
        "continuous front-month futures (XAUUSD via GC, NDX100 via NQ, "
        "SPX500 via ES) covering ~10 years (2016-01 → 2026-04). Detection "
        "settings = `config/settings.py.example` operator-validated defaults. "
        "SPX500 deliberately reuses the NDX100 instrument config — explicit "
        "assumption to test, not a calibration. NOTIFY_QUALITIES = "
        '["A+", "A"] applied. Rollover dates excluded with ±2h windows.'
    )
    lines.append("")
    lines.append(f"Errors: {len(errors_all)} cells skipped (see stderr for details).")
    lines.append("")

    lines.extend(_section_per_instrument_summary(per_instrument, cells_per))
    lines.extend(_section_yearly(per_instrument))
    lines.extend(_section_mt5_overlap(per_instrument))
    spx_lines, spx_verdict = _section_spx_verdict(per_instrument)
    lines.extend(spx_lines)
    pf_lines, portfolio = _section_portfolio(per_instrument)
    lines.extend(pf_lines)
    es_lines, edge_verdict = _section_edge_stability(per_instrument)
    lines.extend(es_lines)
    lines.extend(_section_recommendation(spx_verdict, per_instrument, portfolio))
    lines.extend(_section_sanity_flags(per_instrument, yearly, excluded_per_sym, rollover_leak))

    if errors_all:
        lines.append("## Detection / simulation errors (first 30)")
        lines.append("")
        for e in errors_all[:30]:
            lines.append(f"- {e}")
        if len(errors_all) > 30:
            lines.append(f"- … and {len(errors_all) - 30} more")
        lines.append("")

    _RUNS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _RUNS_DIR / f"{_TIMESTAMP}_{_REPORT_TAG}.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")

    # Stdout summary.
    print()
    print("=== Summary ===")
    for sym in _INSTRUMENTS:
        rows = per_instrument[sym]
        if not rows:
            print(f"  {sym}: no setups detected")
            continue
        all_d = [r["date"] for r in rows]
        months = _months_span(rows, all_d)
        print(
            f"  {sym}: setups={len(rows)} ({len(rows)/months:.2f}/mo), "
            f"mean R={_mean_R(rows):+.3f}, win rate={_win_rate(rows):.1%}, "
            f"DD={_drawdown_R(rows):.2f}R, total R={_total_R(rows):+.2f}"
        )
    print(f"  SPX500 verdict: {spx_verdict}")
    print(f"  Edge stability: {edge_verdict}")
    print(f"  Report: {out_path.relative_to(_REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
