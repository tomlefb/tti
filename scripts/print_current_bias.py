"""Development convenience: print the daily bias for each watched pair.

Reads the committed historical fixtures (``tests/fixtures/historical/``) and
treats the **most recent candle** in each fixture as ``"now"``. Computes:

- ``H4_BIAS`` from the H4 fixture
- ``H1_BIAS`` from the H1 fixture
- ``DAILY_BIAS`` = combined H4+H1 agreement

This is NOT a production tool — it never talks to MT5 and never reflects
live market state. Its only purpose is a quick eyeball check during
detector development.

Run from the repo root:

    python scripts/print_current_bias.py
"""

from __future__ import annotations

import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.detection.bias import compute_daily_bias, compute_timeframe_bias  # noqa: E402
from src.detection.swings import find_swings  # noqa: E402

_FIXTURE_DIR = _REPO_ROOT / "tests" / "fixtures" / "historical"


def _load_settings() -> ModuleType:
    """Load ``config.settings`` if present; else fall back to ``settings.py.example``.

    The example file imports ``config.secrets`` (gitignored), so we inject
    a stub for that import path before exec'ing the example. Same trick as
    ``calibration/run_swing_calibration.py`` — keep them in sync.
    """
    settings_real = _REPO_ROOT / "config" / "settings.py"
    settings_example = _REPO_ROOT / "config" / "settings.py.example"
    target = settings_real if settings_real.exists() else settings_example
    if not target.exists():
        raise SystemExit("ERROR: no config/settings.py or config/settings.py.example found")

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


def main() -> int:
    settings = _load_settings()

    rows: list[tuple[str, str, str, str, str]] = []
    for pair in settings.WATCHED_PAIRS:
        h4_path = _FIXTURE_DIR / f"{pair}_H4.parquet"
        h1_path = _FIXTURE_DIR / f"{pair}_H1.parquet"
        if not h4_path.exists() or not h1_path.exists():
            rows.append((pair, "—", "—", "—", "fixture missing"))
            continue

        df_h4 = pd.read_parquet(h4_path)
        df_h1 = pd.read_parquet(h1_path)

        swings_h4 = find_swings(
            df_h4,
            lookback=settings.SWING_LOOKBACK_H4,
            min_amplitude_atr_mult=settings.MIN_SWING_AMPLITUDE_ATR_MULT_H4,
        )
        swings_h1 = find_swings(
            df_h1,
            lookback=settings.SWING_LOOKBACK_H1,
            min_amplitude_atr_mult=settings.MIN_SWING_AMPLITUDE_ATR_MULT_H1,
        )

        bias_h4 = compute_timeframe_bias(swings_h4, settings.BIAS_SWING_COUNT)
        bias_h1 = compute_timeframe_bias(swings_h1, settings.BIAS_SWING_COUNT)
        bias_daily = compute_daily_bias(
            df_h4=df_h4,
            df_h1=df_h1,
            swing_lookback_h4=settings.SWING_LOOKBACK_H4,
            swing_lookback_h1=settings.SWING_LOOKBACK_H1,
            min_amplitude_atr_mult_h4=settings.MIN_SWING_AMPLITUDE_ATR_MULT_H4,
            min_amplitude_atr_mult_h1=settings.MIN_SWING_AMPLITUDE_ATR_MULT_H1,
            bias_swing_count=settings.BIAS_SWING_COUNT,
            require_h1_confirmation=settings.BIAS_REQUIRE_H1_CONFIRMATION,
        )

        as_of = max(df_h4["time"].max(), df_h1["time"].max()).isoformat()
        rows.append((pair, bias_h4, bias_h1, bias_daily, as_of))

    # Render table.
    header = ("PAIR", "H4_BIAS", "H1_BIAS", "DAILY_BIAS", "AS_OF (UTC)")
    widths = [max(len(header[i]), max(len(r[i]) for r in rows)) for i in range(len(header))]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*header))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print(fmt.format(*r))

    print()
    print(
        "Note: 'as of' is the latest candle in the fixture, not real time. "
        "This is a development tool, not production output."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
