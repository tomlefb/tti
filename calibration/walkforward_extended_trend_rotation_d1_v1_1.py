"""20-year walk-forward — trend_rotation_d1 v1.1, cell 126/5/3.

Loads the Yahoo-Finance D1 fixtures
(``tests/fixtures/historical_extended/yahoo/<ASSET>_D1.parquet``)
for the 15-asset v1.1 universe, runs the pipeline once on
2006-01-01 → 2026-04-30 (≈ 20.3 y, with 2005 excluded as
6-month-momentum warmup), buckets the exits into 2-year sub-
windows, and reports stationnarité metrics.

This is a stationnarité diagnostic on the gate-4-selected cell;
no spec change. Pre-spec verdict bands (stationnarité figées
before analysis) are encoded below.

Outputs
-------
- ``calibration/runs/walkforward_extended_trend_rotation_d1_v1_1_<TS>/``
  - ``inventory.md`` — per-asset coverage on the loaded panel
  - ``walkforward_results.json`` — per sub-window metrics
  - ``analysis.md`` — full report with verdict
"""

from __future__ import annotations

import json
import math
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.strategies.trend_rotation_d1 import (  # noqa: E402
    StrategyParams,
    StrategyState,
    TradeExit,
    build_rebalance_candidates,
)

YAHOO_DIR = REPO_ROOT / "tests" / "fixtures" / "historical_extended" / "yahoo"
RUNS_DIR = REPO_ROOT / "calibration" / "runs"

UNIVERSE = (
    "NDX100", "SPX500", "US30", "US2000", "GER30", "UK100", "JP225",
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD",
    "XAUUSD", "XAGUSD",
    "USOUSD",
    "BTCUSD",
)

# Cell selected at gate 4 v1.1
CELL = {"momentum": 126, "K": 5, "rebalance": 3}

# 20.3-year window (2005 excluded as warmup)
START = pd.Timestamp("2006-01-01", tz="UTC")
END = pd.Timestamp("2026-04-30", tz="UTC")

# 11 sub-windows per spec — 10 of 2y + 1 partial (2026-Q1+)
SUB_WINDOWS: list[tuple[str, str, str]] = [
    ("2006-2007", "2006-01-01", "2007-12-31"),
    ("2008-2009 (GFC)", "2008-01-01", "2009-12-31"),
    ("2010-2011", "2010-01-01", "2011-12-31"),
    ("2012-2013", "2012-01-01", "2013-12-31"),
    ("2014-2015", "2014-01-01", "2015-12-31"),
    ("2016-2017", "2016-01-01", "2017-12-31"),
    ("2018-2019", "2018-01-01", "2019-12-31"),
    ("2020-2021 (COVID)", "2020-01-01", "2021-12-31"),
    ("2022-2023 (Fed hike)", "2022-01-01", "2023-12-31"),
    ("2024-2025", "2024-01-01", "2025-12-31"),
    ("2026-Q1+", "2026-01-01", "2026-04-30"),
]


# ---------------------------------------------------------------------------
# Panel loading
# ---------------------------------------------------------------------------


def load_panel_yahoo() -> dict[str, pd.DataFrame]:
    panel: dict[str, pd.DataFrame] = {}
    for asset in UNIVERSE:
        p = YAHOO_DIR / f"{asset}_D1.parquet"
        if not p.exists():
            raise FileNotFoundError(f"Missing fixture: {p}")
        df = pd.read_parquet(p)
        if "time" in df.columns:
            df["time"] = pd.to_datetime(df["time"], utc=True)
            df = df.set_index("time")
        df.index = df.index.normalize()
        df = df[~df.index.duplicated(keep="first")].sort_index()
        # Keep only OHLC + volume
        keep = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
        panel[asset] = df[keep]
    return panel


def cycle_dates(panel: dict[str, pd.DataFrame], start: pd.Timestamp,
                end: pd.Timestamp) -> list[pd.Timestamp]:
    """Union of trading dates across the panel within [start, end]."""
    all_dates: set[pd.Timestamp] = set()
    for df in panel.values():
        all_dates |= set(df.index)
    return sorted(d for d in all_dates if start <= d <= end)


