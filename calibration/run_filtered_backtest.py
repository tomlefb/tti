"""Filtered backtest — does the system stay tradable after dropping
EURUSD and B-grade setups?

Filters:
  - pairs: XAUUSD, NDX100, GBPUSD (EURUSD dropped)
  - qualities: A+, A only (B dropped)

Same out-of-sample exclusion as ``run_full_backtest.py`` (19 reference
dates from ``calibration/reference_charts/``).

Reuses the simulator and aggregation helpers from
``run_full_backtest`` so the math stays identical between the two
analyses.

Output: ``calibration/runs/{TIMESTAMP}_backtest_filtered.md`` and
``{TIMESTAMP}_cumulative_r_filtered.png`` (gitignored).
"""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
# Allow ``import run_full_backtest`` from the same calibration directory.
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

import run_full_backtest as base  # noqa: E402

from src.detection.setup import build_setup_candidates  # noqa: E402

_RUNS_DIR = _REPO_ROOT / "calibration" / "runs"
_TIMESTAMP = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")

# ---- Filters ---------------------------------------------------------------
FILTER_PAIRS = ["XAUUSD", "NDX100", "GBPUSD"]
FILTER_QUALITIES = {"A+", "A"}

# ---- Unfiltered baseline -- from the most recent run_full_backtest report --
# Report: calibration/runs/2026-04-29T06-42-04Z_full_historical_backtest.md
# These are the canonical comparison numbers; refresh if the unfiltered run
# is re-executed with different defaults.
BASELINE = {
    "total_setups": 106,
    "cells_processed": 787,
    "mean_R_strict": 0.1738,
    "mean_R_realistic": 0.2238,
    "win_rate_strict": 0.230,
    "win_rate_realistic": 0.242,
    "max_drawdown": 15.30,
    "max_consec_sl": 9,
    "total_R_strict": 17.38,
    "months_covered": 11,  # 2025-06 → 2026-04 inclusive
    "setups_per_month": 106 / 11,  # ≈ 9.6
    "report_path": "calibration/runs/2026-04-29T06-42-04Z_full_historical_backtest.md",
}


def _months_covered(rows: list[dict]) -> int:
    """Distinct YYYY-MM buckets present in the rows."""
    return len({r["timestamp_utc"].strftime("%Y-%m") for r in rows})


def _aggregate(rows: list[dict]) -> dict:
    rows_sorted = sorted(rows, key=lambda r: r["timestamp_utc"])
    by_outcome: dict[str, int] = {}
    for r in rows:
        by_outcome[r["outcome"]] = by_outcome.get(r["outcome"], 0) + 1

    cum_strict: list[float] = []
    cum_realistic: list[float] = []
    s_acc = 0.0
    r_acc = 0.0
    for r in rows_sorted:
        s_acc += r["realized_R_strict"]
        r_acc += r["realized_R_realistic"]
        cum_strict.append(s_acc)
        cum_realistic.append(r_acc)

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

    return {
        "rows_sorted": rows_sorted,
        "by_outcome": by_outcome,
        "total_R_strict": s_acc,
        "total_R_realistic": r_acc,
        "mean_R_strict": (sum(rs_for_mean) / len(rs_for_mean)) if rs_for_mean else 0.0,
        "mean_R_realistic": (sum(rr_for_mean) / len(rr_for_mean)) if rr_for_mean else 0.0,
        "win_rate_strict": base._win_rate(by_outcome, realistic=False),
        "win_rate_realistic": base._win_rate(by_outcome, realistic=True),
        "max_consec_sl": base._max_consecutive_sl(rows_sorted),
        "max_drawdown": base._max_drawdown(cum_strict),
        "cum_strict": cum_strict,
        "cum_realistic": cum_realistic,
    }


