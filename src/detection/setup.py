"""Setup orchestrator — wires bias + liquidity + sweep + MSS + POI → Setup.

This module owns the Sprint 3 pipeline. Each sub-detector is implemented
in its own module; this file only orchestrates and applies the
heuristic glue defined in docs/01 §5 and docs/07 §1.3.

Heuristics consolidated here (each documented inline):

- Bias filter on sweeps: bullish bias keeps only bullish-direction
  sweeps (sweeps of lows); bearish bias keeps only bearish-direction
  sweeps. Strict alignment per docs/01 §5 Step 1.
- POI priority FVG > OrderBlock. The orchestrator also computes the
  ``has_alternative_ob_when_fvg`` signal that the grader uses to label
  ``FVG+OB`` confluence.
- TP selection: nearest opposing-liquidity level that yields RR >=
  ``MIN_RR``. Iterate from nearest to furthest. Alternatives:
  "always target the strongest opposing level" or "always target the
  furthest one for max RR". We pick "nearest yielding ≥ MIN_RR" because
  it maximises hit-rate per the operator's strategy (docs/01 §5 Step 4).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Protocol, overload

import pandas as pd

from .bias import compute_daily_bias
from .fvg import FVG, detect_fvgs_in_window
from .grading import Grade, SetupComponents, grade_setup
from .liquidity import (
    AsianRange,
    DailyLevels,
    MarkedLevel,
    SwingLevel,
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
from .mss import MSS, detect_mss
from .order_block import OrderBlock, detect_order_block
from .sweep import Sweep, detect_sweeps

logger = logging.getLogger(__name__)


class SetupSettings(Protocol):
    """Static-typing surface for the configuration object passed to
    ``build_setup_candidates``.

    Any object exposing these attributes (the real ``config.settings``
    module, a ``SimpleNamespace``, a dataclass, …) satisfies the protocol
    structurally — no runtime registration, no inheritance. The Protocol
    is purely a documentation + type-checker aid.

    Tests pass a ``SimpleNamespace`` because ``config.settings`` itself
    pulls in ``config.secrets`` (gitignored) and is therefore unimportable
    in CI / from a fresh checkout.
    """

    # Sessions
    SESSION_ASIA: tuple[int, int, int, int]
    KILLZONE_LONDON: tuple[int, int, int, int]
    KILLZONE_NY: tuple[int, int, int, int]

    # Swings + multi-TF
    SWING_LOOKBACK_H4: int
    SWING_LOOKBACK_H1: int
    SWING_LOOKBACK_M5: int
    MIN_SWING_AMPLITUDE_ATR_MULT_H4: float
    MIN_SWING_AMPLITUDE_ATR_MULT_H1: float
    MIN_SWING_AMPLITUDE_ATR_MULT_M5: float
    BIAS_SWING_COUNT: int
    BIAS_REQUIRE_H1_CONFIRMATION: bool
    H4_H1_TIME_TOLERANCE_CANDLES_H4: int
    H4_H1_PRICE_TOLERANCE_FRACTION: float
    SWING_LEVELS_LOOKBACK_COUNT: int

    # Sweep
    SWEEP_RETURN_WINDOW_CANDLES: int
    SWEEP_DEDUP_TIME_WINDOW_MINUTES: int
    SWEEP_DEDUP_PRICE_TOLERANCE_FRACTION: float

    # MSS
    MSS_DISPLACEMENT_MULTIPLIER: float
    MSS_DISPLACEMENT_LOOKBACK: int

    # FVG
    FVG_ATR_PERIOD: int
    FVG_MIN_SIZE_ATR_MULTIPLIER: float

    # Setup thresholds
    MIN_RR: float
    A_PLUS_RR_THRESHOLD: float
    PARTIAL_TP_RR_TARGET: float

    # Per-instrument
    INSTRUMENT_CONFIG: dict


_OTE_LOW_FRACTION = 0.62
_OTE_HIGH_FRACTION = 0.79
"""Optimal Trade Entry zone — fib retracement of the displacement leg."""

_MSS_LOOKFORWARD_MINUTES = 120
"""How far past the sweep we keep watching for MSS confirmation."""

_FVG_LOOKFORWARD_FROM_MSS_MINUTES = 30
"""FVG candle c2 must fall within this many minutes after MSS confirmation
for the gap to count as 'created by the displacement move'. The MSS
candle itself is included; values below 15 risk missing the c3 candle."""


@dataclass(frozen=True)
class Setup:
    """One detected setup candidate ready for operator review.

    Times are all UTC (display conversion lives in the notification
    layer). Prices are in instrument units (USD for XAU, points for
    indices, decimal for forex).

    Two take-profit levels are exposed:

    - ``tp_runner_*`` is the **structural** TP — the opposing-liquidity
      level the strategy targets per docs/01 §5 Step 4. RR can range
      from ``MIN_RR`` to 18+ on extended legs.
    - ``tp1_*`` is the **partial-exit** TP, capped at
      ``PARTIAL_TP_RR_TARGET`` (5R by default). The operator's tradability
      convention: scale out 50% at TP1 to lock variance, run the rest to
      TP_runner. When the runner itself is below the cap, ``tp1_*`` ==
      ``tp_runner_*`` (no partial benefit).

    Backward-compat properties ``take_profit`` / ``risk_reward`` alias
    the runner — Sprint 4 notification layer can opt into the dual-TP
    fields explicitly when needed.
    """

    timestamp_utc: datetime
    symbol: str
    direction: Literal["long", "short"]
    daily_bias: Literal["bullish", "bearish"]
    killzone: Literal["london", "ny"]

    # Source events
    swept_level_price: float
    swept_level_type: str
    swept_level_strength: str
    sweep: Sweep
    mss: MSS
    poi: FVG | OrderBlock
    poi_type: Literal["FVG", "OrderBlock"]

    # Trade plan
    entry_price: float
    stop_loss: float
    target_level_type: str

    # Structural take profit — opposing liquidity per docs/01 §5 Step 4.
    tp_runner_price: float
    tp_runner_rr: float

    # 5R partial take-profit. When the runner itself is ≤ PARTIAL_TP_RR_TARGET
    # these fields equal tp_runner_*; otherwise tp1_* is capped to entry
    # ± PARTIAL_TP_RR_TARGET × risk and tp1_rr == PARTIAL_TP_RR_TARGET.
    tp1_price: float
    tp1_rr: float

    # Quality
    quality: Grade
    confluences: list[str]

    @property
    def take_profit(self) -> float:
        """Backward-compat alias for ``tp_runner_price``."""
        return self.tp_runner_price

    @property
    def risk_reward(self) -> float:
        """Backward-compat alias for ``tp_runner_rr``."""
        return self.tp_runner_rr


@dataclass(frozen=True)
class RejectedCandidate:
    """A sweep that almost-but-not-quite became a Setup.

    Sprint 6 journals these alongside accepted Setups so the operator can
    audit *why* a notification did not fire (calibration aid). Only
    candidates that survived the bias-direction filter are tracked —
    direction-misaligned sweeps would dwarf the table.

    ``rejection_reason`` is one of:

    - ``"no_mss_after_sweep"`` — no displacement / structure break.
    - ``"no_poi_found"`` — no FVG and no fallback OB.
    - ``"invalid_risk"`` — entry coincides with stop loss.
    - ``"no_opposing_liquidity"`` — no opposing level → can't measure RR.
    - ``"rr_below_threshold"`` — opposing level exists but RR < ``MIN_RR``.
    - ``"grade_rejected"`` — graded but no quality tier matched.
    - ``"killzone_gating"`` — MSS confirms past ``killzone_end``.

    ``sweep_info`` carries enough context to replay the rejection in a
    notebook: the sweep's symbol/timestamp/direction/level.
    """

    timestamp_utc: datetime
    symbol: str
    rejection_reason: str
    sweep_info: dict | None = None


@overload
def build_setup_candidates(
    df_h4: pd.DataFrame,
    df_h1: pd.DataFrame,
    df_m5: pd.DataFrame,
    df_d1: pd.DataFrame,
    target_date: date,
    symbol: str,
    settings: SetupSettings,
    *,
    return_rejected: Literal[False] = False,
    now_utc: datetime | None = None,
) -> list[Setup]: ...


@overload
def build_setup_candidates(
    df_h4: pd.DataFrame,
    df_h1: pd.DataFrame,
    df_m5: pd.DataFrame,
    df_d1: pd.DataFrame,
    target_date: date,
    symbol: str,
    settings: SetupSettings,
    *,
    return_rejected: Literal[True],
    now_utc: datetime | None = None,
) -> tuple[list[Setup], list[RejectedCandidate]]: ...


def build_setup_candidates(
    df_h4: pd.DataFrame,
    df_h1: pd.DataFrame,
    df_m5: pd.DataFrame,
    df_d1: pd.DataFrame,
    target_date: date,
    symbol: str,
    settings: SetupSettings,
    *,
    return_rejected: bool = False,
    now_utc: datetime | None = None,
) -> list[Setup] | tuple[list[Setup], list[RejectedCandidate]]:
    """Build every setup candidate for one symbol on one trading day.

    Pipeline:
        1. Daily bias from H4 + H1 (bias.py). If ``no_trade`` → ``[]``.
        2. For each killzone (London, NY):
            a. Mark Asian range, PDH/PDL, multi-TF swing levels, equal H/L
               at the killzone start (bias is locked once kz starts).
            b. Detect sweeps in the killzone, deduplicated.
            c. Filter by direction matching daily bias.
            d. For each remaining sweep:
                i.   Detect MSS in the post-sweep window.
                ii.  If no MSS → skip.
                iii. Detect FVGs in the displacement window with size
                     filter; if none → fall back to OrderBlock.
                iv.  If no POI either → skip.
                v.   Compute entry / SL / TP / RR.
                vi.  If RR < MIN_RR → skip.
                vii. Grade the candidate; if grade is None → skip.
                viii.Emit Setup.
        3. Return all Setup candidates (operator review picks the best).

    No live MT5 calls — pure function over the four OHLC frames.

    Args:
        df_h4: H4 OHLC frame.
        df_h1: H1 OHLC frame.
        df_m5: M5 OHLC frame (the timeframe everything M5-related runs on).
        df_d1: D1 OHLC frame (used for PDH/PDL only).
        target_date: calendar date in Paris of the trading session.
        symbol: instrument label, e.g. ``"NDX100"``. Must be a key in
            ``settings.INSTRUMENT_CONFIG``.
        settings: any object satisfying the ``SetupSettings`` protocol.
        now_utc: optional production scheduler tick. When set, every
            forward-looking sub-search (FVG c3 closure, sweep return
            candle closure, sweep dedupe pool) is bounded to data
            observable at ``now_utc``. ``None`` (default) is the legacy
            unconstrained mode. The look-ahead audit at
            calibration/runs/FINAL_lookahead_audit_2026-05-01.md
            documents the leakage that motivated this parameter; the
            production scheduler and the audit harness pass
            ``now_utc = next_5min_tick_after(mss_confirm)`` to disable it.

    Returns:
        ``list[Setup]`` (possibly empty) ordered by killzone (London first)
        then by ``timestamp_utc`` ascending.
    """
    instr_cfg = settings.INSTRUMENT_CONFIG[symbol]
    setups: list[Setup] = []
    rejected: list[RejectedCandidate] = []

    for kz_name, kz_session in (
        ("london", settings.KILLZONE_LONDON),
        ("ny", settings.KILLZONE_NY),
    ):
        kz_start_utc, kz_end_utc = paris_session_to_utc(target_date, kz_session)

        # Bias is locked AT killzone start (docs/01 §3). Slice H4/H1 to
        # only data the system would have observed before the killzone
        # opened — ``compute_daily_bias`` itself doesn't take an as-of
        # cutoff, so we slice up-front.
        df_h4_slice = _slice_frame_until(df_h4, kz_start_utc)
        df_h1_slice = _slice_frame_until(df_h1, kz_start_utc)
        bias = compute_daily_bias(
            df_h4=df_h4_slice,
            df_h1=df_h1_slice,
            swing_lookback_h4=settings.SWING_LOOKBACK_H4,
            swing_lookback_h1=settings.SWING_LOOKBACK_H1,
            min_amplitude_atr_mult_h4=settings.MIN_SWING_AMPLITUDE_ATR_MULT_H4,
            min_amplitude_atr_mult_h1=settings.MIN_SWING_AMPLITUDE_ATR_MULT_H1,
            bias_swing_count=settings.BIAS_SWING_COUNT,
            require_h1_confirmation=settings.BIAS_REQUIRE_H1_CONFIRMATION,
        )
        if bias == "no_trade":
            continue

        asian, daily, swings, equals, levels = _build_marked_levels(
            df_m5=df_m5,
            df_d1=df_d1,
            df_h4=df_h4,
            df_h1=df_h1,
            target_date=target_date,
            as_of_utc=kz_start_utc,
            equal_hl_tolerance=instr_cfg["equal_hl_tolerance"],
            settings=settings,
            now_utc=now_utc,
        )

        # The sweep window is bounded by the killzone end and (when set)
        # by ``now_utc`` so the dedupe pool only contains sweeps the
        # production scheduler would already have observed. The
        # `now_utc` push to ``detect_sweeps`` itself enforces the
        # stricter "return candle has closed" rule on top of this.
        sweep_kz_end = min(kz_end_utc, now_utc) if now_utc is not None else kz_end_utc
        sweeps = detect_sweeps(
            df_m5,
            levels,
            killzone_window_utc=(kz_start_utc, sweep_kz_end),
            sweep_buffer=instr_cfg["sweep_buffer"],
            return_window_candles=settings.SWEEP_RETURN_WINDOW_CANDLES,
            dedupe=True,
            dedupe_time_window_minutes=settings.SWEEP_DEDUP_TIME_WINDOW_MINUTES,
            dedupe_price_tolerance_fraction=settings.SWEEP_DEDUP_PRICE_TOLERANCE_FRACTION,
            now_utc=now_utc,
        )

        # Bias-aligned only: bullish bias trades sweeps of lows; bearish
        # bias trades sweeps of highs. Per docs/01 §5 Step 1.
        sweeps = [s for s in sweeps if s.direction == bias]

        for sweep in sweeps:
            outcome = _try_build_setup(
                sweep=sweep,
                bias=bias,
                killzone=kz_name,
                df_m5=df_m5,
                symbol=symbol,
                instr_cfg=instr_cfg,
                levels=levels,
                settings=settings,
                now_utc=now_utc,
            )
            if isinstance(outcome, RejectedCandidate):
                rejected.append(outcome)
                continue
            setup = outcome  # type: Setup

            # Killzone gating per docs/01 §6: notifications must not fire
            # outside London/NY killzones. The detection pipeline can produce
            # setups whose MSS confirms after killzone close (the MSS
            # lookforward window extends past killzone end). These are
            # dropped here. Boundary policy: timestamp == kz_end_utc is kept
            # (inclusive end), strictly greater is dropped.
            if setup.timestamp_utc > kz_end_utc:
                logger.debug(
                    "setup dropped (timestamp_utc=%s > killzone_end_utc=%s) "
                    "symbol=%s killzone=%s",
                    setup.timestamp_utc,
                    kz_end_utc,
                    symbol,
                    kz_name,
                )
                rejected.append(
                    RejectedCandidate(
                        timestamp_utc=setup.timestamp_utc,
                        symbol=symbol,
                        rejection_reason="killzone_gating",
                        sweep_info=_sweep_info(sweep),
                    )
                )
                continue

            setups.append(setup)

    if return_rejected:
        return setups, rejected
    return setups


def _slice_frame_until(df: pd.DataFrame, cutoff_utc: datetime) -> pd.DataFrame:
    """Return rows whose ``time`` is strictly before ``cutoff_utc``.

    Mirrors the same convention ``mark_swing_levels`` applies via
    ``as_of_utc`` — ensures the orchestrator never reads candles that
    wouldn't have been observable at killzone open.
    """
    if len(df) == 0:
        return df
    times = pd.to_datetime(df["time"], utc=True)
    mask = times < cutoff_utc
    return df.loc[mask].reset_index(drop=True)


def _build_marked_levels(
    df_m5: pd.DataFrame,
    df_d1: pd.DataFrame,
    df_h4: pd.DataFrame,
    df_h1: pd.DataFrame,
    target_date: date,
    as_of_utc: datetime,
    equal_hl_tolerance: float,
    settings: SetupSettings,
    now_utc: datetime | None = None,
) -> tuple[
    AsianRange | None,
    DailyLevels | None,
    list[SwingLevel],
    list,
    list[MarkedLevel],
]:
    asian = mark_asian_range(df_m5, target_date, settings.SESSION_ASIA)
    daily = mark_pdh_pdl(df_d1, target_date)
    swings = mark_swing_levels(
        df_h4,
        df_h1,
        as_of_utc=as_of_utc,
        lookback_h4=settings.SWING_LOOKBACK_H4,
        lookback_h1=settings.SWING_LOOKBACK_H1,
        min_amplitude_atr_mult_h4=settings.MIN_SWING_AMPLITUDE_ATR_MULT_H4,
        min_amplitude_atr_mult_h1=settings.MIN_SWING_AMPLITUDE_ATR_MULT_H1,
        n_swings=settings.SWING_LEVELS_LOOKBACK_COUNT,
        h4_h1_time_tolerance_h4_candles=settings.H4_H1_TIME_TOLERANCE_CANDLES_H4,
        h4_h1_price_tolerance_fraction=settings.H4_H1_PRICE_TOLERANCE_FRACTION,
        now_utc=now_utc,
    )
    equals = find_equal_highs_lows(swings, equal_hl_tolerance=equal_hl_tolerance)
    levels = (
        asian_range_to_marked_levels(asian)
        + daily_levels_to_marked_levels(daily)
        + [swing_level_to_marked_level(s) for s in swings]
        + [equal_level_to_marked_level(e) for e in equals]
    )
    return asian, daily, swings, equals, levels


def _try_build_setup(
    *,
    sweep: Sweep,
    bias: Literal["bullish", "bearish"],
    killzone: Literal["london", "ny"],
    df_m5: pd.DataFrame,
    symbol: str,
    instr_cfg: dict,
    levels: list[MarkedLevel],
    settings: SetupSettings,
    now_utc: datetime | None = None,
) -> Setup | RejectedCandidate:
    """Per-sweep pipeline — returns either a ``Setup`` or a ``RejectedCandidate``.

    Sprint 6: every rejection now carries a tagged reason so the scheduler
    can journal it. The semantics for accepted setups are unchanged from
    Sprint 4.
    """
    mss = detect_mss(
        df_m5,
        sweep,
        swing_lookback_m5=settings.SWING_LOOKBACK_M5,
        min_swing_amplitude_atr_mult=settings.MIN_SWING_AMPLITUDE_ATR_MULT_M5,
        displacement_multiplier=settings.MSS_DISPLACEMENT_MULTIPLIER,
        displacement_lookback=settings.MSS_DISPLACEMENT_LOOKBACK,
        max_lookforward_minutes=_MSS_LOOKFORWARD_MINUTES,
        now_utc=now_utc,
    )
    if mss is None:
        return _reject(sweep, symbol, "no_mss_after_sweep")

    # FVG search window — from MSS candle to a small forward horizon.
    # The displacement move is usually 1-3 candles around MSS; we look
    # 6 M5 candles forward to catch FVGs that crystallise just after.
    # When ``now_utc`` is provided, cap the forward horizon so the FVG
    # search cannot reach candles whose data the production scheduler
    # has not yet observed (the "+30min" bound assumed the historical
    # backtest had unrestricted future access — see audit findings).
    fvg_window_start = mss.displacement_candle_time_utc
    fvg_window_end = mss.mss_confirm_candle_time_utc + pd.Timedelta(
        minutes=_FVG_LOOKFORWARD_FROM_MSS_MINUTES
    )
    if now_utc is not None:
        fvg_window_end = min(fvg_window_end, pd.Timestamp(now_utc))
    fvgs = detect_fvgs_in_window(
        df_m5,
        start_time_utc=fvg_window_start.replace(microsecond=0),
        end_time_utc=(
            fvg_window_end.to_pydatetime()
            if hasattr(fvg_window_end, "to_pydatetime")
            else fvg_window_end
        ),
        direction=mss.direction,
        min_size_atr_mult=settings.FVG_MIN_SIZE_ATR_MULTIPLIER,
        atr_period=settings.FVG_ATR_PERIOD,
        now_utc=now_utc,
    )

    ob = detect_order_block(df_m5, mss)

    # POI priority: FVG > OB. If an FVG exists, prefer the FIRST one
    # chronologically (closest to the MSS, most likely to be retested).
    poi: FVG | OrderBlock | None
    poi_type: Literal["FVG", "OrderBlock"]
    if fvgs:
        poi = fvgs[0]
        poi_type = "FVG"
    elif ob is not None:
        poi = ob
        poi_type = "OrderBlock"
    else:
        return _reject(sweep, symbol, "no_poi_found")

    # Trade plan.
    direction: Literal["long", "short"] = "long" if mss.direction == "bullish" else "short"
    entry = poi.proximal
    sl_buffer = float(instr_cfg["sl_buffer"])
    if direction == "long":
        stop_loss = sweep.sweep_extreme_price - sl_buffer
    else:
        stop_loss = sweep.sweep_extreme_price + sl_buffer

    risk = abs(entry - stop_loss)
    if risk <= 0:
        return _reject(sweep, symbol, "invalid_risk")

    tp_choice = _select_take_profit(
        direction=direction,
        entry=entry,
        risk=risk,
        levels=levels,
        sweep=sweep,
        min_rr=settings.MIN_RR,
    )
    if tp_choice is None:
        # Could be either no opposing level at all OR opposing level
        # below MIN_RR. Distinguish for the journal.
        reason = _no_opposing_or_low_rr(direction, entry, levels, sweep)
        return _reject(sweep, symbol, reason)
    tp_runner_price, target_level_type, tp_runner_rr = tp_choice

    tp1_price, tp1_rr = _compute_tp1(
        direction=direction,
        entry=entry,
        risk=risk,
        tp_runner_price=tp_runner_price,
        tp_runner_rr=tp_runner_rr,
        partial_target=float(settings.PARTIAL_TP_RR_TARGET),
    )

    # Grading — defensive RR re-check happens implicitly via grade rules.
    # Grading still operates on tp_runner_rr (the structural RR) — TP1 is
    # a tradability convenience layer, not a quality input.
    ote_overlap = _ote_overlaps_poi(
        poi=poi,
        sweep_extreme=sweep.sweep_extreme_price,
        broken_swing=mss.broken_swing_price,
    )
    components = SetupComponents(
        swept_level_strength=sweep.swept_level_strength,
        poi=poi,
        poi_type=poi_type,
        risk_reward=tp_runner_rr,
        displacement_body_ratio=mss.displacement_body_ratio,
        ote_overlap=ote_overlap,
        has_alternative_ob_when_fvg=(poi_type == "FVG" and ob is not None),
        fvg_min_size_atr_multiplier=settings.FVG_MIN_SIZE_ATR_MULTIPLIER,
        mss_displacement_multiplier=settings.MSS_DISPLACEMENT_MULTIPLIER,
        min_rr=settings.MIN_RR,
        a_plus_rr_threshold=settings.A_PLUS_RR_THRESHOLD,
    )
    grade, confluences = grade_setup(components)
    if grade is None:
        return _reject(sweep, symbol, "grade_rejected")

    return Setup(
        timestamp_utc=mss.mss_confirm_candle_time_utc,
        symbol=symbol,
        direction=direction,
        daily_bias=bias,
        killzone=killzone,
        swept_level_price=sweep.swept_level_price,
        swept_level_type=sweep.swept_level_type,
        swept_level_strength=sweep.swept_level_strength,
        sweep=sweep,
        mss=mss,
        poi=poi,
        poi_type=poi_type,
        entry_price=float(entry),
        stop_loss=float(stop_loss),
        target_level_type=target_level_type,
        tp_runner_price=float(tp_runner_price),
        tp_runner_rr=float(tp_runner_rr),
        tp1_price=float(tp1_price),
        tp1_rr=float(tp1_rr),
        quality=grade,
        confluences=confluences,
    )


def _sweep_info(sweep: Sweep) -> dict:
    """Lightweight context bundle for ``RejectedCandidate.sweep_info``."""
    return {
        "sweep_time_utc": sweep.sweep_candle_time_utc.isoformat(),
        "swept_level_type": sweep.swept_level_type,
        "swept_level_price": float(sweep.swept_level_price),
        "direction": sweep.direction,
    }


def _reject(sweep: Sweep, symbol: str, reason: str) -> RejectedCandidate:
    """Build a ``RejectedCandidate`` keyed off the sweep's confirmation time."""
    return RejectedCandidate(
        timestamp_utc=sweep.sweep_candle_time_utc,
        symbol=symbol,
        rejection_reason=reason,
        sweep_info=_sweep_info(sweep),
    )


