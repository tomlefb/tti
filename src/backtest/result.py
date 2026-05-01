"""``BacktestResult`` — a self-contained, JSON-serialisable record of
one strategy run on one instrument.

The dataclass holds per-setup R outcomes plus precomputed aggregates
(mean R + bootstrap 95% CI, max drawdown in R units, monthly /
semester stability metrics) so any consumer — comparison reports,
A/B tests, sensitivity sweeps — can read a single file and have the
information it needs without re-deriving it from the raw setup list.

Conventions:

- R outcomes are signed: ``+r`` for a winner that hits TP, ``-1.0`` for
  a stop-out, ``0.0`` for entry-not-hit / open-at-horizon. The
  partial-exit convention (50% TP1 / 50% TP_runner) lives upstream in
  the outcome simulator; ``BacktestResult`` only consumes the final R
  number.
- Bootstrap IC is 95% percentile-method on 10k resamples. The seed
  defaults to 42 so two runs over the same setup list return the same
  CI bounds.
- ``mean_r`` and ``median_r`` are computed over **closed** trades
  (excluding entry-not-hit and open-at-horizon). ``mean_r_ci_95`` is
  the bootstrap CI on that same closed sample.
- Max drawdown is computed on the equity curve of closed-trade R
  values in chronological order; entry-not-hit / open-at-horizon
  contribute 0 to the curve so they neither help nor hurt the
  drawdown.
- Setups-per-month uses the **calendar months spanned** by the
  ``period_start..period_end`` window, not the months actually present
  in setups. This keeps the metric stable when a strategy has dry
  months.
- Semester (6-month) bucketing aligns to ISO calendar halves
  (Jan–Jun, Jul–Dec) of each year in the period.

The ``compare`` method runs Welch's t-test on R distributions and
returns ``(delta_mean_r, p_value, delta_ci_95)`` where ``delta_ci_95``
is the 95% CI on the difference of means via percentile bootstrap on
the pooled sample.
"""

from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np

_BOOTSTRAP_RESAMPLES = 10_000
_BOOTSTRAP_SEED = 42


@dataclass(frozen=True)
class SetupRecord:
    """One setup's per-trade record.

    The full ``Setup`` dataclass would be heavy in JSON; we keep only
    the fields the comparison / sensitivity layers actually consume.
    """

    timestamp_utc: str  # ISO format
    instrument: str
    direction: str  # "long" | "short"
    quality: str  # "A+" | "A" | "B"
    realized_r: float
    outcome: str  # "tp_runner_hit" | "tp1_hit_only" | "sl_hit" | "sl_before_entry" | "entry_not_hit" | "open_at_horizon"