def _render_chart(rows_sorted: list[dict], path: Path) -> None:
    if not rows_sorted:
        fig, ax = plt.subplots(figsize=(10, 4))
        ax.text(0.5, 0.5, "No setups detected after filters", ha="center", va="center")
        ax.set_axis_off()
        fig.savefig(path, dpi=120, bbox_inches="tight")
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
    ax.plot(times, cum_s, label="Cumulative R (strict)", color="#27ae60", linewidth=1.6)
    ax.plot(times, cum_r, label="Cumulative R (realistic)", color="#16a085", linewidth=1.6)
    ax.axhline(0.0, color="grey", linewidth=0.8, alpha=0.5)
    ax.set_title(f"Filtered backtest — {','.join(FILTER_PAIRS)} × A+/A only ({_TIMESTAMP})")
    ax.set_xlabel("Setup timestamp (UTC)")
    ax.set_ylabel("Cumulative R")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, dpi=120, bbox_inches="tight")
    plt.close(fig)


def _decision_verdict(mean_R: float, setups_per_month: float) -> tuple[str, str]:
    """Returns (verdict_key, message)."""
    if mean_R < 0.4:
        return (
            "INSUFFICIENT_EDGE",
            "Mean R < 0.4 — not enough edge in the filtered scope. Re-think strategy or add confluences before shipping.",
        )
    if setups_per_month >= 3:
        return (
            "SHIP",
            "Profitable AND tradable. Mean R ≥ 0.4 and ≥ 3 setups/month — ship this filter.",
        )
    return (
        "SPARSE",
        "Profitable but sparse. Mean R ≥ 0.4 but < 3 setups/month — consider expanding the pair list before shipping.",
    )


def _flags(agg: dict, n: int, setups_per_month: float) -> list[str]:
    flags: list[str] = []
    if n < 100:
        flags.append(f"⚠️ INSUFFICIENT SAMPLE: total setups {n} < 100 — results unreliable")
    if agg["mean_R_strict"] < 0:
        flags.append(
            f"⚠️ NEGATIVE EDGE: mean R per setup {agg['mean_R_strict']:+.3f} (strict) — system not profitable"
        )
    if agg["max_consec_sl"] > 10:
        flags.append(
            f"⚠️ HIGH RUIN RISK: max consecutive SL {agg['max_consec_sl']} > 10 — bust risk on 1% account"
        )
    if agg["win_rate_strict"] < 0.20:
        flags.append(f"⚠️ LOW WIN RATE: win rate strict {agg['win_rate_strict']:.1%} < 20%")
    if agg["max_drawdown"] > 15.0:
        flags.append(f"⚠️ HIGH DRAWDOWN: max drawdown {agg['max_drawdown']:.2f}R > 15R")
    if setups_per_month < 3.0:
        flags.append(
            f"⚠️ LOW FREQUENCY: {setups_per_month:.2f} setups/month < 3 — too sparse to be tradable"
        )
    return flags