def _no_opposing_or_low_rr(
    direction: Literal["long", "short"],
    entry: float,
    levels: list[MarkedLevel],
    sweep: Sweep,
) -> str:
    """Discriminate between ``no_opposing_liquidity`` and ``rr_below_threshold``.

    ``_select_take_profit`` returns ``None`` for both cases — we replay the
    opposing-level filter to find out which one applies.
    """
    if direction == "long":
        opposing = [
            lv
            for lv in levels
            if lv.type == "high"
            and lv.price > entry
            and not (lv.label == sweep.swept_level_type and lv.price == sweep.swept_level_price)
        ]
    else:
        opposing = [
            lv
            for lv in levels
            if lv.type == "low"
            and lv.price < entry
            and not (lv.label == sweep.swept_level_type and lv.price == sweep.swept_level_price)
        ]
    return "rr_below_threshold" if opposing else "no_opposing_liquidity"


def _compute_tp1(
    *,
    direction: Literal["long", "short"],
    entry: float,
    risk: float,
    tp_runner_price: float,
    tp_runner_rr: float,
    partial_target: float,
) -> tuple[float, float]:
    """Compute the partial-exit ``(tp1_price, tp1_rr)``.

    When the runner is at or below the partial cap, TP1 collapses onto
    the runner — no partial benefit on small-RR setups. Otherwise TP1
    is set to ``entry ± partial_target × risk`` so the operator can
    place a 50% scale-out at exactly the configured RR.
    """
    if tp_runner_rr <= partial_target:
        return tp_runner_price, tp_runner_rr
    if direction == "long":
        tp1_price = entry + partial_target * risk
    else:
        tp1_price = entry - partial_target * risk
    return tp1_price, partial_target


