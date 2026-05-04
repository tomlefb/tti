"""Walk-forward 20y on cell 126/5/3, universe RESTRICTED to 14
assets (BTCUSD excluded).

Mini-mesure to discriminate:
- "real CSM edge distributed across non-crypto assets" → pooled
  excl-BTC stays > +0.5 R → PROMOTE argument strong.
- "edge dominated by BTC carrier" → pooled excl-BTC collapses
  to < +0.3 R → ARCHIVE or re-spec excluding crypto.

Compares with the 15-asset baseline at commit a30e516.

Run
---
    python -m calibration.walkforward_extended_excl_btc
"""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from calibration.walkforward_extended_trend_rotation_d1_v1_1 import (  # noqa: E402
    CELL,
    END,
    START,
    SUB_WINDOWS,
    bootstrap_ci,
    bucket_by_window,
    cycle_dates,
    load_panel_yahoo,
    run_streaming,
    top_carriers_per_window,
)
from src.strategies.trend_rotation_d1 import StrategyParams  # noqa: E402

# 14-asset universe (excl BTCUSD)
UNIVERSE_14 = (
    "NDX100", "SPX500", "US30", "US2000", "GER30", "UK100", "JP225",
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD",
    "XAUUSD", "XAGUSD",
    "USOUSD",
)

RUNS_DIR = REPO_ROOT / "calibration" / "runs"


# Baseline 15-asset numbers from prior commit a30e516 (walk-forward
# extended). Hard-coded for the comparative report — the JSON of the
# prior run is gitignored.
BASELINE_15 = {
    "n_total": 1000,
    "pooled_mean_r": 1.7112,
    "n_pos_above_0R": 11,
    "n_pos_above_0_3R": 10,
    "buckets": {
        "2006-2007": {"n": 83, "mean_r": 0.6611, "win": 0.470},
        "2008-2009 (GFC)": {"n": 84, "mean_r": 0.5409, "win": 0.560},
        "2010-2011": {"n": 116, "mean_r": 0.4316, "win": 0.509},
        "2012-2013": {"n": 81, "mean_r": 1.2572, "win": 0.605},
        "2014-2015": {"n": 95, "mean_r": 0.7713, "win": 0.421},
        "2016-2017": {"n": 103, "mean_r": 8.4811, "win": 0.573},
        "2018-2019": {"n": 120, "mean_r": 0.3525, "win": 0.475},
        "2020-2021 (COVID)": {"n": 116, "mean_r": 1.6002, "win": 0.491},
        "2022-2023 (Fed hike)": {"n": 96, "mean_r": 0.0989, "win": 0.427},
        "2024-2025": {"n": 95, "mean_r": 2.0177, "win": 0.579},
        "2026-Q1+": {"n": 11, "mean_r": 7.5541, "win": 0.545},
    },
    "pooled_excl_2016_2017": 0.93,  # from FINAL report
}


