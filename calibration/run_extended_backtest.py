"""Extended historical backtest — 12 instruments.

Adds 8 newly-exported instruments (SPX500, US30, GER30, USOUSD,
XAGUSD, BTCUSD, ETHUSD, USDJPY) to the original 4 (XAUUSD, NDX100,
EURUSD, GBPUSD) to evaluate which deserve inclusion in
``WATCHED_PAIRS``.

KNOWN CAVEAT: per-instrument sweep / equal-H/L / SL buffers for
the 8 new pairs are derived heuristically from the M5 median price
(0.03% / 0.02% / 0.05% of typical price). They are NOT operator-
validated against reference charts. Results on the new pairs are
SUGGESTIVE; full calibration per docs/07 §3 would be required
before live deployment.

Out-of-sample exclusion: 19 reference dates excluded ONLY for the
original 4 pairs. The 8 new pairs include all weekday dates (no
calibration was done on them).

This is MEASUREMENT, not calibration. No parameter tuning happens
here.

Output: ``calibration/runs/{TIMESTAMP}_extended_backtest.md``
(gitignored).
"""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

import run_full_backtest as base  # noqa: E402

from src.detection.setup import build_setup_candidates  # noqa: E402

_RUNS_DIR = _REPO_ROOT / "calibration" / "runs"
_TIMESTAMP = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")

# --- Instrument inventory ---------------------------------------------------
ORIGINAL_PAIRS = ["XAUUSD", "NDX100", "EURUSD", "GBPUSD"]
NEW_PAIRS = ["SPX500", "US30", "GER30", "USOUSD", "XAGUSD", "BTCUSD", "ETHUSD", "USDJPY"]
ALL_PAIRS = ORIGINAL_PAIRS + NEW_PAIRS

ASSET_CLASSES: dict[str, list[str]] = {
    "US Equities": ["NDX100", "SPX500", "US30"],
    "EU Equities": ["GER30"],
    "Commodities": ["XAUUSD", "XAGUSD", "USOUSD"],
    "Forex": ["EURUSD", "GBPUSD", "USDJPY"],
    "Crypto": ["BTCUSD", "ETHUSD"],
}

# --- Original instrument config (operator-validated, do NOT modify) ---------
ORIGINAL_INSTRUMENT_CONFIG = {
    "XAUUSD": {"sweep_buffer": 1.0, "equal_hl_tolerance": 0.5, "sl_buffer": 1.0},
    "NDX100": {"sweep_buffer": 5.0, "equal_hl_tolerance": 3.0, "sl_buffer": 5.0},
    "EURUSD": {"sweep_buffer": 0.00050, "equal_hl_tolerance": 0.00030, "sl_buffer": 0.00050},
    "GBPUSD": {"sweep_buffer": 0.00050, "equal_hl_tolerance": 0.00030, "sl_buffer": 0.00050},
}


def _derive_config(typical_price: float) -> dict:
    """Heuristic derivation: percent-of-price sweep / equal / SL buffers."""
    return {
        "sweep_buffer": 0.0003 * typical_price,
        "equal_hl_tolerance": 0.0002 * typical_price,
        "sl_buffer": 0.0005 * typical_price,
    }


def _all_weekday_dates(df_m5: pd.DataFrame) -> list[date]:
    times = pd.to_datetime(df_m5["time"], utc=True)
    return sorted({d for d in set(times.dt.date) if d.weekday() < 5})


def _months_covered(rows: list[dict]) -> int:
    if not rows:
        return 0
    return len({r["timestamp_utc"].strftime("%Y-%m") for r in rows})


def _aggregate(rows: list[dict]) -> dict:
    rows_sorted = sorted(rows, key=lambda r: r["timestamp_utc"])
    by_outcome: dict[str, int] = {}
    for r in rows:
        by_outcome[r["outcome"]] = by_outcome.get(r["outcome"], 0) + 1
    cum_strict: list[float] = []
    s_acc = 0.0
    for r in rows_sorted:
        s_acc += r["realized_R_strict"]
        cum_strict.append(s_acc)
    rs = [
        r["realized_R_strict"]
        for r in rows
        if r["outcome"] not in ("entry_not_hit", "open_at_horizon")
    ]
    return {
        "rows_sorted": rows_sorted,
        "by_outcome": by_outcome,
        "n": len(rows),
        "total_R_strict": s_acc,
        "mean_R_strict": (sum(rs) / len(rs)) if rs else 0.0,
        "win_rate_strict": base._win_rate(by_outcome, realistic=False),
        "max_consec_sl": base._max_consecutive_sl(rows_sorted),
        "max_drawdown": base._max_drawdown(cum_strict),
    }


