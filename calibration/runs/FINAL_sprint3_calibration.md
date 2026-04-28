# Sprint 3 — Final calibration record

**Date**: 2026-04-28

## Final parameters (delta from Sprint 2)

| Key | Sprint 2 | Sprint 3 | Rationale |
|---|---|---|---|
| `MIN_SWING_AMPLITUDE_ATR_MULT` | 1.0 (single) | split per-TF | H4 needs more selectivity than H1/M5 — single key was triggering H4 pivots on intra-day retracements. |
| `MIN_SWING_AMPLITUDE_ATR_MULT_H4` | — | 1.3 | H4-only grid search optimum (F1 81.2%, within 0.6 pp of (2, 1.0); 8/19 sessions ≥80/80 — best of grid). |
| `MIN_SWING_AMPLITUDE_ATR_MULT_H1` | — | 1.0 | unchanged from Sprint 1 (geometric pivots — design choice, preserved). |
| `MIN_SWING_AMPLITUDE_ATR_MULT_M5` | — | 1.0 | new, default for `detect_mss`. |
| `BIAS_REQUIRE_H1_CONFIRMATION` | (implicit True) | False | Sprint 3 diagnostic: H1 confirmation rejected valid bias on clean trending days. |
| `PARTIAL_TP_RR_TARGET` | — | 5.0 | partial-exit cap on high-RR runners — variance reduction. |

All other Sprint 1+2 keys unchanged (MSS displacement, FVG size, sweep
buffer, dedup window, equal-H/L tolerances, etc.).

## Diagnostic chain that produced these decisions

1. **Initial setup integration**: 1/76 cells produced a setup
   (1.3% utilisation). Single setup = XAUUSD 2025-11-04 London short.
2. **Cascade diagnostic** (`run_setup_diagnostic.py`): the bias filter
   eliminates **89.5%** of killzone slots — dominant bottleneck. RR
   filter is the secondary bottleneck (75% rejection on POI-valid
   candidates).
3. **Bias deep-dive on 2025-10-15 XAUUSD** (`run_setup_diagnostic_dive.py`):
   H4 strict structure rejects an operator-clean trending bullish day
   because the intra-day retracement at 2025-10-09 20:00 UTC produces
   an LL @ 3944.72 that passes the unified `MIN_SWING_AMPLITUDE_ATR_MULT=1.0`
   filter and breaks the strict HH/HL ordering. H1 separately classifies
   the day as bullish; the H4∩H1 intersection kills the signal.
4. **Decision**: split amplitude per-TF + drop H1 confirmation. H1
   geometric calibration plateau (F1≈54%, Sprint 1 design choice) is
   preserved verbatim — the change is only in the daily-bias aggregator's
   default policy.
5. **H4 grid search at full granularity** (`run_grid_search_h4_only.py`):
   `lookback ∈ {2, 3}` × `atr_mult ∈ {1.0, 1.3, 1.5, 1.8, 2.0, 2.3, 2.5, 3.0}`.
   Tie-break per spec: when F1 is similar, prefer higher atr_mult.
   Optimum (lookback=2, atr_mult=1.3) selected — F1 within 0.6 pp of
   (2, 1.0), but more selective AND best per-session 80/80 pass-rate.
6. **Re-run integration**: **16/76 cells** with ≥1 setup
   (21% utilisation), 5 A + 11 B, all 4 pairs covered.
7. **RR=18.70 setup investigated** (`run_setup_diagnostic_dive.py`,
   then `setup_followup.md`): 8/8 opposing levels qualify ≥ MIN_RR;
   chosen target `asian_high` is the closest qualifying level (gap
   to second qualifier = 1.25 pts). The runner is far simply because
   the rally is already extended — not an artifact of the heuristic.
   `PARTIAL_TP_RR_TARGET=5.0` added so the operator can scale 50%
   out at TP1 (5R) and let the rest run to TP_runner.

## Acknowledged design limitations (carry forward to Sprint 4+)

- **CHOCH events forming after killzone close** (e.g. 21:00 UTC, 23:00 UTC)
  are not detected. Operator's strategy occasionally trades these. Decision
  deferred: extend killzones in a future sprint if these patterns prove
  material to operator's edge. Per docs/01 §6, this is currently *by design*
  ("outside killzones: no notifications fire").
- **Setups whose MSS confirms after killzone close** (`timestamp_utc >
  killzone_end`) are produced by detection but should be filtered at
  notification time per docs/01 §6. Sprint 4 deliverable; the orchestrator
  intentionally keeps them in its output for auditability.
- **H1 bias confirmation removed** due to F1 plateau (Sprint 1 finding); if
  future regression on live data is observed, `BIAS_REQUIRE_H1_CONFIRMATION`
  can be flipped back to `True` without code changes.
- **Sweep deduplication uses a 30-min window**; setups in volatile
  killzones may compress/expand in ways the dedup heuristic misses.
  Revisit if multi-setup-per-cell becomes noisy in live data.
- **Sprint 2 sweep-buffer calibration was deferred**; defaults still
  hold. Sprint 3 cascade did not flag sweep frequency as a bottleneck,
  so deferral remains acceptable.

## Integration result

| Metric | Value |
|---|---|
| Cells with ≥1 setup | 13/76 (17.1%) |
| Total setups | 16 |
| Grade distribution | 5 A + 11 B |
| Pairs covered | XAUUSD 4, NDX100 5, EURUSD 4, GBPUSD 3 |
| Killzone distribution | London 6, NY 10 |
| Direction balance | long 7, short 9 |
| POI distribution | FVG 7, OrderBlock 9 |
| RR_runner range | 3.0 – 18.7 (TP1 capped at 5.0 for runners > 5R) |

Operator target: ~half of the 18 reference dates → 8-12 setups.
Achieved: 16 (slightly above target). Acceptable for Sprint 3 close.

Sprint 4 will add notification gating, including the killzone-end
filter and quality-based message formatting.
