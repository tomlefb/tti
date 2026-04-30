"""Setup outcome backtest — sanity check on Sprint 3 detection coherence.

Re-runs ``build_setup_candidates`` on the 19 reference dates × 4 pairs
to obtain the same 16 setups the integration test produces, then for
each setup simulates forward on the M5 fixture (24-hour horizon) to
classify the outcome:

- entry_not_hit / sl_before_entry / sl_hit / tp1_hit_only /
  tp_runner_hit / open_at_horizon

Computes realised R per setup using the partial-exit convention
(50% at TP1, 50% to TP_runner or SL). Aggregates across the population
and emits sanity flags if structural anomalies appear.

**This is a SANITY CHECK, not a performance measurement.** 16 setups is
statistically insufficient for winrate inference; do NOT tune
parameters off these numbers (in-sample bias).

Output: ``calibration/runs/{TIMESTAMP}_setup_outcome_backtest.md`` (gitignored).
"""

from __future__ import annotations

import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.detection.setup import Setup, build_setup_candidates  # noqa: E402

_FIXTURE_DIR = _REPO_ROOT / "tests" / "fixtures" / "historical"
_REFERENCE_CHARTS = _REPO_ROOT / "calibration" / "reference_charts"
_RUNS_DIR = _REPO_ROOT / "calibration" / "runs"
_PAIRS = ["XAUUSD", "NDX100", "EURUSD", "GBPUSD"]

_HORIZON_MINUTES = 24 * 60  # 24 hours
_M5_PER_HOUR = 12


def _settings() -> SimpleNamespace:
    """Mirror of config/settings.py.example — kept in sync manually."""
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


def _reference_dates() -> list[date]:
    out: set[date] = set()
    for f in _REFERENCE_CHARTS.glob("*.json"):
        try:
            out.add(date.fromisoformat(f.name.split("_")[0]))
        except ValueError:
            continue
    return sorted(out)


def _load(pair: str) -> dict[str, pd.DataFrame]:
    return {
        tf: pd.read_parquet(_FIXTURE_DIR / f"{pair}_{tf}.parquet")
        for tf in ("D1", "H4", "H1", "M5")
    }