def _classify(agg_all: dict, agg_a: dict) -> str:
    """Decision matrix per spec.

    NEEDS_RECALIBRATION takes priority over INSUFFICIENT_DATA when
    enough setups exist to reveal a 0% win rate (anomalous on the
    derived buffers — likely a calibration issue, not noise).
    """
    n_all = agg_all["n"]
    n_a = agg_a["n"]
    if n_all >= 5 and agg_all["win_rate_strict"] == 0.0:
        return "NEEDS_RECALIBRATION"
    if n_all < 10:
        return "INSUFFICIENT_DATA"
    # KEEP_A_PLUS_AND_A: A-grade-only mean R > 0.4 AND ≥ 3 A-grade setups.
    if n_a >= 3 and agg_a["mean_R_strict"] > 0.4:
        # If even WITH B included the edge is positive, flag KEEP_ALL.
        if agg_all["mean_R_strict"] > 0.0:
            return "KEEP_ALL_QUALITIES"
        return "KEEP_A_PLUS_AND_A"
    return "DROP"


def _flags_for_instrument(agg: dict) -> list[str]:
    flags: list[str] = []
    if agg["n"] < 30:
        flags.append(f"⚠️ INSUFFICIENT SAMPLE (<30 setups, n={agg['n']})")
    if agg["n"] > 0 and agg["mean_R_strict"] < 0:
        flags.append(f"⚠️ NEGATIVE EDGE (mean R {agg['mean_R_strict']:+.3f})")
    if agg["max_drawdown"] > 15.0:
        flags.append(f"⚠️ HIGH DRAWDOWN ({agg['max_drawdown']:.2f}R)")
    if agg["n"] > 5 and agg["win_rate_strict"] == 0.0:
        flags.append(
            f"⚠️ ANOMALOUS (0% win rate with {agg['n']} setups — likely calibration issue)"
        )
    return flags


