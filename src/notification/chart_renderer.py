"""Annotated M5 chart renderer for Telegram notifications.

Produces a single PNG that the operator scans on their phone:

- 80 M5 candles before MSS confirm + 10 candles after (configurable).
- A curated set of HTF liquidity lines: the swept level (always) and
  target level (always) labelled prominently, plus Asian H/L and PDH/PDL
  if in y-range, plus the 2 nearest equal_high/low to entry. All other
  swing levels are dropped — they would clutter the operator's read.
- Highlighted candle backgrounds for the sweep candle (yellow, 1 candle)
  and the MSS confirm candle (blue, 2 candles wide).
- Translucent rectangle for the POI zone (green for long, red for short)
  with a 1-px solid border, spanning POI creation → chart end.
- Solid trade-plan lines (Entry / SL / TP1) plus a dashed TP_runner line
  when the runner exceeds the partial cap. Trade-plan labels are at
  zorder 10 with 12-pt bold text — they always win over level labels.
- Right-margin labels are deconflicted by a 1D collision-avoidance pass:
  trade-plan labels are anchored at price; level labels bump upward when
  they would collide with anything already placed.

Pure-ish: only side effect is writing the PNG to disk. Returns the file
path. No reliance on display backends — uses ``matplotlib`` Agg.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # noqa: E402  — headless, must precede pyplot import
import matplotlib.patches as mpatches  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import mplfinance as mpf  # noqa: E402
import pandas as pd  # noqa: E402

from src.detection.liquidity import MarkedLevel  # noqa: E402
from src.detection.setup import Setup  # noqa: E402

logger = logging.getLogger(__name__)

# Per-level visual spec (line style, color, label prefix).
_LEVEL_STYLE: dict[str, dict] = {
    "asian_high": {"linestyle": "-", "color": "black", "label": "Asian H"},
    "asian_low": {"linestyle": "-", "color": "black", "label": "Asian L"},
    "pdh": {"linestyle": "--", "color": "black", "label": "PDH"},
    "pdl": {"linestyle": "--", "color": "black", "label": "PDL"},
    "equal_high": {"linestyle": "--", "color": "darkorange", "label": "EQ"},
    "equal_low": {"linestyle": "--", "color": "darkorange", "label": "EQ"},
}

# Special line styles for the swept and target levels — drawn from the
# Setup. SWEPT uses dark-orange (#FF8C00) rather than red so the operator
# doesn't confuse it with the SL line at a glance. TARGET is rendered
# only when it doesn't coincide with TP1/TP_R (see ``_curate_displayed_levels``).
_SWEPT_STYLE = {"linestyle": "-", "color": "#FF8C00", "linewidth": 1.4, "alpha": 0.9}
_TARGET_STYLE = {"linestyle": "-", "color": "seagreen", "linewidth": 1.4, "alpha": 0.9}

# Relative tolerance for TARGET ≈ TP dedup. 0.01% covers float-rounding
# slack while still flagging genuinely-distinct prices.
_TARGET_TP_DEDUP_FRACTION = 1e-4

# Trade-plan label/line styling. zorder=10 puts trade-plan layer above
# level lines (zorder=2), POI rectangle (zorder=1), and candle highlights
# (zorder=0). Level-label text is at zorder=5.
_TRADE_PLAN_LINE_ZORDER = 10
_TRADE_PLAN_LABEL_ZORDER = 11
_LEVEL_LABEL_ZORDER = 5

# Vertical separation between right-margin labels, expressed as a fraction
# of the visible y-range. 0.5% spaces them comfortably without crowding;
# trade-plan labels stay anchored at price and force level labels to bump.
_MIN_LABEL_VERTICAL_SEPARATION_FRACTION = 0.005

# y-axis margin above/below the candle / trade-plan envelope. 0.2% on each
# side keeps the candles tall without clipping wicks.
_Y_MARGIN_FRACTION = 0.002

# Number of nearest equal_high/low levels to render around the entry.
_NEAREST_EQ_LEVELS = 2


def render_setup_chart(
    setup: Setup,
    df_m5: pd.DataFrame,
    marked_levels: list[MarkedLevel],
    output_path: Path,
    *,
    lookback_candles: int = 80,
    lookforward_candles: int = 10,
) -> Path:
    """Render an annotated M5 chart and write it to ``output_path``.

    Args:
        setup: the detected setup whose plan is being annotated.
        df_m5: M5 OHLC frame for the same instrument. Must contain
            ``time, open, high, low, close``; ``time`` tz-aware UTC.
        marked_levels: HTF liquidity levels to overlay. Most are filtered
            out — see ``_curate_displayed_levels``.
        output_path: target path for the PNG. Parent directory is
            created if missing. Existing file is overwritten.
        lookback_candles: how many M5 candles before the MSS confirm
            to include in the window. Defaults to 80 (~6.7h).
        lookforward_candles: how many M5 candles after the MSS confirm
            to include. Defaults to 10. If the frame ends earlier
            (live or short fixture) only the available candles are used.

    Returns:
        The ``output_path`` actually written.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    df = _slice_chart_window(
        df_m5=df_m5,
        center_time_utc=setup.timestamp_utc,
        lookback=lookback_candles,
        lookforward=lookforward_candles,
    )
    if len(df) == 0:
        raise ValueError(
            f"No M5 candles around setup {setup.symbol} {setup.timestamp_utc.isoformat()}"
        )

    plot_df = df.set_index("time")[["open", "high", "low", "close"]].copy()
    # mplfinance expects naive datetimes for the DatetimeIndex; convert
    # explicitly so timezone metadata isn't lost downstream silently.
    plot_df.index = plot_df.index.tz_convert("UTC").tz_localize(None)
    plot_df.columns = ["Open", "High", "Low", "Close"]

    # Tighter y-axis: candles + trade plan only. Asian/PDH/PDL outside
    # this band are dropped (they'd be off-screen anyway). This avoids
    # the v2 issue where Asian L compressed the candles vertically.
    y_min, y_max = _compute_y_range(plot_df, setup)

    fig, axes = mpf.plot(
        plot_df,
        type="candle",
        style="charles",
        returnfig=True,
        figsize=(12, 8),
        tight_layout=True,
        warn_too_much_data=10000,
        datetime_format="%m-%d %H:%M",
        xrotation=0,
        ylim=(y_min, y_max),
    )
    ax = axes[0]
    ax.set_ylim(y_min, y_max)  # Belt-and-braces: some mpf versions ignore ylim kwarg.

    # Layer order (painted bottom → top, by zorder):
    #   0  candle highlights (sweep yellow, MSS blue)
    #   1  POI rectangle
    #   2  level lines (curated)
    #   5  level labels (collision-aware)
    #  10  trade-plan lines (Entry/SL/TP1/TP_R)
    #  11  trade-plan labels (anchored at price, never moved)
    _highlight_candle(ax, plot_df, setup.sweep.sweep_candle_time_utc, color="yellow", widen=0)
    _highlight_candle(
        ax, plot_df, setup.mss.mss_confirm_candle_time_utc, color="cornflowerblue", widen=1
    )
    _draw_poi(ax, setup, plot_df)

    # Compute curated levels first so the label layer can deconflict them
    # against the trade-plan labels in a single pass.
    curated = _curate_displayed_levels(setup, marked_levels, y_min, y_max)
    _draw_level_lines(ax, curated)
    _draw_trade_plan_lines(ax, setup, plot_df)
    _draw_all_labels(ax, setup, plot_df, curated, y_min, y_max)

    # Title — ASCII-only quality tag so matplotlib's default font (no emoji
    # support) doesn't render an empty box. Emoji stays in the Telegram
    # caption where the rendering font is up to the operator's device.
    title = (
        f"{setup.symbol} {setup.direction.upper()} "
        f"[{setup.quality}] — "
        f"{setup.timestamp_utc.strftime('%Y-%m-%d')} {setup.killzone.upper()}"
    )
    fig.suptitle(title, fontsize=14)

    fig.savefig(output_path, dpi=100, bbox_inches="tight")
    plt.close(fig)
    return output_path


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _slice_chart_window(
    *,
    df_m5: pd.DataFrame,
    center_time_utc: datetime,
    lookback: int,
    lookforward: int,
) -> pd.DataFrame:
    """Return the slice of ``df_m5`` ``[center − lookback, center + lookforward]``.

    Handles short fixtures gracefully (e.g. tests feed only 30 candles): if
    either side is exhausted, the slice is just whatever candles exist.
    """
    if len(df_m5) == 0:
        return df_m5

    times = pd.to_datetime(df_m5["time"], utc=True)
    idx = (
        int((times >= center_time_utc).idxmax()) if (times >= center_time_utc).any() else len(df_m5)
    )
    start = max(0, idx - lookback)
    end = min(len(df_m5), idx + lookforward + 1)
    return df_m5.iloc[start:end].reset_index(drop=True)