def _render_report(
    rows: list[dict],
    cells_processed: int,
    agg: dict,
    setups_per_month: float,
    months_covered: int,
    chart_path: Path,
    flags: list[str],
    verdict: tuple[str, str],
    errors: list[str],
) -> str:
    n = len(rows)

    lines: list[str] = []
    lines.append(f"# Filtered historical backtest — {_TIMESTAMP}")
    lines.append("")
    lines.append(
        "Same OOS scope as the unfiltered run (19 reference dates excluded), "
        "with two operator-driven filters applied. Detection settings unchanged."
    )
    lines.append("")

    # ---- Filters applied ----
    lines.append("## Filters applied")
    lines.append("")
    lines.append(f"- **Pairs**: {', '.join(FILTER_PAIRS)} (EURUSD dropped)")
    lines.append(f"- **Qualities**: {', '.join(sorted(FILTER_QUALITIES))} (B dropped)")
    lines.append("- Out-of-sample exclusion: 19 reference dates")
    lines.append("")

    # ---- Sanity flags ----
    if flags:
        lines.append("## Sanity flags")
        lines.append("")
        for f in flags:
            lines.append(f"- {f}")
        lines.append("")
    else:
        lines.append("## ✅ All sanity flags clear")
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
    lines.append("## Headline (filtered scope)")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---:|")
    lines.append(f"| Out-of-sample cells processed (excl EURUSD) | {cells_processed} |")
    lines.append(f"| Total setups detected (A+/A) | {n} |")
    util = (n / cells_processed) if cells_processed else 0.0
    lines.append(f"| Setups per cell (utilization) | {util:.3f} |")
    lines.append(f"| Months covered | {months_covered} |")
    lines.append(f"| Setups per month (avg) | {setups_per_month:.2f} |")
    lines.append(f"| Total realized R (strict) | {agg['total_R_strict']:+.2f} |")
    lines.append(f"| Total realized R (realistic) | {agg['total_R_realistic']:+.2f} |")
    lines.append(f"| Mean R per setup (strict) | {agg['mean_R_strict']:+.4f} |")
    lines.append(f"| Mean R per setup (realistic) | {agg['mean_R_realistic']:+.4f} |")
    lines.append(f"| Win rate strict | {agg['win_rate_strict']:.1%} |")
    lines.append(f"| Win rate realistic | {agg['win_rate_realistic']:.1%} |")
    lines.append(f"| Max consecutive SL hits | {agg['max_consec_sl']} |")
    lines.append(f"| Max drawdown (R, strict) | {agg['max_drawdown']:.2f} |")
    lines.append("")

    # ---- Comparison vs unfiltered ----
    lines.append("## Comparison vs unfiltered baseline")
    lines.append("")
    lines.append(f"Baseline source: `{BASELINE['report_path']}`")
    lines.append("")

    def _delta(filt: float, base_v: float, fmt: str = "+.3f") -> str:
        d = filt - base_v
        return f"{d:{fmt}}"

    lines.append("| Metric | Unfiltered | Filtered (no EUR + A only) | Δ |")
    lines.append("|---|---:|---:|---:|")
    lines.append(
        f"| Total setups | {BASELINE['total_setups']} | {n} | "
        f"{n - BASELINE['total_setups']:+d} |"
    )
    lines.append(
        f"| Mean R strict | {BASELINE['mean_R_strict']:+.4f} | "
        f"{agg['mean_R_strict']:+.4f} | "
        f"{_delta(agg['mean_R_strict'], BASELINE['mean_R_strict'], '+.4f')} |"
    )
    lines.append(
        f"| Mean R realistic | {BASELINE['mean_R_realistic']:+.4f} | "
        f"{agg['mean_R_realistic']:+.4f} | "
        f"{_delta(agg['mean_R_realistic'], BASELINE['mean_R_realistic'], '+.4f')} |"
    )
    lines.append(
        f"| Win rate strict | {BASELINE['win_rate_strict']:.1%} | "
        f"{agg['win_rate_strict']:.1%} | "
        f"{(agg['win_rate_strict'] - BASELINE['win_rate_strict']) * 100:+.1f} pp |"
    )
    lines.append(
        f"| Win rate realistic | {BASELINE['win_rate_realistic']:.1%} | "
        f"{agg['win_rate_realistic']:.1%} | "
        f"{(agg['win_rate_realistic'] - BASELINE['win_rate_realistic']) * 100:+.1f} pp |"
    )
    lines.append(
        f"| Max drawdown | {BASELINE['max_drawdown']:.2f}R | "
        f"{agg['max_drawdown']:.2f}R | "
        f"{agg['max_drawdown'] - BASELINE['max_drawdown']:+.2f}R |"
    )
    lines.append(
        f"| Max consec SL | {BASELINE['max_consec_sl']} | {agg['max_consec_sl']} | "
        f"{agg['max_consec_sl'] - BASELINE['max_consec_sl']:+d} |"
    )
    lines.append(
        f"| Setups per month | {BASELINE['setups_per_month']:.2f} | "
        f"{setups_per_month:.2f} | "
        f"{setups_per_month - BASELINE['setups_per_month']:+.2f} |"
    )
    lines.append(
        f"| Total R strict | {BASELINE['total_R_strict']:+.2f} | "
        f"{agg['total_R_strict']:+.2f} | "
        f"{agg['total_R_strict'] - BASELINE['total_R_strict']:+.2f} |"
    )
    lines.append("")

    # ---- By pair ----
    lines.append("## By pair (within filter)")
    lines.append("")
    lines.extend(base._group_table(rows, "pair", order=FILTER_PAIRS))

    # ---- By killzone ----
    lines.append("## By killzone (within filter)")
    lines.append("")
    lines.extend(base._group_table(rows, "killzone", order=["london", "ny"]))

    # ---- By outcome ----
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
        c = agg["by_outcome"].get(label, 0)
        pct = 100.0 * c / n if n else 0.0
        lines.append(f"| {label} | {c} | {pct:.1f}% |")
    lines.append("")

    # ---- Cumulative R curve pointer ----
    lines.append("## Cumulative R curve (filtered)")
    lines.append("")
    lines.append(f"![cumulative R filtered]({chart_path.name})")
    lines.append("")
    lines.append(f"Path: `{chart_path.relative_to(_REPO_ROOT)}`")
    lines.append("")

    # ---- By month ----
    lines.append("## By month (within filter)")
    lines.append("")
    lines.append("| Month | Setups | Total R strict | Cumulative R strict | Drawdown in month |")
    lines.append("|---|---:|---:|---:|---:|")
    by_month: dict[str, list[dict]] = defaultdict(list)
    for r in agg["rows_sorted"]:
        m = r["timestamp_utc"].strftime("%Y-%m")
        by_month[m].append(r)
    cum = 0.0
    for m in sorted(by_month):
        bucket = by_month[m]
        month_total = sum(x["realized_R_strict"] for x in bucket)
        cum += month_total
        local: list[float] = []
        acc = 0.0
        for x in bucket:
            acc += x["realized_R_strict"]
            local.append(acc)
        local_dd = base._max_drawdown(local)
        lines.append(f"| {m} | {len(bucket)} | {month_total:+.2f} | {cum:+.2f} | {local_dd:.2f} |")
    lines.append("")

    # ---- Decision matrix ----
    verdict_key, verdict_msg = verdict
    lines.append("## Decision matrix")
    lines.append("")
    lines.append("Rule:")
    lines.append("")
    lines.append("- Mean R ≥ 0.4 AND setups/month ≥ 3 → **SHIP**")
    lines.append("- Mean R ≥ 0.4 AND setups/month < 3 → **SPARSE** (expand pairs)")
    lines.append("- Mean R < 0.4 → **INSUFFICIENT_EDGE**")
    lines.append("")
    lines.append(f"**Verdict: `{verdict_key}` — {verdict_msg}**")
    lines.append("")

    # ---- Per-setup table (full, since filtered count is much smaller) ----
    lines.append(f"## All {n} filtered setups (most recent first)")
    lines.append("")
    lines.append(
        "| date | pair | killzone | direction | quality | RR runner | "
        "outcome | realized R strict | realized R realistic |"
    )
    lines.append("|---|---|---|---|---|---:|---|---:|---:|")
    for r in list(reversed(agg["rows_sorted"])):
        lines.append(
            f"| {r['date']} | {r['pair']} | {r['killzone']} | {r['direction']} | "
            f"{r['quality']} | {r['tp_runner_rr']:.2f} | {r['outcome']} | "
            f"{r['realized_R_strict']:+.3f} | {r['realized_R_realistic']:+.3f} |"
        )
    lines.append("")

    return "\n".join(lines)


