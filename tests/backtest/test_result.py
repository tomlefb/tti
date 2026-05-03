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


# --- Protocol §9 derived metrics --------------------------------------


def test_projected_annual_return_pct_uses_default_1pct_risk() -> None:
    """Mean R 0.5 × 3.3 setups/mo × 12 × 1.0 % ≈ 19.8 %.

    This is the borderline case from STRATEGY_RESEARCH_PROTOCOL.md
    §3 — just under the 20 % gate.
    """
    # 3.3 setups/mo over 10 months = 33 closed trades.
    setups = [
        _record(f"2024-{((i % 10) + 1):02d}-{((i // 10) % 28 + 1):02d}T10:00:00+00:00", 0.5)
        for i in range(33)
    ]
    res = _build_result(setups, period_start=date(2024, 1, 1), period_end=date(2024, 10, 31))
    # 33 setups / 10 months = 3.3 spm; 0.5 × 3.3 × 12 × 1.0 = 19.8.
    assert res.projected_annual_return_pct == pytest.approx(19.8, rel=1e-6)


def test_projected_annual_return_pct_scales_with_risk_override() -> None:
    setups = [_record(f"2024-{m:02d}-15T10:00:00+00:00", 1.0) for m in range(1, 13)]
    res = BacktestResult.from_setups(
        strategy_name="t",
        instrument="NDX100",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        setups=setups,
        params_used={},
        risk_per_trade_pct=2.0,
    )
    # 12 setups / 12 months = 1.0 spm; 1.0 × 1.0 × 12 × 2.0 = 24.0.
    assert res.projected_annual_return_pct == pytest.approx(24.0)


def test_outlier_robustness_levels_drop_with_trim() -> None:
    """Two large positive outliers should pull mean down when trimmed."""
    base_rs = [0.2] * 30
    rs = [10.0, 10.0] + base_rs + [-1.0, -1.0]  # 34 closed trades
    setups = [
        _record(f"2024-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}T10:00:00+00:00", r, outcome="tp_runner_hit" if r > 0 else "sl_hit")
        for i, r in enumerate(rs)
    ]
    res = _build_result(setups)
    levels = res.outlier_robustness
    assert set(levels) == {"trim_0_0", "trim_2_2", "trim_5_5"}
    m_0 = levels["trim_0_0"]["mean_r"]
    m_2 = levels["trim_2_2"]["mean_r"]
    m_5 = levels["trim_5_5"]["mean_r"]
    # Trimming the 2 huge wins on top should drop mean noticeably.
    assert m_2 < m_0
    # Trimming 5 each side leaves the 30 base values of 0.2 only.
    assert m_5 == pytest.approx(0.2, abs=1e-9)
    assert levels["trim_5_5"]["n_remaining"] == 24


def test_outlier_robustness_skips_levels_when_n_too_small() -> None:
    """When n_closed < 20, only trim_0_0 is reported."""
    setups = [_record(f"2024-{m:02d}-15T10:00:00+00:00", 1.0) for m in range(1, 11)]
    res = _build_result(setups)
    levels = res.outlier_robustness
    assert levels["trim_0_0"] is not None
    assert levels["trim_2_2"] is None
    assert levels["trim_5_5"] is None


def test_temporal_concentration_uniform_distribution() -> None:
    """Spread evenly across N semesters → concentration ≈ 1/N."""
    # 4 semesters: H1 2024, H2 2024, H1 2025, H2 2025. Each gets one
    # +1 R trade so total = 4, max contribution = 1, ratio = 0.25.
    setups = [
        _record("2024-03-15T10:00:00+00:00", 1.0),
        _record("2024-09-15T10:00:00+00:00", 1.0),
        _record("2025-03-15T10:00:00+00:00", 1.0),
        _record("2025-09-15T10:00:00+00:00", 1.0),
    ]
    res = _build_result(setups)
    assert res.temporal_concentration == pytest.approx(0.25)


def test_temporal_concentration_dominant_semester() -> None:
    """One semester carries >50 % of the result."""
    # H1 2024 carries +5 R; the next 3 semesters carry +0.5 each.
    # total = 6.5, max = 5, ratio = 5/6.5 ≈ 0.769.
    setups = [
        _record("2024-03-15T10:00:00+00:00", 5.0),
        _record("2024-09-15T10:00:00+00:00", 0.5),
        _record("2025-03-15T10:00:00+00:00", 0.5),
        _record("2025-09-15T10:00:00+00:00", 0.5),
    ]
    res = _build_result(setups)
    assert res.temporal_concentration == pytest.approx(5.0 / 6.5)
    assert res.temporal_concentration > 0.5  # protocol regime-fitting flag