def _compute_y_range(plot_df: pd.DataFrame, setup: Setup) -> tuple[float, float]:
    """Tightest y-range that contains both the candles and the trade plan.

    Asian / PDH / PDL levels are NOT included — if they fall outside this
    band, they're simply not drawn. This keeps the candles visually
    prominent rather than compressed by a far-away anchor.
    """
    candle_low = float(plot_df["Low"].min())
    candle_high = float(plot_df["High"].max())
    trade_low = min(setup.entry_price, setup.stop_loss, setup.tp1_price)
    trade_high = max(setup.entry_price, setup.stop_loss, setup.tp1_price)

    raw_low = min(candle_low, trade_low)
    raw_high = max(candle_high, trade_high)
    span = raw_high - raw_low
    margin = max(span * _Y_MARGIN_FRACTION, 1e-6)
    return raw_low - margin, raw_high + margin


def _highlight_candle(
    ax,
    plot_df: pd.DataFrame,
    t_utc: datetime,
    color: str,
    widen: int = 0,
) -> None:
    """Paint a translucent vertical band for the candle at ``t_utc``.

    ``widen`` extends the band by N candles on each side (so widen=1 →
    spans 3 candles total — used for the MSS confirm candle to make it
    visually prominent without being wider than visually meaningful).
    """
    naive_t = (
        pd.Timestamp(t_utc).tz_convert("UTC").tz_localize(None)
        if pd.Timestamp(t_utc).tzinfo
        else pd.Timestamp(t_utc)
    )
    if naive_t not in plot_df.index:
        # Closest neighbor — sweep/MSS times are M5-aligned but a fixture
        # may shift seconds; tolerate ±2 minutes.
        deltas = (plot_df.index - naive_t).to_series().abs()
        if deltas.min() > pd.Timedelta(minutes=2):
            return
        naive_t = plot_df.index[deltas.argmin()]
    pos = plot_df.index.get_loc(naive_t)
    half = 0.4 + widen * 0.5
    ax.axvspan(pos - half, pos + half, color=color, alpha=0.3, zorder=0)


