"""Grid search for swing detector parameters — H4 and H1 evaluated separately.

Sweeps ``(SWING_LOOKBACK, MIN_SWING_AMPLITUDE_ATR_MULT)`` against the same
operator-marked annotations that ``run_swing_calibration.py`` consumes,
and reports overall and per-regime F1 for each timeframe.

Output:

- Markdown table to stdout.
- ``calibration/runs/{TIMESTAMP}_grid_search.md`` — same content.

Does NOT mutate ``config/settings.py`` or ``config/settings.py.example``;
the intent is to inform an operator decision, not commit a tuning.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from calibration.run_swing_calibration import (  # noqa: E402
    _REFERENCE_DIR,
    _RUNS_DIR,
    _detected_swings_in_window,
    _discover_annotations,
    _load_fixture,
    _match,
)

_LOOKBACKS = (2, 3)
_ATR_MULTS = (0.5, 0.8, 1.0, 1.3, 1.5, 2.0)
_REGIMES = (
    "dead",
    "range",
    "trending_bearish",
    "trending_bullish",
    "volatile_news",
)
_TIMEFRAMES = ("H4", "H1")
_ATR_PERIOD = 14


def _f1(p: float, r: float) -> float:
    return 2 * p * r / (p + r) if (p + r) > 0 else 0.0


def _evaluate(
    annotations: list,
    lookback: int,
    atr_mult: float,
    timeframe: str,
) -> dict[str, float | int | dict[str, float]]:
    """Run detection + matching for one (lookback, mult, tf) combo.

    Returns aggregated metrics + per-regime F1.
    """
    tp_tot = fp_tot = fn_tot = 0
    per_regime_counts: dict[str, dict[str, int]] = {
        r: {"tp": 0, "fp": 0, "fn": 0} for r in _REGIMES
    }

    for ann in annotations:
        if ann.timeframe != timeframe:
            continue
        df = _load_fixture(ann.pair, ann.timeframe)
        if df is None:
            continue
        detected = _detected_swings_in_window(
            df,
            ann.window_start,
            ann.window_end,
            lookback=lookback,
            atr_mult=atr_mult,
            atr_period=_ATR_PERIOD,
        )
        tp, fp, fn, _, _ = _match(detected, ann.swings, ann.timeframe)
        tp_tot += tp
        fp_tot += fp
        fn_tot += fn
        if ann.regime in per_regime_counts:
            per_regime_counts[ann.regime]["tp"] += tp
            per_regime_counts[ann.regime]["fp"] += fp
            per_regime_counts[ann.regime]["fn"] += fn

    p = tp_tot / (tp_tot + fp_tot) if (tp_tot + fp_tot) > 0 else 0.0
    r = tp_tot / (tp_tot + fn_tot) if (tp_tot + fn_tot) > 0 else 0.0

    per_regime_f1: dict[str, float] = {}
    for regime, c in per_regime_counts.items():
        rp = c["tp"] / (c["tp"] + c["fp"]) if (c["tp"] + c["fp"]) > 0 else 0.0
        rr = c["tp"] / (c["tp"] + c["fn"]) if (c["tp"] + c["fn"]) > 0 else 0.0
        per_regime_f1[regime] = _f1(rp, rr)

    return {
        "tp": tp_tot,
        "fp": fp_tot,
        "fn": fn_tot,
        "precision": p,
        "recall": r,
        "f1": _f1(p, r),
        "per_regime_f1": per_regime_f1,
    }


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _build_table(timeframe: str, results: dict[tuple[int, float], dict]) -> list[str]:
    lines: list[str] = []
    lines.append(f"### {timeframe}")
    lines.append("")
    # Short labels per regime so the table fits at a glance.
    regime_labels = {
        "dead": "dead",
        "range": "range",
        "trending_bearish": "t_bear",
        "trending_bullish": "t_bull",
        "volatile_news": "vol_news",
    }
    header = ["lookback", "ATR×", "TP", "FP", "FN", "Precision", "Recall", "**F1**"] + [
        regime_labels[r] for r in _REGIMES
    ]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    for (lb, mult), m in results.items():
        regime_cells = [_pct(m["per_regime_f1"][r]) for r in _REGIMES]
        row = [
            str(lb),
            f"{mult:g}",
            str(m["tp"]),
            str(m["fp"]),
            str(m["fn"]),
            _pct(m["precision"]),
            _pct(m["recall"]),
            f"**{_pct(m['f1'])}**",
            *regime_cells,
        ]
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    return lines


def main() -> int:
    annotations, invalid = _discover_annotations(_REFERENCE_DIR)
    if invalid:
        for path, err in invalid:
            print(f"[warn] skipping invalid annotation {path}: {err}", file=sys.stderr)
    if not annotations:
        print("No annotations found, nothing to grid-search.")
        return 0

    print(
        f"Evaluating {len(_LOOKBACKS) * len(_ATR_MULTS)} combinations × "
        f"{len(_TIMEFRAMES)} timeframes against {len(annotations)} sessions...",
        file=sys.stderr,
    )

    results_by_tf: dict[str, dict[tuple[int, float], dict]] = {tf: {} for tf in _TIMEFRAMES}
    for tf in _TIMEFRAMES:
        for lb in _LOOKBACKS:
            for mult in _ATR_MULTS:
                results_by_tf[tf][(lb, mult)] = _evaluate(annotations, lb, mult, tf)

    # Identify best per TF (max F1, ties broken by higher precision then recall).
    def _best(combos: dict[tuple[int, float], dict]) -> tuple[int, float]:
        return max(
            combos.keys(),
            key=lambda k: (
                combos[k]["f1"],
                combos[k]["precision"],
                combos[k]["recall"],
            ),
        )

    best_h4 = _best(results_by_tf["H4"])
    best_h1 = _best(results_by_tf["H1"])

    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    lines: list[str] = []
    lines.append(f"# Swing detector grid search — {timestamp}")
    lines.append("")
    lines.append(
        f"Sweep over `lookback ∈ {list(_LOOKBACKS)}` × "
        f"`MIN_SWING_AMPLITUDE_ATR_MULT ∈ {list(_ATR_MULTS)}`, evaluated "
        f"separately per timeframe against {len(annotations)} operator "
        f"sessions. Matching uses the harness defaults (±2 candle / ±0.1% "
        f"H4, ±3 candle / ±0.1% H1)."
    )
    lines.append("")
    lines.append(
        "Per-regime columns are F1 scores. **Bold** F1 = headline overall F1 "
        "for the (lookback, ATR×) combo."
    )
    lines.append("")

    for tf in _TIMEFRAMES:
        lines.extend(_build_table(tf, results_by_tf[tf]))

    # Recommended section.
    h4_metrics = results_by_tf["H4"][best_h4]
    h1_metrics = results_by_tf["H1"][best_h1]

    lines.append("## Recommended")
    lines.append("")
    lines.append(
        f"- **H4 best**: `lookback = {best_h4[0]}`, "
        f"`MIN_SWING_AMPLITUDE_ATR_MULT = {best_h4[1]:g}` → "
        f"P={_pct(h4_metrics['precision'])}, "
        f"R={_pct(h4_metrics['recall'])}, "
        f"F1={_pct(h4_metrics['f1'])}"
    )
    lines.append(
        f"- **H1 best**: `lookback = {best_h1[0]}`, "
        f"`MIN_SWING_AMPLITUDE_ATR_MULT = {best_h1[1]:g}` → "
        f"P={_pct(h1_metrics['precision'])}, "
        f"R={_pct(h1_metrics['recall'])}, "
        f"F1={_pct(h1_metrics['f1'])}"
    )
    lines.append("")

    if best_h4 == best_h1:
        lines.append(
            f"H4 and H1 converge on the same `(lookback, ATR×) = "
            f"({best_h4[0]}, {best_h4[1]:g})` — calibrate both timeframes "
            f"with that single combo if you commit it to settings."
        )
    else:
        lines.append(
            f"H4 and H1 disagree (best H4: `({best_h4[0]}, {best_h4[1]:g})`, "
            f"best H1: `({best_h1[0]}, {best_h1[1]:g})`). The detector "
            f"already takes per-TF ``SWING_LOOKBACK_H4`` / "
            f"``SWING_LOOKBACK_H1`` config keys, but ``MIN_SWING_AMPLITUDE_"
            f"ATR_MULT`` is **shared**. If the chosen multipliers differ, "
            f"introducing a per-TF amplitude config key is the operator's "
            f"call (out of scope for this grid search)."
        )
    lines.append("")
    lines.append(
        "Note: 80% precision **and** 80% recall is the docs/07 §3 step 4 bar. "
        "If no combo crosses both, either widen the matching tolerance "
        "further (revisit operator vs detector pivot conventions), revisit "
        "the strict-fractal definition, or accept a lower bar with "
        "documented rationale."
    )
    lines.append("")

    body = "\n".join(lines)
    _RUNS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _RUNS_DIR / f"{timestamp}_grid_search.md"
    out_path.write_text(body, encoding="utf-8")

    print(body)
    print(f"\nReport saved to {out_path.relative_to(_REPO_ROOT)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
