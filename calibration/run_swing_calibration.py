"""Swing-detector calibration harness — see docs/07 §3.

Runs ``src.detection.swings.find_swings`` against the operator's hand-marked
reference annotations and emits a markdown report comparing the two.

Inputs (fixtures + annotations are committed to the repo):

- ``tests/fixtures/historical/{PAIR}_{TF}.parquet`` — OHLC frames.
- ``calibration/reference_charts/{DATE}_{PAIR}_{TF}.json`` — operator's
  ground-truth swing markings for a specific session.

Output:

- ``calibration/runs/{TIMESTAMP}_swing_calibration.md`` — markdown report.

This script does NOT mutate ``config/settings.py.example``. Tuning is the
operator's call (docs/04 — Things Claude Code should NOT do without asking).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.detection.swings import find_swings  # noqa: E402

_REFERENCE_DIR = _REPO_ROOT / "calibration" / "reference_charts"
_RUNS_DIR = _REPO_ROOT / "calibration" / "runs"
_FIXTURE_DIR = _REPO_ROOT / "tests" / "fixtures" / "historical"

_VALID_PAIRS = {"XAUUSD", "NDX100", "EURUSD", "GBPUSD"}
_VALID_TFS = {"H4", "H1"}
_VALID_REGIMES = {
    "trending_bullish",
    "trending_bearish",
    "range",
    "volatile_news",
    "dead",
}
_VALID_TYPES = {"high", "low"}

_TF_TO_TIMEDELTA = {
    "H4": pd.Timedelta(hours=4),
    "H1": pd.Timedelta(hours=1),
}

# Matching tolerances.
#
# Operator pivot annotation marks the first candle touching the structural
# high/low. Detector's strict fractal marks the confirmed pivot candle,
# which can fall 1-3 candles later in flat-zone tops/bottoms. The wider
# time tolerance accounts for this without being so loose that genuinely
# different pivots match. H1 needs more slack than H4 because flat zones
# span more candles at lower timeframes.
_TIME_TOLERANCE_CANDLES_BY_TF: dict[str, int] = {
    "H4": 2,
    "H1": 3,
}
_PRICE_TOLERANCE_FRACTION = 0.001  # 0.1 %

_PASSING_THRESHOLD = 0.80  # 80 %, per docs/07 §3 step 4


# ---------------------------------------------------------------------------
# Settings loader (with fallback to settings.py.example so the harness is
# runnable on the dev Mac, where config/settings.py is gitignored).
# ---------------------------------------------------------------------------


def _load_settings_with_example_fallback() -> ModuleType:
    """Import ``config.settings`` if present; else load ``settings.py.example``.

    The .example file imports ``config.secrets`` (gitignored too), so we
    inject a stub module for ``config.secrets`` before exec'ing the example.
    """
    settings_real = _REPO_ROOT / "config" / "settings.py"
    settings_example = _REPO_ROOT / "config" / "settings.py.example"
    target = settings_real if settings_real.exists() else settings_example
    if not target.exists():
        raise FileNotFoundError("Neither config/settings.py nor config/settings.py.example exists.")

    if "config.secrets" not in sys.modules:
        secrets_stub = ModuleType("config.secrets")
        for name in (
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
            "MT5_LOGIN",
            "MT5_PASSWORD",
            "MT5_SERVER",
        ):
            setattr(secrets_stub, name, None)
        sys.modules["config.secrets"] = secrets_stub

    # ``settings.py.example`` lacks a recognised .py suffix, so explicitly
    # use a SourceFileLoader rather than importlib.util.spec_from_file_location.
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader("config.settings", str(target))
    module = ModuleType(loader.name)
    module.__file__ = str(target)
    sys.modules["config.settings"] = module
    loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Annotation schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Annotation:
    """Operator-marked reference annotation for a single session."""

    path: Path
    date: str
    pair: str
    timeframe: str
    regime: str
    window_start: pd.Timestamp
    window_end: pd.Timestamp
    swings: list[dict[str, Any]]  # each: {"type", "time": Timestamp, "price", "note"}
    annotator_confidence: str
    annotation_notes: str


def _validate_and_parse(raw: dict[str, Any], path: Path) -> Annotation:
    """Validate one JSON payload; raise ``ValueError`` on bad shape."""

    def _need(key: str) -> Any:
        if key not in raw:
            raise ValueError(f"missing key: {key!r}")
        return raw[key]

    pair = _need("pair")
    if pair not in _VALID_PAIRS:
        raise ValueError(f"unknown pair {pair!r}; expected one of {sorted(_VALID_PAIRS)}")

    tf = _need("timeframe")
    if tf not in _VALID_TFS:
        raise ValueError(f"unknown timeframe {tf!r}; expected one of {sorted(_VALID_TFS)}")

    regime = _need("regime")
    if regime not in _VALID_REGIMES:
        raise ValueError(f"unknown regime {regime!r}; expected one of {sorted(_VALID_REGIMES)}")

    window = _need("annotation_window_utc")
    if not isinstance(window, dict) or "start" not in window or "end" not in window:
        raise ValueError("annotation_window_utc must have 'start' and 'end'")

    start = pd.Timestamp(window["start"])
    end = pd.Timestamp(window["end"])
    if start.tzinfo is None:
        start = start.tz_localize("UTC")
    if end.tzinfo is None:
        end = end.tz_localize("UTC")
    if end <= start:
        raise ValueError(f"window end {end} must be after start {start}")

    raw_swings = _need("swings")
    if not isinstance(raw_swings, list):
        raise ValueError("swings must be a list")

    parsed_swings: list[dict[str, Any]] = []
    for i, s in enumerate(raw_swings):
        if not isinstance(s, dict):
            raise ValueError(f"swings[{i}] must be a dict")
        s_type = s.get("type")
        if s_type not in _VALID_TYPES:
            raise ValueError(
                f"swings[{i}].type {s_type!r} invalid; expected one of {sorted(_VALID_TYPES)}"
            )
        s_time = pd.Timestamp(s.get("time_utc"))
        if s_time.tzinfo is None:
            s_time = s_time.tz_localize("UTC")
        try:
            s_price = float(s["price"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"swings[{i}].price invalid: {exc}") from exc
        parsed_swings.append(
            {
                "type": s_type,
                "time": s_time,
                "price": s_price,
                "note": str(s.get("note", "")),
            }
        )

    return Annotation(
        path=path,
        date=str(_need("date")),
        pair=pair,
        timeframe=tf,
        regime=regime,
        window_start=start,
        window_end=end,
        swings=parsed_swings,
        annotator_confidence=str(raw.get("annotator_confidence", "")),
        annotation_notes=str(raw.get("annotation_notes", "")),
    )


def _discover_annotations(directory: Path) -> tuple[list[Annotation], list[tuple[Path, str]]]:
    """Discover and parse all JSON annotation files; return (valid, invalid)."""
    valid: list[Annotation] = []
    invalid: list[tuple[Path, str]] = []
    if not directory.exists():
        return valid, invalid
    for json_path in sorted(directory.glob("*.json")):
        try:
            with json_path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
            ann = _validate_and_parse(payload, json_path)
            valid.append(ann)
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            invalid.append((json_path, f"{type(exc).__name__}: {exc}"))
    return valid, invalid


# ---------------------------------------------------------------------------
# Detection + matching
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionResult:
    annotation: Annotation
    tp: int
    fp: int
    fn: int
    false_positives: list[dict[str, Any]]
    false_negatives: list[dict[str, Any]]

    @property
    def precision(self) -> float:
        d = self.tp + self.fp
        return self.tp / d if d > 0 else 0.0

    @property
    def recall(self) -> float:
        d = self.tp + self.fn
        return self.tp / d if d > 0 else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) > 0 else 0.0


def _detected_swings_in_window(
    df: pd.DataFrame,
    window_start: pd.Timestamp,
    window_end: pd.Timestamp,
    *,
    lookback: int,
    atr_mult: float,
    atr_period: int,
) -> list[dict[str, Any]]:
    """Run the detector on the full fixture, then filter to the window.

    We run on the full fixture (not a sliced view) so ATR has enough warm-up
    history; we keep only swings whose timestamp falls inside the operator's
    annotation window.
    """
    swings = find_swings(
        df,
        lookback=lookback,
        min_amplitude_atr_mult=atr_mult,
        atr_period=atr_period,
    )
    sig = swings[swings["swing_type"].notna()]
    if sig.empty:
        return []
    times = df.loc[sig.index, "time"]
    in_window = (times >= window_start) & (times <= window_end)
    sig = sig.loc[in_window.values]
    times_in = df.loc[sig.index, "time"]
    out = [
        {"type": t, "time": pd.Timestamp(ts), "price": float(p)}
        for t, ts, p in zip(
            sig["swing_type"].tolist(),
            times_in.tolist(),
            sig["swing_price"].tolist(),
            strict=True,
        )
    ]
    return out


def _match(
    detected: list[dict[str, Any]],
    annotated: list[dict[str, Any]],
    timeframe: str,
) -> tuple[int, int, int, list[dict[str, Any]], list[dict[str, Any]]]:
    """Greedy match annotated → detected within (time, price) tolerance.

    For each annotated swing, find the closest-in-time detected swing of
    the same type that is still within both tolerances; mark both as
    matched. Anything left over is FP / FN.
    """
    time_tol = _TF_TO_TIMEDELTA[timeframe] * _TIME_TOLERANCE_CANDLES_BY_TF[timeframe]
    matched_detected: set[int] = set()
    tp = 0
    fns: list[dict[str, Any]] = []

    for ann_sw in annotated:
        best: int | None = None
        best_dt: pd.Timedelta | None = None
        for j, det in enumerate(detected):
            if j in matched_detected:
                continue
            if det["type"] != ann_sw["type"]:
                continue
            dt = abs(det["time"] - ann_sw["time"])
            if dt > time_tol:
                continue
            price_tol = _PRICE_TOLERANCE_FRACTION * abs(ann_sw["price"])
            if abs(det["price"] - ann_sw["price"]) > price_tol:
                continue
            if best is None or dt < best_dt:
                best = j
                best_dt = dt
        if best is not None:
            matched_detected.add(best)
            tp += 1
        else:
            fns.append(ann_sw)

    fps = [det for j, det in enumerate(detected) if j not in matched_detected]
    return tp, len(fps), len(fns), fps, fns


# ---------------------------------------------------------------------------
# Aggregation + reporting
# ---------------------------------------------------------------------------


def _aggregate(results: list[SessionResult], key_fn) -> dict[str, dict[str, float]]:
    grouped: dict[str, dict[str, int]] = {}
    for r in results:
        k = key_fn(r)
        bucket = grouped.setdefault(k, {"tp": 0, "fp": 0, "fn": 0, "n": 0})
        bucket["tp"] += r.tp
        bucket["fp"] += r.fp
        bucket["fn"] += r.fn
        bucket["n"] += 1
    out: dict[str, dict[str, float]] = {}
    for k, b in grouped.items():
        tp, fp, fn = b["tp"], b["fp"], b["fn"]
        p = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        rc = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = 2 * p * rc / (p + rc) if (p + rc) > 0 else 0.0
        out[k] = {
            "sessions": b["n"],
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": p,
            "recall": rc,
            "f1": f1,
        }
    return out


def _format_pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _build_report(
    results: list[SessionResult],
    invalid_files: list[tuple[Path, str]],
    config: dict[str, Any],
    timestamp: str,
) -> str:
    lines: list[str] = []
    lines.append(f"# Swing calibration run — {timestamp}")
    lines.append("")
    lines.append("## Config used")
    lines.append("")
    lines.append("| Key | Value |")
    lines.append("|---|---|")
    for k, v in config.items():
        lines.append(f"| `{k}` | `{v}` |")
    lines.append("")

    # Overall.
    lines.append("## Overall")
    lines.append("")
    overall = _aggregate(results, lambda _: "all")
    if overall:
        agg = overall["all"]
        lines.append("| Sessions | TP | FP | FN | Precision | Recall | F1 |")
        lines.append("|---|---|---|---|---|---|---|")
        lines.append(
            f"| {agg['sessions']} | {agg['tp']} | {agg['fp']} | {agg['fn']} "
            f"| {_format_pct(agg['precision'])} "
            f"| {_format_pct(agg['recall'])} "
            f"| {_format_pct(agg['f1'])} |"
        )
    else:
        lines.append("_no sessions matched_")
    lines.append("")

    # Breakdowns.
    for label, key_fn in [
        ("Per timeframe", lambda r: r.annotation.timeframe),
        ("Per pair", lambda r: r.annotation.pair),
        ("Per regime", lambda r: r.annotation.regime),
    ]:
        lines.append(f"## {label}")
        lines.append("")
        agg = _aggregate(results, key_fn)
        if not agg:
            lines.append("_no sessions_")
            lines.append("")
            continue
        lines.append("| Group | Sessions | TP | FP | FN | Precision | Recall | F1 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for k in sorted(agg.keys()):
            v = agg[k]
            lines.append(
                f"| {k} | {v['sessions']} | {v['tp']} | {v['fp']} | {v['fn']} "
                f"| {_format_pct(v['precision'])} "
                f"| {_format_pct(v['recall'])} "
                f"| {_format_pct(v['f1'])} |"
            )
        lines.append("")

    # Per-session detail.
    lines.append("## Per-session detail")
    lines.append("")
    lines.append("| Date | Pair | TF | Regime | TP | FP | FN | Precision | Recall |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in sorted(
        results, key=lambda x: (x.annotation.date, x.annotation.pair, x.annotation.timeframe)
    ):
        a = r.annotation
        lines.append(
            f"| {a.date} | {a.pair} | {a.timeframe} | {a.regime} "
            f"| {r.tp} | {r.fp} | {r.fn} "
            f"| {_format_pct(r.precision)} | {_format_pct(r.recall)} |"
        )
    lines.append("")

    # Sessions below threshold.
    lines.append(f"## Sessions below {int(_PASSING_THRESHOLD * 100)}% precision OR recall")
    lines.append("")
    failing = [
        r for r in results if r.precision < _PASSING_THRESHOLD or r.recall < _PASSING_THRESHOLD
    ]
    if not failing:
        lines.append("_none_ ✅")
    else:
        lines.append("| Date | Pair | TF | Regime | Precision | Recall |")
        lines.append("|---|---|---|---|---|---|")
        for r in sorted(
            failing, key=lambda x: (x.annotation.date, x.annotation.pair, x.annotation.timeframe)
        ):
            a = r.annotation
            lines.append(
                f"| {a.date} | {a.pair} | {a.timeframe} | {a.regime} "
                f"| {_format_pct(r.precision)} | {_format_pct(r.recall)} |"
            )
    lines.append("")

    # FP / FN samples.
    fps_all: list[tuple[Annotation, dict[str, Any]]] = []
    fns_all: list[tuple[Annotation, dict[str, Any]]] = []
    for r in results:
        for fp in r.false_positives:
            fps_all.append((r.annotation, fp))
        for fn in r.false_negatives:
            fns_all.append((r.annotation, fn))

    def _fmt_swing_row(a: Annotation, sw: dict[str, Any], note: str = "") -> str:
        return (
            f"| {a.date} | {a.pair} | {a.timeframe} | {sw['type']} "
            f"| {sw['time'].isoformat()} | {sw['price']:.5f} | {note} |"
        )

    lines.append("## Most common false positives (up to 10)")
    lines.append("")
    if not fps_all:
        lines.append("_none_")
    else:
        lines.append("| Date | Pair | TF | Type | Time (UTC) | Price | Candidate reason |")
        lines.append("|---|---|---|---|---|---|---|")
        for a, sw in fps_all[:10]:
            tol = _TIME_TOLERANCE_CANDLES_BY_TF[a.timeframe]
            lines.append(
                _fmt_swing_row(
                    a,
                    sw,
                    f"no operator mark within ±{tol} candle / "
                    f"±{_PRICE_TOLERANCE_FRACTION * 100:g}% — likely noise",
                )
            )
    lines.append("")

    lines.append("## Most common false negatives (up to 10)")
    lines.append("")
    if not fns_all:
        lines.append("_none_")
    else:
        lines.append("| Date | Pair | TF | Type | Time (UTC) | Price | Candidate reason |")
        lines.append("|---|---|---|---|---|---|---|")
        for a, sw in fns_all[:10]:
            tol = _TIME_TOLERANCE_CANDLES_BY_TF[a.timeframe]
            lines.append(
                _fmt_swing_row(
                    a,
                    sw,
                    f"no detector pivot within ±{tol} candle / "
                    f"±{_PRICE_TOLERANCE_FRACTION * 100:g}% — amplitude below filter "
                    f"or fractal lookback too wide",
                )
            )
    lines.append("")

    # Invalid files.
    if invalid_files:
        lines.append("## Skipped (invalid) annotation files")
        lines.append("")
        lines.append("| Path | Error |")
        lines.append("|---|---|")
        for path, err in invalid_files:
            lines.append(f"| `{path.relative_to(_REPO_ROOT)}` | {err} |")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _load_fixture(pair: str, tf: str) -> pd.DataFrame | None:
    path = _FIXTURE_DIR / f"{pair}_{tf}.parquet"
    if not path.exists():
        return None
    return pd.read_parquet(path)


def main() -> int:
    annotations, invalid = _discover_annotations(_REFERENCE_DIR)
    if invalid:
        for path, err in invalid:
            print(
                f"[warn] skipping invalid annotation {path.relative_to(_REPO_ROOT)}: {err}",
                file=sys.stderr,
            )
    if not annotations:
        print("No annotations found, skipping calibration")
        return 0

    settings = _load_settings_with_example_fallback()
    config_used = {
        "SWING_LOOKBACK_H4": settings.SWING_LOOKBACK_H4,
        "SWING_LOOKBACK_H1": settings.SWING_LOOKBACK_H1,
        "MIN_SWING_AMPLITUDE_ATR_MULT": settings.MIN_SWING_AMPLITUDE_ATR_MULT,
        "BIAS_SWING_COUNT": settings.BIAS_SWING_COUNT,
        "ATR_PERIOD": 14,
    }

    results: list[SessionResult] = []
    for ann in annotations:
        df = _load_fixture(ann.pair, ann.timeframe)
        if df is None:
            print(
                f"[warn] no fixture for {ann.pair}_{ann.timeframe}; " f"skipping {ann.path.name}",
                file=sys.stderr,
            )
            continue

        lookback = (
            settings.SWING_LOOKBACK_H4 if ann.timeframe == "H4" else settings.SWING_LOOKBACK_H1
        )
        detected = _detected_swings_in_window(
            df,
            ann.window_start,
            ann.window_end,
            lookback=lookback,
            atr_mult=settings.MIN_SWING_AMPLITUDE_ATR_MULT,
            atr_period=14,
        )
        tp, fp, fn, fps, fns = _match(detected, ann.swings, ann.timeframe)
        results.append(
            SessionResult(
                annotation=ann,
                tp=tp,
                fp=fp,
                fn=fn,
                false_positives=fps,
                false_negatives=fns,
            )
        )

    if not results:
        print("No matched fixtures for any annotation; nothing to report.")
        return 0

    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    report = _build_report(results, invalid, config_used, timestamp)
    _RUNS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _RUNS_DIR / f"{timestamp}_swing_calibration.md"
    out_path.write_text(report, encoding="utf-8")

    overall = _aggregate(results, lambda _: "all")["all"]
    failing = [
        r for r in results if r.precision < _PASSING_THRESHOLD or r.recall < _PASSING_THRESHOLD
    ]

    print(f"Calibration report written to {out_path.relative_to(_REPO_ROOT)}")
    print(f"  sessions evaluated : {overall['sessions']}")
    print(f"  overall precision  : {_format_pct(overall['precision'])}")
    print(f"  overall recall     : {_format_pct(overall['recall'])}")
    print(f"  overall F1         : {_format_pct(overall['f1'])}")
    print(
        f"  sessions <{int(_PASSING_THRESHOLD * 100)}% P or R : " f"{len(failing)} / {len(results)}"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
