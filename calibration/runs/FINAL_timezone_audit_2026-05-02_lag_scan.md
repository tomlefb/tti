# MT5 vs Dukascopy — wide-window lag scan

Cross-correlation of close-to-close 5-min returns at lags from
**−480 min to +480 min by 5-min steps**, on four single-month windows
chosen to bracket every DST regime.

A non-zero peak indicates that one source's timestamps are systematically
offset from the other's. A constant peak across instruments and months
within a regime points to a single systemic cause (timezone), not a
per-bar artefact.

## Per-month best lag and correlation

| Window           | Regime              | Instrument | Best lag | Peak corr | n bars |
|------------------|---------------------|------------|---------:|----------:|-------:|
| Jul 2025         | EU DST (EEST = UTC+3) | XAUUSD   | **+180 min** | 0.897 | 5664 |
| Jul 2025         | EU DST              | NDX100     | **+180 min** | 0.759 | 5396 |
| Jul 2025         | EU DST              | SPX500     | **+180 min** | 0.734 | 5412 |
| Sep 2025         | EU DST              | XAUUSD     | **+180 min** | 0.867 | 5394 |
| Sep 2025         | EU DST              | NDX100     | **+180 min** | 0.794 | 5196 |
| Sep 2025         | EU DST              | SPX500     | **+180 min** | 0.636 | 5189 |
| Dec 2025         | EET = UTC+2         | XAUUSD     | **+120 min** | 0.927 | 5445 |
| Dec 2025         | EET                 | NDX100     | **+120 min** | 0.900 | 5259 |
| Dec 2025         | EET                 | SPX500     | **+120 min** | 0.884 | 5257 |
| Feb 2026         | EET                 | XAUUSD     | **+120 min** | 0.895 | 5189 |
| Feb 2026         | EET                 | NDX100     | **+120 min** | 0.883 | 5001 |
| Feb 2026         | EET                 | SPX500     | **+120 min** | 0.885 | 5001 |

For reference, the correlation at lag 0 in every cell of the table is
near zero (`|corr| ≤ 0.04`), so the scan is unambiguous about the peak
being elsewhere.

The earlier multi-month scan (Sep–Nov 2025) showed mixed peaks
(`+120` for indices, `+180` for XAU) because the window straddles the
end-October EU DST transition; restricted to single months on either
side of the switch, every cell collapses onto a single offset that
matches the local broker timezone.

## Interpretation

The peak shifts by exactly one hour across the DST boundary, which
is the signature of a **broker server running on Athens / Cyprus time
(EET in winter, EEST in summer)**. MT5 returns its `time` field as
"POSIX seconds in broker wallclock", and the export script then casts
that to UTC without converting, so the parquet's `time` column is the
broker's local clock mislabelled as UTC.

Concretely: when the MT5 fixture says ``2025-09-15 14:00:00`` it
actually represents the bar `[11:00, 11:05) UTC` (UTC+3 in summer);
the same row in winter would represent `[12:00, 12:05) UTC` (UTC+2).

## Validation: apply the correction, re-measure

Shifting MT5's index by `−offset` before intersecting with Dukascopy
collapses the sign-agreement and return-correlation to the values
expected for two faithful captures of the same market:

| Window     | Instrument | Sign agree (before → after) | Return corr (before → after) |
|------------|------------|-----------------------------:|------------------------------:|
| Jul 2025   | XAUUSD     | 0.493 → **0.970** | 0.006 → **0.998** |
| Jul 2025   | NDX100     | 0.498 → **0.974** | −0.005 → **0.998** |
| Jul 2025   | SPX500     | 0.483 → **0.940** |  0.029 → **0.995** |
| Sep 2025   | XAUUSD     | 0.502 → **0.975** | −0.022 → **0.997** |
| Sep 2025   | NDX100     | 0.487 → **0.969** | −0.019 → **0.997** |
| Sep 2025   | SPX500     | 0.483 → **0.922** | −0.022 → **0.910** |
| Dec 2025   | XAUUSD     | 0.501 → **0.975** | −0.010 → **0.997** |
| Dec 2025   | NDX100     | 0.504 → **0.977** |  0.035 → **0.999** |
| Dec 2025   | SPX500     | 0.495 → **0.924** |  0.025 → **0.998** |
| Feb 2026   | XAUUSD     | 0.503 → **0.963** |  0.025 → **0.992** |
| Feb 2026   | NDX100     | 0.508 → **0.970** |  0.037 → **0.994** |
| Feb 2026   | SPX500     | 0.496 → **0.949** |  0.014 → **0.994** |

Sign agreement collapses from chance level to 0.92–0.98, and return
correlation from ~0 to 0.99+. The diagnosis is conclusive.
