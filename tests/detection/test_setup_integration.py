"""Integration test for the full Sprint 3 setup pipeline.

Iterates the 18 reference dates × 4 watched pairs through
``build_setup_candidates`` and asserts:

1. No crashes.
2. Returned ``Setup`` objects have the expected schema and reasonable
   internal consistency (entry between SL and TP, RR matches the
   computed reward/risk, etc.).
3. Per-(date, pair) cap of 6 setups — most days yield 0-2; volatile or
   FOMC days may produce up to ~5. 6 is the loose ceiling.

Side-effect (deliberate, mirrors the Sprint 2 sweep integration test):
writes a markdown report at
``calibration/runs/{TIMESTAMP}_setup_integration.md`` listing every setup
detected, with per-date / per-pair / per-quality breakdowns. The
operator spot-checks this report on TradingView before committing.

Default config values mirror ``config/settings.py.example``; kept in
sync manually because ``config.settings`` imports gitignored secrets.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from src.detection.setup import build_setup_candidates


# ----- mirror of config/settings.py.example -----------------------------------
def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        SESSION_ASIA=(2, 0, 6, 0),
        KILLZONE_LONDON=(9, 0, 12, 0),
        KILLZONE_NY=(15, 30, 18, 0),
        SWING_LOOKBACK_H4=2,
        SWING_LOOKBACK_H1=2,
        SWING_LOOKBACK_M5=2,
        MIN_SWING_AMPLITUDE_ATR_MULT_H4=1.3,
        MIN_SWING_AMPLITUDE_ATR_MULT_H1=1.0,
        MIN_SWING_AMPLITUDE_ATR_MULT_M5=1.0,
        BIAS_SWING_COUNT=4,
        BIAS_REQUIRE_H1_CONFIRMATION=False,
        H4_H1_TIME_TOLERANCE_CANDLES_H4=2,
        H4_H1_PRICE_TOLERANCE_FRACTION=0.001,
        SWING_LEVELS_LOOKBACK_COUNT=5,
        SWEEP_RETURN_WINDOW_CANDLES=2,
        SWEEP_DEDUP_TIME_WINDOW_MINUTES=30,
        SWEEP_DEDUP_PRICE_TOLERANCE_FRACTION=0.001,
        MSS_DISPLACEMENT_MULTIPLIER=1.5,
        MSS_DISPLACEMENT_LOOKBACK=20,
        FVG_ATR_PERIOD=14,
        FVG_MIN_SIZE_ATR_MULTIPLIER=0.3,
        MIN_RR=3.0,
        A_PLUS_RR_THRESHOLD=4.0,
        PARTIAL_TP_RR_TARGET=5.0,
        INSTRUMENT_CONFIG={
            "XAUUSD": {"sweep_buffer": 1.0, "equal_hl_tolerance": 0.5, "sl_buffer": 1.0},
            "NDX100": {"sweep_buffer": 5.0, "equal_hl_tolerance": 3.0, "sl_buffer": 5.0},
            "EURUSD": {
                "sweep_buffer": 0.00050,
                "equal_hl_tolerance": 0.00030,
                "sl_buffer": 0.00050,
            },
            "GBPUSD": {
                "sweep_buffer": 0.00050,
                "equal_hl_tolerance": 0.00030,
                "sl_buffer": 0.00050,
            },
        },
    )


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_FIXTURE_DIR = _REPO_ROOT / "tests" / "fixtures" / "historical"
_REFERENCE_CHARTS = _REPO_ROOT / "calibration" / "reference_charts"
_RUNS_DIR = _REPO_ROOT / "calibration" / "runs"
_PAIRS = ["XAUUSD", "NDX100", "EURUSD", "GBPUSD"]
_PER_CELL_CAP = 6


def _reference_dates() -> list[date]:
    dates: set[date] = set()
    if not _REFERENCE_CHARTS.exists():
        return []
    for f in _REFERENCE_CHARTS.glob("*.json"):
        date_str = f.name.split("_")[0]
        try:
            dates.add(date.fromisoformat(date_str))
        except ValueError:
            continue
    return sorted(dates)


@pytest.fixture(scope="module")
def fixtures() -> dict[str, dict[str, pd.DataFrame]]:
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


def _format_report(rows: list[dict], dates: list[date]) -> tuple[str, str]:
    lines: list[str] = []
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    lines.append(f"# Setup integration report — {timestamp}")
    lines.append("")
    lines.append(f"Reference dates: {len(dates)} dates × {len(_PAIRS)} pairs.")
    lines.append("")

    settings = _settings()
    lines.append("## Config used")
    lines.append("")
    lines.append("| Key | Value |")
    lines.append("|---|---|")
    for k in (
        "MIN_SWING_AMPLITUDE_ATR_MULT_H4",
        "MIN_SWING_AMPLITUDE_ATR_MULT_H1",
        "MIN_SWING_AMPLITUDE_ATR_MULT_M5",
        "BIAS_REQUIRE_H1_CONFIRMATION",
        "SWEEP_RETURN_WINDOW_CANDLES",
        "SWEEP_DEDUP_TIME_WINDOW_MINUTES",
        "SWEEP_DEDUP_PRICE_TOLERANCE_FRACTION",
        "MSS_DISPLACEMENT_MULTIPLIER",
        "MSS_DISPLACEMENT_LOOKBACK",
        "FVG_MIN_SIZE_ATR_MULTIPLIER",
        "MIN_RR",
        "A_PLUS_RR_THRESHOLD",
    ):
        lines.append(f"| `{k}` | `{getattr(settings, k)}` |")
    lines.append("")

    total = sum(len(r["setups"]) for r in rows)
    by_pair: dict[str, int] = {}
    by_kz: dict[str, int] = {}
    by_quality: dict[str, int] = {}
    by_direction: dict[str, int] = {}
    by_poi_type: dict[str, int] = {}

    for r in rows:
        for s in r["setups"]:
            by_pair[r["pair"]] = by_pair.get(r["pair"], 0) + 1
            by_kz[s["killzone"]] = by_kz.get(s["killzone"], 0) + 1
            by_quality[s["quality"]] = by_quality.get(s["quality"], 0) + 1
            by_direction[s["direction"]] = by_direction.get(s["direction"], 0) + 1
            by_poi_type[s["poi_type"]] = by_poi_type.get(s["poi_type"], 0) + 1

    lines.append("## Headline counts")
    lines.append("")
    lines.append(f"- Total setups: **{total}** across {len(rows)} (date × pair) cells")
    lines.append("- By pair:")
    for k in sorted(by_pair):
        lines.append(f"  - {k}: {by_pair[k]}")
    lines.append("- By killzone:")
    for k in sorted(by_kz):
        lines.append(f"  - {k}: {by_kz[k]}")
    lines.append("- By quality:")
    for k in sorted(by_quality):
        lines.append(f"  - {k}: {by_quality[k]}")
    lines.append("- By direction:")
    for k in sorted(by_direction):
        lines.append(f"  - {k}: {by_direction[k]}")
    lines.append("- By POI type:")
    for k in sorted(by_poi_type):
        lines.append(f"  - {k}: {by_poi_type[k]}")
    lines.append("")

    lines.append("## Per (date, pair) setups")
    lines.append("")
    lines.append("| Date | Pair | # | Detail |")
    lines.append("|---|---|---|---|")
    for r in sorted(rows, key=lambda x: (x["date"], x["pair"])):
        if not r["setups"]:
            detail = "—"
        else:
            detail = "<br>".join(
                f"{s['timestamp_utc'][11:16]} {s['killzone']} {s['direction']} "
                f"`{s['quality']}` POI={s['poi_type']} entry={s['entry_price']:.5g} "
                f"SL={s['stop_loss']:.5g} "
                f"TP1={s['tp1_price']:.5g}({s['tp1_rr']:.2f}R) "
                f"TPR={s['tp_runner_price']:.5g}({s['tp_runner_rr']:.2f}R) "
                f"swept=`{s['swept_level_type']}` ({s['swept_level_strength']}) "
                f"target=`{s['target_level_type']}` "
                f"conf=[{','.join(s['confluences'])}]"
                for s in r["setups"]
            )
        lines.append(f"| {r['date']} | {r['pair']} | {len(r['setups'])} | {detail} |")
    lines.append("")
    return "\n".join(lines), timestamp


def test_setup_pipeline_runs_on_all_reference_dates(
    fixtures: dict[str, dict[str, pd.DataFrame]],
) -> None:
    dates = _reference_dates()
    if not dates:
        pytest.skip("no reference dates available")

    settings = _settings()
    rows: list[dict] = []

    for target_date in dates:
        for pair in _PAIRS:
            setups = build_setup_candidates(
                df_h4=fixtures[pair]["H4"],
                df_h1=fixtures[pair]["H1"],
                df_m5=fixtures[pair]["M5"],
                df_d1=fixtures[pair]["D1"],
                target_date=target_date,
                symbol=pair,
                settings=settings,
            )

            assert (
                0 <= len(setups) <= _PER_CELL_CAP
            ), f"{target_date} {pair}: {len(setups)} setups outside [0, {_PER_CELL_CAP}]"

            for s in setups:
                # Schema sanity.
                assert s.symbol == pair
                assert s.direction in ("long", "short")
                assert s.daily_bias in ("bullish", "bearish")
                assert s.killzone in ("london", "ny")
                assert s.quality in ("A+", "A", "B")
                assert s.poi_type in ("FVG", "OrderBlock")
                assert s.risk_reward >= settings.MIN_RR
                # Bias / direction alignment.
                if s.daily_bias == "bullish":
                    assert s.direction == "long"
                else:
                    assert s.direction == "short"
                # Trade plan internal consistency.
                if s.direction == "long":
                    assert s.stop_loss < s.entry_price < s.take_profit
                else:
                    assert s.stop_loss > s.entry_price > s.take_profit
                # RR matches reported reward/risk within float slack.
                risk = abs(s.entry_price - s.stop_loss)
                reward = abs(s.take_profit - s.entry_price)
                assert risk > 0
                assert s.risk_reward == pytest.approx(reward / risk, rel=1e-6)

            rows.append(
                {
                    "date": target_date.isoformat(),
                    "pair": pair,
                    "setups": [
                        {
                            "timestamp_utc": s.timestamp_utc.isoformat(),
                            "killzone": s.killzone,
                            "direction": s.direction,
                            "quality": s.quality,
                            "poi_type": s.poi_type,
                            "entry_price": s.entry_price,
                            "stop_loss": s.stop_loss,
                            "tp_runner_price": s.tp_runner_price,
                            "tp_runner_rr": s.tp_runner_rr,
                            "tp1_price": s.tp1_price,
                            "tp1_rr": s.tp1_rr,
                            "swept_level_type": s.swept_level_type,
                            "swept_level_strength": s.swept_level_strength,
                            "target_level_type": s.target_level_type,
                            "confluences": s.confluences,
                        }
                        for s in setups
                    ],
                }
            )

    report, timestamp = _format_report(rows, dates)
    _RUNS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _RUNS_DIR / f"{timestamp}_setup_integration.md"
    out_path.write_text(report, encoding="utf-8")
    print(f"\nSetup integration report: {out_path.relative_to(_REPO_ROOT)}")