def main() -> int:
    settings_proto = base._settings()
    excluded = base._reference_dates()

    print("=== Extended backtest (12 instruments) ===")
    print()

    # Load fixtures + derive per-instrument config.
    fixtures: dict[str, dict] = {}
    instrument_config: dict[str, dict] = dict(ORIGINAL_INSTRUMENT_CONFIG)
    typical_prices: dict[str, float] = {}
    fixture_ranges: dict[str, tuple[date, date]] = {}
    weekday_dates: dict[str, list[date]] = {}

    for pair in ALL_PAIRS:
        bundle = base._load_pair(pair)
        fixtures[pair] = bundle
        m5 = bundle["M5"]
        median = float(m5["close"].median())
        typical_prices[pair] = median
        if pair not in instrument_config:
            instrument_config[pair] = _derive_config(median)
        weekdays = _all_weekday_dates(m5)
        weekday_dates[pair] = weekdays
        fixture_ranges[pair] = (weekdays[0], weekdays[-1]) if weekdays else (None, None)

    # Print derived config.
    print("Per-instrument config used:")
    print(f"{'pair':<8} {'median':>14} {'sweep_buf':>14} {'equal_hl':>14} {'sl_buf':>14}  source")
    for pair in ALL_PAIRS:
        cfg = instrument_config[pair]
        src = "operator-validated" if pair in ORIGINAL_PAIRS else "derived"
        print(
            f"{pair:<8} {typical_prices[pair]:>14.4f} {cfg['sweep_buffer']:>14.5f} "
            f"{cfg['equal_hl_tolerance']:>14.5f} {cfg['sl_buffer']:>14.5f}  {src}"
        )
    print()

    # Build a settings namespace with the merged INSTRUMENT_CONFIG.
    from types import SimpleNamespace

    settings = SimpleNamespace(**vars(settings_proto))
    settings.INSTRUMENT_CONFIG = instrument_config

    # OOS dates per instrument.
    per_pair_oos: dict[str, list[date]] = {}
    for pair in ALL_PAIRS:
        if pair in ORIGINAL_PAIRS:
            per_pair_oos[pair] = [d for d in weekday_dates[pair] if d not in excluded]
        else:
            per_pair_oos[pair] = list(weekday_dates[pair])
        print(
            f"  {pair}: weekdays={len(weekday_dates[pair])}, "
            f"OOS={len(per_pair_oos[pair])}"
            + (
                " (19 ref dates excluded)"
                if pair in ORIGINAL_PAIRS
                else " (no calibration → all dates OOS)"
            )
        )

    cells_total = sum(len(v) for v in per_pair_oos.values())
    print(f"  Total cells: {cells_total}")
    print()
    print("Running detection (this may take 20-40 min)...")
    print()

    # Detection + simulation.
    rows: list[dict] = []
    errors: list[str] = []
    cells_processed = 0
    for pair in ALL_PAIRS:
        bundle = fixtures[pair]
        kept = 0
        for d in per_pair_oos[pair]:
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
    print(f"Total setups across 12 instruments: {len(rows)} (errors: {len(errors)})")
    print()

    # Aggregate per instrument.
    rows_by_pair: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        rows_by_pair[r["pair"]].append(r)

    # ALL qualities aggregate per pair.
    agg_all: dict[str, dict] = {p: _aggregate(rows_by_pair.get(p, [])) for p in ALL_PAIRS}
    # A-only aggregate per pair.
    agg_a: dict[str, dict] = {
        p: _aggregate([r for r in rows_by_pair.get(p, []) if r["quality"] in ("A+", "A")])
        for p in ALL_PAIRS
    }

    # Decision matrix.
    decision: dict[str, str] = {p: _classify(agg_all[p], agg_a[p]) for p in ALL_PAIRS}

    # Suggested WATCHED_PAIRS = pairs with KEEP verdicts.
    suggested = [p for p in ALL_PAIRS if decision[p] in ("KEEP_A_PLUS_AND_A", "KEEP_ALL_QUALITIES")]

    # ---- Build report ----
    lines: list[str] = []
    lines.append(f"# Extended historical backtest — {_TIMESTAMP}")
    lines.append("")
    lines.append(
        "Measurement-only run on 12 instruments. The 8 new pairs use "
        "**heuristically-derived sweep buffers** (0.03% / 0.02% / 0.05% of "
        "M5 median price). These are NOT operator-validated; results on those "
        "pairs are suggestive, not authoritative."
    )
    lines.append("")
    lines.append(
        "OOS exclusion: 19 reference dates excluded for the original 4 pairs only. "
        "New pairs include all weekday dates."
    )
    lines.append("")

    # ---- Per-instrument config table ----
    lines.append("## Per-instrument config used")
    lines.append("")
    lines.append("| Instrument | Median price | sweep_buffer | equal_hl_tol | sl_buffer | Source |")
    lines.append("|---|---:|---:|---:|---:|---|")
    for pair in ALL_PAIRS:
        cfg = instrument_config[pair]
        src = "operator-validated" if pair in ORIGINAL_PAIRS else "derived (% of price)"
        lines.append(
            f"| {pair} | {typical_prices[pair]:.4f} | {cfg['sweep_buffer']:.5f} | "
            f"{cfg['equal_hl_tolerance']:.5f} | {cfg['sl_buffer']:.5f} | {src} |"
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

    # ---- Section 1 — Per-instrument summary (ALL qualities) ----
    lines.append("## Section 1 — Per-instrument summary (all qualities)")
    lines.append("")
    lines.append(
        "| Instrument | M5 range | Months | OOS dates | Setups | Setups/month | Win rate | Mean R | Total R | Max DD |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for pair in ALL_PAIRS:
        ag = agg_all[pair]
        m5_range = f"{fixture_ranges[pair][0]} → {fixture_ranges[pair][1]}"
        months = _months_covered(rows_by_pair.get(pair, []))
        per_m = (ag["n"] / months) if months else 0.0
        lines.append(
            f"| {pair} | {m5_range} | {months} | {len(per_pair_oos[pair])} | "
            f"{ag['n']} | {per_m:.2f} | {ag['win_rate_strict']:.1%} | "
            f"{ag['mean_R_strict']:+.3f} | {ag['total_R_strict']:+.2f} | "
            f"{ag['max_drawdown']:.2f} |"
        )
    lines.append("")

    # ---- Section 2 — Filter scenario (A+/A only) ----
    lines.append("## Section 2 — Filter scenario (A+/A only)")
    lines.append("")
    lines.append("| Instrument | Setups (A only) | Win rate | Mean R | Total R | Max DD |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for pair in ALL_PAIRS:
        ag = agg_a[pair]
        lines.append(
            f"| {pair} | {ag['n']} | {ag['win_rate_strict']:.1%} | "
            f"{ag['mean_R_strict']:+.3f} | {ag['total_R_strict']:+.2f} | "
            f"{ag['max_drawdown']:.2f} |"
        )
    lines.append("")

    # ---- Section 3 — Asset class grouping ----
    lines.append("## Section 3 — Asset class grouping")
    lines.append("")
    asset_class_summary: list[tuple[str, dict, str | None]] = []
    for klass, members in ASSET_CLASSES.items():
        klass_rows = [r for p in members for r in rows_by_pair.get(p, [])]
        klass_rows_a = [r for r in klass_rows if r["quality"] in ("A+", "A")]
        ag_class = _aggregate(klass_rows)
        ag_class_a = _aggregate(klass_rows_a)
        # Best instrument by mean R within the class (require ≥ 3 setups).
        best = None
        best_score = float("-inf")
        for p in members:
            ap = agg_all[p]
            if ap["n"] >= 3 and ap["mean_R_strict"] > best_score:
                best_score = ap["mean_R_strict"]
                best = p
        asset_class_summary.append((klass, ag_class, best))

        lines.append(f"### {klass} — {', '.join(members)}")
        lines.append("")
        lines.append("| Scope | Setups | Win rate | Mean R | Total R | Max DD |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        lines.append(
            f"| All qualities | {ag_class['n']} | {ag_class['win_rate_strict']:.1%} | "
            f"{ag_class['mean_R_strict']:+.3f} | {ag_class['total_R_strict']:+.2f} | "
            f"{ag_class['max_drawdown']:.2f} |"
        )
        lines.append(
            f"| A+/A only | {ag_class_a['n']} | {ag_class_a['win_rate_strict']:.1%} | "
            f"{ag_class_a['mean_R_strict']:+.3f} | {ag_class_a['total_R_strict']:+.2f} | "
            f"{ag_class_a['max_drawdown']:.2f} |"
        )
        lines.append("")
        if best:
            lines.append(
                f"Best within class (mean R, all qualities, n≥3): **{best}** "
                f"({agg_all[best]['mean_R_strict']:+.3f} R, n={agg_all[best]['n']})"
            )
        else:
            lines.append("Best within class: insufficient data (no member has ≥ 3 setups).")
        lines.append("")

        # Cross-instrument correlation: same-day setup co-occurrence.
        if len(members) > 1:
            dates_per: dict[str, set[date]] = {
                p: {datetime.fromisoformat(r["date"]).date() for r in rows_by_pair.get(p, [])}
                for p in members
            }
            same_dir_per: dict[str, dict[date, set[str]]] = {p: defaultdict(set) for p in members}
            for p in members:
                for r in rows_by_pair.get(p, []):
                    same_dir_per[p][datetime.fromisoformat(r["date"]).date()].add(r["direction"])
            common_pairs: list[str] = []
            from itertools import combinations

            for a, b in combinations(members, 2):
                inter = dates_per[a] & dates_per[b]
                if not inter:
                    common_pairs.append(f"  - {a} ∩ {b}: 0 common-date setups")
                    continue
                same_dir = sum(1 for d in inter if same_dir_per[a][d] & same_dir_per[b][d])
                common_pairs.append(
                    f"  - {a} ∩ {b}: {len(inter)} common dates, {same_dir} same-direction "
                    f"({same_dir / len(inter):.0%})"
                )
            lines.append("Cross-instrument co-occurrence:")
            lines.append("")
            lines.extend(common_pairs)
            lines.append("")

    # ---- Section 4 — Decision matrix ----
    lines.append("## Section 4 — Decision matrix")
    lines.append("")
    lines.append(
        "Rules:\n"
        "- **KEEP_A_PLUS_AND_A**: A-only mean R > 0.4 AND A-only setups ≥ 3.\n"
        "- **KEEP_ALL_QUALITIES**: as above AND all-qualities mean R > 0 (B doesn't drag).\n"
        "- **NEEDS_RECALIBRATION**: ≥ 5 setups with 0% win rate (likely buffer mis-set).\n"
        "- **INSUFFICIENT_DATA**: < 10 setups across the full backtest.\n"
        "- **DROP**: otherwise."
    )
    lines.append("")
    lines.append(
        "| Instrument | Setups (all) | Mean R (all) | A-only setups | Mean R (A) | Verdict |"
    )
    lines.append("|---|---:|---:|---:|---:|---|")
    for pair in ALL_PAIRS:
        a_all = agg_all[pair]
        a_a = agg_a[pair]
        lines.append(
            f"| {pair} | {a_all['n']} | {a_all['mean_R_strict']:+.3f} | {a_a['n']} | "
            f"{a_a['mean_R_strict']:+.3f} | **{decision[pair]}** |"
        )
    lines.append("")

    # ---- Section 5 — Suggested WATCHED_PAIRS ----
    lines.append("## Section 5 — Suggested WATCHED_PAIRS")
    lines.append("")
    lines.append("```python")
    lines.append(f"WATCHED_PAIRS = {suggested!r}")
    lines.append("```")
    lines.append("")
    lines.append("**Rationale:**")
    lines.append("")
    for pair in ALL_PAIRS:
        a_all = agg_all[pair]
        a_a = agg_a[pair]
        verdict = decision[pair]
        if pair in suggested:
            why = (
                f"included — A-only mean R {a_a['mean_R_strict']:+.3f} on "
                f"{a_a['n']} setups passes the SHIP threshold."
            )
        else:
            if verdict == "INSUFFICIENT_DATA":
                why = f"excluded — only {a_all['n']} setups detected; can't conclude."
            elif verdict == "NEEDS_RECALIBRATION":
                why = (
                    f"excluded — {a_all['n']} setups but 0% win rate suggests the "
                    f"derived sweep buffer is wrong; recalibrate per docs/07 §3 before retrying."
                )
            elif verdict == "DROP":
                if a_a["n"] < 3:
                    why = f"excluded — only {a_a['n']} A-grade setups (< 3 threshold)."
                else:
                    why = (
                        f"excluded — A-only mean R {a_a['mean_R_strict']:+.3f} below "
                        f"+0.4 SHIP threshold."
                    )
            else:
                why = f"excluded — verdict {verdict}."
        flags = _flags_for_instrument(a_all)
        if pair not in ORIGINAL_PAIRS:
            why += " (NB: derived buffers, not operator-validated.)"
        flag_suffix = f" {' '.join(flags)}" if flags else ""
        lines.append(f"- **{pair}** ({verdict}): {why}{flag_suffix}")
    lines.append("")

    # ---- Section 6 — Sanity flags ----
    lines.append("## Section 6 — Sanity flags per instrument")
    lines.append("")
    lines.append("| Instrument | Flags |")
    lines.append("|---|---|")
    for pair in ALL_PAIRS:
        flags = _flags_for_instrument(agg_all[pair])
        lines.append(f"| {pair} | {' '.join(flags) if flags else '✅ clear'} |")
    lines.append("")

    # ---- Save report ----
    _RUNS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = _RUNS_DIR / f"{_TIMESTAMP}_extended_backtest.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")

    # ---- Stdout summary ----
    print("=== Section 1 — Per-instrument summary ===")
    print(
        f"{'pair':<8} {'months':>6} {'setups':>7} {'/mo':>6} {'win':>6} "
        f"{'meanR':>8} {'totR':>8} {'maxDD':>7}  verdict"
    )
    for pair in ALL_PAIRS:
        ag = agg_all[pair]
        months = _months_covered(rows_by_pair.get(pair, []))
        per_m = (ag["n"] / months) if months else 0.0
        print(
            f"{pair:<8} {months:>6} {ag['n']:>7} {per_m:>6.2f} "
            f"{ag['win_rate_strict']:>6.1%} {ag['mean_R_strict']:>+8.3f} "
            f"{ag['total_R_strict']:>+8.2f} {ag['max_drawdown']:>7.2f}  {decision[pair]}"
        )
    print()
    print("=== Section 3 — Asset class summary ===")
    print(f"{'class':<14} {'setups':>7} {'win':>6} {'meanR':>8} {'totR':>8}  best")
    for klass, ag, best in asset_class_summary:
        best_str = f"{best} ({agg_all[best]['mean_R_strict']:+.3f}R)" if best else "—"
        print(
            f"{klass:<14} {ag['n']:>7} {ag['win_rate_strict']:>6.1%} "
            f"{ag['mean_R_strict']:>+8.3f} {ag['total_R_strict']:>+8.2f}  {best_str}"
        )
    print()
    print("=== Section 4 — Decision matrix verdicts ===")
    for pair in ALL_PAIRS:
        print(f"  {pair:<8} → {decision[pair]}")
    print()
    print("=== Section 5 — Suggested WATCHED_PAIRS ===")
    print(f"WATCHED_PAIRS = {suggested!r}")
    print()
    print(f"Report: {report_path.relative_to(_REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