@dataclass(frozen=True)
class BacktestResult:
    """A self-contained record of one strategy run on one instrument."""

    strategy_name: str
    instrument: str
    period_start: str  # ISO date
    period_end: str  # ISO date

    n_setups: int
    n_wins: int
    win_rate: float

    mean_r: float
    mean_r_ci_95: tuple[float, float]
    median_r: float
    max_dd_r: float

    setups_per_month: float

    cv_monthly: float
    fraction_positive_semesters: float

    setups: list[SetupRecord]
    params_used: dict[str, Any]

    run_timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")

    # ------------------------------------------------------------------
    # Construction helper.
    # ------------------------------------------------------------------
    @classmethod
    def from_setups(
        cls,
        *,
        strategy_name: str,
        instrument: str,
        period_start: date,
        period_end: date,
        setups: list[SetupRecord],
        params_used: dict[str, Any],
        bootstrap_seed: int = _BOOTSTRAP_SEED,
        run_timestamp: str | None = None,
    ) -> BacktestResult:
        """Build a ``BacktestResult`` from a list of per-setup records.

        The list is taken as-is; if you want NOTIFY_QUALITIES gating,
        pre-filter before calling this.
        """
        closed = [s for s in setups if s.outcome not in ("entry_not_hit", "open_at_horizon")]
        rs = [s.realized_r for s in closed]

        wins = sum(1 for s in closed if s.outcome in ("tp1_hit_only", "tp_runner_hit"))
        losses = sum(1 for s in closed if s.outcome in ("sl_hit", "sl_before_entry"))
        win_rate = wins / (wins + losses) if (wins + losses) else 0.0
        mean_r = sum(rs) / len(rs) if rs else 0.0
        median_r = statistics.median(rs) if rs else 0.0

        mean_r_ci_95 = _bootstrap_mean_ci(rs, _BOOTSTRAP_RESAMPLES, bootstrap_seed)

        max_dd = _max_drawdown_r(setups)

        months = _months_spanned(period_start, period_end)
        spm = len(setups) / months if months else 0.0

        cv = _cv_monthly(setups)
        frac_pos_sem = _fraction_positive_semesters(setups)

        return cls(
            strategy_name=strategy_name,
            instrument=instrument,
            period_start=period_start.isoformat(),
            period_end=period_end.isoformat(),
            n_setups=len(setups),
            n_wins=wins,
            win_rate=win_rate,
            mean_r=mean_r,
            mean_r_ci_95=mean_r_ci_95,
            median_r=median_r,
            max_dd_r=max_dd,
            setups_per_month=spm,
            cv_monthly=cv,
            fraction_positive_semesters=frac_pos_sem,
            setups=list(setups),
            params_used=params_used,
            run_timestamp=(
                run_timestamp if run_timestamp is not None else datetime.utcnow().isoformat() + "Z"
            ),
        )

    # ------------------------------------------------------------------
    # IO.
    # ------------------------------------------------------------------
    def to_json(self, path: Path | str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        d = asdict(self)
        # tuples become lists in JSON; round-trip restores the tuple.
        path.write_text(json.dumps(d, indent=2, sort_keys=True, default=str))

    @classmethod
    def from_json(cls, path: Path | str) -> BacktestResult:
        path = Path(path)
        d = json.loads(path.read_text())
        d["setups"] = [SetupRecord(**s) for s in d["setups"]]
        d["mean_r_ci_95"] = tuple(d["mean_r_ci_95"])
        return cls(**d)

    # ------------------------------------------------------------------
    # Comparison.
    # ------------------------------------------------------------------
    def compare(self, other: BacktestResult, *, bootstrap_seed: int = _BOOTSTRAP_SEED) -> dict:
        """Welch's t-test on closed-trade R distributions plus a
        bootstrap 95% CI on the mean delta.

        Returns a dict with keys ``delta_mean_r``, ``p_value``,
        ``delta_ci_95``, ``n_self`` and ``n_other`` (closed-trade
        counts).
        """
        a = [
            s.realized_r
            for s in self.setups
            if s.outcome not in ("entry_not_hit", "open_at_horizon")
        ]
        b = [
            s.realized_r
            for s in other.setups
            if s.outcome not in ("entry_not_hit", "open_at_horizon")
        ]
        if not a or not b:
            return {
                "delta_mean_r": (self.mean_r - other.mean_r),
                "p_value": float("nan"),
                "delta_ci_95": (float("nan"), float("nan")),
                "n_self": len(a),
                "n_other": len(b),
            }
        delta = self.mean_r - other.mean_r
        p = _welch_p_value(a, b)
        ci = _bootstrap_delta_ci(a, b, _BOOTSTRAP_RESAMPLES, bootstrap_seed)
        return {
            "delta_mean_r": delta,
            "p_value": p,
            "delta_ci_95": ci,
            "n_self": len(a),
            "n_other": len(b),
        }


# ----------------------------------------------------------------------
# Internal helpers.
# ----------------------------------------------------------------------
def _bootstrap_mean_ci(rs: list[float], n_resamples: int, seed: int) -> tuple[float, float]:
    if not rs:
        return (0.0, 0.0)
    arr = np.asarray(rs, dtype="float64")
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(arr), size=(n_resamples, len(arr)))
    means = arr[idx].mean(axis=1)
    lo = float(np.percentile(means, 2.5))
    hi = float(np.percentile(means, 97.5))
    return (lo, hi)


