"""Development convenience: dump marked liquidity + sweeps for one date/pair.

Usage:
    python scripts/print_liquidity_and_sweeps.py --date 2025-09-17 --pair NDX100

Reads the committed historical fixtures and prints, in order:

    1. Asian range (H/L + UTC times)
    2. PDH / PDL (with the source D1 date used)
    3. The last N swing levels (H4 ∩ H1 confluence promotion) with their
       strength tag
    4. Equal H / L clusters, if any
    5. Sweeps detected in the London killzone
    6. Sweeps detected in the NY killzone

This is NOT a production tool — it never talks to MT5 and never reflects
live market state. Visual chart rendering is Sprint 4.

The script loads ``config.settings`` if present; else ``settings.py.example``
with a stubbed ``config.secrets``. Same trick as
``calibration/run_swing_calibration.py``.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date as date_type
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.detection.liquidity import (  # noqa: E402
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
from src.detection.sweep import detect_sweeps  # noqa: E402

_FIXTURE_DIR = _REPO_ROOT / "tests" / "fixtures" / "historical"


def _load_settings() -> ModuleType:
    """Load ``config.settings``; fall back to ``settings.py.example``."""
    settings_real = _REPO_ROOT / "config" / "settings.py"
    settings_example = _REPO_ROOT / "config" / "settings.py.example"
    target = settings_real if settings_real.exists() else settings_example
    if not target.exists():
        raise SystemExit("ERROR: no config/settings.py or config/settings.py.example")

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

    loader = SourceFileLoader("config.settings", str(target))
    module = ModuleType(loader.name)
    module.__file__ = str(target)
    sys.modules["config.settings"] = module
    loader.exec_module(module)
    return module


def _section(title: str) -> None:
    print()
    print(f"== {title} ==")


def _fmt_price(p: float, pair: str) -> str:
    # FX needs more decimals than indices/metals.
    if pair in ("EURUSD", "GBPUSD"):
        return f"{p:.5f}"
    return f"{p:.3f}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--date",
        required=True,
        type=date_type.fromisoformat,
        help="Trading date in YYYY-MM-DD",
    )
    parser.add_argument(
        "--pair",
        required=True,
        choices=["XAUUSD", "NDX100", "EURUSD", "GBPUSD"],
        help="Watched pair",
    )
    args = parser.parse_args()

    settings = _load_settings()
    target_date = args.date
    pair = args.pair

    pair_data: dict[str, pd.DataFrame] = {}
    for tf in ("D1", "H4", "H1", "M5"):
        path = _FIXTURE_DIR / f"{pair}_{tf}.parquet"
        if not path.exists():
            print(f"ERROR: fixture missing: {path}", file=sys.stderr)
            return 2
        pair_data[tf] = pd.read_parquet(path)

    print(f"Liquidity + sweeps  —  pair={pair}  date={target_date.isoformat()}")
    print("Source: committed fixtures (NOT live MT5). " "All times displayed in UTC.")

    # 1. Asian range
    asian = mark_asian_range(pair_data["M5"], target_date, settings.SESSION_ASIA)
    _section("Asian range")
    if asian is None:
        print("(no Asia session data for this date — weekend or holiday)")
    else:
        print(
            f"  high  {_fmt_price(asian.asian_high, pair)} "
            f"@ {asian.asian_high_time_utc.isoformat()}"
        )
        print(
            f"  low   {_fmt_price(asian.asian_low, pair)} "
            f"@ {asian.asian_low_time_utc.isoformat()}"
        )

    # 2. PDH / PDL
    daily = mark_pdh_pdl(pair_data["D1"], target_date)
    _section("Previous day H / L")
    if daily is None:
        print("(no D1 candle within walkback window)")
    else:
        print(f"  pdh   {_fmt_price(daily.pdh, pair)} (source D1: {daily.source_date})")
        print(f"  pdl   {_fmt_price(daily.pdl, pair)} (source D1: {daily.source_date})")

    # Mark swing levels at the start of London — matches docs/01 §3
    # (bias / liquidity locked at killzone start).
    london_start_utc, london_end_utc = paris_session_to_utc(target_date, settings.KILLZONE_LONDON)
    ny_start_utc, ny_end_utc = paris_session_to_utc(target_date, settings.KILLZONE_NY)

    swings = mark_swing_levels(
        pair_data["H4"],
        pair_data["H1"],
        as_of_utc=london_start_utc,
        lookback_h4=settings.SWING_LOOKBACK_H4,
        lookback_h1=settings.SWING_LOOKBACK_H1,
        min_amplitude_atr_mult=settings.MIN_SWING_AMPLITUDE_ATR_MULT,
        n_swings=settings.SWING_LEVELS_LOOKBACK_COUNT,
        h4_h1_time_tolerance_h4_candles=settings.H4_H1_TIME_TOLERANCE_CANDLES_H4,
        h4_h1_price_tolerance_fraction=settings.H4_H1_PRICE_TOLERANCE_FRACTION,
    )
    _section(
        f"Swing levels (as of {london_start_utc.isoformat()}, "
        f"lookback={settings.SWING_LEVELS_LOOKBACK_COUNT})"
    )
    if not swings:
        print("(none)")
    else:
        for s in swings:
            print(
                f"  {s.time_utc.isoformat()}  {s.timeframe}  "
                f"{s.type:<5} {_fmt_price(s.price, pair):>12}  "
                f"strength={s.strength}  touches={s.touches}"
            )

    # 3. Equal H / L
    equals = find_equal_highs_lows(swings, settings.INSTRUMENT_CONFIG[pair]["equal_hl_tolerance"])
    _section("Equal H / L clusters")
    if not equals:
        print("(none)")
    else:
        for eq in equals:
            members = ", ".join(_fmt_price(m.price, pair) for m in eq.member_levels)
            print(
                f"  {eq.type:<5} avg={_fmt_price(eq.cluster_avg_price, pair)}  "
                f"members=[{members}]"
            )

    # Build unified MarkedLevel list once.
    levels = (
        asian_range_to_marked_levels(asian)
        + daily_levels_to_marked_levels(daily)
        + [swing_level_to_marked_level(s) for s in swings]
        + [equal_level_to_marked_level(e) for e in equals]
    )

    sweep_buffer = settings.INSTRUMENT_CONFIG[pair]["sweep_buffer"]

    for kz_name, (start_utc, end_utc) in (
        ("London killzone", (london_start_utc, london_end_utc)),
        ("NY killzone", (ny_start_utc, ny_end_utc)),
    ):
        sweeps = detect_sweeps(
            pair_data["M5"],
            levels,
            killzone_window_utc=(start_utc, end_utc),
            sweep_buffer=sweep_buffer,
            return_window_candles=settings.SWEEP_RETURN_WINDOW_CANDLES,
        )
        _section(
            f"{kz_name} sweeps ({start_utc.isoformat()} → {end_utc.isoformat()}, "
            f"{len(sweeps)} found)"
        )
        if not sweeps:
            print("(none)")
        else:
            for s in sweeps:
                print(
                    f"  {s.sweep_candle_time_utc.isoformat()}  "
                    f"{s.direction:<7} swept `{s.swept_level_type}` "
                    f"({s.swept_level_strength}) @ "
                    f"{_fmt_price(s.swept_level_price, pair)}  "
                    f"extreme={_fmt_price(s.sweep_extreme_price, pair)}  "
                    f"excursion={_fmt_price(s.excursion, pair)}  "
                    f"return={s.return_candle_time_utc.isoformat()}"
                )

    print()
    print(
        "Note: this script reads static fixtures, not live MT5. Visual "
        "chart rendering arrives in Sprint 4."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