def _simulate_outcome(setup: Setup, df_m5: pd.DataFrame) -> dict:
    """Forward-simulate one setup over a 24h M5 horizon.

    Same-candle ambiguity policy:
        - If a candle reaches BOTH entry and SL on the entry-finding
          pass, classify as ``sl_before_entry`` (conservative).
        - If a candle reaches BOTH SL and TP (1 or runner) on the
          outcome pass, prefer SL (conservative).

    Post-TP1 horizon-exhausted: the spec's table doesn't explicitly
    cover "TP1 hit, neither SL nor runner reached within remaining
    horizon". We classify as ``tp1_hit_only`` and use its R formula —
    conservative on the runner half, matches spec's category set.

    Returns:
        dict with keys: outcome, entry_hit_time_utc (or None),
        resolution_time_utc (or None), realized_R, time_to_entry_minutes,
        time_to_resolution_minutes.
    """
    times = pd.to_datetime(df_m5["time"], utc=True)
    # Iterate from the MSS-confirm candle inclusive onward.
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
            "realized_R": 0.0,
            "time_to_entry_minutes": None,
            "time_to_resolution_minutes": None,
        }

    entry_time = _t(entry_idx)
    if sl_before_entry_flag:
        return {
            "outcome": "sl_before_entry",
            "entry_hit_time_utc": entry_time,
            "resolution_time_utc": entry_time,
            "realized_R": -1.0,
            "time_to_entry_minutes": _mins(entry_time),
            "time_to_resolution_minutes": _mins(entry_time),
        }

    # Phase 2 — after entry, race SL vs TP1.
    tp1_idx: int | None = None
    for i in range(entry_idx, n):
        if direction == "long":
            sl_now = lows[i] <= sl
            tp1_now = highs[i] >= tp1
        else:
            sl_now = highs[i] >= sl
            tp1_now = lows[i] <= tp1
        if sl_now and tp1_now:
            t = _t(i)
            return {
                "outcome": "sl_hit",
                "entry_hit_time_utc": entry_time,
                "resolution_time_utc": t,
                "realized_R": -1.0,
                "time_to_entry_minutes": _mins(entry_time),
                "time_to_resolution_minutes": _mins(t),
            }
        if sl_now:
            t = _t(i)
            return {
                "outcome": "sl_hit",
                "entry_hit_time_utc": entry_time,
                "resolution_time_utc": t,
                "realized_R": -1.0,
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
            "realized_R": 0.0,  # excluded from R averages per spec
            "time_to_entry_minutes": _mins(entry_time),
            "time_to_resolution_minutes": None,
        }

    tp1_time = _t(tp1_idx)
    if same_tps:
        # TP1 == TP_runner: full close at this level.
        return {
            "outcome": "tp_runner_hit",
            "entry_hit_time_utc": entry_time,
            "resolution_time_utc": tp1_time,
            "realized_R": 0.5 * setup.tp1_rr + 0.5 * setup.tp_runner_rr,
            "time_to_entry_minutes": _mins(entry_time),
            "time_to_resolution_minutes": _mins(tp1_time),
        }

    # Phase 3 — after TP1, race SL vs TP_runner on remaining 50%.
    for j in range(tp1_idx + 1, n):
        if direction == "long":
            sl_now = lows[j] <= sl
            tpr_now = highs[j] >= tpr
        else:
            sl_now = highs[j] >= sl
            tpr_now = lows[j] <= tpr
        if sl_now and tpr_now:
            t = _t(j)
            return {
                "outcome": "tp1_hit_only",
                "entry_hit_time_utc": entry_time,
                "resolution_time_utc": t,
                "realized_R": (setup.tp1_rr - 1.0) / 2.0,
                "time_to_entry_minutes": _mins(entry_time),
                "time_to_resolution_minutes": _mins(t),
            }
        if sl_now:
            t = _t(j)
            return {
                "outcome": "tp1_hit_only",
                "entry_hit_time_utc": entry_time,
                "resolution_time_utc": t,
                "realized_R": (setup.tp1_rr - 1.0) / 2.0,
                "time_to_entry_minutes": _mins(entry_time),
                "time_to_resolution_minutes": _mins(t),
            }
        if tpr_now:
            t = _t(j)
            return {
                "outcome": "tp_runner_hit",
                "entry_hit_time_utc": entry_time,
                "resolution_time_utc": t,
                "realized_R": 0.5 * setup.tp1_rr + 0.5 * setup.tp_runner_rr,
                "time_to_entry_minutes": _mins(entry_time),
                "time_to_resolution_minutes": _mins(t),
            }

    # Post-TP1 horizon exhausted — classify as tp1_hit_only per spec
    # (conservative: assumes the runner half eventually stops at SL).
    return {
        "outcome": "tp1_hit_only",
        "entry_hit_time_utc": entry_time,
        "resolution_time_utc": tp1_time,
        "realized_R": (setup.tp1_rr - 1.0) / 2.0,
        "time_to_entry_minutes": _mins(entry_time),
        "time_to_resolution_minutes": _mins(tp1_time),
    }


def _no_data_outcome() -> dict:
    return {
        "outcome": "open_at_horizon",
        "entry_hit_time_utc": None,
        "resolution_time_utc": None,
        "realized_R": 0.0,
        "time_to_entry_minutes": None,
        "time_to_resolution_minutes": None,
    }


def _bucket(value: float | None, bins: list[float]) -> str:
    """Return the label of the first bin upper-bound that ``value`` fits
    under (``"X+"`` for the overflow bin)."""
    if value is None:
        return "n/a"
    for upper in bins:
        if value < upper:
            return f"<{upper:g}"
    return f">={bins[-1]:g}"


def _ascii_histogram(values: list[float], bins: list[float]) -> list[str]:
    """Produce a markdown bullet list ``- bin: count ``+`★`★…``."""
    counts = {f"<{u:g}": 0 for u in bins}
    counts[f">={bins[-1]:g}"] = 0
    for v in values:
        counts[_bucket(v, bins)] += 1
    max_count = max(counts.values()) if counts else 1
    bar_scale = 30 / max_count if max_count else 1
    lines = []
    for label, c in counts.items():
        bar = "★" * max(int(round(c * bar_scale)), 1 if c > 0 else 0)
        lines.append(f"  - `{label:<6}` : {c:>2}  {bar}")
    return lines