def _bootstrap_delta_ci(
    a: list[float], b: list[float], n_resamples: int, seed: int
) -> tuple[float, float]:
    arr_a = np.asarray(a, dtype="float64")
    arr_b = np.asarray(b, dtype="float64")
    rng = np.random.default_rng(seed)
    idx_a = rng.integers(0, len(arr_a), size=(n_resamples, len(arr_a)))
    idx_b = rng.integers(0, len(arr_b), size=(n_resamples, len(arr_b)))
    deltas = arr_a[idx_a].mean(axis=1) - arr_b[idx_b].mean(axis=1)
    lo = float(np.percentile(deltas, 2.5))
    hi = float(np.percentile(deltas, 97.5))
    return (lo, hi)


def _welch_p_value(a: list[float], b: list[float]) -> float:
    """Two-sided Welch's t-test p-value via scipy if available, else
    a hand-rolled normal-approximation fallback."""
    try:
        from scipy import stats  # noqa: PLC0415

        t, p = stats.ttest_ind(a, b, equal_var=False)
        return float(p)
    except Exception:  # pragma: no cover — numpy fallback
        ma = sum(a) / len(a)
        mb = sum(b) / len(b)
        va = sum((x - ma) ** 2 for x in a) / max(len(a) - 1, 1)
        vb = sum((x - mb) ** 2 for x in b) / max(len(b) - 1, 1)
        denom = math.sqrt(va / len(a) + vb / len(b))
        if denom == 0:
            return float("nan")
        t = (ma - mb) / denom
        # Welch–Satterthwaite df is unused in the normal-approximation
        # fallback below — we drop into a z-test rather than use a
        # t-distribution, which is good enough for the rare scipy-less
        # path.
        z = abs(t)
        # two-sided
        p = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(z / math.sqrt(2.0))))
        return p


def _max_drawdown_r(setups: list[SetupRecord]) -> float:
    """Equity-curve peak-to-trough drawdown in R units. Closed trades
    contribute their realized_r; entry_not_hit / open_at_horizon
    contribute zero. The curve is built in chronological order."""
    rs = [(s.timestamp_utc, s.realized_r) for s in setups]
    rs.sort()
    acc = 0.0
    peak = 0.0
    worst = 0.0
    for _, r in rs:
        acc += r
        peak = max(peak, acc)
        worst = min(worst, acc - peak)
    return -worst


def _months_spanned(start: date, end: date) -> int:
    if end < start:
        return 0
    return (end.year - start.year) * 12 + (end.month - start.month) + 1


def _cv_monthly(setups: list[SetupRecord]) -> float:
    """Coefficient of variation of monthly mean R. Months with no
    closed trades are skipped. Returns ``inf`` when the absolute mean
    of monthly means is zero (signal of no edge)."""
    by_month: dict[str, list[float]] = defaultdict(list)
    for s in setups:
        if s.outcome in ("entry_not_hit", "open_at_horizon"):
            continue
        key = s.timestamp_utc[:7]  # YYYY-MM
        by_month[key].append(s.realized_r)
    monthly_means: list[float] = []
    for k in sorted(by_month):
        rs = by_month[k]
        if not rs:
            continue
        monthly_means.append(sum(rs) / len(rs))
    if not monthly_means:
        return float("inf")
    mean = sum(monthly_means) / len(monthly_means)
    if mean == 0:
        return float("inf")
    var = sum((x - mean) ** 2 for x in monthly_means) / len(monthly_means)
    sd = math.sqrt(var)
    return sd / abs(mean)


def _fraction_positive_semesters(setups: list[SetupRecord]) -> float:
    """Fraction of 6-month buckets (H1, H2 of each calendar year) with
    closed-trade mean R > 0. Buckets with no closed trades are
    excluded from both numerator and denominator."""
    by_sem: dict[str, list[float]] = defaultdict(list)
    for s in setups:
        if s.outcome in ("entry_not_hit", "open_at_horizon"):
            continue
        ts = datetime.fromisoformat(s.timestamp_utc.replace("Z", "+00:00"))
        half = "H1" if ts.month <= 6 else "H2"
        by_sem[f"{ts.year}-{half}"].append(s.realized_r)
    if not by_sem:
        return 0.0
    pos = sum(1 for rs in by_sem.values() if (sum(rs) / len(rs)) > 0)
    return pos / len(by_sem)