def test_temporal_concentration_is_none_when_no_closed_trades() -> None:
    setups = [_record("2024-01-15T10:00:00+00:00", 0.0, outcome="entry_not_hit")]
    res = _build_result(setups)
    assert res.temporal_concentration is None


def test_temporal_concentration_is_none_when_total_r_zero() -> None:
    """Two equal +1 / -1 trades net to zero — nothing to attribute."""
    setups = [
        _record("2024-03-15T10:00:00+00:00", 1.0),
        _record("2024-09-15T10:00:00+00:00", -1.0, outcome="sl_hit"),
    ]
    res = _build_result(setups)
    assert res.temporal_concentration is None


def test_vs_buy_and_hold_negative_when_strategy_lags() -> None:
    """Mean R 0.5 × 3.0/mo × 12 × 1 % = 18 %/yr vs B&H +25 %/yr → −7."""
    # 36 setups over 12 months → 3.0 spm.
    setups = [
        _record(f"2024-{(i % 12) + 1:02d}-{((i % 28) + 1):02d}T10:00:00+00:00", 0.5)
        for i in range(36)
    ]
    res = BacktestResult.from_setups(
        strategy_name="t",
        instrument="NDX100",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        setups=setups,
        params_used={},
        bh_close_start=100.0,
        bh_close_end=125.0,
    )
    bh = res.vs_buy_and_hold
    assert bh is not None
    assert bh["bh_total_return_pct"] == pytest.approx(25.0)
    assert bh["bh_annualized_pct"] == pytest.approx(25.0, abs=0.05)
    assert bh["strategy_annualized_pct"] == pytest.approx(18.0)
    assert bh["strategy_minus_bh_pct"] < 0


def test_vs_buy_and_hold_is_none_when_prices_not_provided() -> None:
    setups = [_record("2024-03-15T10:00:00+00:00", 1.0)]
    res = _build_result(setups)
    assert res.vs_buy_and_hold is None


def test_vs_buy_and_hold_is_none_on_degenerate_inputs() -> None:
    setups = [_record("2024-03-15T10:00:00+00:00", 1.0)]
    res = BacktestResult.from_setups(
        strategy_name="t",
        instrument="NDX100",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        setups=setups,
        params_used={},
        bh_close_start=0.0,  # invalid
        bh_close_end=125.0,
    )
    assert res.vs_buy_and_hold is None


def test_to_json_round_trip_preserves_new_metrics(tmp_path: Path) -> None:
    """Round-trip must preserve the four protocol §9 fields."""
    setups = [
        _record(f"2024-{m:02d}-15T10:00:00+00:00", 0.7) for m in range(1, 13)
    ]
    res = BacktestResult.from_setups(
        strategy_name="t",
        instrument="NDX100",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        setups=setups,
        params_used={},
        risk_per_trade_pct=1.5,
        bh_close_start=100.0,
        bh_close_end=110.0,
    )
    p = tmp_path / "r.json"
    res.to_json(p)
    loaded = BacktestResult.from_json(p)
    assert loaded.risk_per_trade_pct == pytest.approx(1.5)
    assert loaded.projected_annual_return_pct == pytest.approx(res.projected_annual_return_pct)
    assert loaded.outlier_robustness == res.outlier_robustness
    assert loaded.temporal_concentration == pytest.approx(res.temporal_concentration)
    assert loaded.vs_buy_and_hold == res.vs_buy_and_hold


def test_from_json_handles_legacy_payload_without_new_fields(tmp_path: Path) -> None:
    """Loading a legacy JSON (no protocol §9 fields) must use defaults."""
    setups = [_record(f"2024-{m:02d}-15T10:00:00+00:00", 1.0) for m in range(1, 4)]
    res = _build_result(setups)
    # Strip the new fields manually to simulate a pre-extension JSON.
    import json as _json

    p = tmp_path / "legacy.json"
    res.to_json(p)
    payload = _json.loads(p.read_text())
    for k in (
        "projected_annual_return_pct",
        "outlier_robustness",
        "temporal_concentration",
        "vs_buy_and_hold",
        "risk_per_trade_pct",
    ):
        payload.pop(k, None)
    p.write_text(_json.dumps(payload))
    loaded = BacktestResult.from_json(p)
    assert loaded.risk_per_trade_pct == 1.0  # default
    assert loaded.outlier_robustness == {}  # default factory
    assert loaded.temporal_concentration is None
    assert loaded.vs_buy_and_hold is None
    # projected_annual_return_pct still computes from mean_r/spm.
    assert loaded.projected_annual_return_pct == pytest.approx(
        loaded.mean_r * loaded.setups_per_month * 12.0 * 1.0
    )