def main() -> int:
    settings = base._settings()
    excluded = base._reference_dates()

    print("=== Filtered backtest ===")
    print(f"  Pairs    : {FILTER_PAIRS}")
    print(f"  Qualities: {sorted(FILTER_QUALITIES)}")
    print()

    fixtures: dict[str, dict] = {}
    per_pair_dates: dict[str, list[date]] = {}
    cells_processed = 0
    for pair in FILTER_PAIRS:
        fixtures[pair] = base._load_pair(pair)
        all_weekdays = base._trading_dates_for_pair(fixtures[pair]["M5"])
        oos = [d for d in all_weekdays if d not in excluded]
        per_pair_dates[pair] = oos
        cells_processed += len(oos)
        print(f"  {pair}: weekday={len(all_weekdays)} OOS={len(oos)}")

    print(f"  Cells to process: {cells_processed}")
    print()
    print("Running detection...")

    rows: list[dict] = []
    errors: list[str] = []
    for pair in FILTER_PAIRS:
        bundle = fixtures[pair]
        for d in per_pair_dates[pair]:
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
                if s.quality not in FILTER_QUALITIES:
                    continue
                try:
                    outcome = base._simulate_outcome(s, bundle["M5"])
                except Exception as exc:
                    msg = (
                        f"{d} {pair} {s.timestamp_utc}: simulate error — "
                        f"{type(exc).__name__}: {exc}"
                    )
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
        print(f"  {pair} done (kept {sum(1 for r in rows if r['pair'] == pair)})")

    print()
    print(f"Total filtered setups: {len(rows)} (errors: {len(errors)})")

    agg = _aggregate(rows)
    months_covered = _months_covered(rows) if rows else 1
    setups_per_month = (len(rows) / months_covered) if months_covered else 0.0
    flags = _flags(agg, len(rows), setups_per_month)
    verdict = _decision_verdict(agg["mean_R_strict"], setups_per_month)

    _RUNS_DIR.mkdir(parents=True, exist_ok=True)
    chart_path = _RUNS_DIR / f"{_TIMESTAMP}_cumulative_r_filtered.png"
    _render_chart(agg["rows_sorted"], chart_path)

    body = _render_report(
        rows=rows,
        cells_processed=cells_processed,
        agg=agg,
        setups_per_month=setups_per_month,
        months_covered=months_covered,
        chart_path=chart_path,
        flags=flags,
        verdict=verdict,
        errors=errors,
    )
    report_path = _RUNS_DIR / f"{_TIMESTAMP}_backtest_filtered.md"
    report_path.write_text(body, encoding="utf-8")

    # ---- Stdout summary ----
    print()
    print("=== Headline (filtered) ===")
    print(f"  Total setups               : {len(rows)}")
    print(f"  Months covered             : {months_covered}")
    print(f"  Setups per month           : {setups_per_month:.2f}")
    print(f"  Mean R per setup (strict)  : {agg['mean_R_strict']:+.4f}")
    print(f"  Mean R per setup (realist) : {agg['mean_R_realistic']:+.4f}")
    print(f"  Win rate strict            : {agg['win_rate_strict']:.1%}")
    print(f"  Win rate realistic         : {agg['win_rate_realistic']:.1%}")
    print(f"  Max consecutive SL         : {agg['max_consec_sl']}")
    print(f"  Max drawdown (R, strict)   : {agg['max_drawdown']:.2f}")
    print(f"  Total R strict             : {agg['total_R_strict']:+.2f}")
    print()
    print("=== Comparison vs unfiltered ===")
    print(f"  Setups       : {BASELINE['total_setups']:>4}  →  {len(rows):>4}")
    print(
        f"  Mean R strict: {BASELINE['mean_R_strict']:+.4f}  →  {agg['mean_R_strict']:+.4f}  "
        f"(Δ {agg['mean_R_strict'] - BASELINE['mean_R_strict']:+.4f})"
    )
    print(
        f"  Win rate     : {BASELINE['win_rate_strict']:.1%}    →  {agg['win_rate_strict']:.1%}    "
        f"(Δ {(agg['win_rate_strict'] - BASELINE['win_rate_strict']) * 100:+.1f} pp)"
    )
    print(
        f"  Max DD       : {BASELINE['max_drawdown']:.2f}R   →  {agg['max_drawdown']:.2f}R   "
        f"(Δ {agg['max_drawdown'] - BASELINE['max_drawdown']:+.2f}R)"
    )
    print(f"  Setups/month : {BASELINE['setups_per_month']:.2f}    →  {setups_per_month:.2f}")
    print()
    if flags:
        print(f"=== Sanity flags ({len(flags)}) ===")
        for f in flags:
            print(f"  - {f}")
    else:
        print("=== Sanity flags === ✅ all clear")
    print()
    verdict_key, verdict_msg = verdict
    print(f"=== Decision matrix verdict: {verdict_key} ===")
    print(f"  {verdict_msg}")
    print()
    print(f"Report: {report_path.relative_to(_REPO_ROOT)}")
    print(f"Chart : {chart_path.relative_to(_REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