def _draw_poi(ax, setup: Setup, plot_df: pd.DataFrame) -> None:
    """Translucent rectangle for the POI zone, with a crisp 1-px border.

    Spans from POI creation time to chart-end. The fill at alpha=0.25
    keeps candles visible; the solid border (alpha=0.9) makes the zone
    boundaries unambiguous against busy candle wicks.
    """
    poi = setup.poi
    poi_color = "green" if setup.direction == "long" else "red"

    poi_time_utc = poi.c2_time_utc if hasattr(poi, "c2_time_utc") else poi.candle_time_utc

    naive_t = (
        pd.Timestamp(poi_time_utc).tz_convert("UTC").tz_localize(None)
        if pd.Timestamp(poi_time_utc).tzinfo
        else pd.Timestamp(poi_time_utc)
    )
    if naive_t < plot_df.index[0]:
        x_start = 0
    elif naive_t > plot_df.index[-1]:
        return
    else:
        deltas = (plot_df.index - naive_t).to_series().abs()
        x_start = int(deltas.argmin())
    x_end = len(plot_df) - 1

    poi_low = min(poi.proximal, poi.distal)
    poi_high = max(poi.proximal, poi.distal)

    # Fill (translucent) + outline (opaque) drawn as separate patches so
    # alpha doesn't drag the border down with the fill.
    fill = mpatches.Rectangle(
        (x_start - 0.5, poi_low),
        x_end - x_start + 1,
        poi_high - poi_low,
        linewidth=0,
        facecolor=poi_color,
        alpha=0.25,
        zorder=1,
    )
    border = mpatches.Rectangle(
        (x_start - 0.5, poi_low),
        x_end - x_start + 1,
        poi_high - poi_low,
        linewidth=1.0,
        edgecolor=poi_color,
        facecolor="none",
        alpha=0.9,
        zorder=1,
    )
    ax.add_patch(fill)
    ax.add_patch(border)


# ---------------------------------------------------------------------------
# Curated levels + label collision avoidance
# ---------------------------------------------------------------------------