def write_report(out_path: Path, *, buckets: dict, top3: dict,
                 pooled_mean_r: float, n_total: int,
                 wallclock_s: float) -> Path:
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    L: list[str] = []
    L.append(f"# Walk-forward 20y excl-BTC — trend_rotation_d1 v1.1 cell 126/5/3 ({ts})")
    L.append("")
    L.append(
        "Mini-mesure: cellule 126/5/3 re-runnée sur univers à 14 actifs "
        "(BTCUSD exclu), même fenêtre 2006-01-01 → 2026-04-30, mêmes "
        "11 sub-windows que le run 15-actifs (commit `a30e516`)."
    )
    L.append("")
    L.append(f"Wallclock: {wallclock_s:.1f} s.")
    L.append("")

    # Verdict pre-spec
    if pooled_mean_r > 0.5:
        verdict = "EDGE STRUCTUREL CSM (PROMOTE-supporting)"
        emoji = "✅"
    elif pooled_mean_r >= 0.3:
        verdict = "EDGE BORDERLINE (REVIEW)"
        emoji = "⚠️"
    else:
        verdict = "EDGE BTC-DEPENDENT (ARCHIVE / re-spec)"
        emoji = "❌"

    L.append(f"## Verdict: {emoji} **{verdict}**")
    L.append("")
    L.append(f"- Pooled mean_r 14-actifs (excl BTC): **{pooled_mean_r:+.4f} R** (n={n_total})")
    L.append(f"- Baseline 15-actifs: pooled = {BASELINE_15['pooled_mean_r']:+.4f} R (n={BASELINE_15['n_total']})")
    L.append(f"- Baseline 15-actifs excl 2016-2017 outlier: {BASELINE_15['pooled_excl_2016_2017']:+.4f} R")
    L.append("")

    L.append("## 1. Comparative tableau 11 sub-windows")
    L.append("")
    L.append("| Sub-window | n_15 | mean_r_15 | n_14 | mean_r_14 | Δ mean_r | win_14 |")
    L.append("|---|---:|---:|---:|---:|---:|---:|")
    n_pos_14 = 0
    n_pos_03_14 = 0
    for label, _, _ in SUB_WINDOWS:
        b15 = BASELINE_15["buckets"].get(label, {})
        b14 = buckets.get(label, {})
        if b14.get("n", 0) == 0:
            L.append(
                f"| {label} | {b15.get('n', 0)} | "
                f"{b15.get('mean_r', 0):+.4f} | 0 | n/a | n/a | n/a |"
            )
            continue
        delta = b14["mean_r"] - b15.get("mean_r", 0)
        if b14["mean_r"] > 0:
            n_pos_14 += 1
        if b14["mean_r"] > 0.3:
            n_pos_03_14 += 1
        L.append(
            f"| {label} | {b15.get('n', 0)} | {b15.get('mean_r', 0):+.4f} | "
            f"{b14['n']} | {b14['mean_r']:+.4f} | {delta:+.4f} | "
            f"{b14['win_rate']:.1%} |"
        )
    L.append("")
    L.append(f"- Sub-windows mean_r > 0 (14-actifs): **{n_pos_14} / 11** (baseline 15-actifs: {BASELINE_15['n_pos_above_0R']} / 11)")
    L.append(f"- Sub-windows mean_r > +0.3 R (14-actifs): **{n_pos_03_14} / 11** (baseline 15-actifs: {BASELINE_15['n_pos_above_0_3R']} / 11)")
    L.append("")

    # Top-3 carriers per sub-window
    L.append("## 2. Top-3 carriers per sub-window (14-actifs)")
    L.append("")
    L.append(
        "Avec BTC retiré, qui porte l'edge maintenant? Si la rotation "
        "des leaders multi-décennie persiste, l'edge CSM est structurel. "
        "Si tout s'effondre, l'edge était portfolio-wide-by-BTC-and-noise."
    )
    L.append("")
    L.append("| Sub-window | Top-3 (asset / n / sum_r) |")
    L.append("|---|---|")
    for label, _, _ in SUB_WINDOWS:
        rows = top3.get(label, [])
        if not rows:
            L.append(f"| {label} | — |")
            continue
        cells = [f"{r['asset']} (n={r['n']}, {r['sum_r']:+.1f} R)" for r in rows]
        L.append(f"| {label} | {' / '.join(cells)} |")
    L.append("")

    # Quantitative deltas
    L.append("## 3. Comparaison quantitative")
    L.append("")
    L.append("| Métrique | Run 15-actifs | Run 14-actifs (excl BTC) | Δ |")
    L.append("|---|---:|---:|---:|")
    L.append(f"| n_total | {BASELINE_15['n_total']} | {n_total} | {n_total - BASELINE_15['n_total']:+d} |")
    L.append(f"| Pooled mean_r | {BASELINE_15['pooled_mean_r']:+.4f} | {pooled_mean_r:+.4f} | {pooled_mean_r - BASELINE_15['pooled_mean_r']:+.4f} |")
    L.append(f"| Sub-windows pos > 0 | {BASELINE_15['n_pos_above_0R']}/11 | {n_pos_14}/11 | {n_pos_14 - BASELINE_15['n_pos_above_0R']:+d} |")
    L.append(f"| Sub-windows pos > +0.3R | {BASELINE_15['n_pos_above_0_3R']}/11 | {n_pos_03_14}/11 | {n_pos_03_14 - BASELINE_15['n_pos_above_0_3R']:+d} |")
    L.append("")

    # Verdict & next steps
    L.append("## 4. Recommendation")
    L.append("")
    if pooled_mean_r > 0.5:
        L.append(
            "Edge CSM **structurel** confirmé hors-BTC: pooled mean_r "
            f"= {pooled_mean_r:+.4f} R sur {n_total} trades 14-actifs, "
            "au-dessus du seuil PROMOTE +0.5 R. La stratégie capture "
            "des régimes trending sur métaux + équities + FX, pas "
            "uniquement le BTC bull. **Argument fort pour PROMOTE gate 5** "
            "(option A du FINAL walk-forward report)."
        )
    elif pooled_mean_r >= 0.3:
        L.append(
            "Edge **borderline** hors-BTC: pooled mean_r = "
            f"{pooled_mean_r:+.4f} R sur {n_total} trades 14-actifs, "
            "dans la bande [+0.3, +0.5] R. La stratégie produit un edge "
            "mesurable sans BTC mais à magnitude modeste. **REVIEW** — "
            "opérateur discussion sur viabilité économique post-corrections."
        )
    else:
        L.append(
            "Edge **BTC-dependent**: pooled mean_r = "
            f"{pooled_mean_r:+.4f} R hors-BTC, sous le seuil +0.3 R. "
            "La stratégie 126/5/3 était essentiellement un carrier BTC. "
            "PROMOTE gate 5 deviendrait un pari sur BTC plus que sur "
            "CSM générique multi-asset. **ARCHIVE** ou **re-spec v1.2** "
            "avec exclusion explicite de la crypto."
        )
    L.append("")

    out_path.write_text("\n".join(L) + "\n")
    return out_path