def _select_take_profit(
    *,
    direction: Literal["long", "short"],
    entry: float,
    risk: float,
    levels: list[MarkedLevel],
    sweep: Sweep,
    min_rr: float,
) -> tuple[float, str, float] | None:
    """Pick the nearest opposing-liquidity level yielding RR >= ``min_rr``.

    Long setup ⇒ look at "high" levels strictly above ``entry``.
    Short setup ⇒ look at "low" levels strictly below ``entry``.
    The level that triggered the sweep is excluded (we don't re-target
    the level we just took out).

    Returns:
        ``(take_profit_price, level_label, risk_reward)`` or ``None`` if
        no opposing level reaches the minimum RR.
    """
    if direction == "long":
        opposing = [lv for lv in levels if lv.type == "high" and lv.price > entry]
        opposing.sort(key=lambda lv: lv.price - entry)
    else:
        opposing = [lv for lv in levels if lv.type == "low" and lv.price < entry]
        opposing.sort(key=lambda lv: entry - lv.price)

    for lv in opposing:
        # Skip the level we just swept.
        if lv.label == sweep.swept_level_type and lv.price == sweep.swept_level_price:
            continue
        reward = abs(lv.price - entry)
        rr = reward / risk
        if rr >= min_rr:
            return (float(lv.price), lv.label, float(rr))
    return None


