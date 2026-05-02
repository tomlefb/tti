"""Phase 1 — setup-level comparison MT5 vs Databento.

Investigates the divergence flagged in
``2026-04-30T21-55-44Z_extended_10y_backtest.md``: same overlap window
(2025-06 → 2026-04, A/A+ only) but contradictory mean R between the two
data sources.

Method:
    1. Re-run ``build_setup_candidates`` on both fixtures for every Paris
       weekday in the overlap window, for XAUUSD and NDX100. Capture
       both accepted Setups and rejected candidates (the latter to
       diagnose pipeline failures).
    2. Match each MT5 A/A+ setup to its closest Databento counterpart
       by (date, killzone, direction, time ±15 min).
    3. For each MT5 setup, simulate the outcome on its own M5 source.
       For each matched Databento setup, simulate on Databento M5.
    4. Emit per-setup mapping table + summary stats + initial root-cause
       hypothesis.

Read-only on detector code; settings = operator-validated defaults.
Output: ``calibration/runs/{TS}_mt5_vs_databento_phase1.md`` (gitignored).
"""

from __future__ import annotations

import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.detection.setup import RejectedCandidate, Setup, build_setup_candidates  # noqa: E402

from calibration.run_extended_10y_backtest import (  # noqa: E402
    FixtureCache,
    M5Cache,
    _excluded_paris_dates,
    _rollovers_utc,
    _settings,
    _simulate_outcome,
    _trading_dates_for,
)

_MT5_FIXTURE_DIR = _REPO_ROOT / "tests" / "fixtures" / "historical"
_DB_FIXTURE_DIR = _REPO_ROOT / "tests" / "fixtures" / "historical_extended" / "processed"
_RUNS_DIR = _REPO_ROOT / "calibration" / "runs"
_PAIRS = ["XAUUSD", "NDX100"]
_NOTIFY_QUALITIES = ("A+", "A")
_TZ_PARIS = ZoneInfo("Europe/Paris")
_MATCH_TOLERANCE = timedelta(minutes=15)
_TIMESTAMP = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")


@dataclass
class SetupRecord:
    source: str  # "MT5" or "Databento"
    pair: str
    date: date
    timestamp_utc: datetime
    killzone: str
    direction: str
    quality: str
    entry: float
    sl: float
    tp1: float
    tpr: float
    rr_runner: float
    swept_level_price: float
    swept_level_type: str
    outcome: str
    realized_R: float


def _load_mt5(pair: str) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for tf in ("D1", "H4", "H1", "M5"):
        df = pd.read_parquet(_MT5_FIXTURE_DIR / f"{pair}_{tf}.parquet")
        if df["time"].dt.tz is None:
            df["time"] = df["time"].dt.tz_localize("UTC")
        out[tf] = df
    return out


def _load_databento(pair: str) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for tf in ("D1", "H4", "H1", "M5"):
        df = pd.read_parquet(_DB_FIXTURE_DIR / f"{pair}_{tf}.parquet")
        if df["time"].dt.tz is None:
            df["time"] = df["time"].dt.tz_localize("UTC")
        out[tf] = df
    return out


def _record(setup: Setup, source: str, m5_cache: M5Cache, pair: str, d: date) -> SetupRecord:
    out = _simulate_outcome(setup, m5_cache)
    return SetupRecord(
        source=source,
        pair=pair,
        date=d,
        timestamp_utc=setup.timestamp_utc,
        killzone=setup.killzone,
        direction=setup.direction,
        quality=setup.quality,
        entry=setup.entry_price,
        sl=setup.stop_loss,
        tp1=setup.tp1_price,
        tpr=setup.tp_runner_price,
        rr_runner=setup.tp_runner_rr,
        swept_level_price=setup.swept_level_price,
        swept_level_type=setup.swept_level_type,
        outcome=out["outcome"],
        realized_R=out["realized_R_strict"],
    )