class _LineSpec:
    """One level line + its right-margin label.

    Holds the y-coord (price), label text, and matplotlib style. The label
    layer (``_draw_all_labels``) reads the `y` and may shift it via the
    collision-avoidance pass; the line itself is drawn unconditionally
    at `price`.
    """

    __slots__ = ("price", "label", "linestyle", "color", "linewidth", "alpha")

    def __init__(
        self,
        *,
        price: float,
        label: str,
        linestyle: str,
        color: str,
        linewidth: float = 1.0,
        alpha: float = 0.7,
    ):
        self.price = price
        self.label = label
        self.linestyle = linestyle
        self.color = color
        self.linewidth = linewidth
        self.alpha = alpha


def _curate_displayed_levels(
    setup: Setup,
    marked_levels: list[MarkedLevel],
    y_min: float,
    y_max: float,
) -> list[_LineSpec]:
    """Pick the small set of levels the operator actually needs to see.

    Always shown:
        - SWEPT (the level the setup just took out)
        - TARGET (the structural take-profit level)

    Conditionally shown (only if inside [y_min, y_max] AND distinct from
    swept/target):
        - Asian High, Asian Low
        - PDH, PDL
        - 2 nearest equal_high / equal_low to entry

    All other swing_h1 / swing_h4 levels are deliberately dropped — the
    pipeline produces them as candidate liquidity but they clutter the
    operator's read on the chart (Issue 1 from sprint 4 visual review).
    """
    items: list[_LineSpec] = []

    items.append(
        _LineSpec(
            price=setup.swept_level_price,
            label=f"SWEPT: {setup.swept_level_type} @ {setup.swept_level_price}",
            linestyle=_SWEPT_STYLE["linestyle"],
            color=_SWEPT_STYLE["color"],
            linewidth=_SWEPT_STYLE["linewidth"],
            alpha=_SWEPT_STYLE["alpha"],
        )
    )

    # Render TARGET only when it doesn't coincide with TP1/TP_R. In Sprint
    # 4 ``setup.tp_runner_price`` is set from the chosen target level, so
    # the TARGET line almost always overlaps the trade-plan TP line — and
    # the trade-plan label already names the target via its RR. Suppress
    # the duplicate; keep the line only if a future detector decouples
    # target-of-record from the actual TP price.
    target_price = setup.tp_runner_price
    target_close_to_tp1 = (
        abs(target_price - setup.tp1_price) <= abs(setup.tp1_price) * _TARGET_TP_DEDUP_FRACTION
    )
    target_close_to_runner = (
        abs(target_price - setup.tp_runner_price)
        <= abs(setup.tp_runner_price) * _TARGET_TP_DEDUP_FRACTION
    )
    if not (target_close_to_tp1 or target_close_to_runner):
        items.append(
            _LineSpec(
                price=target_price,
                label=f"TARGET: {setup.target_level_type} @ {target_price}",
                linestyle=_TARGET_STYLE["linestyle"],
                color=_TARGET_STYLE["color"],
                linewidth=_TARGET_STYLE["linewidth"],
                alpha=_TARGET_STYLE["alpha"],
            )
        )

    swept_target_keys = {
        (round(setup.swept_level_price, 6), setup.swept_level_type),
        (round(setup.tp_runner_price, 6), setup.target_level_type),
    }

    def _in_range(p: float) -> bool:
        return y_min <= p <= y_max

    def _is_swept_or_target(lv: MarkedLevel) -> bool:
        return (round(lv.price, 6), lv.label) in swept_target_keys

    for lv in marked_levels:
        if lv.label not in ("asian_high", "asian_low", "pdh", "pdl"):
            continue
        if not _in_range(lv.price) or _is_swept_or_target(lv):
            continue
        style = _LEVEL_STYLE[lv.label]
        items.append(
            _LineSpec(
                price=lv.price,
                label=f"{style['label']} @ {lv.price}",
                linestyle=style["linestyle"],
                color=style["color"],
            )
        )

    eq_levels = [
        lv
        for lv in marked_levels
        if lv.label in ("equal_high", "equal_low")
        and _in_range(lv.price)
        and not _is_swept_or_target(lv)
    ]
    eq_levels.sort(key=lambda lv: abs(lv.price - setup.entry_price))
    for lv in eq_levels[:_NEAREST_EQ_LEVELS]:
        style = _LEVEL_STYLE[lv.label]
        items.append(
            _LineSpec(
                price=lv.price,
                label=f"{style['label']} @ {lv.price}",
                linestyle=style["linestyle"],
                color=style["color"],
                linewidth=1.6,
            )
        )

    return items


