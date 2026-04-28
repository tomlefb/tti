"""Smoke tests for ``src.notification.chart_renderer``.

DO NOT pixel-compare images; visual QA is the operator's job. We only
verify that the function produces a valid, non-empty PNG and tolerates
short M5 frames without crashing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.detection.fvg import FVG
from src.detection.liquidity import MarkedLevel
from src.detection.mss import MSS
from src.detection.order_block import OrderBlock
from src.detection.setup import Setup
from src.detection.sweep import Sweep
from src.notification.chart_renderer import render_setup_chart


def _make_m5_frame(n_candles: int, start_time: datetime) -> pd.DataFrame:
    """Synthetic M5 OHLC. Trending up with mild noise; deterministic via seed."""
    rng = np.random.default_rng(seed=42)
    times = [start_time + timedelta(minutes=5 * i) for i in range(n_candles)]
    base = 4350 + np.cumsum(rng.normal(0, 1.5, n_candles))
    opens = base
    closes = base + rng.normal(0, 1.5, n_candles)
    highs = np.maximum(opens, closes) + np.abs(rng.normal(0, 1.5, n_candles))
    lows = np.minimum(opens, closes) - np.abs(rng.normal(0, 1.5, n_candles))
    return pd.DataFrame(
        {
            "time": pd.to_datetime(times, utc=True),
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
        }
    )


def _make_setup(
    *,
    timestamp_utc: datetime,
    direction: str = "short",
    has_runner: bool = False,
    poi_kind: str = "FVG",
) -> Setup:
    sweep = Sweep(
        direction="bearish" if direction == "short" else "bullish",
        swept_level_price=4380.0,
        swept_level_type="asian_high" if direction == "short" else "asian_low",
        swept_level_strength="structural",
        sweep_candle_time_utc=timestamp_utc - timedelta(minutes=5),
        sweep_extreme_price=4382.5 if direction == "short" else 4347.5,
        return_candle_time_utc=timestamp_utc - timedelta(minutes=5),
        excursion=2.5,
    )
    mss = MSS(
        direction="bearish" if direction == "short" else "bullish",
        sweep=sweep,
        broken_swing_time_utc=timestamp_utc,
        broken_swing_price=4365.0 if direction == "short" else 4345.0,
        mss_confirm_candle_time_utc=timestamp_utc,
        mss_confirm_candle_close=4364.0 if direction == "short" else 4346.0,
        displacement_body_ratio=2.1,
        displacement_candle_time_utc=timestamp_utc,
    )
    poi: FVG | OrderBlock
    if poi_kind == "FVG":
        poi = FVG(
            direction="bearish" if direction == "short" else "bullish",
            proximal=4360.0 if direction == "short" else 4350.0,
            distal=4366.0 if direction == "short" else 4344.0,
            c1_time_utc=timestamp_utc - timedelta(minutes=10),
            c2_time_utc=timestamp_utc - timedelta(minutes=5),
            c3_time_utc=timestamp_utc,
            size=6.0,
            size_atr_ratio=1.0,
        )
    else:
        poi = OrderBlock(
            direction="bearish" if direction == "short" else "bullish",
            proximal=4360.0 if direction == "short" else 4350.0,
            distal=4366.0 if direction == "short" else 4344.0,
            candle_time_utc=timestamp_utc - timedelta(minutes=10),
        )

    entry = 4360.0 if direction == "short" else 4350.0
    sl = 4375.0 if direction == "short" else 4335.0
    tp_runner = (
        4080.0
        if (direction == "short" and has_runner)
        else (4304.0 if direction == "short" else 4400.0)
    )
    if has_runner:
        # TP1 capped at 5R; risk = |entry - sl| = 15 ⇒ TP1 = entry ± 75.
        tp1 = entry - 75.0 if direction == "short" else entry + 75.0
        tp1_rr = 5.0
        tp_runner_rr = abs(tp_runner - entry) / 15.0
    else:
        tp1 = tp_runner
        tp1_rr = abs(tp_runner - entry) / 15.0
        tp_runner_rr = tp1_rr

    return Setup(
        timestamp_utc=timestamp_utc,
        symbol="XAUUSD",
        direction=direction,  # type: ignore[arg-type]
        daily_bias="bearish" if direction == "short" else "bullish",
        killzone="ny",
        swept_level_price=sweep.swept_level_price,
        swept_level_type=sweep.swept_level_type,
        swept_level_strength="structural",
        sweep=sweep,
        mss=mss,
        poi=poi,
        poi_type=poi_kind,  # type: ignore[arg-type]
        entry_price=entry,
        stop_loss=sl,
        target_level_type="swing_h1_low",
        tp_runner_price=tp_runner,
        tp_runner_rr=tp_runner_rr,
        tp1_price=tp1,
        tp1_rr=tp1_rr,
        quality="A",
        confluences=["FVG+OB"] + (["high_rr_runner"] if has_runner else []),
    )


def _basic_levels() -> list[MarkedLevel]:
    return [
        MarkedLevel(price=4380.0, type="high", label="asian_high", strength="structural"),
        MarkedLevel(price=4340.0, type="low", label="asian_low", strength="structural"),
        MarkedLevel(price=4395.0, type="high", label="pdh", strength="structural"),
        MarkedLevel(price=4320.0, type="low", label="pdl", strength="structural"),
    ]


def test_render_basic_setup_writes_valid_png(tmp_path: Path) -> None:
    """Renders a valid PNG that PIL can fully decode at sane dimensions.

    Regression: a previous implementation positioned right-margin text at
    a Timestamp x-coordinate on mplfinance's integer-positional axis,
    which matplotlib converted to a date-number ~20000. Combined with
    ``bbox_inches="tight"`` on savefig, this produced a 234543×741 PNG
    that macOS Preview and most viewers refused to render. PNG magic
    bytes alone don't catch it — we now also assert sane dimensions and
    PIL.load() success (which decodes pixels, not just headers).
    """
    from PIL import Image

    timestamp = datetime(2026, 1, 2, 16, 35, tzinfo=UTC)
    df = _make_m5_frame(120, timestamp - timedelta(minutes=5 * 80))
    out = tmp_path / "basic.png"

    written = render_setup_chart(
        setup=_make_setup(timestamp_utc=timestamp),
        df_m5=df,
        marked_levels=_basic_levels(),
        output_path=out,
    )
    assert written == out
    assert out.exists()
    # Real charts are well above 10 KB; threshold is just a sanity floor.
    assert out.stat().st_size > 10 * 1024
    # PNG magic bytes.
    with out.open("rb") as f:
        magic = f.read(8)
    assert magic == b"\x89PNG\r\n\x1a\n", f"not a valid PNG: {magic!r}"

    # PIL fully decodes the image — verify() only validates headers, but
    # load() actually reads pixel data and would surface format issues.
    # We also pin sane dimensions: anything wider than ~30 inches at 100
    # dpi (3000 px) suggests off-axis content pulled by ``bbox_inches="tight"``.
    img = Image.open(out)
    img.load()
    assert img.format == "PNG"
    width, height = img.size
    assert width <= 3000, f"PNG width {width}px is unreasonable — viewers will refuse it"
    assert height <= 2000, f"PNG height {height}px is unreasonable"


def test_render_handles_short_with_runner(tmp_path: Path) -> None:
    """When tp_runner_rr ≠ tp1_rr, TP_R line renders without crashing."""
    timestamp = datetime(2026, 1, 2, 16, 35, tzinfo=UTC)
    df = _make_m5_frame(120, timestamp - timedelta(minutes=5 * 80))
    out = tmp_path / "with_runner.png"
    setup = _make_setup(timestamp_utc=timestamp, has_runner=True)
    assert setup.tp_runner_rr != setup.tp1_rr  # precondition

    render_setup_chart(
        setup=setup,
        df_m5=df,
        marked_levels=_basic_levels(),
        output_path=out,
    )
    assert out.exists() and out.stat().st_size > 10 * 1024


def test_render_handles_few_candles_without_crashing(tmp_path: Path) -> None:
    """30 candles ≪ the 80 lookback default — must not crash."""
    timestamp = datetime(2026, 1, 2, 16, 35, tzinfo=UTC)
    df = _make_m5_frame(30, timestamp - timedelta(minutes=5 * 25))
    out = tmp_path / "short_frame.png"

    render_setup_chart(
        setup=_make_setup(timestamp_utc=timestamp, poi_kind="OrderBlock"),
        df_m5=df,
        marked_levels=_basic_levels(),
        output_path=out,
    )
    assert out.exists() and out.stat().st_size > 10 * 1024


def test_render_creates_output_directory(tmp_path: Path) -> None:
    """Parent dir is created if missing — operator's runtime_charts/ may not exist on first run."""
    timestamp = datetime(2026, 1, 2, 16, 35, tzinfo=UTC)
    df = _make_m5_frame(80, timestamp - timedelta(minutes=5 * 60))
    nested = tmp_path / "deep" / "nested" / "out.png"

    render_setup_chart(
        setup=_make_setup(timestamp_utc=timestamp),
        df_m5=df,
        marked_levels=[],
        output_path=nested,
    )
    assert nested.exists()


def test_render_empty_frame_raises(tmp_path: Path) -> None:
    """Calling with an empty frame is a programmer error — fail loud, not silent."""
    timestamp = datetime(2026, 1, 2, 16, 35, tzinfo=UTC)
    df = pd.DataFrame({"time": [], "open": [], "high": [], "low": [], "close": []})
    df["time"] = pd.to_datetime(df["time"], utc=True)
    out = tmp_path / "empty.png"
    with pytest.raises(ValueError):
        render_setup_chart(
            setup=_make_setup(timestamp_utc=timestamp),
            df_m5=df,
            marked_levels=[],
            output_path=out,
        )