def _detect(
    pair: str,
    cache: FixtureCache,
    m5_cache: M5Cache,
    paris_dates: list[date],
    settings: SimpleNamespace,
) -> tuple[list[SetupRecord], list[tuple[date, RejectedCandidate]]]:
    accepted: list[SetupRecord] = []
    rejected: list[tuple[date, RejectedCandidate]] = []
    for d in paris_dates:
        end_utc = datetime(d.year, d.month, d.day, tzinfo=UTC) + timedelta(days=2)
        window = cache.slice_until(end_utc, days_lookback=60)
        try:
            res = build_setup_candidates(
                df_h4=window["H4"],
                df_h1=window["H1"],
                df_m5=window["M5"],
                df_d1=window["D1"],
                target_date=d,
                symbol=pair,
                settings=settings,
                return_rejected=True,
            )
        except Exception as exc:
            sys.stderr.write(f"{pair} {d}: detection failed — {type(exc).__name__}: {exc}\n")
            continue
        setups, rejs = res
        for s in setups:
            if s.quality in _NOTIFY_QUALITIES:
                accepted.append(_record(s, source="", m5_cache=m5_cache, pair=pair, d=d))
        for r in rejs:
            rejected.append((d, r))
    return accepted, rejected


def _match_mt5_to_db(
    mt5_set: SetupRecord,
    db_setups: list[SetupRecord],
    db_rejected: list[tuple[date, RejectedCandidate]],
) -> tuple[SetupRecord | None, list[tuple[date, RejectedCandidate]]]:
    """Closest Databento match by (killzone+direction, ±15 min). If none,
    return list of plausibly-related rejected candidates (same direction,
    same date)."""
    same_kz_dir = [
        d
        for d in db_setups
        if d.date == mt5_set.date
        and d.killzone == mt5_set.killzone
        and d.direction == mt5_set.direction
    ]
    in_window = [
        d for d in same_kz_dir if abs(d.timestamp_utc - mt5_set.timestamp_utc) <= _MATCH_TOLERANCE
    ]
    if in_window:
        in_window.sort(key=lambda d: abs(d.timestamp_utc - mt5_set.timestamp_utc))
        return in_window[0], []
    # Same date+kz+direction but outside time tolerance — return as a
    # weaker match so the report can call it out.
    if same_kz_dir:
        same_kz_dir.sort(key=lambda d: abs(d.timestamp_utc - mt5_set.timestamp_utc))
        return same_kz_dir[0], []
    # No accepted match — collect candidate rejections on same date/dir.
    rejs = [
        (d, r)
        for d, r in db_rejected
        if d == mt5_set.date
        and r.sweep_info is not None
        and r.sweep_info.get("direction") == mt5_set.direction
    ]
    return None, rejs


def _format_setup_short(s: SetupRecord) -> str:
    return (
        f"{s.timestamp_utc.strftime('%H:%M')} {s.killzone} {s.direction} "
        f"{s.quality} entry={s.entry:.2f} R={s.realized_R:+.2f}"
    )