def _ote_overlaps_poi(
    *,
    poi: FVG | OrderBlock,
    sweep_extreme: float,
    broken_swing: float,
) -> bool:
    """Does ``[poi.proximal, poi.distal]`` intersect the OTE 0.62-0.79 zone?

    The displacement leg runs from ``sweep_extreme`` to ``broken_swing``.
    Direction is inferred from the sign of the leg (broken_swing >
    sweep_extreme ⇒ bullish leg, retracement is downward).
    """
    leg_low = min(sweep_extreme, broken_swing)
    leg_high = max(sweep_extreme, broken_swing)
    leg = leg_high - leg_low
    if leg <= 0:
        return False

    if broken_swing > sweep_extreme:
        # Bullish: retracement DOWN from leg_high.
        ote_high = leg_high - _OTE_LOW_FRACTION * leg
        ote_low = leg_high - _OTE_HIGH_FRACTION * leg
    else:
        # Bearish: retracement UP from leg_low.
        ote_low = leg_low + _OTE_LOW_FRACTION * leg
        ote_high = leg_low + _OTE_HIGH_FRACTION * leg

    poi_low = min(poi.proximal, poi.distal)
    poi_high = max(poi.proximal, poi.distal)
    return poi_high >= ote_low and poi_low <= ote_high
