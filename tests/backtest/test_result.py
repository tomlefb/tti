"""Unit tests for ``src.backtest.result.BacktestResult``."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from src.backtest.result import BacktestResult, SetupRecord


def _record(ts: str, r: float, outcome: str = "tp_runner_hit", quality: str = "A") -> SetupRecord:
    return SetupRecord(
        timestamp_utc=ts,
        instrument="NDX100",
        direction="long",
        quality=quality,
        realized_r=r,
        outcome=outcome,
    )


def _build_result(setups: list[SetupRecord], **kwargs) -> BacktestResult:
    defaults = {
        "strategy_name": "test",
        "instrument": "NDX100",
        "period_start": date(2024, 1, 1),
        "period_end": date(2025, 12, 31),
        "setups": setups,
        "params_used": {"min_rr": 3.0},
    }
    defaults.update(kwargs)
    return BacktestResult.from_setups(**defaults)


# --- Bootstrap CI -----------------------------------------------------


def test_bootstrap_ci_is_seeded_and_reproducible() -> None:
    setups = [
        _record("2024-01-15T10:00:00+00:00", 1.0),
        _record("2024-02-15T10:00:00+00:00", -1.0, outcome="sl_hit"),
        _record("2024-03-15T10:00:00+00:00", 2.5),
        _record("2024-04-15T10:00:00+00:00", -1.0, outcome="sl_hit"),
        _record("2024-05-15T10:00:00+00:00", 3.0),
    ]
    a = _build_result(setups, run_timestamp="x")
    b = _build_result(setups, run_timestamp="y")
    assert a.mean_r_ci_95 == b.mean_r_ci_95


def test_bootstrap_ci_brackets_mean() -> None:
    setups = [_record(f"2024-0{i+1}-15T10:00:00+00:00", float(i) - 2) for i in range(8)]
    res = _build_result(setups)
    lo, hi = res.mean_r_ci_95
    assert lo <= res.mean_r <= hi


def test_bootstrap_ci_empty_when_no_closed_trades() -> None:
    setups = [_record("2024-01-15T10:00:00+00:00", 0.0, outcome="entry_not_hit")]
    res = _build_result(setups)
    assert res.mean_r_ci_95 == (0.0, 0.0)
    assert res.mean_r == 0.0


# --- Semester / monthly stability ------------------------------------


def test_fraction_positive_semesters_counts_only_buckets_with_trades() -> None:
    setups = [
        _record("2024-01-15T10:00:00+00:00", 2.0),  # H1 2024 positive
        _record("2024-08-15T10:00:00+00:00", -1.0, outcome="sl_hit"),  # H2 2024 negative
        _record("2025-03-15T10:00:00+00:00", 3.0),  # H1 2025 positive
    ]
    res = _build_result(setups)
    # 2 of 3 semesters are positive.
    assert res.fraction_positive_semesters == pytest.approx(2 / 3)


def test_cv_monthly_with_constant_mean_is_zero() -> None:
    setups = [
        _record("2024-01-15T10:00:00+00:00", 1.0),
        _record("2024-02-15T10:00:00+00:00", 1.0),
        _record("2024-03-15T10:00:00+00:00", 1.0),
    ]
    res = _build_result(setups)
    assert res.cv_monthly == pytest.approx(0.0)


def test_max_drawdown_r_chronological_equity_curve() -> None:
    # +1, +1, +1, -1, -1 → peak 3, trough 1 → DD 2.
    setups = [
        _record("2024-01-15T10:00:00+00:00", 1.0),
        _record("2024-02-15T10:00:00+00:00", 1.0),
        _record("2024-03-15T10:00:00+00:00", 1.0),
        _record("2024-04-15T10:00:00+00:00", -1.0, outcome="sl_hit"),
        _record("2024-05-15T10:00:00+00:00", -1.0, outcome="sl_hit"),
    ]
    res = _build_result(setups)
    assert res.max_dd_r == pytest.approx(2.0)


# --- JSON round-trip --------------------------------------------------


def test_json_round_trip(tmp_path: Path) -> None:
    setups = [
        _record("2024-01-15T10:00:00+00:00", 1.0),
        _record("2024-02-15T10:00:00+00:00", -1.0, outcome="sl_hit"),
        _record("2024-03-15T10:00:00+00:00", 2.5),
    ]
    res = _build_result(setups, run_timestamp="2024-03-15T11:00:00Z")
    p = tmp_path / "result.json"
    res.to_json(p)
    loaded = BacktestResult.from_json(p)
    assert loaded.strategy_name == res.strategy_name
    assert loaded.n_setups == res.n_setups
    assert loaded.mean_r == res.mean_r
    assert loaded.mean_r_ci_95 == res.mean_r_ci_95
    assert loaded.fraction_positive_semesters == res.fraction_positive_semesters
    assert loaded.setups == res.setups
    assert loaded.params_used == res.params_used


def test_json_is_deterministic_modulo_timestamp(tmp_path: Path) -> None:
    setups = [
        _record("2024-01-15T10:00:00+00:00", 1.0),
        _record("2024-02-15T10:00:00+00:00", -1.0, outcome="sl_hit"),
    ]
    a = _build_result(setups, run_timestamp="2024-03-15T11:00:00Z-RUN-A")
    b = _build_result(setups, run_timestamp="2024-03-15T11:00:01Z-RUN-B")
    pa = tmp_path / "a.json"
    pb = tmp_path / "b.json"
    a.to_json(pa)
    b.to_json(pb)
    text_a = pa.read_text().replace(a.run_timestamp, "STAMP")
    text_b = pb.read_text().replace(b.run_timestamp, "STAMP")
    assert text_a == text_b


# --- Compare ----------------------------------------------------------


def test_compare_returns_signed_delta_and_pvalue() -> None:
    a_setups = [
        _record("2024-01-15T10:00:00+00:00", 2.0),
        _record("2024-02-15T10:00:00+00:00", 2.0),
        _record("2024-03-15T10:00:00+00:00", 2.0),
        _record("2024-04-15T10:00:00+00:00", 2.0),
    ]
    b_setups = [
        _record("2024-01-15T10:00:00+00:00", -1.0, outcome="sl_hit"),
        _record("2024-02-15T10:00:00+00:00", -1.0, outcome="sl_hit"),
        _record("2024-03-15T10:00:00+00:00", -1.0, outcome="sl_hit"),
        _record("2024-04-15T10:00:00+00:00", -1.0, outcome="sl_hit"),
    ]
    a = _build_result(a_setups)
    b = _build_result(b_setups)
    out = a.compare(b)
    assert out["delta_mean_r"] == pytest.approx(3.0)
    # All-equal samples yield p=nan via scipy or our fallback's denom guard.
    assert isinstance(out["p_value"], float)
    lo, hi = out["delta_ci_95"]
    assert lo <= 3.0 <= hi


def test_compare_handles_empty_other() -> None:
    a = _build_result([_record("2024-01-15T10:00:00+00:00", 1.0)])
    b = _build_result([_record("2024-01-15T10:00:00+00:00", 0.0, outcome="entry_not_hit")])
    out = a.compare(b)
    assert out["n_other"] == 0