def _draw_level_lines(ax, curated: list[_LineSpec]) -> None:
    """Draw each curated level's horizontal line. Labels are drawn separately
    so the collision-avoidance pass can deconflict them against trade-plan
    labels in a single sort."""
    for spec in curated:
        ax.axhline(
            spec.price,
            linestyle=spec.linestyle,
            color=spec.color,
            linewidth=spec.linewidth,
            alpha=spec.alpha,
            zorder=2,
        )


def _draw_trade_plan_lines(ax, setup: Setup, plot_df: pd.DataFrame) -> None:
    """Solid lines for entry/SL/TP1, dashed for TP_runner when distinct.

    Lines only — labels are drawn by ``_draw_all_labels`` together with the
    curated level labels so the collision-avoidance pass can deconflict
    them against each other in a single sweep.
    """
    ax.axhline(
        setup.entry_price,
        color="blue",
        linestyle="-",
        linewidth=1.5,
        zorder=_TRADE_PLAN_LINE_ZORDER,
    )
    ax.axhline(
        setup.stop_loss, color="red", linestyle="-", linewidth=1.5, zorder=_TRADE_PLAN_LINE_ZORDER
    )
    ax.axhline(
        setup.tp1_price, color="green", linestyle="-", linewidth=1.5, zorder=_TRADE_PLAN_LINE_ZORDER
    )
    if setup.tp_runner_rr != setup.tp1_rr:
        ax.axhline(
            setup.tp_runner_price,
            color="lightgreen",
            linestyle="--",
            linewidth=1.5,
            zorder=_TRADE_PLAN_LINE_ZORDER,
        )


def _draw_all_labels(
    ax,
    setup: Setup,
    plot_df: pd.DataFrame,
    curated: list[_LineSpec],
    y_min: float,
    y_max: float,
) -> None:
    """Place all right-margin labels with a 1D collision-avoidance pass.

    Trade-plan labels (Entry / SL / TP1 / TP_R) are anchored at price and
    drawn at zorder 11 with 12-pt bold text. Level labels are movable and
    drawn at zorder 5 with 9-pt regular text — when they would collide
    with anything already placed (other levels or trade-plan labels),
    they bump upward by ``min_sep``.
    """
    label_x = len(plot_df) - 1 + 0.5
    min_sep = (y_max - y_min) * _MIN_LABEL_VERTICAL_SEPARATION_FRACTION

    # Build the trade-plan label set first. These are anchored — they
    # never move — so they're the seed of the "occupied" y list.
    fixed_items: list[tuple[float, str, str]] = [
        (setup.entry_price, f"  Entry @ {setup.entry_price}", "blue"),
        (setup.stop_loss, f"  SL @ {setup.stop_loss}", "red"),
        (setup.tp1_price, f"  TP1 @ {setup.tp1_price} (RR {setup.tp1_rr:.2f})", "green"),
    ]
    if setup.tp_runner_rr != setup.tp1_rr:
        fixed_items.append(
            (
                setup.tp_runner_price,
                f"  TP_R @ {setup.tp_runner_price} (RR {setup.tp_runner_rr:.2f})",
                "darkgreen",
            )
        )

    placed_ys: list[float] = []
    for y, text, color in fixed_items:
        ax.text(
            label_x,
            y,
            text,
            color=color,
            fontsize=12,
            fontweight="bold",
            verticalalignment="center",
            zorder=_TRADE_PLAN_LABEL_ZORDER,
        )
        placed_ys.append(y)

    # Movable level labels — sort by ascending price and bump if close.
    # Float bias: bump by min_sep * 1.001 so the next iteration's distance
    # check (`abs(p - target_y) < min_sep`) reliably passes despite float
    # rounding. The naive `+ min_sep` produced a fixed-point loop where
    # subtraction rounded back below the threshold and the loop never exited.
    # Hard iteration cap is a belt-and-braces guard against pathological inputs.
    movables = sorted(curated, key=lambda s: s.price)
    bump_step = min_sep * 1.001 if min_sep > 0 else 1e-3
    for spec in movables:
        target_y = spec.price
        for _ in range(50):
            collision = next((p for p in placed_ys if abs(p - target_y) < min_sep), None)
            if collision is None:
                break
            target_y = collision + bump_step
        # If pushed past y_max, just clamp — operator can still read it.
        if target_y > y_max:
            target_y = y_max
        ax.text(
            label_x,
            target_y,
            f"  {spec.label}",
            color=spec.color,
            fontsize=9,
            verticalalignment="center",
            zorder=_LEVEL_LABEL_ZORDER,
        )
        placed_ys.append(target_y)
