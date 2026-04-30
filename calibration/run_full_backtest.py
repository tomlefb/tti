"""Full historical backtest — out-of-sample edge measurement.

Runs ``build_setup_candidates`` on every weekday in the committed M5
fixtures EXCEPT the reference dates already used for Sprint 1-3
calibration, then forward-simulates each detected setup over a 24h M5
horizon to classify the outcome (reusing the simulator from
``run_setup_outcome_backtest.py``).

Aggregates realized R with two conventions:

    realized_R_strict     — sl_before_entry counts as -1.0 (full risk)
    realized_R_realistic  — sl_before_entry counts as 0.0 (limit never filled)

Emits a markdown report + cumulative-R PNG under
``calibration/runs/`` (gitignored). Operator reviews; no parameter
tuning happens here.

NB: reference-chart JSON files yield 19 unique dates (not 18 as the
operator's task brief assumed). The exclusion uses the actual JSON
contents and prints the real count — see Step 1 stdout.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.detection.setup import Setup, build_setup_candidates  # noqa: E402

_FIXTURE_DIR = _REPO_ROOT / "tests" / "fixtures" / "historical"
_REFERENCE_CHARTS = _REPO_ROOT / "calibration" / "reference_charts"
_RUNS_DIR = _REPO_ROOT / "calibration" / "runs"
_PAIRS = ["XAUUSD", "NDX100", "EURUSD", "GBPUSD"]

_HORIZON_MINUTES = 24 * 60
_TIMESTAMP = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")


# ---------------------------------------------------------------------------
# Settings — mirror of config/settings.py.example (canonical defaults).
# ---------------------------------------------------------------------------
def _settings() -> SimpleNamespace:
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
            "NDX100": {"sweep_buffer": 5.0, "equal_hl_tolerance": 3.0, "sl_buffer": 5.0},
            "EURUSD": {
                "sweep_buffer": 0.00050,
                "equal_hl_tolerance": 0.00030,
                "sl_buffer": 0.00050,
            },
            "GBPUSD": {
                "sweep_buffer": 0.00050,
                "equal_hl_tolerance": 0.00030,
                "sl_buffer": 0.00050,
            },
        },
    )


# ---------------------------------------------------------------------------
# Date enumeration
# ---------------------------------------------------------------------------
def _reference_dates() -> set[date]:
    out: set[date] = set()
    for f in _REFERENCE_CHARTS.glob("*.json"):
        try:
            out.add(date.fromisoformat(f.name.split("_")[0]))
        except ValueError:
            continue
    return out


def _load_pair(pair: str) -> dict[str, pd.DataFrame]:
    return {
        tf: pd.read_parquet(_FIXTURE_DIR / f"{pair}_{tf}.parquet")
        for tf in ("D1", "H4", "H1", "M5")
    }


def _trading_dates_for_pair(df_m5: pd.DataFrame) -> list[date]:
    """All weekdays present in the M5 fixture (Mon-Fri)."""
    times = pd.to_datetime(df_m5["time"], utc=True)
    dates = sorted(set(times.dt.date))
    return [d for d in dates if d.weekday() < 5]


# ---------------------------------------------------------------------------
# Outcome simulator (reuses logic from run_setup_outcome_backtest.py).
# ---------------------------------------------------------------------------
def _simulate_outcome(setup: Setup, df_m5: pd.DataFrame) -> dict:
    times = pd.to_datetime(df_m5["time"], utc=True)
    mask = times >= setup.timestamp_utc
    if not mask.any():
        return _no_data_outcome()

    horizon_end = setup.timestamp_utc + timedelta(minutes=_HORIZON_MINUTES)
    in_horizon = mask & (times <= horizon_end)
    sub = df_m5.loc[in_horizon].reset_index(drop=True)
    if len(sub) == 0:
        return _no_data_outcome()

    sub_times = pd.to_datetime(sub["time"], utc=True)
    lows = sub["low"].to_numpy(dtype="float64")
    highs = sub["high"].to_numpy(dtype="float64")
    n = len(sub)

    direction = setup.direction
    entry = setup.entry_price
    sl = setup.stop_loss
    tp1 = setup.tp1_price
    tpr = setup.tp_runner_price
    same_tps = abs(tp1 - tpr) < 1e-9

    def _t(i: int) -> datetime:
        return sub_times.iloc[i].to_pydatetime()

    def _mins(t: datetime) -> float:
        return (t - setup.timestamp_utc).total_seconds() / 60.0

    # Phase 1 — find entry.
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
            "entry_hit_time_utc": None,
            "resolution_time_utc": None,
            "realized_R_strict": 0.0,
            "realized_R_realistic": 0.0,
            "time_to_entry_minutes": None,
            "time_to_resolution_minutes": None,
        }

    entry_time = _t(entry_idx)
    if sl_before_entry_flag:
        return {
            "outcome": "sl_before_entry",
            "entry_hit_time_utc": entry_time,
            "resolution_time_utc": entry_time,
            "realized_R_strict": -1.0,
            "realized_R_realistic": 0.0,
            "time_to_entry_minutes": _mins(entry_time),
            "time_to_resolution_minutes": _mins(entry_time),
        }

    # Phase 2 — race SL vs TP1.
    tp1_idx: int | None = None
    for i in range(entry_idx, n):
        if direction == "long":
            sl_now = lows[i] <= sl
            tp1_now = highs[i] >= tp1
        else:
            sl_now = highs[i] >= sl
            tp1_now = lows[i] <= tp1
        if sl_now:
            t = _t(i)
            return {
                "outcome": "sl_hit",
                "entry_hit_time_utc": entry_time,
                "resolution_time_utc": t,
                "realized_R_strict": -1.0,
                "realized_R_realistic": -1.0,
                "time_to_entry_minutes": _mins(entry_time),
                "time_to_resolution_minutes": _mins(t),
            }
        if tp1_now:
            tp1_idx = i
            break

    if tp1_idx is None:
        return {
            "outcome": "open_at_horizon",
            "entry_hit_time_utc": entry_time,
            "resolution_time_utc": None,
            "realized_R_strict": 0.0,
            "realized_R_realistic": 0.0,
            "time_to_entry_minutes": _mins(entry_time),
            "time_to_resolution_minutes": None,
        }

    tp1_time = _t(tp1_idx)
    if same_tps:
        r = 0.5 * setup.tp1_rr + 0.5 * setup.tp_runner_rr
        return {
            "outcome": "tp_runner_hit",
            "entry_hit_time_utc": entry_time,
            "resolution_time_utc": tp1_time,
            "realized_R_strict": r,
            "realized_R_realistic": r,
            "time_to_entry_minutes": _mins(entry_time),
            "time_to_resolution_minutes": _mins(tp1_time),
        }

    # Phase 3 — race SL vs TP_runner on remaining 50%.
    for j in range(tp1_idx + 1, n):
        if direction == "long":
            sl_now = lows[j] <= sl
            tpr_now = highs[j] >= tpr
        else:
            sl_now = highs[j] >= sl
            tpr_now = lows[j] <= tpr
        if sl_now:
            t = _t(j)
            r = (setup.tp1_rr - 1.0) / 2.0
            return {
                "outcome": "tp1_hit_only",
                "entry_hit_time_utc": entry_time,
                "resolution_time_utc": t,
                "realized_R_strict": r,
                "realized_R_realistic": r,
                "time_to_entry_minutes": _mins(entry_time),
                "time_to_resolution_minutes": _mins(t),
            }
        if tpr_now:
            t = _t(j)
            r = 0.5 * setup.tp1_rr + 0.5 * setup.tp_runner_rr
            return {
                "outcome": "tp_runner_hit",
                "entry_hit_time_utc": entry_time,
                "resolution_time_utc": t,
                "realized_R_strict": r,
                "realized_R_realistic": r,
                "time_to_entry_minutes": _mins(entry_time),
                "time_to_resolution_minutes": _mins(t),
            }

    # Post-TP1 horizon exhausted — conservative classification.
    r = (setup.tp1_rr - 1.0) / 2.0
    return {
        "outcome": "tp1_hit_only",
        "entry_hit_time_utc": entry_time,
        "resolution_time_utc": tp1_time,
        "realized_R_strict": r,
        "realized_R_realistic": r,
        "time_to_entry_minutes": _mins(entry_time),
        "time_to_resolution_minutes": _mins(tp1_time),
    }


def _no_data_outcome() -> dict:
    return {
        "outcome": "open_at_horizon",
        "entry_hit_time_utc": None,
        "resolution_time_utc": None,
        "realized_R_strict": 0.0,
        "realized_R_realistic": 0.0,
        "time_to_entry_minutes": None,
        "time_to_resolution_minutes": None,
    }


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------
def _max_consecutive_sl(rows_sorted: list[dict]) -> int:
    """Longest run of sl_hit outcomes (sl_before_entry NOT counted as a
    real loss for streak purposes — limit order never filled)."""
    best = cur = 0
    for r in rows_sorted:
        if r["outcome"] == "sl_hit":
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def _max_drawdown(cum_curve: list[float]) -> float:
    """Largest peak-to-trough drop on the cumulative curve."""
    if not cum_curve:
        return 0.0
    peak = cum_curve[0]
    worst = 0.0
    for v in cum_curve:
        if v > peak:
            peak = v
        worst = min(worst, v - peak)
    return -worst  # report as positive R magnitude


def _win_rate(by_outcome: dict[str, int], *, realistic: bool) -> float:
    wins = by_outcome.get("tp1_hit_only", 0) + by_outcome.get("tp_runner_hit", 0)
    losses = by_outcome.get("sl_hit", 0)
    if not realistic:
        losses += by_outcome.get("sl_before_entry", 0)
    denom = wins + losses
    return wins / denom if denom else 0.0


def _group_table(rows: list[dict], key: str, order: list[str] | None = None) -> list[str]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r[key]].append(r)
    keys = order if order else sorted(groups.keys())
    out = [
        f"| {key} | N | Win rate strict | Win rate realistic | Mean R strict | "
        "Mean R realistic | Total R strict | Total R realistic |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for k in keys:
        g = groups.get(k)
        if not g:
            continue
        bo: dict[str, int] = {}
        for r in g:
            bo[r["outcome"]] = bo.get(r["outcome"], 0) + 1
        wr_s = _win_rate(bo, realistic=False)
        wr_r = _win_rate(bo, realistic=True)
        # Mean R: exclude entry_not_hit + open_at_horizon (no fill, no info).
        rs = [
            r["realized_R_strict"]
            for r in g
            if r["outcome"] not in ("entry_not_hit", "open_at_horizon")
        ]
        rr = [
            r["realized_R_realistic"]
            for r in g
            if r["outcome"] not in ("entry_not_hit", "open_at_horizon")
        ]
        mean_s = sum(rs) / len(rs) if rs else 0.0
        mean_r = sum(rr) / len(rr) if rr else 0.0
        total_s = sum(r["realized_R_strict"] for r in g)
        total_r = sum(r["realized_R_realistic"] for r in g)
        out.append(
            f"| {k} | {len(g)} | {wr_s:.1%} | {wr_r:.1%} | {mean_s:+.3f} | "
            f"{mean_r:+.3f} | {total_s:+.2f} | {total_r:+.2f} |"
        )
    out.append("")
    return out


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------
def _render_report(
    rows: list[dict],
    cells_processed: int,
    excluded_dates: list[date],
    per_pair_oos: dict[str, int],
    chart_path: Path,
    errors: list[str],
) -> tuple[str, list[str]]:
    n = len(rows)

    by_outcome: dict[str, int] = {}
    for r in rows:
        by_outcome[r["outcome"]] = by_outcome.get(r["outcome"], 0) + 1

    rows_sorted = sorted(rows, key=lambda r: r["timestamp_utc"])
    cum_strict: list[float] = []
    cum_realistic: list[float] = []
    s_acc = 0.0
    r_acc = 0.0
    for r in rows_sorted:
        s_acc += r["realized_R_strict"]
        r_acc += r["realized_R_realistic"]
        cum_strict.append(s_acc)
        cum_realistic.append(r_acc)

    total_R_strict = s_acc
    total_R_realistic = r_acc

    rs_for_mean = [
        r["realized_R_strict"]
        for r in rows
        if r["outcome"] not in ("entry_not_hit", "open_at_horizon")
    ]
    rr_for_mean = [
        r["realized_R_realistic"]
        for r in rows
        if r["outcome"] not in ("entry_not_hit", "open_at_horizon")
    ]
    mean_R_strict = sum(rs_for_mean) / len(rs_for_mean) if rs_for_mean else 0.0
    mean_R_realistic = sum(rr_for_mean) / len(rr_for_mean) if rr_for_mean else 0.0

    win_rate_strict = _win_rate(by_outcome, realistic=False)
    win_rate_realistic = _win_rate(by_outcome, realistic=True)

    max_consec_sl = _max_consecutive_sl(rows_sorted)
    max_dd = _max_drawdown(cum_strict)

    # ---- Sanity flags ----
    flags: list[str] = []
    if n < 100:
        flags.append(f"⚠️ INSUFFICIENT SAMPLE: total setups {n} < 100 — results unreliable")
    if mean_R_strict < 0:
        flags.append(
            f"⚠️ NEGATIVE EDGE: mean R per setup {mean_R_strict:+.3f} (strict) — system not profitable as-is"
        )
    if max_consec_sl > 10:
        flags.append(
            f"⚠️ HIGH RUIN RISK: max consecutive SL {max_consec_sl} > 10 — bust risk on 1% account"
        )
    if win_rate_strict < 0.20:
        flags.append(
            f"⚠️ LOW WIN RATE: win rate strict {win_rate_strict:.1%} < 20% — even with RR 4:1 may be marginal"
        )
    if max_dd > 15.0:
        flags.append(
            f"⚠️ HIGH DRAWDOWN: max drawdown {max_dd:.2f}R > 15R — challenge bust likely during a bad streak"
        )

    lines: list[str] = []
    lines.append(f"# Full historical backtest — {_TIMESTAMP}")
    lines.append("")
    lines.append(
        "Out-of-sample edge measurement. Detection settings = "
        "`config/settings.py.example` defaults (Sprint 3 calibrated values). "
        "**No parameter tuning based on these results** — measurement only."
    )
    lines.append("")
    lines.append(
        f"Excluded dates: {len(excluded_dates)} reference dates from "
        "`calibration/reference_charts/`. (NB: spec assumed 18 — actual JSON "
        "yields 19; result reflects the real exclusion set.)"
    )
    lines.append("")

    # Sanity flags FIRST.
    if flags:
        lines.append("## Sanity flags")
        lines.append("")
        for f in flags:
            lines.append(f"- {f}")
        lines.append("")
    else:
        lines.append("## ✅ All sanity flags clear — proceed to analysis.")
        lines.append("")

    if errors:
        lines.append(f"## Errors during run ({len(errors)} cells skipped)")
        lines.append("")
        for e in errors[:30]:
            lines.append(f"- {e}")
        if len(errors) > 30:
            lines.append(f"- … and {len(errors) - 30} more")
        lines.append("")

    # ---- Headline ----
    lines.append("## Headline")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---:|")
    lines.append(f"| Out-of-sample cells processed | {cells_processed} |")
    lines.append(f"| Total setups detected | {n} |")
    util = (n / cells_processed) if cells_processed else 0.0
    lines.append(f"| Setups per cell (utilization) | {util:.3f} |")
    lines.append(f"| Total realized R (strict) | {total_R_strict:+.2f} |")
    lines.append(f"| Total realized R (realistic) | {total_R_realistic:+.2f} |")
    lines.append(f"| Mean R per setup (strict) | {mean_R_strict:+.4f} |")
    lines.append(f"| Mean R per setup (realistic) | {mean_R_realistic:+.4f} |")
    lines.append(f"| Win rate strict | {win_rate_strict:.1%} |")
    lines.append(f"| Win rate realistic | {win_rate_realistic:.1%} |")
    lines.append(f"| Max consecutive SL hits | {max_consec_sl} |")
    lines.append(f"| Max drawdown (R, strict) | {max_dd:.2f} |")
    lines.append("")

    lines.append("Per-pair OOS dates:")
    lines.append("")
    for pair in _PAIRS:
        lines.append(f"- {pair}: {per_pair_oos.get(pair, 0)}")
    lines.append("")

    # ---- By quality / pair / killzone / direction ----
    lines.append("## By quality")
    lines.append("")
    lines.extend(_group_table(rows, "quality", order=["A+", "A", "B"]))

    lines.append("## By pair")
    lines.append("")
    lines.extend(_group_table(rows, "pair", order=_PAIRS))

    lines.append("## By killzone")
    lines.append("")
    lines.extend(_group_table(rows, "killzone", order=["london", "ny"]))

    lines.append("## By direction")
    lines.append("")
    lines.extend(_group_table(rows, "direction", order=["long", "short"]))

    # ---- By outcome category ----
    lines.append("## By outcome category")
    lines.append("")
    lines.append("| Outcome | N | % of total |")
    lines.append("|---|---:|---:|")
    for label in (
        "entry_not_hit",
        "sl_before_entry",
        "sl_hit",
        "tp1_hit_only",
        "tp_runner_hit",
        "open_at_horizon",
    ):
        c = by_outcome.get(label, 0)
        pct = 100.0 * c / n if n else 0.0
        lines.append(f"| {label} | {c} | {pct:.1f}% |")
    lines.append("")

    # ---- Cumulative R curve (chart pointer) ----
    lines.append("## Cumulative R curve")
    lines.append("")
    lines.append(f"![cumulative R]({chart_path.name})")
    lines.append("")
    lines.append(f"Path: `{chart_path.relative_to(_REPO_ROOT)}`")
    lines.append("")

    # ---- Monthly breakdown ----
    lines.append("## Monthly breakdown")
    lines.append("")
    lines.append("| Month | Setups | Total R strict | Cumulative R strict | Drawdown in month |")
    lines.append("|---|---:|---:|---:|---:|")
    by_month: dict[str, list[dict]] = defaultdict(list)
    for r in rows_sorted:
        m = r["timestamp_utc"].strftime("%Y-%m")
        by_month[m].append(r)
    cum = 0.0
    for m in sorted(by_month):
        bucket = by_month[m]
        month_total = sum(x["realized_R_strict"] for x in bucket)
        cum += month_total
        # local drawdown inside the month
        local = []
        acc = 0.0
        for x in bucket:
            acc += x["realized_R_strict"]
            local.append(acc)
        local_dd = _max_drawdown(local)
        lines.append(f"| {m} | {len(bucket)} | {month_total:+.2f} | {cum:+.2f} | {local_dd:.2f} |")
    lines.append("")

    # ---- Last 50 setups ----
    lines.append("## Last 50 setups (most recent first)")
    lines.append("")
    lines.append(
        "| date | pair | killzone | direction | quality | RR runner | "
        "outcome | realized R strict | realized R realistic |"
    )
    lines.append("|---|---|---|---|---|---:|---|---:|---:|")
    for r in list(reversed(rows_sorted))[:50]:
        lines.append(
            f"| {r['date']} | {r['pair']} | {r['killzone']} | {r['direction']} | "
            f"{r['quality']} | {r['tp_runner_rr']:.2f} | {r['outcome']} | "
            f"{r['realized_R_strict']:+.3f} | {r['realized_R_realistic']:+.3f} |"
        )
    lines.append("")

    return "\n".join(lines), flags


def _render_chart(rows_sorted: list[dict], chart_path: Path) -> None:
    if not rows_sorted:
        # Still produce an empty chart so the path holds.
        fig, ax = plt.subplots(figsize=(10, 4))
        ax.text(0.5, 0.5, "No setups detected", ha="center", va="center")
        ax.set_axis_off()
        fig.savefig(chart_path, dpi=120, bbox_inches="tight")
        plt.close(fig)
        return

    times = [r["timestamp_utc"] for r in rows_sorted]
    cum_s: list[float] = []
    cum_r: list[float] = []
    s_acc = 0.0
    r_acc = 0.0
    for r in rows_sorted:
        s_acc += r["realized_R_strict"]
        r_acc += r["realized_R_realistic"]
        cum_s.append(s_acc)
        cum_r.append(r_acc)

    fig, ax = plt.subplots(figsize=(11, 5))
    ax.plot(times, cum_s, label="Cumulative R (strict)", color="#c0392b", linewidth=1.6)
    ax.plot(times, cum_r, label="Cumulative R (realistic)", color="#2980b9", linewidth=1.6)
    ax.axhline(0.0, color="grey", linewidth=0.8, alpha=0.5)
    ax.set_title(f"Full historical backtest — cumulative R ({_TIMESTAMP})")
    ax.set_xlabel("Setup timestamp (UTC)")
    ax.set_ylabel("Cumulative R")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(chart_path, dpi=120, bbox_inches="tight")
    plt.close(fig)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    settings = _settings()
    excluded = _reference_dates()

    print("=== Step 1 — enumerating out-of-sample dates ===")
    fixtures: dict[str, dict[str, pd.DataFrame]] = {}
    per_pair_dates: dict[str, list[date]] = {}
    per_pair_oos: dict[str, int] = {}
    for pair in _PAIRS:
        fixtures[pair] = _load_pair(pair)
        all_weekdays = _trading_dates_for_pair(fixtures[pair]["M5"])
        oos = [d for d in all_weekdays if d not in excluded]
        per_pair_dates[pair] = oos
        per_pair_oos[pair] = len(oos)
        print(
            f"  {pair}: total weekday dates={len(all_weekdays)}, "
            f"reference excluded={len([d for d in all_weekdays if d in excluded])}, "
            f"OOS={len(oos)}"
        )
    print(f"  Excluded reference dates count: {len(excluded)}")
    if len(excluded) != 18:
        print(
            f"  NOTE: spec assumed 18; actual reference-chart JSON yields "
            f"{len(excluded)} unique dates — using actual."
        )

    print()
    print("=== Step 2 — running detection on each (date, pair) cell ===")
    rows: list[dict] = []
    errors: list[str] = []
    cells_processed = 0
    for pair in _PAIRS:
        bundle = fixtures[pair]
        for d in per_pair_dates[pair]:
            cells_processed += 1
            try:
                setups = build_setup_candidates(
                    df_h4=bundle["H4"],
                    df_h1=bundle["H1"],
                    df_m5=bundle["M5"],
                    df_d1=bundle["D1"],
                    target_date=d,
                    symbol=pair,
                    settings=settings,
                )
            except Exception as exc:
                msg = f"{d} {pair}: detection error — {type(exc).__name__}: {exc}"
                errors.append(msg)
                # Truncated traceback to stderr for debugging.
                sys.stderr.write(msg + "\n")
                continue
            for s in setups:
                try:
                    outcome = _simulate_outcome(s, bundle["M5"])
                except Exception as exc:
                    msg = f"{d} {pair} {s.timestamp_utc}: simulate error — {type(exc).__name__}: {exc}"
                    errors.append(msg)
                    sys.stderr.write(msg + "\n")
                    continue
                rows.append(
                    {
                        "date": d.isoformat(),
                        "pair": pair,
                        "timestamp_utc": s.timestamp_utc,
                        "killzone": s.killzone,
                        "direction": s.direction,
                        "quality": s.quality,
                        "tp1_rr": s.tp1_rr,
                        "tp_runner_rr": s.tp_runner_rr,
                        **outcome,
                    }
                )
        print(f"  {pair} done: {len(per_pair_dates[pair])} cells")
    print(
        f"  Cells processed: {cells_processed} | setups detected: {len(rows)} | "
        f"errors: {len(errors)}"
    )

    print()
    print("=== Step 3-5 — aggregating, rendering chart and report ===")
    rows_sorted = sorted(rows, key=lambda r: r["timestamp_utc"])
    _RUNS_DIR.mkdir(parents=True, exist_ok=True)
    chart_path = _RUNS_DIR / f"{_TIMESTAMP}_cumulative_r.png"
    _render_chart(rows_sorted, chart_path)

    body, flags = _render_report(
        rows=rows,
        cells_processed=cells_processed,
        excluded_dates=sorted(excluded),
        per_pair_oos=per_pair_oos,
        chart_path=chart_path,
        errors=errors,
    )
    report_path = _RUNS_DIR / f"{_TIMESTAMP}_full_historical_backtest.md"
    report_path.write_text(body, encoding="utf-8")

    # ---- Step 6 — stdout summary ----
    n = len(rows)
    by_outcome: dict[str, int] = {}
    for r in rows:
        by_outcome[r["outcome"]] = by_outcome.get(r["outcome"], 0) + 1
    rs = [
        r["realized_R_strict"]
        for r in rows
        if r["outcome"] not in ("entry_not_hit", "open_at_horizon")
    ]
    rr = [
        r["realized_R_realistic"]
        for r in rows
        if r["outcome"] not in ("entry_not_hit", "open_at_horizon")
    ]
    mean_R_strict = sum(rs) / len(rs) if rs else 0.0
    mean_R_realistic = sum(rr) / len(rr) if rr else 0.0
    cum_strict: list[float] = []
    acc = 0.0
    for r in rows_sorted:
        acc += r["realized_R_strict"]
        cum_strict.append(acc)
    max_dd = _max_drawdown(cum_strict)

    print()
    print("=== Summary ===")
    print(f"  Total setups detected     : {n}")
    print(f"  Mean R per setup (strict) : {mean_R_strict:+.4f}")
    print(f"  Mean R per setup (realist): {mean_R_realistic:+.4f}")
    print(f"  Win rate strict           : {_win_rate(by_outcome, realistic=False):.1%}")
    print(f"  Win rate realistic        : {_win_rate(by_outcome, realistic=True):.1%}")
    print(f"  Max drawdown (R, strict)  : {max_dd:.2f}")
    if flags:
        print(f"  Sanity flags              : {len(flags)} triggered:")
        for f in flags:
            print(f"    - {f}")
    else:
        print("  Sanity flags              : ✅ all clear")
    print(f"  Report                    : {report_path.relative_to(_REPO_ROOT)}")
    print(f"  Chart                     : {chart_path.relative_to(_REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