def _render_report(rows: list[dict]) -> tuple[str, list[str]]:
    """Render the full markdown report. Returns (body, sanity_flags)."""
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    n = len(rows)
    by_outcome: dict[str, int] = {}
    realized_R = []
    times_entry = []
    times_resolution = []
    for r in rows:
        by_outcome[r["outcome"]] = by_outcome.get(r["outcome"], 0) + 1
        # Aggregate R: exclude open_at_horizon per spec.
        if r["outcome"] != "open_at_horizon":
            realized_R.append(r["realized_R"])
        if r["time_to_entry_minutes"] is not None:
            times_entry.append(r["time_to_entry_minutes"])
        if r["time_to_resolution_minutes"] is not None:
            times_resolution.append(r["time_to_resolution_minutes"])

    sum_R = sum(realized_R)
    mean_R = sum_R / len(realized_R) if realized_R else 0.0
    mean_te = sum(times_entry) / len(times_entry) if times_entry else None
    mean_tr = sum(times_resolution) / len(times_resolution) if times_resolution else None

    # --- Sanity flags ---
    flags: list[str] = []
    rate_eni = by_outcome.get("entry_not_hit", 0) / n if n else 0
    rate_sl = by_outcome.get("sl_hit", 0) / n if n else 0
    rate_tpr = by_outcome.get("tp_runner_hit", 0) / n if n else 0
    rate_tp1_only = by_outcome.get("tp1_hit_only", 0) / n if n else 0
    if rate_eni > 0.30:
        flags.append(
            f"⚠️ Flag #1 — entry_not_hit rate {rate_eni:.0%} > 30%: many setups "
            f"never see entry. Possible POI selection issue (POI proximal too far)."
        )
    if rate_sl > 0.70:
        flags.append(
            f"⚠️ Flag #2 — sl_hit rate {rate_sl:.0%} > 70%: most entries reach SL "
            f"before any TP. Possible direction or SL placement issue."
        )
    if rate_tpr < 0.05 and rate_tp1_only < 0.20:
        flags.append(
            f"⚠️ Flag #3 — tp_runner_hit {rate_tpr:.0%} < 5% AND tp1_hit_only "
            f"{rate_tp1_only:.0%} < 20%: almost no TPs hit at all. Detection "
            f"direction may be inverted."
        )
    if mean_tr is not None and mean_tr < 30:
        flags.append(
            f"⚠️ Flag #4 — mean time-to-resolution {mean_tr:.0f} min < 30 min: "
            f"setups resolve too fast, suggesting SL too tight or fixtures too short."
        )
    if mean_tr is not None and mean_tr > 12 * 60:
        flags.append(
            f"⚠️ Flag #5 — mean time-to-resolution {mean_tr / 60:.1f}h > 12h: "
            f"setups drag, suggesting TP1 too far or POI badly positioned."
        )

    lines: list[str] = []
    lines.append(f"# Setup outcome backtest — {timestamp}")
    lines.append("")
    if flags:
        lines.append("## ⚠️ Sanity flags triggered")
        lines.append("")
        for f in flags:
            lines.append(f"- {f}")
        lines.append("")
    else:
        lines.append("## ✅ No sanity flags — detection appears structurally coherent.")
        lines.append("")

    lines.append("## Caveat")
    lines.append("")
    lines.append(
        f"{n} setups is statistically insufficient for winrate inference. This "
        "report is a SANITY CHECK on detection coherence, not a performance "
        "measurement. Do NOT tune parameters based on these numbers — that's "
        "in-sample bias."
    )
    lines.append("")

    # --- Per-setup table ---
    lines.append("## Per-setup outcome table")
    lines.append("")
    lines.append(
        "| date | pair | killzone | dir | quality | tp1_rr | tp_runner_rr | "
        "outcome | realized_R | time_to_entry | time_to_resolution |"
    )
    lines.append("|---|---|---|---|---|---:|---:|---|---:|---|---|")
    for r in sorted(rows, key=lambda x: (x["date"], x["pair"], x["killzone"])):
        te = (
            f"{r['time_to_entry_minutes']:.0f}min"
            if r["time_to_entry_minutes"] is not None
            else "—"
        )
        tr = (
            f"{r['time_to_resolution_minutes']:.0f}min"
            if r["time_to_resolution_minutes"] is not None
            else "—"
        )
        lines.append(
            f"| {r['date']} | {r['pair']} | {r['killzone']} | {r['direction']} | "
            f"{r['quality']} | {r['tp1_rr']:.2f} | {r['tp_runner_rr']:.2f} | "
            f"{r['outcome']} | {r['realized_R']:+.2f} | {te} | {tr} |"
        )
    lines.append("")

    # --- Aggregate ---
    lines.append(f"## Aggregate ({n} setups)")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Total setups | {n} |")
    for label in (
        "entry_not_hit",
        "sl_before_entry",
        "sl_hit",
        "tp1_hit_only",
        "tp_runner_hit",
        "open_at_horizon",
    ):
        c = by_outcome.get(label, 0)
        pct = 100.0 * c / n if n else 0
        lines.append(f"| {label} | {c} ({pct:.0f}%) |")
    lines.append(f"| Sum realised R (excludes open_at_horizon) | {sum_R:+.2f} |")
    lines.append(f"| Mean realised R per setup | {mean_R:+.2f} |")
    if mean_te is not None:
        lines.append(f"| Mean time-to-entry | {mean_te / 60:.2f}h ({mean_te:.0f}min) |")
    if mean_tr is not None:
        lines.append(f"| Mean time-to-resolution | {mean_tr / 60:.2f}h ({mean_tr:.0f}min) |")
    lines.append("")

    # --- Sub-aggregates ---
    def _sub_aggregate(group_key: str, group_values: list[str]) -> list[str]:
        out = [
            f"## By {group_key}",
            "",
            "| Group | N | Mean R | Outcome breakdown |",
            "|---|---:|---:|---|",
        ]
        for g in group_values:
            grp = [r for r in rows if r[group_key] == g]
            if not grp:
                continue
            r_vals = [r["realized_R"] for r in grp if r["outcome"] != "open_at_horizon"]
            mean = sum(r_vals) / len(r_vals) if r_vals else 0.0
            ob: dict[str, int] = {}
            for r in grp:
                ob[r["outcome"]] = ob.get(r["outcome"], 0) + 1
            ob_str = ", ".join(f"{k}={v}" for k, v in sorted(ob.items()))
            out.append(f"| {g} | {len(grp)} | {mean:+.2f} | {ob_str} |")
        out.append("")
        return out

    lines.extend(_sub_aggregate("quality", ["A+", "A", "B"]))
    lines.extend(_sub_aggregate("killzone", ["london", "ny"]))
    lines.extend(_sub_aggregate("direction", ["long", "short"]))

    # --- Histograms ---
    lines.append("## Time distributions")
    lines.append("")
    lines.append("Time-to-entry (minutes from MSS confirm to entry fill):")
    lines.append("")
    lines.extend(_ascii_histogram(times_entry, [15, 60, 240, 720, 1440]))
    lines.append("")
    lines.append("Time-to-resolution (minutes from MSS confirm to SL/TP outcome):")
    lines.append("")
    lines.extend(_ascii_histogram(times_resolution, [60, 240, 720, 1440]))
    lines.append("")

    return "\n".join(lines), flags


