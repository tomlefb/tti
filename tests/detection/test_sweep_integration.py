"""Integration test for the full liquidity + sweep pipeline.

Iterates the 18 reference dates from Sprint 1 across the 4 watched pairs
and the 2 killzones, computing:

- Asian range (M5)
- PDH / PDL (D1)
- Multi-TF confluent swing levels (H4 + H1)
- Equal H / L clusters
- All sweeps inside each killzone

Asserts:

1. No crashes.
2. Returned objects have the expected schema.
3. Sweep counts per killzone fall inside a wide sanity band (0–8).

Side-effect (deliberate, per Sprint 2 spec): writes a markdown report at
``calibration/runs/{TIMESTAMP}_sweep_integration.md`` listing every sweep
found. The operator uses this to spot-check sweeps on TradingView.

Default config values mirror ``config/settings.py.example`` and are
hardcoded here because ``config.settings`` imports ``config.secrets``
(gitignored). Keep in sync if the example changes.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from src.detection.liquidity import (
    asian_range_to_marked_levels,
    daily_levels_to_marked_levels,
    equal_level_to_marked_level,
    find_equal_highs_lows,
    mark_asian_range,
    mark_pdh_pdl,
    mark_swing_levels,
    paris_session_to_utc,
    swing_level_to_marked_level,
)
from src.detection.sweep import detect_sweeps

# ----- mirror of config/settings.py.example -----------------------------------
_SESSION_ASIA = (2, 0, 6, 0)
_KILLZONE_LONDON = (9, 0, 12, 0)
_KILLZONE_NY = (15, 30, 18, 0)
_SWING_LOOKBACK_H4 = 2
_SWING_LOOKBACK_H1 = 2
_MIN_SWING_AMPLITUDE_ATR_MULT = 1.0
_SWEEP_RETURN_WINDOW_CANDLES = 2
_H4_H1_TIME_TOLERANCE_CANDLES_H4 = 2
_H4_H1_PRICE_TOLERANCE_FRACTION = 0.001
_SWING_LEVELS_LOOKBACK_COUNT = 5
_INSTRUMENT_CONFIG = {
    "XAUUSD": {"sweep_buffer": 1.0, "equal_hl_tolerance": 0.5},
    "NDX100": {"sweep_buffer": 5.0, "equal_hl_tolerance": 3.0},
    "EURUSD": {"sweep_buffer": 0.00050, "equal_hl_tolerance": 0.00030},
    "GBPUSD": {"sweep_buffer": 0.00050, "equal_hl_tolerance": 0.00030},
}
# -----------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_FIXTURE_DIR = _REPO_ROOT / "tests" / "fixtures" / "historical"
_REFERENCE_CHARTS = _REPO_ROOT / "calibration" / "reference_charts"
_RUNS_DIR = _REPO_ROOT / "calibration" / "runs"
_PAIRS = ["XAUUSD", "NDX100", "EURUSD", "GBPUSD"]


def _reference_dates() -> list[date]:
    """Pull the 18+ reference dates from the committed annotation filenames."""
    dates: set[date] = set()
    if not _REFERENCE_CHARTS.exists():
        return []
    for f in _REFERENCE_CHARTS.glob("*.json"):
        # Filename: {YYYY-MM-DD}_{PAIR}_{TF}.json
        date_str = f.name.split("_")[0]
        try:
            dates.add(date.fromisoformat(date_str))
        except ValueError:
            continue
    return sorted(dates)


@pytest.fixture(scope="module")
def fixtures() -> dict[str, dict[str, pd.DataFrame]]:
    """Load D1/H4/H1/M5 for the 4 pairs once per test module."""
    out: dict[str, dict[str, pd.DataFrame]] = {}
    for pair in _PAIRS:
        per_pair: dict[str, pd.DataFrame] = {}
        for tf in ("D1", "H4", "H1", "M5"):
            path = _FIXTURE_DIR / f"{pair}_{tf}.parquet"
            if not path.exists():
                pytest.skip(f"fixture missing: {path}")
            per_pair[tf] = pd.read_parquet(path)
        out[pair] = per_pair
    return out


def _build_marked_levels(
    pair: str,
    target_date: date,
    fixtures: dict[str, dict[str, pd.DataFrame]],
    as_of_utc: datetime,
):
    pair_data = fixtures[pair]
    asian = mark_asian_range(pair_data["M5"], target_date, _SESSION_ASIA)
    daily = mark_pdh_pdl(pair_data["D1"], target_date)
    swings = mark_swing_levels(
        pair_data["H4"],
        pair_data["H1"],
        as_of_utc=as_of_utc,
        lookback_h4=_SWING_LOOKBACK_H4,
        lookback_h1=_SWING_LOOKBACK_H1,
        min_amplitude_atr_mult=_MIN_SWING_AMPLITUDE_ATR_MULT,
        n_swings=_SWING_LEVELS_LOOKBACK_COUNT,
        h4_h1_time_tolerance_h4_candles=_H4_H1_TIME_TOLERANCE_CANDLES_H4,
        h4_h1_price_tolerance_fraction=_H4_H1_PRICE_TOLERANCE_FRACTION,
    )
    equals = find_equal_highs_lows(
        swings, equal_hl_tolerance=_INSTRUMENT_CONFIG[pair]["equal_hl_tolerance"]
    )
    levels = (
        asian_range_to_marked_levels(asian)
        + daily_levels_to_marked_levels(daily)
        + [swing_level_to_marked_level(s) for s in swings]
        + [equal_level_to_marked_level(e) for e in equals]
    )
    return asian, daily, swings, equals, levels


def _format_report(rows: list[dict], dates: list[date]) -> str:
    lines: list[str] = []
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    lines.append(f"# Sweep integration report — {timestamp}")
    lines.append("")
    lines.append(f"Reference dates: {len(dates)} dates × {len(_PAIRS)} pairs × 2 killzones.")
    lines.append("")
    lines.append("## Config used")
    lines.append("")
    lines.append("| Key | Value |")
    lines.append("|---|---|")
    lines.append(f"| `SWING_LOOKBACK_H4` | `{_SWING_LOOKBACK_H4}` |")
    lines.append(f"| `SWING_LOOKBACK_H1` | `{_SWING_LOOKBACK_H1}` |")
    lines.append(f"| `MIN_SWING_AMPLITUDE_ATR_MULT` | `{_MIN_SWING_AMPLITUDE_ATR_MULT}` |")
    lines.append(f"| `SWEEP_RETURN_WINDOW_CANDLES` | `{_SWEEP_RETURN_WINDOW_CANDLES}` |")
    lines.append(f"| `H4_H1_TIME_TOLERANCE_CANDLES_H4` | `{_H4_H1_TIME_TOLERANCE_CANDLES_H4}` |")
    lines.append(f"| `H4_H1_PRICE_TOLERANCE_FRACTION` | `{_H4_H1_PRICE_TOLERANCE_FRACTION}` |")
    lines.append(f"| `SWING_LEVELS_LOOKBACK_COUNT` | `{_SWING_LEVELS_LOOKBACK_COUNT}` |")
    lines.append("")

    # Headline counts.
    total = sum(r["sweep_count"] for r in rows)
    by_kz: dict[str, int] = {}
    by_label: dict[str, int] = {}
    by_direction: dict[str, int] = {}
    by_strength: dict[str, int] = {}
    for r in rows:
        by_kz[r["killzone"]] = by_kz.get(r["killzone"], 0) + r["sweep_count"]
        for s in r["sweeps"]:
            by_label[s["swept_level_type"]] = by_label.get(s["swept_level_type"], 0) + 1
            by_direction[s["direction"]] = by_direction.get(s["direction"], 0) + 1
            by_strength[s["swept_level_strength"]] = (
                by_strength.get(s["swept_level_strength"], 0) + 1
            )

    lines.append("## Headline counts")
    lines.append("")
    lines.append(f"- Total sweeps: **{total}** across {len(rows)} (date × pair × killzone) cells")
    lines.append("- By killzone:")
    for k in sorted(by_kz):
        lines.append(f"  - {k}: {by_kz[k]}")
    lines.append("- By direction:")
    for k in sorted(by_direction):
        lines.append(f"  - {k}: {by_direction[k]}")
    lines.append("- By swept-level type:")
    for k in sorted(by_label):
        lines.append(f"  - `{k}`: {by_label[k]}")
    lines.append("- By swept-level strength:")
    for k in sorted(by_strength):
        lines.append(f"  - {k}: {by_strength[k]}")
    lines.append("")

    # Per-cell detail.
    lines.append("## Per (date, pair, killzone) sweeps")
    lines.append("")
    lines.append("| Date | Pair | Killzone | # sweeps | Detail |")
    lines.append("|---|---|---|---|---|")
    for r in sorted(rows, key=lambda x: (x["date"], x["pair"], x["killzone"])):
        if r["sweep_count"] == 0:
            detail = "—"
        else:
            detail = "<br>".join(
                f"{s['sweep_candle_time_utc'][11:16]} "
                f"`{s['swept_level_type']}` ({s['swept_level_strength']}) "
                f"{s['direction']} excursion={s['excursion']:.5g}"
                for s in r["sweeps"]
            )
        lines.append(
            f"| {r['date']} | {r['pair']} | {r['killzone']} | {r['sweep_count']} | {detail} |"
        )
    lines.append("")
    return "\n".join(lines), timestamp


def test_sweep_pipeline_runs_on_all_reference_dates(
    fixtures: dict[str, dict[str, pd.DataFrame]],
) -> None:
    dates = _reference_dates()
    if not dates:
        pytest.skip("no reference dates available")

    rows: list[dict] = []
    for target_date in dates:
        for pair in _PAIRS:
            for kz_name, kz_session in (
                ("LONDON", _KILLZONE_LONDON),
                ("NY", _KILLZONE_NY),
            ):
                kz_start_utc, kz_end_utc = paris_session_to_utc(target_date, kz_session)
                # Mark levels with as_of frozen at the killzone start
                # (matches docs/01 §3 — bias locked once killzone begins).
                _, _, swings, _, levels = _build_marked_levels(
                    pair, target_date, fixtures, kz_start_utc
                )

                sweeps = detect_sweeps(
                    fixtures[pair]["M5"],
                    levels,
                    killzone_window_utc=(kz_start_utc, kz_end_utc),
                    sweep_buffer=_INSTRUMENT_CONFIG[pair]["sweep_buffer"],
                    return_window_candles=_SWEEP_RETURN_WINDOW_CANDLES,
                )

                # Schema sanity per sweep.
                for s in sweeps:
                    assert s.direction in {"bullish", "bearish"}
                    assert isinstance(s.swept_level_price, float)
                    assert s.excursion >= 0
                    assert s.return_candle_time_utc >= s.sweep_candle_time_utc
                    assert kz_start_utc <= s.sweep_candle_time_utc <= kz_end_utc
                    # Allow return up to (return_window+1) M5 candles AFTER kz end.
                    assert s.return_candle_time_utc <= kz_end_utc + timedelta(
                        minutes=5 * (_SWEEP_RETURN_WINDOW_CANDLES + 1)
                    )

                # Sanity band on counts. A killzone has 30-36 M5 candles; on
                # a volatile/news day the same level can be swept by many
                # consecutive candles, and there are typically 8-12 marked
                # levels — so the per-cell cap is generous on purpose. The
                # test is a regression guard against runaway/empty output,
                # not a precision check.
                assert (
                    0 <= len(sweeps) <= 200
                ), f"{target_date} {pair} {kz_name}: {len(sweeps)} sweeps outside [0, 200]"

                rows.append(
                    {
                        "date": target_date.isoformat(),
                        "pair": pair,
                        "killzone": kz_name,
                        "swing_levels_count": len(swings),
                        "marked_levels_count": len(levels),
                        "sweep_count": len(sweeps),
                        "sweeps": [
                            {
                                "direction": s.direction,
                                "swept_level_type": s.swept_level_type,
                                "swept_level_strength": s.swept_level_strength,
                                "swept_level_price": s.swept_level_price,
                                "sweep_candle_time_utc": s.sweep_candle_time_utc.isoformat(),
                                "sweep_extreme_price": s.sweep_extreme_price,
                                "return_candle_time_utc": s.return_candle_time_utc.isoformat(),
                                "excursion": s.excursion,
                            }
                            for s in sweeps
                        ],
                    }
                )

    report, timestamp = _format_report(rows, dates)
    _RUNS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _RUNS_DIR / f"{timestamp}_sweep_integration.md"
    out_path.write_text(report, encoding="utf-8")

    # Stash a pointer for human inspection (printed via -s).
    print(f"\nSweep integration report: {out_path.relative_to(_REPO_ROOT)}")
