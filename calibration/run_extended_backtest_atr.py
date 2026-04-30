"""Extended historical backtest — 12 instruments, ATR-fraction config.

Re-runs the extended backtest with sweep / equal-H/L / SL buffers
derived as **fractions of M5 ATR(14)** instead of fractions of price,
to test whether the strategy generalizes to non-XAU/NDX instruments
with proper volatility normalization.

Coefficients (reverse-engineered from the operator-validated XAU/NDX
config — XAU sweep_buffer/M5 ATR ≈ 17%, NDX ≈ 14% → use 15%):

    SWEEP_BUFFER_ATR_FRACTION = 0.15
    EQUAL_HL_ATR_FRACTION     = 0.10
    SL_BUFFER_ATR_FRACTION    = 0.15

The original 4 pairs keep their operator-validated buffers — the
point is to test generalization to NEW instruments, not to retune
the existing ones.

Output: ``calibration/runs/{TIMESTAMP}_extended_backtest_atr.md``.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

import run_extended_backtest as ext  # noqa: E402
import run_full_backtest as base  # noqa: E402

from src.detection.setup import build_setup_candidates  # noqa: E402
from src.detection.swings import _atr  # noqa: E402

_RUNS_DIR = _REPO_ROOT / "calibration" / "runs"
_TIMESTAMP = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")

# ---- Coefficients -----------------------------------------------------------
SWEEP_BUFFER_ATR_FRACTION = 0.15
EQUAL_HL_ATR_FRACTION = 0.10
SL_BUFFER_ATR_FRACTION = 0.15

# ---- Previous %-of-price baseline (from extended_backtest report) -----------
# Source: calibration/runs/2026-04-29T07-47-59Z_extended_backtest.md
PREV_MEAN_R = {
    "XAUUSD": 0.070,
    "NDX100": 0.644,
    "EURUSD": -1.000,
    "GBPUSD": 0.286,
    "SPX500": -0.658,
    "US30": -0.109,
    "GER30": -0.518,
    "USOUSD": -0.188,
    "XAGUSD": -0.534,
    "BTCUSD": -0.112,
    "ETHUSD": 0.215,
    "USDJPY": -0.875,
}
PREV_SETUPS = {
    "XAUUSD": 50,
    "NDX100": 28,
    "EURUSD": 11,
    "GBPUSD": 17,
    "SPX500": 27,
    "US30": 35,
    "GER30": 53,
    "USOUSD": 54,
    "XAGUSD": 49,
    "BTCUSD": 28,
    "ETHUSD": 52,
    "USDJPY": 28,
}
PREV_VERDICT = {
    "XAUUSD": "KEEP_ALL_QUALITIES",
    "NDX100": "KEEP_ALL_QUALITIES",
    "EURUSD": "NEEDS_RECALIBRATION",
    "GBPUSD": "DROP",
    "SPX500": "DROP",
    "US30": "DROP",
    "GER30": "DROP",
    "USOUSD": "DROP",
    "XAGUSD": "DROP",
    "BTCUSD": "DROP",
    "ETHUSD": "DROP",
    "USDJPY": "DROP",
}
PREV_REPORT = "calibration/runs/2026-04-29T07-47-59Z_extended_backtest.md"


def _derive_atr_config(median_atr: float) -> dict:
    return {
        "sweep_buffer": SWEEP_BUFFER_ATR_FRACTION * median_atr,
        "equal_hl_tolerance": EQUAL_HL_ATR_FRACTION * median_atr,
        "sl_buffer": SL_BUFFER_ATR_FRACTION * median_atr,
    }


def main() -> int:
    settings_proto = base._settings()
    excluded = base._reference_dates()

    print("=== Extended backtest (ATR-fraction config) ===")
    print(
        f"  SWEEP_BUFFER_ATR_FRACTION = {SWEEP_BUFFER_ATR_FRACTION} | "
        f"EQUAL_HL_ATR_FRACTION = {EQUAL_HL_ATR_FRACTION} | "
        f"SL_BUFFER_ATR_FRACTION = {SL_BUFFER_ATR_FRACTION}"
    )
    print()

    # Load fixtures, compute typical M5 ATR per pair, derive config.
    fixtures: dict[str, dict] = {}
    instrument_config: dict[str, dict] = dict(ext.ORIGINAL_INSTRUMENT_CONFIG)
    typical_prices: dict[str, float] = {}
    typical_atrs: dict[str, float] = {}
    weekday_dates: dict[str, list[date]] = {}
    fixture_ranges: dict[str, tuple[date, date]] = {}

    for pair in ext.ALL_PAIRS:
        bundle = base._load_pair(pair)
        fixtures[pair] = bundle
        m5 = bundle["M5"]
        typical_prices[pair] = float(m5["close"].median())
        atr = _atr(m5, 14).dropna()
        typical_atrs[pair] = float(atr.median())
        if pair not in instrument_config:
            instrument_config[pair] = _derive_atr_config(typical_atrs[pair])
        weekdays = ext._all_weekday_dates(m5)
        weekday_dates[pair] = weekdays
        fixture_ranges[pair] = (weekdays[0], weekdays[-1]) if weekdays else (None, None)

    # Print derived config side-by-side with %-of-price baseline (for new pairs).
    print("Per-instrument config (ATR-fraction vs previous %-of-price):")
    print(
        f"{'pair':<8} {'price':>12} {'ATR(14)':>10} "
        f"{'sweep_pct':>11} {'sweep_atr':>11} "
        f"{'sl_pct':>10} {'sl_atr':>10}  source"
    )
    pct_sweep = {p: 0.0003 * typical_prices[p] for p in ext.NEW_PAIRS}
    pct_sl = {p: 0.0005 * typical_prices[p] for p in ext.NEW_PAIRS}
    for pair in ext.ALL_PAIRS:
        cfg = instrument_config[pair]
        if pair in ext.ORIGINAL_PAIRS:
            print(
                f"{pair:<8} {typical_prices[pair]:>12.4f} {typical_atrs[pair]:>10.4f} "
                f"{cfg['sweep_buffer']:>11.5f} {cfg['sweep_buffer']:>11.5f} "
                f"{cfg['sl_buffer']:>10.5f} {cfg['sl_buffer']:>10.5f}  operator-validated"
            )
        else:
            print(
                f"{pair:<8} {typical_prices[pair]:>12.4f} {typical_atrs[pair]:>10.4f} "
                f"{pct_sweep[pair]:>11.5f} {cfg['sweep_buffer']:>11.5f} "
                f"{pct_sl[pair]:>10.5f} {cfg['sl_buffer']:>10.5f}  derived (ATR)"
            )
    print()

    # Build settings and run detection.
    settings = SimpleNamespace(**vars(settings_proto))
    settings.INSTRUMENT_CONFIG = instrument_config

    per_pair_oos: dict[str, list[date]] = {}
    for pair in ext.ALL_PAIRS:
        if pair in ext.ORIGINAL_PAIRS:
            per_pair_oos[pair] = [d for d in weekday_dates[pair] if d not in excluded]
        else:
            per_pair_oos[pair] = list(weekday_dates[pair])

    cells_total = sum(len(v) for v in per_pair_oos.values())
    print(f"Total cells to process: {cells_total}")
    print()
    print("Running detection...")
    print()

    rows: list[dict] = []
    errors: list[str] = []
    for pair in ext.ALL_PAIRS:
        bundle = fixtures[pair]
        kept = 0
        for d in per_pair_oos[pair]:
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
                sys.stderr.write(msg + "\n")
                continue
            for s in setups:
                try:
                    outcome = base._simulate_outcome(s, bundle["M5"])
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
                kept += 1
        print(f"  {pair} done: {len(per_pair_oos[pair])} cells → {kept} setups")

    print()
    print(f"Total setups: {len(rows)} (errors: {len(errors)})")
    print()

    # ---- Aggregations ----
    rows_by_pair: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        rows_by_pair[r["pair"]].append(r)

    agg_all = {p: ext._aggregate(rows_by_pair.get(p, [])) for p in ext.ALL_PAIRS}
    agg_a = {
        p: ext._aggregate([r for r in rows_by_pair.get(p, []) if r["quality"] in ("A+", "A")])
        for p in ext.ALL_PAIRS
    }
    decision = {p: ext._classify(agg_all[p], agg_a[p]) for p in ext.ALL_PAIRS}
    suggested = [
        p for p in ext.ALL_PAIRS if decision[p] in ("KEEP_A_PLUS_AND_A", "KEEP_ALL_QUALITIES")
    ]

    # ---- Build report ----
    lines: list[str] = []
    lines.append(f"# Extended backtest — ATR-fraction config — {_TIMESTAMP}")
    lines.append("")
    lines.append(
        "Same 12 instruments and OOS scope as the previous extended backtest. "
        "Only the per-instrument buffer derivation changed: the 8 new pairs now "
        "use **ATR-fraction normalization** "
        f"(sweep={SWEEP_BUFFER_ATR_FRACTION}×ATR, equal_hl={EQUAL_HL_ATR_FRACTION}×ATR, "
        f"sl={SL_BUFFER_ATR_FRACTION}×ATR). The original 4 pairs keep their "
        "operator-validated buffers."
    )
    lines.append("")
    lines.append(f"Previous %-of-price report: `{PREV_REPORT}`")
    lines.append("")

    # ---- ATR-fraction config table ----
    lines.append("## Per-instrument config — ATR-fraction vs %-of-price")
    lines.append("")
    lines.append(
        "| Instrument | Median price | M5 ATR(14) | sweep (% prev) | sweep (ATR new) | sl (% prev) | sl (ATR new) | Source |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---|")
    for pair in ext.ALL_PAIRS:
        cfg = instrument_config[pair]
        if pair in ext.ORIGINAL_PAIRS:
            lines.append(
                f"| {pair} | {typical_prices[pair]:.4f} | {typical_atrs[pair]:.4f} | "
                f"{cfg['sweep_buffer']:.5f} | {cfg['sweep_buffer']:.5f} | "
                f"{cfg['sl_buffer']:.5f} | {cfg['sl_buffer']:.5f} | operator-validated |"
            )
        else:
            lines.append(
                f"| {pair} | {typical_prices[pair]:.4f} | {typical_atrs[pair]:.4f} | "
                f"{pct_sweep[pair]:.5f} | {cfg['sweep_buffer']:.5f} | "
                f"{pct_sl[pair]:.5f} | {cfg['sl_buffer']:.5f} | derived (ATR) |"
            )
    lines.append("")

    if errors:
        lines.append(f"## Errors during run ({len(errors)} cells skipped)")
        lines.append("")
        for e in errors[:30]:
            lines.append(f"- {e}")
        if len(errors) > 30:
            lines.append(f"- … and {len(errors) - 30} more")
        lines.append("")

    # ---- Per-instrument summary ----
    lines.append("## Section 1 — Per-instrument summary (all qualities, ATR config)")
    lines.append("")
    lines.append(
        "| Instrument | M5 range | Months | OOS dates | Setups | Setups/month | Win rate | Mean R | Total R | Max DD |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for pair in ext.ALL_PAIRS:
        ag = agg_all[pair]
        m5_range = f"{fixture_ranges[pair][0]} → {fixture_ranges[pair][1]}"
        months = ext._months_covered(rows_by_pair.get(pair, []))
        per_m = (ag["n"] / months) if months else 0.0
        lines.append(
            f"| {pair} | {m5_range} | {months} | {len(per_pair_oos[pair])} | "
            f"{ag['n']} | {per_m:.2f} | {ag['win_rate_strict']:.1%} | "
            f"{ag['mean_R_strict']:+.3f} | {ag['total_R_strict']:+.2f} | "
            f"{ag['max_drawdown']:.2f} |"
        )
    lines.append("")

    # ---- Comparison %-of-price vs ATR-fraction (new pairs) ----
    lines.append("## Comparison %-of-price vs ATR-fraction (8 new pairs)")
    lines.append("")
    lines.append(
        "| Instrument | Setups (%) | Setups (ATR) | Mean R (%) | Mean R (ATR) | Δ Mean R | Verdict (%) | Verdict (ATR) |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---|---|")
    for pair in ext.NEW_PAIRS:
        prev = PREV_MEAN_R[pair]
        new = agg_all[pair]["mean_R_strict"]
        lines.append(
            f"| {pair} | {PREV_SETUPS[pair]} | {agg_all[pair]['n']} | "
            f"{prev:+.3f} | {new:+.3f} | {new - prev:+.3f} | "
            f"{PREV_VERDICT[pair]} | {decision[pair]} |"
        )
    lines.append("")

    # ---- Original 4 control (should be identical) ----
    lines.append("## Control: original 4 pairs (should match prior run within float noise)")
    lines.append("")
    lines.append("| Instrument | Setups (%) | Setups (ATR) | Mean R (%) | Mean R (ATR) | Δ |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for pair in ext.ORIGINAL_PAIRS:
        prev = PREV_MEAN_R[pair]
        new = agg_all[pair]["mean_R_strict"]
        lines.append(
            f"| {pair} | {PREV_SETUPS[pair]} | {agg_all[pair]['n']} | "
            f"{prev:+.3f} | {new:+.3f} | {new - prev:+.3f} |"
        )
    lines.append("")

    # ---- Decision matrix (new) ----
    lines.append("## Decision matrix (ATR-fraction)")
    lines.append("")
    lines.append(
        "Same rules as the prior run:\n"
        "- **KEEP_A_PLUS_AND_A**: A-only mean R > 0.4 AND A-only setups ≥ 3.\n"
        "- **KEEP_ALL_QUALITIES**: as above AND all-qualities mean R > 0.\n"
        "- **NEEDS_RECALIBRATION**: ≥ 5 setups with 0% win rate.\n"
        "- **INSUFFICIENT_DATA**: < 10 setups across the full backtest.\n"
        "- **DROP**: otherwise."
    )
    lines.append("")
    lines.append(
        "| Instrument | Setups (all) | Mean R (all) | A-only setups | Mean R (A) | Verdict (ATR) | Verdict (%) |"
    )
    lines.append("|---|---:|---:|---:|---:|---|---|")
    for pair in ext.ALL_PAIRS:
        a_all = agg_all[pair]
        a_a = agg_a[pair]
        lines.append(
            f"| {pair} | {a_all['n']} | {a_all['mean_R_strict']:+.3f} | "
            f"{a_a['n']} | {a_a['mean_R_strict']:+.3f} | "
            f"**{decision[pair]}** | {PREV_VERDICT[pair]} |"
        )
    lines.append("")

    # ---- Suggested WATCHED_PAIRS ----
    lines.append("## Suggested WATCHED_PAIRS (ATR-fraction)")
    lines.append("")
    lines.append("```python")
    lines.append(f"WATCHED_PAIRS = {suggested!r}")
    lines.append("```")
    lines.append("")
    lines.append("**Rationale:**")
    lines.append("")
    for pair in ext.ALL_PAIRS:
        a_all = agg_all[pair]
        a_a = agg_a[pair]
        verdict = decision[pair]
        prev_v = PREV_VERDICT[pair]
        change_note = "" if verdict == prev_v else f" (changed from {prev_v} under %-of-price)"
        if pair in suggested:
            why = f"included — A-only mean R {a_a['mean_R_strict']:+.3f} on " f"{a_a['n']} setups"
        else:
            if verdict == "INSUFFICIENT_DATA":
                why = f"excluded — only {a_all['n']} setups detected"
            elif verdict == "NEEDS_RECALIBRATION":
                why = f"excluded — {a_all['n']} setups with 0% win rate"
            elif verdict == "DROP":
                if a_a["n"] < 3:
                    why = f"excluded — only {a_a['n']} A-grade setups (< 3)"
                else:
                    why = f"excluded — A-only mean R {a_a['mean_R_strict']:+.3f} below +0.4"
            else:
                why = f"verdict {verdict}"
        if pair not in ext.ORIGINAL_PAIRS:
            why += " (ATR-derived buffers, not operator-validated)"
        lines.append(f"- **{pair}** ({verdict}{change_note}): {why}.")
    lines.append("")

    # ---- Sanity flags per instrument ----
    lines.append("## Sanity flags per instrument")
    lines.append("")
    lines.append("| Instrument | Flags |")
    lines.append("|---|---|")
    for pair in ext.ALL_PAIRS:
        flags = ext._flags_for_instrument(agg_all[pair])
        lines.append(f"| {pair} | {' '.join(flags) if flags else '✅ clear'} |")
    lines.append("")

    # ---- Save report ----
    _RUNS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = _RUNS_DIR / f"{_TIMESTAMP}_extended_backtest_atr.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")

    # ---- Stdout summary ----
    print("=== Comparison %-of-price vs ATR-fraction (8 new pairs) ===")
    print(
        f"{'pair':<8} {'set %':>6} {'set ATR':>8} {'mR %':>8} "
        f"{'mR ATR':>8} {'Δ mR':>8}  {'verdict %':<22} {'verdict ATR':<22}"
    )
    for pair in ext.NEW_PAIRS:
        prev = PREV_MEAN_R[pair]
        new = agg_all[pair]["mean_R_strict"]
        print(
            f"{pair:<8} {PREV_SETUPS[pair]:>6} {agg_all[pair]['n']:>8} "
            f"{prev:>+8.3f} {new:>+8.3f} {new - prev:>+8.3f}  "
            f"{PREV_VERDICT[pair]:<22} {decision[pair]:<22}"
        )
    print()
    print("=== Control: original 4 pairs ===")
    print(f"{'pair':<8} {'set %':>6} {'set ATR':>8} {'mR %':>8} {'mR ATR':>8} {'Δ':>8}")
    for pair in ext.ORIGINAL_PAIRS:
        prev = PREV_MEAN_R[pair]
        new = agg_all[pair]["mean_R_strict"]
        print(
            f"{pair:<8} {PREV_SETUPS[pair]:>6} {agg_all[pair]['n']:>8} "
            f"{prev:>+8.3f} {new:>+8.3f} {new - prev:>+8.3f}"
        )
    print()
    print("=== Decision matrix (ATR-fraction) ===")
    for pair in ext.ALL_PAIRS:
        change = (
            "" if decision[pair] == PREV_VERDICT[pair] else f"  ← changed from {PREV_VERDICT[pair]}"
        )
        print(f"  {pair:<8} → {decision[pair]}{change}")
    print()
    print("=== Suggested WATCHED_PAIRS (ATR-fraction) ===")
    print(f"WATCHED_PAIRS = {suggested!r}")
    print()
    print(f"Report: {report_path.relative_to(_REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
