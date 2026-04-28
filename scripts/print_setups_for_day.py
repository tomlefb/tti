"""Development convenience: print all setup candidates for one date / pair.

Usage:
    python scripts/print_setups_for_day.py --date 2025-09-17 --pair NDX100

Reads the committed historical fixtures and prints, in order:

    1. Daily bias (per killzone — bias is locked at killzone start).
    2. For each killzone, every Setup candidate with full breakdown:
       entry, SL, TP, RR, quality, confluences, swept level, target.

This is NOT a production tool — it never talks to MT5. Visual chart
rendering arrives in Sprint 4.

The script loads ``config.settings`` if present; else
``settings.py.example`` with a stubbed ``config.secrets``. Mirrors
``print_liquidity_and_sweeps.py``.
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

from src.detection.bias import compute_daily_bias  # noqa: E402
from src.detection.liquidity import paris_session_to_utc  # noqa: E402
from src.detection.setup import build_setup_candidates  # noqa: E402

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
    if pair in ("EURUSD", "GBPUSD"):
        return f"{p:.5f}"
    return f"{p:.3f}"


def _slice_until(df: pd.DataFrame, cutoff_utc) -> pd.DataFrame:
    if len(df) == 0:
        return df
    times = pd.to_datetime(df["time"], utc=True)
    return df.loc[times < cutoff_utc].reset_index(drop=True)


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

    print(f"Setup candidates  —  pair={pair}  date={target_date.isoformat()}")
    print("Source: committed fixtures (NOT live MT5). All times displayed in UTC.")

    # Per-killzone bias diagnostic — same slicing the orchestrator uses.
    _section("Daily bias (per killzone, locked at killzone start)")
    for kz_name, kz in (
        ("london", settings.KILLZONE_LONDON),
        ("ny", settings.KILLZONE_NY),
    ):
        kz_start, _ = paris_session_to_utc(target_date, kz)
        sl_h4 = _slice_until(pair_data["H4"], kz_start)
        sl_h1 = _slice_until(pair_data["H1"], kz_start)
        bias = compute_daily_bias(
            sl_h4,
            sl_h1,
            swing_lookback_h4=settings.SWING_LOOKBACK_H4,
            swing_lookback_h1=settings.SWING_LOOKBACK_H1,
            min_amplitude_atr_mult_h4=settings.MIN_SWING_AMPLITUDE_ATR_MULT_H4,
            min_amplitude_atr_mult_h1=settings.MIN_SWING_AMPLITUDE_ATR_MULT_H1,
            bias_swing_count=settings.BIAS_SWING_COUNT,
            require_h1_confirmation=settings.BIAS_REQUIRE_H1_CONFIRMATION,
        )
        print(f"  {kz_name:<6} (cutoff {kz_start.isoformat()}): {bias}")

    setups = build_setup_candidates(
        df_h4=pair_data["H4"],
        df_h1=pair_data["H1"],
        df_m5=pair_data["M5"],
        df_d1=pair_data["D1"],
        target_date=target_date,
        symbol=pair,
        settings=settings,
    )

    by_kz: dict[str, list] = {"london": [], "ny": []}
    for s in setups:
        by_kz[s.killzone].append(s)

    for kz_name in ("london", "ny"):
        kz_setups = by_kz[kz_name]
        _section(f"{kz_name.upper()} killzone setups ({len(kz_setups)} found)")
        if not kz_setups:
            print("(none)")
            continue
        for s in kz_setups:
            print(f"  [{s.quality}] {s.timestamp_utc.isoformat()}  {s.direction.upper()}")
            print(f"      bias              : {s.daily_bias}")
            print(
                f"      swept             : `{s.swept_level_type}` "
                f"({s.swept_level_strength}) @ {_fmt_price(s.swept_level_price, pair)}"
            )
            print(
                f"      sweep extreme     : "
                f"{_fmt_price(s.sweep_extreme_price if hasattr(s, 'sweep_extreme_price') else s.sweep.sweep_extreme_price, pair)}"
            )
            print(f"      MSS broken swing  : {_fmt_price(s.mss.broken_swing_price, pair)}")
            print(
                f"      MSS displacement  : ratio={s.mss.displacement_body_ratio:.2f} "
                f"@ {s.mss.displacement_candle_time_utc.isoformat()}"
            )
            print(
                f"      POI ({s.poi_type:<10}): proximal={_fmt_price(s.poi.proximal, pair)} "
                f"distal={_fmt_price(s.poi.distal, pair)}"
            )
            print(f"      entry             : {_fmt_price(s.entry_price, pair)}")
            print(f"      stop loss         : {_fmt_price(s.stop_loss, pair)}")
            print(
                f"      TP1               : {_fmt_price(s.tp1_price, pair)} " f"(RR {s.tp1_rr:.2f})"
            )
            # Only show TP_runner separately when it differs from TP1
            # (i.e. the runner exceeds PARTIAL_TP_RR_TARGET).
            if s.tp_runner_price != s.tp1_price:
                print(
                    f"      TP_runner         : {_fmt_price(s.tp_runner_price, pair)} "
                    f"(RR {s.tp_runner_rr:.2f})  target=`{s.target_level_type}`"
                )
            else:
                print(f"      target            : `{s.target_level_type}`")
            print(f"      confluences       : {', '.join(s.confluences) or '(none)'}")
            print()

    print()
    print(
        "Note: this script reads static fixtures, not live MT5. Visual "
        "chart rendering arrives in Sprint 4."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
