"""H4-only grid search for the swing detector — Sprint 3 amendment.

Sprint 1's `run_grid_search.py` swept ``(lookback, atr_mult)`` over both
H4 and H1 against a SHARED ``MIN_SWING_AMPLITUDE_ATR_MULT`` key. The
Sprint 3 diagnostic dive on XAUUSD 2025-10-15 surfaced that the unified
1.0 multiplier triggers H4 pivots on intra-day retracements (e.g. the
2025-10-09 20:00 LL @ 3944.72 inside an otherwise clean H4 bullish leg),
killing daily bias on clean trending days.

The amplitude key is now per-timeframe (Sprint 3 split). This script
re-runs the grid search on H4 ONLY, with a finer set:

    lookback ∈ {2, 3}
    atr_mult ∈ {1.0, 1.3, 1.5, 1.8, 2.0, 2.3, 2.5, 3.0}

Tie-break: when multiple (lookback, atr_mult) tie on F1, prefer the
combo with HIGHER atr_mult (more selective ⇒ fewer false bias signals).

Output:

- Markdown report to ``calibration/runs/{TIMESTAMP}_grid_search_h4_only.md``.
- Best combo printed to stdout.

Does NOT mutate ``config/settings.py.example``; the operator commits
the chosen value.
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
_ATR_MULTS = (1.0, 1.3, 1.5, 1.8, 2.0, 2.3, 2.5, 3.0)
_REGIMES = (
    "dead",
    "range",
    "trending_bearish",
    "trending_bullish",
    "volatile_news",
)
_ATR_PERIOD = 14


def _f1(p: float, r: float) -> float:
    return 2 * p * r / (p + r) if (p + r) > 0 else 0.0


def _evaluate(annotations: list, lookback: int, atr_mult: float) -> dict:
    tp_tot = fp_tot = fn_tot = 0
    per_regime: dict[str, dict[str, int]] = {r: {"tp": 0, "fp": 0, "fn": 0} for r in _REGIMES}
    sessions_pass = 0
    sessions_eval = 0

    for ann in annotations:
        if ann.timeframe != "H4":
            continue
        df = _load_fixture(ann.pair, ann.timeframe)
        if df is None:
            continue
        sessions_eval += 1
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
        if ann.regime in per_regime:
            per_regime[ann.regime]["tp"] += tp
            per_regime[ann.regime]["fp"] += fp
            per_regime[ann.regime]["fn"] += fn
        # Per-session 80/80 bar (docs/07 §3 step 4).
        sp = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        sr = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        if sp >= 0.80 and sr >= 0.80:
            sessions_pass += 1

    p = tp_tot / (tp_tot + fp_tot) if (tp_tot + fp_tot) > 0 else 0.0
    r = tp_tot / (tp_tot + fn_tot) if (tp_tot + fn_tot) > 0 else 0.0

    per_regime_f1: dict[str, float] = {}
    for regime, c in per_regime.items():
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
        "sessions_pass_80_80": sessions_pass,
        "sessions_eval": sessions_eval,
        "per_regime_f1": per_regime_f1,
    }


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _table(results: dict[tuple[int, float], dict]) -> list[str]:
    regime_short = {
        "dead": "dead",
        "range": "range",
        "trending_bearish": "t_bear",
        "trending_bullish": "t_bull",
        "volatile_news": "vol_news",
    }
    header = ["lookback", "ATR×", "TP", "FP", "FN", "P", "R", "**F1**", "≥80/80"]
    header += [regime_short[r] for r in _REGIMES]
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
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
            f"{m['sessions_pass_80_80']}/{m['sessions_eval']}",
            *regime_cells,
        ]
        lines.append("| " + " | ".join(row) + " |")
    return lines


def main() -> int:
    annotations, invalid = _discover_annotations(_REFERENCE_DIR)
    if invalid:
        for path, err in invalid:
            print(f"[warn] invalid annotation {path}: {err}", file=sys.stderr)
    if not annotations:
        print("No annotations found.")
        return 0

    h4_count = sum(1 for a in annotations if a.timeframe == "H4")
    print(
        f"Evaluating {len(_LOOKBACKS) * len(_ATR_MULTS)} (lookback, ATR×) combos "
        f"on {h4_count} H4 sessions...",
        file=sys.stderr,
    )

    results: dict[tuple[int, float], dict] = {}
    for lb in _LOOKBACKS:
        for mult in _ATR_MULTS:
            results[(lb, mult)] = _evaluate(annotations, lb, mult)

    # Tie-break: max F1, then HIGHER atr_mult (more selective), then
    # higher precision, then higher recall.
    def sort_key(k):
        m = results[k]
        return (m["f1"], k[1], m["precision"], m["recall"])

    best = max(results.keys(), key=sort_key)
    bm = results[best]

    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    lines: list[str] = []
    lines.append(f"# H4-only swing-detector grid search — {timestamp}")
    lines.append("")
    lines.append(
        f"Sprint 3 amendment: re-tune H4 alone after splitting "
        f"`MIN_SWING_AMPLITUDE_ATR_MULT` per timeframe. Sweep "
        f"`lookback ∈ {list(_LOOKBACKS)}` × `atr_mult ∈ {list(_ATR_MULTS)}` "
        f"against {h4_count} operator-annotated H4 sessions. Tie-break: "
        f"higher atr_mult preferred (more selective ⇒ fewer false bias "
        f"signals; rationale in `calibration/runs/2026-04-28T14-18-20Z_setup_diagnostic_dive.md`)."
    )
    lines.append("")
    lines.append("## Grid")
    lines.append("")
    lines.extend(_table(results))
    lines.append("")
    lines.append("## Recommended")
    lines.append("")
    lines.append(
        f"- **H4 best**: `lookback = {best[0]}`, "
        f"`MIN_SWING_AMPLITUDE_ATR_MULT_H4 = {best[1]:g}` → "
        f"P={_pct(bm['precision'])}, R={_pct(bm['recall'])}, F1={_pct(bm['f1'])}"
    )
    lines.append(
        f"- Sessions passing the docs/07 §3 step 4 80/80 bar: "
        f"{bm['sessions_pass_80_80']}/{bm['sessions_eval']}"
    )
    lines.append("")
    lines.append(
        "Per-regime F1 for the chosen combo:  "
        + ", ".join(f"{r}={_pct(bm['per_regime_f1'][r])}" for r in _REGIMES)
    )
    lines.append("")

    body = "\n".join(lines)
    _RUNS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _RUNS_DIR / f"{timestamp}_grid_search_h4_only.md"
    out_path.write_text(body, encoding="utf-8")

    print(body)
    print(f"\nReport: {out_path.relative_to(_REPO_ROOT)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
