"""Setup-pipeline cascade trace — funnel diagnostic.

Instruments ``src.detection.setup.build_setup_candidates`` WITHOUT
modifying any detector code. Wraps the detection callables imported
into ``src.detection.setup`` with spy functions that count outcomes,
runs the orchestrator on the 19 reference dates × 4 watched pairs,
then writes:

- Markdown report at ``calibration/runs/{TIMESTAMP}_setup_cascade.md``:
  one row per (date, pair, killzone) with how many candidates survive
  each filter step, plus a funnel-summary section at the top.
- Funnel summary printed to stdout.

The spies re-call the originals — the pipeline still does exactly
what it would do unmonitored. We just observe.

Usage:
    venv/bin/python calibration/run_setup_diagnostic.py
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Imported AFTER sys.path patch so we resolve the project tree.
from src.detection import setup as setup_mod  # noqa: E402
from src.detection.liquidity import paris_session_to_utc  # noqa: E402

_FIXTURE_DIR = _REPO_ROOT / "tests" / "fixtures" / "historical"
_REFERENCE_CHARTS = _REPO_ROOT / "calibration" / "reference_charts"
_RUNS_DIR = _REPO_ROOT / "calibration" / "runs"
_PAIRS = ["XAUUSD", "NDX100", "EURUSD", "GBPUSD"]
_KILLZONES = ("london", "ny")


# Mirror of config/settings.py.example — kept in sync manually because
# config.settings imports gitignored secrets. Same approach as the
# Sprint 2 / Sprint 3 integration tests.
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


def _reference_dates() -> list[date]:
    out: set[date] = set()
    for f in _REFERENCE_CHARTS.glob("*.json"):
        try:
            out.add(date.fromisoformat(f.name.split("_")[0]))
        except ValueError:
            continue
    return sorted(out)


def _empty_kz_state() -> dict:
    return {
        "bias": None,
        "sweeps_total": 0,
        "sweeps_aligned": 0,
        "sweeps_with_mss": 0,
        "sweeps_with_poi": 0,
        "setups_after_rr": 0,
        "setups_final": 0,
    }


@contextmanager
def _instrument(state: dict, kz_windows: dict):
    """Patch ``src.detection.setup``'s module-level detector names with
    spies that update ``state`` in place. The originals are re-invoked
    so the pipeline behaviour is unchanged.

    ``kz_windows`` maps killzone name → ``(start_utc, end_utc)``; used
    to bucket spy calls by killzone.
    """
    # Snapshot originals.
    o_bias = setup_mod.compute_daily_bias
    o_sweeps = setup_mod.detect_sweeps
    o_mss = setup_mod.detect_mss
    o_fvg = setup_mod.detect_fvgs_in_window
    o_ob = setup_mod.detect_order_block
    o_tp = setup_mod._select_take_profit
    o_grade = setup_mod.grade_setup

    # Bias call ordering tracks killzone — orchestrator iterates
    # london first, then ny.
    bias_seq = list(_KILLZONES)
    bias_idx = [0]

    # Per-sweep state shared across the MSS → FVG → OB → TP → grade
    # spies. Set by spy_mss; read by the others.
    cursor: dict = {"kz": None, "fvg_seen": False, "ob_seen": False}

    def kz_of_kz_start(start_utc):
        for name, (s, _) in kz_windows.items():
            if s == start_utc:
                return name
        return None

    def spy_bias(*a, **kw):
        result = o_bias(*a, **kw)
        if bias_idx[0] < len(bias_seq):
            state[bias_seq[bias_idx[0]]]["bias"] = result
            bias_idx[0] += 1
        return result

    def spy_sweeps(*a, **kw):
        result = o_sweeps(*a, **kw)
        kz_start, _ = kw["killzone_window_utc"]
        kz = kz_of_kz_start(kz_start)
        if kz is not None:
            # `result` here is post-dedup, post-killzone-window; the
            # orchestrator's bias filter happens AFTER this call. So
            # `sweeps_total` is dedup'd-in-killzone, regardless of bias.
            state[kz]["sweeps_total"] = len(result)
        return result

    def spy_mss(df_m5, sweep, **kw):
        # Reaching this means orchestrator's bias filter let the sweep
        # through — counts as one aligned sweep.
        kz = None
        for name, (s, e) in kz_windows.items():
            if s <= sweep.sweep_candle_time_utc <= e:
                kz = name
                break
        cursor["kz"] = kz
        cursor["fvg_seen"] = False
        cursor["ob_seen"] = False
        if kz is not None:
            state[kz]["sweeps_aligned"] += 1
        result = o_mss(df_m5, sweep, **kw)
        if result is not None and kz is not None:
            state[kz]["sweeps_with_mss"] += 1
        return result

    def spy_fvg(*a, **kw):
        result = o_fvg(*a, **kw)
        cursor["fvg_seen"] = bool(result)
        return result

    def spy_ob(df_m5, mss, **kw):
        result = o_ob(df_m5, mss, **kw)
        cursor["ob_seen"] = result is not None
        # POI status settles after both FVG and OB return (orchestrator
        # always calls both when MSS exists).
        kz = cursor["kz"]
        if kz is not None and (cursor["fvg_seen"] or cursor["ob_seen"]):
            state[kz]["sweeps_with_poi"] += 1
        return result

    def spy_tp(**kw):
        result = o_tp(**kw)
        kz = cursor["kz"]
        if result is not None and kz is not None:
            state[kz]["setups_after_rr"] += 1
        return result

    def spy_grade(components):
        grade, confs = o_grade(components)
        kz = cursor["kz"]
        if grade is not None and kz is not None:
            state[kz]["setups_final"] += 1
        return grade, confs

    setup_mod.compute_daily_bias = spy_bias
    setup_mod.detect_sweeps = spy_sweeps
    setup_mod.detect_mss = spy_mss
    setup_mod.detect_fvgs_in_window = spy_fvg
    setup_mod.detect_order_block = spy_ob
    setup_mod._select_take_profit = spy_tp
    setup_mod.grade_setup = spy_grade
    try:
        yield
    finally:
        setup_mod.compute_daily_bias = o_bias
        setup_mod.detect_sweeps = o_sweeps
        setup_mod.detect_mss = o_mss
        setup_mod.detect_fvgs_in_window = o_fvg
        setup_mod.detect_order_block = o_ob
        setup_mod._select_take_profit = o_tp
        setup_mod.grade_setup = o_grade


def _run_one(symbol: str, target_date: date, pair_data: dict, settings) -> dict:
    """Run the orchestrator for one (date, pair); return per-killzone state."""
    london = paris_session_to_utc(target_date, settings.KILLZONE_LONDON)
    ny = paris_session_to_utc(target_date, settings.KILLZONE_NY)
    kz_windows = {"london": london, "ny": ny}
    state = {"london": _empty_kz_state(), "ny": _empty_kz_state()}

    with _instrument(state, kz_windows):
        setup_mod.build_setup_candidates(
            df_h4=pair_data["H4"],
            df_h1=pair_data["H1"],
            df_m5=pair_data["M5"],
            df_d1=pair_data["D1"],
            target_date=target_date,
            symbol=symbol,
            settings=settings,
        )
    return state


def _aligned_str(kz_state: dict) -> str:
    """``sweeps_aligned`` is undefined when bias is no_trade (orchestrator
    skips before the bias filter). Display as ``—`` in that case."""
    if kz_state["bias"] in (None, "no_trade"):
        return "—"
    return str(kz_state["sweeps_aligned"])


def _format_summary(rows: list[dict]) -> tuple[list[str], dict]:
    """Compute the funnel summary (≥1 surviving step per killzone slot)."""
    total_slots = len(rows) * 2  # 2 killzones per (date, pair)

    def slots_with(predicate) -> int:
        c = 0
        for r in rows:
            for kz in _KILLZONES:
                if predicate(r["state"][kz]):
                    c += 1
        return c

    funnel = {
        "total_slots": total_slots,
        "bias_active": slots_with(lambda s: s["bias"] not in (None, "no_trade")),
        "sweep_aligned_ge1": slots_with(lambda s: s["sweeps_aligned"] >= 1),
        "mss_ge1": slots_with(lambda s: s["sweeps_with_mss"] >= 1),
        "poi_ge1": slots_with(lambda s: s["sweeps_with_poi"] >= 1),
        "rr_ge1": slots_with(lambda s: s["setups_after_rr"] >= 1),
        "final_ge1": slots_with(lambda s: s["setups_final"] >= 1),
    }

    def pct(n: int) -> str:
        return f"{100.0 * n / total_slots:.1f}%" if total_slots else "—"

    lines = [
        f"Funnel summary across {len(rows)} (date × pair) cells, " f"{total_slots} killzone slots",
        "",
        "| Step | Slots surviving | % of total |",
        "|---|---:|---:|",
        f"| Total killzone slots | {total_slots} | 100% |",
        f"| Bias != no_trade | {funnel['bias_active']} | {pct(funnel['bias_active'])} |",
        f"| ≥1 sweep aligned with bias | {funnel['sweep_aligned_ge1']} | "
        f"{pct(funnel['sweep_aligned_ge1'])} |",
        f"| ≥1 sweep with valid MSS | {funnel['mss_ge1']} | {pct(funnel['mss_ge1'])} |",
        f"| ≥1 sweep with valid POI | {funnel['poi_ge1']} | {pct(funnel['poi_ge1'])} |",
        f"| ≥1 candidate with RR ≥ MIN_RR | {funnel['rr_ge1']} | {pct(funnel['rr_ge1'])} |",
        f"| Final setups returned | {funnel['final_ge1']} | {pct(funnel['final_ge1'])} |",
    ]
    return lines, funnel


def _format_report(rows: list[dict], settings) -> tuple[str, str, dict]:
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    lines: list[str] = []
    lines.append(f"# Setup pipeline cascade report — {timestamp}")
    lines.append("")
    lines.append(
        "Generated by `calibration/run_setup_diagnostic.py`. "
        "Same code paths as `build_setup_candidates`, instrumented via spies."
    )
    lines.append("")
    lines.append("## Config used")
    lines.append("")
    lines.append("| Key | Value |")
    lines.append("|---|---|")
    for k in (
        "MIN_SWING_AMPLITUDE_ATR_MULT_H4",
        "MIN_SWING_AMPLITUDE_ATR_MULT_H1",
        "MIN_SWING_AMPLITUDE_ATR_MULT_M5",
        "BIAS_SWING_COUNT",
        "BIAS_REQUIRE_H1_CONFIRMATION",
        "SWEEP_RETURN_WINDOW_CANDLES",
        "SWEEP_DEDUP_TIME_WINDOW_MINUTES",
        "SWEEP_DEDUP_PRICE_TOLERANCE_FRACTION",
        "MSS_DISPLACEMENT_MULTIPLIER",
        "MSS_DISPLACEMENT_LOOKBACK",
        "FVG_MIN_SIZE_ATR_MULTIPLIER",
        "MIN_RR",
        "A_PLUS_RR_THRESHOLD",
    ):
        lines.append(f"| `{k}` | `{getattr(settings, k)}` |")
    lines.append("")

    summary_lines, funnel = _format_summary(rows)
    lines.append("## Funnel summary")
    lines.append("")
    lines.extend(summary_lines)
    lines.append("")

    lines.append("## Per (date, pair, killzone) cascade")
    lines.append("")
    lines.append(
        "| date | pair | killzone | bias | sweeps_total | sweeps_aligned | "
        "sweeps_with_mss | sweeps_with_poi | setups_after_rr | setups_final |"
    )
    lines.append("|---|---|---|---|---:|---:|---:|---:|---:|---:|")
    for r in sorted(rows, key=lambda x: (x["date"], x["pair"])):
        for kz in _KILLZONES:
            s = r["state"][kz]
            lines.append(
                f"| {r['date']} | {r['pair']} | {kz} | {s['bias'] or '—'} | "
                f"{s['sweeps_total']} | {_aligned_str(s)} | "
                f"{s['sweeps_with_mss']} | {s['sweeps_with_poi']} | "
                f"{s['setups_after_rr']} | {s['setups_final']} |"
            )
    lines.append("")
    return "\n".join(lines), timestamp, funnel


def main() -> int:
    settings = _settings()
    dates = _reference_dates()
    if not dates:
        print("ERROR: no reference dates found", file=sys.stderr)
        return 2

    fixtures: dict[str, dict[str, pd.DataFrame]] = {}
    for pair in _PAIRS:
        per_pair: dict[str, pd.DataFrame] = {}
        for tf in ("D1", "H4", "H1", "M5"):
            path = _FIXTURE_DIR / f"{pair}_{tf}.parquet"
            if not path.exists():
                print(f"ERROR: fixture missing: {path}", file=sys.stderr)
                return 2
            per_pair[tf] = pd.read_parquet(path)
        fixtures[pair] = per_pair

    rows: list[dict] = []
    for d in dates:
        for pair in _PAIRS:
            state = _run_one(pair, d, fixtures[pair], settings)
            rows.append({"date": d.isoformat(), "pair": pair, "state": state})

    report, timestamp, funnel = _format_report(rows, settings)
    _RUNS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _RUNS_DIR / f"{timestamp}_setup_cascade.md"
    out_path.write_text(report, encoding="utf-8")

    # Stdout funnel summary.
    print(f"Cascade report: {out_path.relative_to(_REPO_ROOT)}")
    print()
    summary_lines, _ = _format_summary(rows)
    for line in summary_lines:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