def main() -> int:
    settings = _settings()
    dates = _reference_dates()
    if not dates:
        print("ERROR: no reference dates found", file=sys.stderr)
        return 2

    fixtures = {pair: _load(pair) for pair in _PAIRS}

    rows: list[dict] = []
    for d in dates:
        for pair in _PAIRS:
            setups = build_setup_candidates(
                df_h4=fixtures[pair]["H4"],
                df_h1=fixtures[pair]["H1"],
                df_m5=fixtures[pair]["M5"],
                df_d1=fixtures[pair]["D1"],
                target_date=d,
                symbol=pair,
                settings=settings,
            )
            for s in setups:
                outcome = _simulate_outcome(s, fixtures[pair]["M5"])
                rows.append(
                    {
                        "date": d.isoformat(),
                        "pair": pair,
                        "killzone": s.killzone,
                        "direction": s.direction,
                        "quality": s.quality,
                        "tp1_rr": s.tp1_rr,
                        "tp_runner_rr": s.tp_runner_rr,
                        **outcome,
                    }
                )

    body, flags = _render_report(rows)
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    _RUNS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _RUNS_DIR / f"{timestamp}_setup_outcome_backtest.md"
    out_path.write_text(body, encoding="utf-8")

    # Print stdout: aggregate + flags.
    lines = body.splitlines()
    # Print flags section + the aggregate table.
    started = False
    for line in lines:
        if line.startswith("## ✅") or line.startswith("## ⚠️"):
            started = True
        if started:
            print(line)
        if line.startswith("## By "):
            break
    print(f"\nReport: {out_path.relative_to(_REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
