"""Targeted dive — two read-only investigations of the Sprint 3 pipeline.

**Investigation 1 — NDX100 2026-01-02 NY**
Cascade report shows 4 POI-valid candidates, all rejected at RR. Dump
the full pipeline state of each candidate: sweep, MSS, POI, entry, SL,
every opposing-liquidity level considered with distance + RR + reason
for acceptance/rejection.

**Investigation 2 — XAUUSD 2025-10-15**
Operator's annotation labels this as a clean trending bullish day, yet
both killzones produce ``bias=no_trade``. Dump the H4 / H1 swing
sequences and the per-timeframe ``compute_timeframe_bias`` outputs to
identify whether the discrepancy lives in the swing detector, the
broken-structure heuristic, or the H4∩H1 intersection.

NO detector code is modified. Spies wrap module-level callables to
record arguments / outputs while re-invoking the originals.

Usage:
    venv/bin/python calibration/run_setup_diagnostic_dive.py
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

from src.detection import setup as setup_mod  # noqa: E402
from src.detection.bias import compute_timeframe_bias  # noqa: E402
from src.detection.liquidity import paris_session_to_utc  # noqa: E402
from src.detection.swings import find_swings  # noqa: E402

_FIXTURE_DIR = _REPO_ROOT / "tests" / "fixtures" / "historical"
_RUNS_DIR = _REPO_ROOT / "calibration" / "runs"


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        SESSION_ASIA=(2, 0, 6, 0),
        KILLZONE_LONDON=(9, 0, 12, 0),
        KILLZONE_NY=(15, 30, 18, 0),
        SWING_LOOKBACK_H4=2,
        SWING_LOOKBACK_H1=2,
        SWING_LOOKBACK_M5=2,
        MIN_SWING_AMPLITUDE_ATR_MULT_H4=1.0,
        MIN_SWING_AMPLITUDE_ATR_MULT_H1=1.0,
        MIN_SWING_AMPLITUDE_ATR_MULT_M5=1.0,
        BIAS_REQUIRE_H1_CONFIRMATION=False,
        BIAS_SWING_COUNT=4,
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


def _load_fixture(pair: str) -> dict[str, pd.DataFrame]:
    return {
        tf: pd.read_parquet(_FIXTURE_DIR / f"{pair}_{tf}.parquet")
        for tf in ("D1", "H4", "H1", "M5")
    }


def _slice_until(df: pd.DataFrame, cutoff_utc) -> pd.DataFrame:
    if len(df) == 0:
        return df
    times = pd.to_datetime(df["time"], utc=True)
    return df.loc[times < cutoff_utc].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Investigation 1 — per-candidate dump
# ---------------------------------------------------------------------------


@contextmanager
def _capture_candidates(records: list[dict], target_kz_window):
    """Spy on the per-sweep pipeline. Records one dict per sweep that
    reached MSS detection within ``target_kz_window``.
    """
    o_mss = setup_mod.detect_mss
    o_fvg = setup_mod.detect_fvgs_in_window
    o_ob = setup_mod.detect_order_block
    o_tp = setup_mod._select_take_profit

    cur: dict = {}

    def in_target(sweep) -> bool:
        s, e = target_kz_window
        return s <= sweep.sweep_candle_time_utc <= e

    def spy_mss(df_m5, sweep, **kw):
        result = o_mss(df_m5, sweep, **kw)
        if in_target(sweep):
            cur.clear()
            cur.update({"sweep": sweep, "mss": result, "fvgs": [], "ob": None, "tp": None})
            records.append(cur.copy())
        return result

    def spy_fvg(*a, **kw):
        result = o_fvg(*a, **kw)
        if records and "sweep" in records[-1]:
            records[-1]["fvgs"] = list(result)
        return result

    def spy_ob(df_m5, mss, **kw):
        result = o_ob(df_m5, mss, **kw)
        if records and "sweep" in records[-1]:
            records[-1]["ob"] = result
        return result

    def spy_tp(**kw):
        result = o_tp(**kw)
        if records and "sweep" in records[-1]:
            records[-1]["tp_args"] = kw
            records[-1]["tp"] = result
        return result

    setup_mod.detect_mss = spy_mss
    setup_mod.detect_fvgs_in_window = spy_fvg
    setup_mod.detect_order_block = spy_ob
    setup_mod._select_take_profit = spy_tp
    try:
        yield
    finally:
        setup_mod.detect_mss = o_mss
        setup_mod.detect_fvgs_in_window = o_fvg
        setup_mod.detect_order_block = o_ob
        setup_mod._select_take_profit = o_tp


def _fmt(v: float, pair: str) -> str:
    if pair in ("EURUSD", "GBPUSD"):
        return f"{v:.5f}"
    return f"{v:.3f}"


def _describe_tp_walk(record: dict, pair: str, min_rr: float) -> list[str]:
    """Re-walk the TP search for one candidate, annotating every opposing
    level with distance, RR, and accept/reject reason. Mirrors
    ``setup._select_take_profit`` exactly so the trace matches reality.
    """
    args = record.get("tp_args")
    if args is None:
        return ["    (POI step did not call _select_take_profit)"]
    direction = args["direction"]
    entry = args["entry"]
    risk = args["risk"]
    levels = args["levels"]
    sweep = args["sweep"]

    if direction == "long":
        opposing = [lv for lv in levels if lv.type == "high" and lv.price > entry]
        opposing.sort(key=lambda lv: lv.price - entry)
    else:
        opposing = [lv for lv in levels if lv.type == "low" and lv.price < entry]
        opposing.sort(key=lambda lv: entry - lv.price)

    out: list[str] = []
    out.append(
        f"    direction={direction}  entry={_fmt(entry, pair)}  "
        f"SL_distance(risk)={_fmt(risk, pair)}  MIN_RR={min_rr}"
    )
    out.append(f"    opposing-liquidity candidates ({len(opposing)} of correct side):")
    if not opposing:
        out.append("      (none — no opposing-side levels above/below entry)")
    chosen_done = False
    for lv in opposing:
        reward = abs(lv.price - entry)
        rr = reward / risk if risk > 0 else 0.0
        is_swept_level = lv.label == sweep.swept_level_type and lv.price == sweep.swept_level_price
        if is_swept_level:
            verdict = "REJECTED — same level as the sweep we just took out"
        elif rr < min_rr:
            verdict = f"REJECTED — RR {rr:.2f} < MIN_RR {min_rr}"
        elif chosen_done:
            verdict = f"(would qualify with RR {rr:.2f} but earlier match already chosen)"
        else:
            verdict = f"CHOSEN — RR {rr:.2f} ≥ MIN_RR {min_rr}"
            chosen_done = True
        out.append(
            f"      • `{lv.label}` ({lv.strength}) @ {_fmt(lv.price, pair)}  "
            f"distance={_fmt(reward, pair)}  RR={rr:.2f}  → {verdict}"
        )
    out.append(
        f"    _select_take_profit() returned: {record['tp']!r}  "
        f"(None ⇒ no opposing level reached MIN_RR; orchestrator skips this candidate)"
    )
    return out


def _investigation_1(out: list[str]) -> None:
    out.append("# Investigation 1 — NDX100 2026-01-02 NY — why all 4 candidates fail RR")
    out.append("")
    settings = _settings()
    pair = "NDX100"
    target_date = date(2026, 1, 2)
    pair_data = _load_fixture(pair)
    ny_window = paris_session_to_utc(target_date, settings.KILLZONE_NY)

    records: list[dict] = []
    with _capture_candidates(records, ny_window):
        setup_mod.build_setup_candidates(
            df_h4=pair_data["H4"],
            df_h1=pair_data["H1"],
            df_m5=pair_data["M5"],
            df_d1=pair_data["D1"],
            target_date=target_date,
            symbol=pair,
            settings=settings,
        )

    out.append(f"Killzone NY window: {ny_window[0].isoformat()} → {ny_window[1].isoformat()}")
    out.append(
        f"Per-instrument config: "
        f"sweep_buffer={settings.INSTRUMENT_CONFIG[pair]['sweep_buffer']} pts, "
        f"sl_buffer={settings.INSTRUMENT_CONFIG[pair]['sl_buffer']} pts"
    )
    out.append(f"Captured candidates that reached MSS detection: {len(records)}")
    out.append("")

    for idx, rec in enumerate(records, 1):
        sweep = rec["sweep"]
        mss = rec["mss"]
        out.append(f"## Candidate #{idx}")
        out.append("")
        out.append("### Sweep")
        out.append(f"- direction          : `{sweep.direction}`")
        out.append(
            f"- swept level        : `{sweep.swept_level_type}` "
            f"({sweep.swept_level_strength}) @ {_fmt(sweep.swept_level_price, pair)}"
        )
        out.append(
            f"- sweep extreme      : {_fmt(sweep.sweep_extreme_price, pair)}  "
            f"(excursion={_fmt(sweep.excursion, pair)})"
        )
        out.append(f"- sweep candle (UTC) : {sweep.sweep_candle_time_utc.isoformat()}")
        out.append(f"- return candle (UTC): {sweep.return_candle_time_utc.isoformat()}")
        out.append("")
        out.append("### MSS")
        if mss is None:
            out.append("- (no MSS detected within max_lookforward — orchestrator skipped)")
            out.append("")
            continue
        out.append(
            f"- broken swing       : {_fmt(mss.broken_swing_price, pair)} "
            f"@ {mss.broken_swing_time_utc.isoformat()}"
        )
        out.append(
            f"- displacement ratio : {mss.displacement_body_ratio:.2f} "
            f"(≥ MSS_DISPLACEMENT_MULTIPLIER={settings.MSS_DISPLACEMENT_MULTIPLIER})"
        )
        out.append(f"- displacement candle: {mss.displacement_candle_time_utc.isoformat()}")
        out.append(
            f"- MSS confirm candle : {mss.mss_confirm_candle_time_utc.isoformat()}  "
            f"close={_fmt(mss.mss_confirm_candle_close, pair)}"
        )
        out.append("")

        out.append("### POI selection")
        fvgs = rec["fvgs"]
        ob = rec["ob"]
        if fvgs:
            poi = fvgs[0]
            poi_type = "FVG"
            out.append(
                f"- {len(fvgs)} FVG(s) detected; first chosen as POI "
                f"(orchestrator: FVG > OrderBlock priority)"
            )
            out.append(
                f"- POI type           : FVG  proximal={_fmt(poi.proximal, pair)}  "
                f"distal={_fmt(poi.distal, pair)}  "
                f"size_atr_ratio={poi.size_atr_ratio:.2f}"
            )
        elif ob is not None:
            poi = ob
            poi_type = "OrderBlock"
            out.append("- 0 FVGs; falling back to OrderBlock")
            out.append(
                f"- POI type           : OrderBlock  proximal={_fmt(poi.proximal, pair)}  "
                f"distal={_fmt(poi.distal, pair)}  candle_time={poi.candle_time_utc.isoformat()}"
            )
        else:
            out.append("- (no FVG and no OrderBlock — orchestrator skipped)")
            out.append("")
            continue

        out.append("")
        out.append("### Entry / SL / TP")
        sl_buffer = settings.INSTRUMENT_CONFIG[pair]["sl_buffer"]
        if mss.direction == "bullish":
            stop = sweep.sweep_extreme_price - sl_buffer
            direction_disp = "long"
        else:
            stop = sweep.sweep_extreme_price + sl_buffer
            direction_disp = "short"
        entry = poi.proximal
        out.append(f"- entry              : {_fmt(entry, pair)}  (POI proximal)")
        out.append(
            f"- stop_loss          : {_fmt(stop, pair)}  "
            f"(sweep_extreme {'-' if direction_disp == 'long' else '+'} sl_buffer={sl_buffer})"
        )
        out.append(f"- direction          : {direction_disp}  poi_type={poi_type}")
        out.extend(_describe_tp_walk(rec, pair, settings.MIN_RR))
        out.append("")

        out.append("### Verdict")
        if rec["tp"] is None:
            out.append(
                "- **REJECTED** at TP step: no opposing-liquidity level reached "
                f"MIN_RR={settings.MIN_RR}."
            )
        else:
            tp_price, tp_label, tp_rr = rec["tp"]
            out.append(
                f"- TP step accepted: target=`{tp_label}` @ {_fmt(tp_price, pair)}  RR={tp_rr:.2f}"
            )
            out.append(
                "- (Further filtering: grading. Check the cascade report for final outcome.)"
            )
        out.append("")


# ---------------------------------------------------------------------------
# Investigation 2 — bias deep-dive on XAUUSD 2025-10-15
# ---------------------------------------------------------------------------


def _significant_swings_with_time(swings_df, df_source, cutoff_utc) -> list[dict]:
    sig = swings_df[swings_df["swing_type"].notna()]
    if sig.empty:
        return []
    times = pd.to_datetime(df_source.loc[sig.index, "time"], utc=True)
    out: list[dict] = []
    for t, swing_type, swing_price in zip(
        times, sig["swing_type"], sig["swing_price"], strict=False
    ):
        py_t = t.to_pydatetime()
        if py_t > cutoff_utc:
            continue
        out.append({"type": swing_type, "price": float(swing_price), "time": py_t})
    return out


def _explain_timeframe_bias(swings: list[dict], bias_swing_count: int) -> str:
    """Replicate compute_timeframe_bias's reasoning in human-readable form."""
    if len(swings) < bias_swing_count:
        return (
            f"no_trade — only {len(swings)} significant swings available, "
            f"need {bias_swing_count}"
        )
    window = swings[-bias_swing_count:]
    highs = [s["price"] for s in window if s["type"] == "high"]
    lows = [s["price"] for s in window if s["type"] == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return (
            f"no_trade — insufficient pivots in last {bias_swing_count} "
            f"(highs={len(highs)}, lows={len(lows)}; need ≥2 each)"
        )
    bull_h = all(b > a for a, b in zip(highs, highs[1:], strict=False))
    bull_l = all(b > a for a, b in zip(lows, lows[1:], strict=False))
    bear_h = all(b < a for a, b in zip(highs, highs[1:], strict=False))
    bear_l = all(b < a for a, b in zip(lows, lows[1:], strict=False))
    if bull_h and bull_l:
        return "bullish — strict HH on highs AND strict HL on lows"
    if bear_h and bear_l:
        return "bearish — strict LH on highs AND strict LL on lows"
    diag = []
    if not bull_h and not bear_h:
        diag.append("highs broken (mixed direction)")
    if not bull_l and not bear_l:
        diag.append("lows broken (mixed direction)")
    if bull_h and not bull_l:
        diag.append("highs HH but lows NOT HL")
    if bull_l and not bull_h:
        diag.append("lows HL but highs NOT HH")
    if bear_h and not bear_l:
        diag.append("highs LH but lows NOT LL")
    if bear_l and not bear_h:
        diag.append("lows LL but highs NOT LH")
    return "no_trade — " + "; ".join(diag) if diag else "no_trade — pattern unclassified"


def _investigation_2(out: list[str]) -> None:
    out.append("# Investigation 2 — XAUUSD 2025-10-15 bias deep-dive")
    out.append("")
    out.append(
        "Operator labels this as a clean trending-bullish day "
        "(rallye HH/HL en plein milieu). Observed pipeline output: "
        "`bias=no_trade` on both killzones."
    )
    out.append("")
    settings = _settings()
    pair = "XAUUSD"
    target_date = date(2025, 10, 15)
    pair_data = _load_fixture(pair)

    for kz_name, kz_session in (
        ("london", settings.KILLZONE_LONDON),
        ("ny", settings.KILLZONE_NY),
    ):
        kz_start, _ = paris_session_to_utc(target_date, kz_session)
        out.append(f"## {kz_name.upper()} killzone — cutoff {kz_start.isoformat()}")
        out.append("")
        sl_h4 = _slice_until(pair_data["H4"], kz_start)
        sl_h1 = _slice_until(pair_data["H1"], kz_start)
        out.append(f"H4 candles in slice: {len(sl_h4)}  /  H1 candles in slice: {len(sl_h1)}")

        for tf_name, df_slice, lookback, atr_mult in (
            ("H4", sl_h4, settings.SWING_LOOKBACK_H4, settings.MIN_SWING_AMPLITUDE_ATR_MULT_H4),
            ("H1", sl_h1, settings.SWING_LOOKBACK_H1, settings.MIN_SWING_AMPLITUDE_ATR_MULT_H1),
        ):
            out.append("")
            out.append(
                f"### {tf_name} swings (lookback={lookback}, " f"min_amplitude_atr_mult={atr_mult})"
            )
            sig_df = find_swings(
                df_slice,
                lookback=lookback,
                min_amplitude_atr_mult=atr_mult,
            )
            sig = _significant_swings_with_time(sig_df, df_slice, kz_start)
            if not sig:
                out.append("  (no significant swings in slice)")
            else:
                out.append(
                    f"  Last {min(settings.BIAS_SWING_COUNT, len(sig))} of "
                    f"{len(sig)} significant swings (chronological):"
                )
                tail = sig[-settings.BIAS_SWING_COUNT :]
                for s in tail:
                    out.append(
                        f"    - {s['time'].isoformat()}  {s['type']:<5}  "
                        f"@ {_fmt(s['price'], pair)}"
                    )
            tf_bias = compute_timeframe_bias(sig_df, settings.BIAS_SWING_COUNT)
            explanation = _explain_timeframe_bias(sig, settings.BIAS_SWING_COUNT)
            out.append(f"  → compute_timeframe_bias = `{tf_bias}` — {explanation}")

        out.append("")
        # Final intersection (recompute_daily_bias-style).
        h4_bias = compute_timeframe_bias(
            find_swings(
                sl_h4,
                lookback=settings.SWING_LOOKBACK_H4,
                min_amplitude_atr_mult=settings.MIN_SWING_AMPLITUDE_ATR_MULT_H4,
            ),
            settings.BIAS_SWING_COUNT,
        )
        h1_bias = compute_timeframe_bias(
            find_swings(
                sl_h1,
                lookback=settings.SWING_LOOKBACK_H1,
                min_amplitude_atr_mult=settings.MIN_SWING_AMPLITUDE_ATR_MULT_H1,
            ),
            settings.BIAS_SWING_COUNT,
        )
        if settings.BIAS_REQUIRE_H1_CONFIRMATION:
            if h4_bias == h1_bias and h4_bias in ("bullish", "bearish"):
                final = h4_bias
            else:
                final = "no_trade"
        else:
            final = h4_bias
        out.append(
            f"### Intersection: H4=`{h4_bias}`  H1=`{h1_bias}`  "
            f"(require_h1_confirmation={settings.BIAS_REQUIRE_H1_CONFIRMATION}) "
            f"→ final=`{final}`"
        )
        out.append("")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def main() -> int:
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    out: list[str] = []
    out.append(f"# Setup pipeline diagnostic dive — {timestamp}")
    out.append("")
    out.append(
        "Read-only investigations. No detector code or config touched. "
        "Spies wrap module-level callables to capture per-candidate detail."
    )
    out.append("")

    _investigation_1(out)
    out.append("---")
    out.append("")
    _investigation_2(out)

    text = "\n".join(out)
    _RUNS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _RUNS_DIR / f"{timestamp}_setup_diagnostic_dive.md"
    out_path.write_text(text, encoding="utf-8")

    print(text)
    print()
    print(f"Saved to: {out_path.relative_to(_REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