def main() -> int:
    t0 = time.perf_counter()
    print("Loading 14-asset panel (excl BTCUSD)...", flush=True)
    full_panel = load_panel_yahoo()
    panel = {a: full_panel[a] for a in UNIVERSE_14}
    print(f"  {len(panel)} assets", flush=True)

    params = StrategyParams(
        universe=UNIVERSE_14,
        momentum_lookback_days=CELL["momentum"],
        K=CELL["K"],
        rebalance_frequency_days=CELL["rebalance"],
        risk_per_trade_pct=1.0,
        atr_period=20,
        atr_explosive_threshold=5.0,
        atr_regime_lookback=90,
    )

    print(f"Running cell {CELL} on 14-actifs, {START.date()} → {END.date()}...", flush=True)
    dates = cycle_dates(panel, START, END)
    print(f"  {len(dates)} cycle dates", flush=True)
    exits, _ = run_streaming(panel, params, dates)
    print(f"  {len(exits)} closed trades", flush=True)

    pooled_mean_r = sum(e.return_r for e in exits) / len(exits) if exits else 0.0
    print(f"  pooled mean_r = {pooled_mean_r:+.4f}", flush=True)

    buckets = bucket_by_window(exits)
    top3 = top_carriers_per_window(exits)

    for label, _, _ in SUB_WINDOWS:
        b = buckets[label]
        if b["n"] == 0:
            print(f"  {label:<22}: n=0", flush=True)
            continue
        baseline = BASELINE_15["buckets"].get(label, {})
        delta = b["mean_r"] - baseline.get("mean_r", 0)
        print(
            f"  {label:<22}: n={b['n']:>3}, mean_r={b['mean_r']:+.4f} "
            f"(baseline {baseline.get('mean_r', 0):+.3f}, Δ {delta:+.4f})",
            flush=True,
        )

    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    out_path = RUNS_DIR / f"walkforward_excl_btc_{ts}.md"
    wallclock = time.perf_counter() - t0
    write_report(
        out_path,
        buckets=buckets,
        top3=top3,
        pooled_mean_r=pooled_mean_r,
        n_total=len(exits),
        wallclock_s=wallclock,
    )

    # Also dump JSON
    json_path = RUNS_DIR / f"walkforward_excl_btc_{ts}.json"
    json_path.write_text(json.dumps({
        "universe": list(UNIVERSE_14),
        "cell": CELL,
        "n_total": len(exits),
        "pooled_mean_r": pooled_mean_r,
        "buckets": buckets,
        "top3": top3,
    }, indent=2, default=str))

    print(f"\nReport: {out_path}")
    print(f"Total wallclock: {wallclock:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
