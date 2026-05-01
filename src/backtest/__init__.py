"""Backtest harness — tick-by-tick simulation of the production scheduler.

Phase B of the look-ahead remediation plan introduced this package so
backtests can call the leak-free detector path
(``build_setup_candidates(now_utc=tick)``) at every simulated
APScheduler firing instead of once per killzone with ``now_utc=None``.

See ``calibration/runs/FINAL_lookahead_audit_phase_a_complete_2026-05-01.md``
for the audit context that motivated this module.
"""