def main() -> int:
    settings = _settings()
    print(f"=== MT5 vs Databento — Phase 1 — {_TIMESTAMP} ===", flush=True)

    print("Step 1 — load fixtures …", flush=True)
    mt5_fixtures = {p: _load_mt5(p) for p in _PAIRS}
    db_fixtures = {p: _load_databento(p) for p in _PAIRS}
    mt5_caches = {p: FixtureCache(mt5_fixtures[p]) for p in _PAIRS}
    db_caches = {p: FixtureCache(db_fixtures[p]) for p in _PAIRS}
    mt5_m5 = {p: M5Cache(mt5_fixtures[p]["M5"]) for p in _PAIRS}
    db_m5 = {p: M5Cache(db_fixtures[p]["M5"]) for p in _PAIRS}

    # Overlap window = MT5 fixture range, intersected with Databento.
    print("Step 2 — enumerate overlap Paris dates …", flush=True)
    overlap_dates_per_pair: dict[str, list[date]] = {}
    for p in _PAIRS:
        mt5_paris = set(_trading_dates_for(mt5_fixtures[p]["M5"]))
        db_paris = set(_trading_dates_for(db_fixtures[p]["M5"]))
        excluded_db = _excluded_paris_dates(_rollovers_utc(p))
        overlap = sorted(mt5_paris & db_paris - excluded_db)
        overlap_dates_per_pair[p] = overlap
        print(
            f"  {p}: MT5={len(mt5_paris)}, DB={len(db_paris)}, "
            f"DB-rollover-excl={len(excluded_db)}, overlap={len(overlap)}"
        )

    print("Step 3 — detect on MT5 …", flush=True)
    mt5_records: dict[str, list[SetupRecord]] = {}
    mt5_rejected: dict[str, list[tuple[date, RejectedCandidate]]] = {}
    for p in _PAIRS:
        rec, rej = _detect(p, mt5_caches[p], mt5_m5[p], overlap_dates_per_pair[p], settings)
        for r in rec:
            r.source = "MT5"
        mt5_records[p] = rec
        mt5_rejected[p] = rej
        print(f"  {p}: {len(rec)} A/A+ setups, {len(rej)} rejected candidates", flush=True)

    print("Step 4 — detect on Databento …", flush=True)
    db_records: dict[str, list[SetupRecord]] = {}
    db_rejected: dict[str, list[tuple[date, RejectedCandidate]]] = {}
    for p in _PAIRS:
        rec, rej = _detect(p, db_caches[p], db_m5[p], overlap_dates_per_pair[p], settings)
        for r in rec:
            r.source = "Databento"
        db_records[p] = rec
        db_rejected[p] = rej
        print(f"  {p}: {len(rec)} A/A+ setups, {len(rej)} rejected candidates", flush=True)

    print("Step 5 — match MT5 → Databento …", flush=True)
    rows: list[dict] = []  # one row per MT5 setup (with optional db match)
    db_matched_ids: set[int] = set()  # id() of matched DB records
    for p in _PAIRS:
        for m in mt5_records[p]:
            db_match, candidate_rejs = _match_mt5_to_db(m, db_records[p], db_rejected[p])
            time_delta_min: float | None = None
            match_quality = "none"
            db_outcome = None
            db_R = None
            db_quality = None
            db_entry = None
            db_kz = None
            db_dir = None
            db_swept = None
            db_ts_str = None
            reject_reasons: list[str] = []
            if db_match is not None:
                db_matched_ids.add(id(db_match))
                dt = abs(db_match.timestamp_utc - m.timestamp_utc)
                time_delta_min = dt.total_seconds() / 60.0
                if dt <= _MATCH_TOLERANCE:
                    match_quality = "matched_close"
                else:
                    match_quality = "matched_loose_same_kz_dir"
                db_outcome = db_match.outcome
                db_R = db_match.realized_R
                db_quality = db_match.quality
                db_entry = db_match.entry
                db_kz = db_match.killzone
                db_dir = db_match.direction
                db_swept = db_match.swept_level_price
                db_ts_str = db_match.timestamp_utc.strftime("%H:%M")
            elif candidate_rejs:
                match_quality = "rejected_only"
                reject_reasons = sorted({r.rejection_reason for _, r in candidate_rejs})
            rows.append(
                {
                    "pair": p,
                    "date": m.date,
                    "mt5_ts": m.timestamp_utc.strftime("%H:%M"),
                    "mt5_kz": m.killzone,
                    "mt5_dir": m.direction,
                    "mt5_quality": m.quality,
                    "mt5_entry": m.entry,
                    "mt5_swept": m.swept_level_price,
                    "mt5_outcome": m.outcome,
                    "mt5_R": m.realized_R,
                    "match": match_quality,
                    "delta_min": time_delta_min,
                    "db_ts": db_ts_str,
                    "db_kz": db_kz,
                    "db_dir": db_dir,
                    "db_quality": db_quality,
                    "db_entry": db_entry,
                    "db_swept": db_swept,
                    "db_outcome": db_outcome,
                    "db_R": db_R,
                    "reject_reasons": reject_reasons,
                }
            )

    # Databento-only A/A+ setups (no MT5 counterpart by symmetric matching).
    print("Step 6 — find Databento-only setups …", flush=True)
    db_only_rows: list[dict] = []
    for p in _PAIRS:
        for d in db_records[p]:
            if id(d) in db_matched_ids:
                continue
            # Symmetric check: any MT5 setup at same date+kz+dir within ±15 min?
            cand = [
                m
                for m in mt5_records[p]
                if m.date == d.date
                and m.killzone == d.killzone
                and m.direction == d.direction
                and abs(m.timestamp_utc - d.timestamp_utc) <= _MATCH_TOLERANCE
            ]
            if cand:
                continue
            db_only_rows.append(
                {
                    "pair": p,
                    "date": d.date,
                    "ts": d.timestamp_utc.strftime("%H:%M"),
                    "kz": d.killzone,
                    "dir": d.direction,
                    "quality": d.quality,
                    "outcome": d.outcome,
                    "R": d.realized_R,
                    "entry": d.entry,
                    "swept": d.swept_level_price,
                }
            )

    # ------------------------------------------------------------------
    # Render report
    # ------------------------------------------------------------------
    print("Step 7 — render report …", flush=True)
    lines: list[str] = []
    lines.append(f"# MT5 vs Databento — Phase 1 setup-level comparison — {_TIMESTAMP}")
    lines.append("")
    lines.append(
        "Investigates why the 10-year Databento backtest disagrees with the "
        "11-month MT5 backtest on the same overlap window. Method: re-run "
        "the operator-validated detection pipeline on both fixture sets, "
        "match MT5 A/A+ setups to their Databento counterparts by "
        "(date, killzone, direction, ±15 min), simulate outcomes on each "
        "source's own M5 data."
    )
    lines.append("")
    lines.append("## Headline counts")
    lines.append("")
    lines.append("| Source | Pair | Cells (overlap) | A/A+ setups | Rejected candidates |")
    lines.append("|---|---|---:|---:|---:|")
    for p in _PAIRS:
        lines.append(
            f"| MT5 | {p} | {len(overlap_dates_per_pair[p])} | "
            f"{len(mt5_records[p])} | {len(mt5_rejected[p])} |"
        )
        lines.append(
            f"| Databento | {p} | {len(overlap_dates_per_pair[p])} | "
            f"{len(db_records[p])} | {len(db_rejected[p])} |"
        )
    lines.append("")

    # Match summary.
    n_total = len(rows)
    n_close = sum(1 for r in rows if r["match"] == "matched_close")
    n_loose = sum(1 for r in rows if r["match"] == "matched_loose_same_kz_dir")
    n_rejected = sum(1 for r in rows if r["match"] == "rejected_only")
    n_none = sum(1 for r in rows if r["match"] == "none")

    lines.append("## Match summary (MT5 → Databento)")
    lines.append("")
    lines.append("| Match quality | Count | % |")
    lines.append("|---|---:|---:|")
    lines.append(
        f"| matched_close (≤±15 min) | {n_close} | "
        f"{100.0*n_close/n_total if n_total else 0:.1f}% |"
    )
    lines.append(
        f"| matched_loose (same date+kz+dir but >±15 min) | {n_loose} | "
        f"{100.0*n_loose/n_total if n_total else 0:.1f}% |"
    )
    lines.append(
        f"| rejected_only (DB pipeline rejected a same-direction candidate) | "
        f"{n_rejected} | {100.0*n_rejected/n_total if n_total else 0:.1f}% |"
    )
    lines.append(
        f"| none (no DB candidate at all) | {n_none} | "
        f"{100.0*n_none/n_total if n_total else 0:.1f}% |"
    )
    lines.append("")

    # Aggregate R comparison on close matches.
    close_rows = [r for r in rows if r["match"] == "matched_close"]
    if close_rows:
        mt5_mean_R_close = sum(r["mt5_R"] for r in close_rows) / len(close_rows)
        db_mean_R_close = sum(r["db_R"] for r in close_rows) / len(close_rows)
        n_outcome_agree = sum(
            1 for r in close_rows if (r["mt5_R"] > 0) == (r["db_R"] > 0)
        )
        lines.append("## Outcome agreement on `matched_close` pairs")
        lines.append("")
        lines.append(
            f"- N={len(close_rows)} pairs"
        )
        lines.append(
            f"- Mean R MT5 on these: {mt5_mean_R_close:+.3f}"
        )
        lines.append(
            f"- Mean R Databento on these: {db_mean_R_close:+.3f}"
        )
        lines.append(
            f"- Same-sign outcome agreement: {n_outcome_agree}/{len(close_rows)} "
            f"({100.0*n_outcome_agree/len(close_rows):.1f}%)"
        )
        lines.append("")
    else:
        lines.append("## Outcome agreement on `matched_close` pairs")
        lines.append("")
        lines.append("- No close matches — see per-setup table.")
        lines.append("")

    # Per-setup table.
    lines.append("## Per-setup mapping")
    lines.append("")
    lines.append(
        "| Date | Pair | Dir | Kz | MT5 ts | MT5 entry | MT5 swept | "
        "MT5 outcome | MT5 R | Match | Δ min | DB ts | DB entry | "
        "DB swept | DB outcome | DB R | Reject reasons |"
    )
    lines.append(
        "|---|---|---|---|---|---:|---:|---|---:|---|---:|---|---:|---:|---|---:|---|"
    )
    for r in sorted(rows, key=lambda x: (x["date"], x["pair"], x["mt5_ts"])):
        d = r["delta_min"]
        delta_str = f"{d:.0f}" if d is not None else "—"
        db_ts = r["db_ts"] or "—"
        db_entry = f"{r['db_entry']:.2f}" if r["db_entry"] is not None else "—"
        db_swept = f"{r['db_swept']:.2f}" if r["db_swept"] is not None else "—"
        db_outcome = r["db_outcome"] or "—"
        db_R = f"{r['db_R']:+.2f}" if r["db_R"] is not None else "—"
        rr = ", ".join(r["reject_reasons"]) if r["reject_reasons"] else "—"
        lines.append(
            f"| {r['date']} | {r['pair']} | {r['mt5_dir']} | {r['mt5_kz']} | "
            f"{r['mt5_ts']} | {r['mt5_entry']:.2f} | {r['mt5_swept']:.2f} | "
            f"{r['mt5_outcome']} | {r['mt5_R']:+.2f} | {r['match']} | "
            f"{delta_str} | {db_ts} | {db_entry} | {db_swept} | "
            f"{db_outcome} | {db_R} | {rr} |"
        )
    lines.append("")

    # Reject reason histogram.
    lines.append("## Reject-reason histogram (rows where match = rejected_only)")
    lines.append("")
    rr_counts: dict[str, int] = defaultdict(int)
    for r in rows:
        if r["match"] != "rejected_only":
            continue
        for rr in r["reject_reasons"]:
            rr_counts[rr] += 1
    if rr_counts:
        lines.append("| Reason | Count |")
        lines.append("|---|---:|")
        for rr, c in sorted(rr_counts.items(), key=lambda x: -x[1]):
            lines.append(f"| {rr} | {c} |")
    else:
        lines.append("- (none)")
    lines.append("")

    # Databento-only setups.
    lines.append(f"## Databento-only A/A+ setups (no MT5 counterpart) — N={len(db_only_rows)}")
    lines.append("")
    if db_only_rows:
        lines.append("| Date | Pair | Kz | Dir | Quality | DB ts | Outcome | R |")
        lines.append("|---|---|---|---|---|---|---|---:|")
        for r in sorted(db_only_rows, key=lambda x: (x["date"], x["pair"])):
            lines.append(
                f"| {r['date']} | {r['pair']} | {r['kz']} | {r['dir']} | "
                f"{r['quality']} | {r['ts']} | {r['outcome']} | {r['R']:+.2f} |"
            )
    else:
        lines.append("- (none)")
    lines.append("")

    # Initial hypothesis.
    lines.append("## Initial root-cause hypothesis")
    lines.append("")
    if n_total == 0:
        lines.append("- No MT5 setups in overlap window; cannot conclude.")
    else:
        pct_close = 100.0 * n_close / n_total
        if pct_close >= 70:
            lines.append(
                f"- {pct_close:.1f}% of MT5 setups have a close DB match — "
                "**detection-side divergence is small**. The mean R disagreement "
                "must come from **outcome simulation on different M5 paths** "
                "(price path between entry and SL/TP differs across sources). "
                "→ Phase 2 (structural candle comparison) recommended."
            )
        elif pct_close + 100.0 * n_loose / n_total >= 70:
            lines.append(
                f"- Only {pct_close:.1f}% close matches, but "
                f"{100.0 * (n_close + n_loose) / n_total:.1f}% same-direction "
                "matches in same killzone — detection sees roughly the same "
                "story but at different sub-timestamps. Likely cause: "
                "different sweep/MSS timing due to different M5 wick paths."
            )
        else:
            lines.append(
                f"- Only {pct_close + 100.0*n_loose/n_total:.1f}% same-direction "
                "matches — **the two sources disagree on whether a setup exists**. "
                "Likely cause: structural pipeline failure on Databento "
                "(bias / sweep / MSS detection)."
            )
        if rr_counts:
            top_reason = max(rr_counts.items(), key=lambda x: x[1])
            lines.append(
                f"- Dominant rejection reason on Databento for MT5-detected "
                f"setups: **{top_reason[0]}** ({top_reason[1]} cases). "
                "This pinpoints the failing pipeline stage."
            )
        if len(db_only_rows) > 2 * n_close:
            lines.append(
                f"- Databento has {len(db_only_rows)} A/A+ setups with no MT5 "
                f"counterpart vs {n_close} matched. Detection on Databento "
                "fires a different *population* of setups, not the same setups "
                "with different outcomes."
            )

    # Price-level offset diagnostic.
    if close_rows:
        offset_samples = []
        for r in close_rows:
            offset_samples.append((r["pair"], r["mt5_entry"], r["db_entry"]))
        lines.append("")
        lines.append("### Price-level offset on matched pairs")
        lines.append("")
        lines.append("| Pair | MT5 entry | DB entry | Δ (DB - MT5) | Δ % |")
        lines.append("|---|---:|---:|---:|---:|")
        for p, mt5_e, db_e in offset_samples:
            delta = db_e - mt5_e
            pct = (delta / mt5_e * 100.0) if mt5_e else 0.0
            lines.append(f"| {p} | {mt5_e:.2f} | {db_e:.2f} | {delta:+.2f} | {pct:+.2f}% |")
    lines.append("")

    # Write report.
    _RUNS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _RUNS_DIR / f"{_TIMESTAMP}_mt5_vs_databento_phase1.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")

    # ---- stdout summary ----
    print()
    print("=== Phase 1 summary ===")
    print(f"  MT5 A/A+ setups in overlap : {n_total}")
    print(f"  matched close (≤±15 min)   : {n_close}")
    print(f"  matched loose (same kz/dir): {n_loose}")
    print(f"  rejected on DB only        : {n_rejected}")
    print(f"  no DB candidate at all     : {n_none}")
    print(f"  DB-only A/A+ setups        : {len(db_only_rows)}")
    if close_rows:
        print(
            f"  Mean R on close pairs      : MT5={mt5_mean_R_close:+.3f}, "
            f"DB={db_mean_R_close:+.3f}, agreement={n_outcome_agree}/{len(close_rows)}"
        )
    print(f"  Report                     : {out_path.relative_to(_REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
