"""Final portfolio validation — XAU + NDX + ETH × A+/A only.

Confirms the live-deployment numbers using the exact ``WATCHED_PAIRS``
and ``NOTIFY_QUALITIES`` settings from ``config/settings.py.example``.
This is the last sanity check before the operator turns on paper
trading.

Output: ``calibration/runs/{TIMESTAMP}_final_portfolio_validation.md``
(gitignored).
"""

from __future__ import annotations

import sys
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

WATCHED_PAIRS = ["XAUUSD", "NDX100", "ETHUSD"]
NOTIFY_QUALITIES = {"A+", "A"}

# OOS exclusion only applies to operator-validated pairs (XAU/NDX).
# ETH had no manual calibration, so all dates are OOS for it.
EXCLUDE_REF_DATES_FOR = {"XAUUSD", "NDX100"}

# ETH-specific config matching the live settings.py.example values.
ETH_CONFIG = {
    "sweep_buffer": 1.18,
    "equal_hl_tolerance": 0.79,
    "sl_buffer": 1.18,
}


def _all_weekday_dates(df_m5: pd.DataFrame) -> list[date]:
    times = pd.to_datetime(df_m5["time"], utc=True)
    return sorted({d for d in set(times.dt.date) if d.weekday() < 5})


def main() -> int:
    settings = base._settings()
    # Inject the live ETHUSD config.
    settings.INSTRUMENT_CONFIG = {**settings.INSTRUMENT_CONFIG, "ETHUSD": ETH_CONFIG}

    excluded = base._reference_dates()

    print(f"=== Final portfolio validation ({_TIMESTAMP}) ===")
    print(f"  WATCHED_PAIRS    = {WATCHED_PAIRS}")
    print(f"  NOTIFY_QUALITIES = {sorted(NOTIFY_QUALITIES)}")
    print()

    fixtures: dict[str, dict] = {p: base._load_pair(p) for p in WATCHED_PAIRS}
    per_pair_dates: dict[str, list[date]] = {}
    cells_processed = 0
    for pair in WATCHED_PAIRS:
        weekdays = _all_weekday_dates(fixtures[pair]["M5"])
        if pair in EXCLUDE_REF_DATES_FOR:
            oos = [d for d in weekdays if d not in excluded]
            label = f"({len([d for d in weekdays if d in excluded])} ref dates excluded)"
        else:
            oos = list(weekdays)
            label = "(no calibration → all weekdays OOS)"
        per_pair_dates[pair] = oos
        cells_processed += len(oos)
        print(f"  {pair}: weekdays={len(weekdays)}, OOS={len(oos)} {label}")
    print(f"  Total cells: {cells_processed}")
    print()
    print("Running detection...")
    print()

    # ---- Detection + simulation across all 3 pairs ----
    all_rows: list[dict] = []
    notifiable_rows: list[dict] = []
    errors: list[str] = []
    for pair in WATCHED_PAIRS:
        bundle = fixtures[pair]
        kept_all = 0
        kept_notif = 0
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
                errors.append(f"{d} {pair}: {type(exc).__name__}: {exc}")
                continue
            for s in setups:
                try:
                    outcome = base._simulate_outcome(s, bundle["M5"])
                except Exception as exc:
                    errors.append(f"{d} {pair} {s.timestamp_utc}: {type(exc).__name__}: {exc}")
                    continue
                row = {
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
                all_rows.append(row)
                kept_all += 1
                if s.quality in NOTIFY_QUALITIES:
                    notifiable_rows.append(row)
                    kept_notif += 1
        print(
            f"  {pair} done: {len(per_pair_dates[pair])} cells → "
            f"{kept_all} setups ({kept_notif} A+/A notifiable)"
        )

    # ---- Aggregate over notifiable subset (the live-deployment view) ----
    rows_sorted = sorted(notifiable_rows, key=lambda r: r["timestamp_utc"])
    by_outcome: dict[str, int] = {}
    for r in notifiable_rows:
        by_outcome[r["outcome"]] = by_outcome.get(r["outcome"], 0) + 1
    cum_strict: list[float] = []
    s_acc = 0.0
    for r in rows_sorted:
        s_acc += r["realized_R_strict"]
        cum_strict.append(s_acc)
    rs = [
        r["realized_R_strict"]
        for r in notifiable_rows
        if r["outcome"] not in ("entry_not_hit", "open_at_horizon")
    ]
    mean_R_strict = (sum(rs) / len(rs)) if rs else 0.0
    rr = [
        r["realized_R_realistic"]
        for r in notifiable_rows
        if r["outcome"] not in ("entry_not_hit", "open_at_horizon")
    ]
    mean_R_realistic = (sum(rr) / len(rr)) if rr else 0.0
    win_rate_strict = base._win_rate(by_outcome, realistic=False)
    win_rate_realistic = base._win_rate(by_outcome, realistic=True)
    max_dd = base._max_drawdown(cum_strict)
    max_consec_sl = base._max_consecutive_sl(rows_sorted)
    months_covered = (
        len({r["timestamp_utc"].strftime("%Y-%m") for r in notifiable_rows})
        if notifiable_rows
        else 0
    )
    setups_per_month = (len(notifiable_rows) / months_covered) if months_covered else 0.0

    # Per-pair breakdown (notifiable only).
    by_pair: dict[str, list[dict]] = {p: [] for p in WATCHED_PAIRS}
    for r in notifiable_rows:
        by_pair[r["pair"]].append(r)

    # ---- Report ----
    lines: list[str] = []
    lines.append(f"# Final portfolio validation — {_TIMESTAMP}")
    lines.append("")
    lines.append(
        "Live-deployment configuration sanity check. Same backtest engine, "
        "same OOS protocol, but using **only** the live `WATCHED_PAIRS` and "
        "filtering to `NOTIFY_QUALITIES` (A+/A). The numbers below are what "
        "Telegram would push if this 21-month window replayed."
    )
    lines.append("")
    lines.append("## Configuration in effect")
    lines.append("")
    lines.append("```python")
    lines.append(f"WATCHED_PAIRS    = {WATCHED_PAIRS!r}")
    lines.append(f"NOTIFY_QUALITIES = {sorted(NOTIFY_QUALITIES)!r}")
    lines.append("INSTRUMENT_CONFIG['ETHUSD'] = " + repr(ETH_CONFIG))
    lines.append("```")
    lines.append("")

    if errors:
        lines.append(f"### Errors ({len(errors)} cells skipped)")
        lines.append("")
        for e in errors[:20]:
            lines.append(f"- {e}")
        lines.append("")

    # ---- Headline ----
    lines.append("## Headline (notifiable subset = live Telegram traffic)")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---:|")
    lines.append(f"| OOS cells processed | {cells_processed} |")
    lines.append(f"| Setups detected (all qualities) | {len(all_rows)} |")
    lines.append(f"| Setups notifiable (A+/A) | {len(notifiable_rows)} |")
    lines.append(f"| Notifiable share | {len(notifiable_rows) / len(all_rows):.1%} |")
    lines.append(f"| Months covered | {months_covered} |")
    lines.append(f"| Setups/month (A+/A) | {setups_per_month:.2f} |")
    lines.append(f"| Total realized R (strict) | {s_acc:+.2f} |")
    lines.append(f"| Mean R per setup (strict) | {mean_R_strict:+.4f} |")
    lines.append(f"| Mean R per setup (realistic) | {mean_R_realistic:+.4f} |")
    lines.append(f"| Win rate strict | {win_rate_strict:.1%} |")
    lines.append(f"| Win rate realistic | {win_rate_realistic:.1%} |")
    lines.append(f"| Max consecutive SL | {max_consec_sl} |")
    lines.append(f"| Max drawdown (R, strict) | {max_dd:.2f} |")
    lines.append("")

    # ---- Per-pair breakdown ----
    lines.append("## Per-pair (notifiable subset)")
    lines.append("")
    lines.append("| Pair | Setups | Win rate | Mean R | Total R | Max DD |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for pair in WATCHED_PAIRS:
        rows_p = by_pair[pair]
        bo: dict[str, int] = {}
        for r in rows_p:
            bo[r["outcome"]] = bo.get(r["outcome"], 0) + 1
        cum: list[float] = []
        ac = 0.0
        for r in sorted(rows_p, key=lambda x: x["timestamp_utc"]):
            ac += r["realized_R_strict"]
            cum.append(ac)
        rs_p = [
            r["realized_R_strict"]
            for r in rows_p
            if r["outcome"] not in ("entry_not_hit", "open_at_horizon")
        ]
        m = (sum(rs_p) / len(rs_p)) if rs_p else 0.0
        lines.append(
            f"| {pair} | {len(rows_p)} | "
            f"{base._win_rate(bo, realistic=False):.1%} | "
            f"{m:+.3f} | {ac:+.2f} | {base._max_drawdown(cum):.2f} |"
        )
    lines.append("")

    # ---- Outcome distribution ----
    lines.append("## Outcome distribution (notifiable)")
    lines.append("")
    lines.append("| Outcome | N | % |")
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
        pct = 100.0 * c / len(notifiable_rows) if notifiable_rows else 0.0
        lines.append(f"| {label} | {c} | {pct:.1f}% |")
    lines.append("")

    _RUNS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = _RUNS_DIR / f"{_TIMESTAMP}_final_portfolio_validation.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")

    # ---- Stdout ----
    print()
    print("=== Final portfolio validation summary ===")
    print(f"  Total setups detected     : {len(all_rows)}")
    print(f"  Setups notifiable (A+/A)  : {len(notifiable_rows)}")
    print(f"  Setups per month (A+/A)   : {setups_per_month:.2f}")
    print(f"  Mean R per setup (strict) : {mean_R_strict:+.4f}")
    print(f"  Mean R per setup (realist): {mean_R_realistic:+.4f}")
    print(f"  Win rate strict           : {win_rate_strict:.1%}")
    print(f"  Win rate realistic        : {win_rate_realistic:.1%}")
    print(f"  Max consecutive SL        : {max_consec_sl}")
    print(f"  Max drawdown (R, strict)  : {max_dd:.2f}")
    print(f"  Total R strict            : {s_acc:+.2f}")
    print(f"  Report                    : {report_path.relative_to(_REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