def run_streaming(panel: dict[str, pd.DataFrame], params: StrategyParams,
                  dates: list[pd.Timestamp]) -> tuple[list[TradeExit], StrategyState]:
    state = StrategyState()
    exits: list[TradeExit] = []
    for now in dates:
        new_exits = build_rebalance_candidates(
            panel, params, state, now_utc=now.to_pydatetime()
        )
        exits.extend(new_exits)
    return exits, state


# ---------------------------------------------------------------------------
# Bootstrap CI (95 %) on mean R
# ---------------------------------------------------------------------------


def bootstrap_ci(rs: list[float], n_iter: int = 2000,
                 seed: int = 12345) -> tuple[float, float]:
    import random
    if not rs:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(rs)
    means = []
    for _ in range(n_iter):
        sample = [rs[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    lo = means[int(0.025 * n_iter)]
    hi = means[int(0.975 * n_iter)]
    return lo, hi


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------


def inventory(panel: dict[str, pd.DataFrame]) -> list[dict]:
    rows = []
    for asset in UNIVERSE:
        df = panel[asset]
        # Coverage over the 2006-2026 window
        within = df.loc[(df.index >= START) & (df.index <= END)]
        first = within.index.min() if len(within) else None
        last = within.index.max() if len(within) else None
        years = (last - first).days / 365.25 if first is not None else 0.0
        diffs = within.index.to_series().diff().dt.days
        gaps_10 = int((diffs > 10).sum())
        rows.append({
            "asset": asset,
            "first_in_window": first.date().isoformat() if first is not None else None,
            "last_in_window": last.date().isoformat() if last is not None else None,
            "years_in_window": round(years, 2),
            "n_bars": len(within),
            "gaps_gt_10d": gaps_10,
        })
    return rows


# ---------------------------------------------------------------------------
# Walk-forward bucketing
# ---------------------------------------------------------------------------


def bucket_by_window(exits: list[TradeExit]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for label, s, e in SUB_WINDOWS:
        ws = pd.Timestamp(s, tz="UTC")
        we = pd.Timestamp(e, tz="UTC")
        bucket = [
            ex for ex in exits
            if ws <= pd.Timestamp(ex.exit_timestamp_utc).tz_convert("UTC") <= we
        ]
        n = len(bucket)
        if n == 0:
            out[label] = {
                "label": label, "window": f"{s} → {e}",
                "n": 0, "mean_r": None, "win_rate": None,
                "ci_low": None, "ci_high": None,
                "total_r": 0.0, "n_months": 0.0,
                "setups_per_month": 0.0, "proj_annual": None,
            }
            continue
        rs = [ex.return_r for ex in bucket]
        mean_r = sum(rs) / n
        win = sum(1 for r in rs if r > 0) / n
        n_months = (we - ws).days / 30.4375
        spm = n / n_months if n_months > 0 else 0.0
        proj = mean_r * spm * 12.0
        ci_lo, ci_hi = bootstrap_ci(rs) if n >= 30 else (None, None)
        out[label] = {
            "label": label,
            "window": f"{s} → {e}",
            "n": n,
            "mean_r": mean_r,
            "win_rate": win,
            "ci_low": ci_lo,
            "ci_high": ci_hi,
            "total_r": sum(rs),
            "n_months": round(n_months, 2),
            "setups_per_month": spm,
            "proj_annual": proj,
        }
    return out


# ---------------------------------------------------------------------------
# Top-3 sanity per sub-window
# ---------------------------------------------------------------------------


def top_carriers_per_window(exits: list[TradeExit]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for label, s, e in SUB_WINDOWS:
        ws = pd.Timestamp(s, tz="UTC")
        we = pd.Timestamp(e, tz="UTC")
        bucket = [
            ex for ex in exits
            if ws <= pd.Timestamp(ex.exit_timestamp_utc).tz_convert("UTC") <= we
        ]
        if not bucket:
            out[label] = []
            continue
        by_asset: dict[str, list[float]] = {}
        for ex in bucket:
            by_asset.setdefault(ex.asset, []).append(ex.return_r)
        rows = []
        for asset, rs in by_asset.items():
            rows.append({
                "asset": asset,
                "n": len(rs),
                "mean_r": sum(rs) / len(rs),
                "sum_r": sum(rs),
            })
        rows.sort(key=lambda r: -abs(r["sum_r"]))
        out[label] = rows[:3]
    return out


# ---------------------------------------------------------------------------
# Pooled stats + verdict
# ---------------------------------------------------------------------------


def stationnarité_verdict(buckets: dict[str, dict],
                          pooled_mean_r: float,
                          n_total: int) -> tuple[str, dict]:
    """Pre-specified verdict thresholds (figées before analysis)."""
    n_pos = sum(
        1 for b in buckets.values()
        if b["mean_r"] is not None and b["mean_r"] > 0
    )
    n_pos_03 = sum(
        1 for b in buckets.values()
        if b["mean_r"] is not None and b["mean_r"] > 0.3
    )
    n_with_data = sum(1 for b in buckets.values() if b["n"] > 0)

    abs_means = [
        abs(b["mean_r"])
        for b in buckets.values()
        if b["mean_r"] is not None
    ]
    abs_means.sort()
    median_abs = abs_means[len(abs_means) // 2] if abs_means else 0.0
    max_abs = max(abs_means) if abs_means else 0.0
    variance_ratio = max_abs / median_abs if median_abs > 0 else math.inf

    stats = {
        "n_pos_above_0R": n_pos,
        "n_pos_above_0_3R": n_pos_03,
        "n_with_data": n_with_data,
        "pooled_mean_r": pooled_mean_r,
        "n_total": n_total,
        "max_abs_mean_r_over_median": variance_ratio,
    }

    # PROMOTE thresholds
    if (
        n_pos >= 7
        and n_pos_03 >= 4
        and pooled_mean_r > 0.3
        and variance_ratio < 5.0
    ):
        return "PROMOTE", stats
    # REVIEW
    if (
        n_pos in range(5, 7)
        and n_pos_03 >= 2
        and pooled_mean_r > 0.1
    ):
        return "REVIEW", stats
    # ARCHIVE
    return "ARCHIVE", stats


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def write_inventory_md(out_dir: Path, inv_rows: list[dict]) -> Path:
    p = out_dir / "inventory.md"
    L = []
    L.append("# Walk-forward extended panel — Yahoo inventory")
    L.append("")
    L.append(f"Window: {START.date()} → {END.date()} (target ≈ 20.3 y).")
    L.append("")
    L.append("| Asset | First | Last | Years | n_bars | Gaps > 10 d |")
    L.append("|---|---|---|---:|---:|---:|")
    for r in inv_rows:
        L.append(
            f"| {r['asset']} | {r['first_in_window']} | {r['last_in_window']} | "
            f"{r['years_in_window']} | {r['n_bars']} | {r['gaps_gt_10d']} |"
        )
    L.append("")
    p.write_text("\n".join(L) + "\n")
    return p


def write_analysis_md(*, out_dir: Path, inv_rows: list[dict],
                      buckets: dict[str, dict],
                      top3_per_window: dict[str, list[dict]],
                      pooled_mean_r: float, n_total: int,
                      verdict: str, verdict_stats: dict,
                      wallclock_s: float) -> Path:
    p = out_dir / "analysis.md"
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    L: list[str] = []
    L.append(f"# Walk-forward 20y — trend_rotation_d1 v1.1 cell 126/5/3 ({ts})")
    L.append("")
    L.append(
        "Source: Yahoo Finance D1 fixtures "
        "(`tests/fixtures/historical_extended/yahoo/`). "
        "Cell: gate-4-selected 126/5/3. "
        "Window: 2006-01-01 → 2026-04-30 (≈ 20.3 y, 2005 excluded "
        "as 6-month-momentum warmup)."
    )
    L.append("")
    L.append(f"Wallclock: {wallclock_s:.1f} s.")
    L.append("")

    L.append("## Verdict pre-specified (figé avant analyse)")
    L.append("")
    L.append("- **PROMOTE** (edge stable): ≥ 7/11 sub-windows with mean_r > 0, ≥ 4/11 with mean_r > +0.3 R, pooled mean_r > +0.3 R, variance ratio max/median < 5×.")
    L.append("- **REVIEW** (edge cyclique): 5-6/11 positives, ≥ 2/11 above +0.3 R, pooled > +0.1 R.")
    L.append("- **ARCHIVE** (edge artefactuel): ≤ 4/11 positives, edge concentrated in 1-2 recent buckets, or pooled near zero/negative.")
    L.append("")

    overall_pos_emoji = {"PROMOTE": "✅", "REVIEW": "⚠️", "ARCHIVE": "❌"}[verdict]
    L.append(f"- **Verdict mesuré**: {overall_pos_emoji} **{verdict}**")
    L.append(f"- Pooled mean_r: **{pooled_mean_r:+.4f} R** (n={n_total} closed trades)")
    L.append(f"- Sub-windows with mean_r > 0: **{verdict_stats['n_pos_above_0R']} / 11**")
    L.append(f"- Sub-windows with mean_r > +0.3 R: **{verdict_stats['n_pos_above_0_3R']} / 11**")
    L.append(f"- Variance ratio (max |mean_r| / median |mean_r|): **{verdict_stats['max_abs_mean_r_over_median']:.2f}×**")
    L.append("")

    # Per-window table
    L.append("## 1. Walk-forward 11 sub-windows")
    L.append("")
    L.append("| Sub-window | window | n | mean_r | win | trades/mo | proj % | CI low | CI high | total R |")
    L.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for label, _, _ in SUB_WINDOWS:
        b = buckets[label]
        if b["n"] == 0:
            L.append(f"| {label} | {b['window']} | 0 | n/a | n/a | n/a | n/a | n/a | n/a | 0 |")
            continue
        ci_lo = f"{b['ci_low']:+.3f}" if b["ci_low"] is not None else "n/a"
        ci_hi = f"{b['ci_high']:+.3f}" if b["ci_high"] is not None else "n/a"
        L.append(
            f"| {label} | {b['window']} | {b['n']} | "
            f"{b['mean_r']:+.4f} | {b['win_rate']:.1%} | "
            f"{b['setups_per_month']:.2f} | "
            f"{b['proj_annual']:+.1f}% | {ci_lo} | {ci_hi} | "
            f"{b['total_r']:+.2f} R |"
        )
    L.append("")

    # Top-3 carriers per window
    L.append("## 2. Top-3 carriers per sub-window")
    L.append("")
    L.append(
        "Identifies the assets contributing the most |total R| in each sub-"
        "window. Comparison across décennies tells whether the strategy "
        "captures different régimes via different leaders, or always rides "
        "the same names."
    )
    L.append("")
    L.append("| Sub-window | Top-3 carriers (asset / n / sum_r) |")
    L.append("|---|---|")
    for label, _, _ in SUB_WINDOWS:
        rows = top3_per_window.get(label, [])
        if not rows:
            L.append(f"| {label} | — |")
            continue
        cells = [
            f"{r['asset']} (n={r['n']}, {r['sum_r']:+.1f} R)"
            for r in rows
        ]
        L.append(f"| {label} | {' / '.join(cells)} |")
    L.append("")

    # Inventory recap
    L.append("## 3. Panel inventory (2006-2026 window)")
    L.append("")
    L.append("| Asset | First | Last | Years | n_bars | Gaps > 10 d |")
    L.append("|---|---|---|---:|---:|---:|")
    for r in inv_rows:
        L.append(
            f"| {r['asset']} | {r['first_in_window']} | {r['last_in_window']} | "
            f"{r['years_in_window']} | {r['n_bars']} | {r['gaps_gt_10d']} |"
        )
    L.append("")

    # Verdict-driven recommendation
    L.append("## 4. Recommendation")
    L.append("")
    if verdict == "PROMOTE":
        L.append(
            "Stationnarité confirmée: l'edge est présent et défendable "
            "sur 20 ans de data multi-source. Le drift +1.361 R observé "
            "au gate 4 v1.1 holdout est un effet régime-fenêtre mais "
            "l'edge sous-jacent est cohérent. Suggested next: gate 5 "
            "Databento partial cross-check + opérateur path-decision sur "
            "magnitude (corrigée H4 risk-parity) pour Sprint 7 deployment."
        )
    elif verdict == "REVIEW":
        L.append(
            f"Stationnarité partielle: {verdict_stats['n_pos_above_0R']}/11 "
            "sub-windows positives, edge cyclique avec dépendance régime. "
            "Pooled mean_r positif mais variance inter-décennies notable. "
            "Suggested next: opérateur discussion — soit accepter l'edge "
            "comme cyclique-mais-réel et déployer avec position sizing "
            "réduit, soit archiver sur la base de la non-stationnarité."
        )
    else:
        L.append(
            "Stationnarité non confirmée: l'edge mesuré sur 6.4 y "
            "(2019-2026) ne se reproduit pas sur 20 ans. La cellule 126/5/3 "
            "est probablement régime-fit sur la fenêtre récente. Per spec "
            "v1.1 footer, la classe HTF cross-sectional momentum multi-asset "
            "est considérée structurellement non-viable pour le contexte "
            "opérateur."
        )
    L.append("")
    L.append(
        "Methodological note: this walk-forward uses Yahoo Finance D1 OHLC "
        "as the data source. Yahoo's coverage on FX (start 2003-12) and "
        "BTC (start 2014-09) limits early sub-windows on those assets — "
        "the pipeline naturally drops them via the insufficient-history "
        "filter §2.6. The signed-PnL distribution may therefore differ "
        "subtly from a hypothetical 20-y back-adjusted Dukascopy panel; "
        "the qualitative direction (pos vs neg sub-windows, magnitude "
        "ordering) is the load-bearing measurement."
    )
    L.append("")

    p.write_text("\n".join(L) + "\n")
    return p


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    t0 = time.perf_counter()
    print(f"Loading panel from {YAHOO_DIR}...", flush=True)
    panel = load_panel_yahoo()
    inv_rows = inventory(panel)

    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    out_dir = RUNS_DIR / f"walkforward_extended_trend_rotation_d1_v1_1_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    write_inventory_md(out_dir, inv_rows)

    print(f"\nRunning cell {CELL} on {START.date()} → {END.date()}...", flush=True)
    params = StrategyParams(
        universe=UNIVERSE,
        momentum_lookback_days=CELL["momentum"],
        K=CELL["K"],
        rebalance_frequency_days=CELL["rebalance"],
        risk_per_trade_pct=1.0,
        atr_period=20,
        atr_explosive_threshold=5.0,
        atr_regime_lookback=90,
    )
    dates = cycle_dates(panel, START, END)
    print(f"  {len(dates)} cycle dates", flush=True)
    exits, _ = run_streaming(panel, params, dates)
    print(f"  {len(exits)} closed trades over the 20-y window", flush=True)

    pooled_mean_r = (
        sum(e.return_r for e in exits) / len(exits) if exits else 0.0
    )
    print(f"  pooled mean_r = {pooled_mean_r:+.4f}", flush=True)

    print("\nBucketing into sub-windows...", flush=True)
    buckets = bucket_by_window(exits)
    for label, _, _ in SUB_WINDOWS:
        b = buckets[label]
        if b["n"] == 0:
            print(f"  {label:<22}: n=0", flush=True)
            continue
        print(
            f"  {label:<22}: n={b['n']:>3}, mean_r={b['mean_r']:+.4f}, "
            f"win={b['win_rate']:.1%}, total_r={b['total_r']:+.2f}",
            flush=True,
        )

    top3 = top_carriers_per_window(exits)

    verdict, vstats = stationnarité_verdict(buckets, pooled_mean_r, len(exits))
    print(f"\nVerdict: {verdict}", flush=True)

    # Save JSON
    json_data = {
        "cell": CELL,
        "window": [str(START.date()), str(END.date())],
        "n_total": len(exits),
        "pooled_mean_r": pooled_mean_r,
        "verdict": verdict,
        "verdict_stats": vstats,
        "buckets": buckets,
        "top3_per_window": top3,
        "inventory": inv_rows,
    }
    (out_dir / "walkforward_results.json").write_text(
        json.dumps(json_data, indent=2, default=str)
    )

    wallclock = time.perf_counter() - t0
    write_analysis_md(
        out_dir=out_dir,
        inv_rows=inv_rows,
        buckets=buckets,
        top3_per_window=top3,
        pooled_mean_r=pooled_mean_r,
        n_total=len(exits),
        verdict=verdict,
        verdict_stats=vstats,
        wallclock_s=wallclock,
    )

    print(f"\nReport: {out_dir}/analysis.md")
    print(f"Total wallclock: {wallclock:.1f}s")
    return 0 if verdict == "PROMOTE" else (1 if verdict == "REVIEW" else 2)


if __name__ == "__main__":
    sys.exit(main())
