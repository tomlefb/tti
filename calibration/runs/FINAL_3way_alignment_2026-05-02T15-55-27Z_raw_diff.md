# 3-way alignment — raw diff per instrument (2026-05-02T15-55-27Z)

Pair labels: **Duk** = Dukascopy bid M5 (parquet cache), **MT5** = MetaTrader 5 broker CFD export (`tests/fixtures/historical/`), **DBN** = Databento back-adjusted futures, Panama processing (`tests/fixtures/historical_extended/processed_adjusted/`).

Sample: 10 weekday dates drawn uniformly from each instrument's 3-source common window (numpy seed=42). All M5 timestamps where all three sources have a bar are kept; metrics are computed on that intersection.

## XAUUSD

- Common window: `2025-06-20 14:00:00+00:00` → `2026-04-27 22:45:00+00:00` (311 days span)
- Sampled days (n=10): 2025-07-16, 2025-07-17, 2025-07-18, 2025-08-21, 2025-10-29, 2025-10-30, 2026-01-02, 2026-01-21, 2026-02-06, 2026-03-10
- Common M5 bars after intersection: **578**

| Pair | N bars | Close MAD abs | Close MAD rel | Close p95 abs | Return Pearson | Body sign agree |
|---|---:|---:|---:|---:|---:|---:|
| Duk vs MT5 | 578 | 18.3499 | 0.4134% | 50.3505 | 0.9573 | 0.5104 |
| Duk vs DBN | 578 | 1.9594 | 0.0460% | 8.5220 | 0.9980 | 0.6505 |
| MT5 vs DBN | 578 | 18.5288 | 0.4172% | 51.2115 | 0.9566 | 0.4048 |

Detailed OHLC quantiles (absolute / relative %):

| Pair | open p99 abs | high p99 abs | low p99 abs | close p99 abs | open p95 rel% | close p95 rel% |
|---|---:|---:|---:|---:|---:|---:|
| Duk vs MT5 | 61.3680 | 63.0687 | 63.2095 | 62.6200 | 1.0789% | 1.0902% |
| Duk vs DBN | 14.9029 | 19.7226 | 10.6910 | 14.2087 | 0.1977% | 0.1979% |
| MT5 vs DBN | 63.1699 | 66.6653 | 63.2314 | 62.9808 | 1.0783% | 1.0813% |

## NDX100

- Common window: `2025-06-20 11:15:00+00:00` → `2026-04-27 22:45:00+00:00` (311 days span)
- Sampled days (n=10): 2025-08-14, 2025-10-09, 2025-10-22, 2025-10-31, 2025-11-18, 2025-12-08, 2026-01-05, 2026-02-13, 2026-03-02, 2026-03-30
- Common M5 bars after intersection: **2419**

| Pair | N bars | Close MAD abs | Close MAD rel | Close p95 abs | Return Pearson | Body sign agree |
|---|---:|---:|---:|---:|---:|---:|
| Duk vs MT5 | 2419 | 60.4501 | 0.2444% | 194.5822 | 0.9232 | 0.5110 |
| Duk vs DBN | 2419 | 115.5466 | 0.4721% | 382.7773 | 0.9911 | 0.9330 |
| MT5 vs DBN | 2419 | 136.3623 | 0.5550% | 318.0400 | 0.9072 | 0.4965 |

Detailed OHLC quantiles (absolute / relative %):

| Pair | open p99 abs | high p99 abs | low p99 abs | close p99 abs | open p95 rel% | close p95 rel% |
|---|---:|---:|---:|---:|---:|---:|
| Duk vs MT5 | 271.5534 | 270.0053 | 277.8258 | 271.4704 | 0.7783% | 0.7796% |
| Duk vs DBN | 401.4344 | 387.1432 | 406.5000 | 397.6800 | 1.6420% | 1.6404% |
| MT5 vs DBN | 477.1512 | 469.1116 | 486.3204 | 475.7700 | 1.3368% | 1.3288% |

## SPX500

- Common window: `2024-11-26 16:45:00+00:00` → `2026-04-29 16:50:00+00:00` (519 days span)
- Sampled days (n=10): 2024-12-31, 2025-02-18, 2025-04-15, 2025-05-27, 2025-07-15, 2025-10-13, 2025-11-20, 2025-12-18, 2026-03-05, 2026-04-13
- Common M5 bars after intersection: **2405**

| Pair | N bars | Close MAD abs | Close MAD rel | Close p95 abs | Return Pearson | Body sign agree |
|---|---:|---:|---:|---:|---:|---:|
| Duk vs MT5 | 2405 | 14.3265 | 0.2268% | 39.1074 | 0.9729 | 0.5077 |
| Duk vs DBN | 2405 | 201.0227 | 3.2177% | 333.3870 | 0.9957 | 0.9056 |
| MT5 vs DBN | 2405 | 201.8410 | 3.2296% | 333.2500 | 0.9692 | 0.4748 |

Detailed OHLC quantiles (absolute / relative %):

| Pair | open p99 abs | high p99 abs | low p99 abs | close p99 abs | open p95 rel% | close p95 rel% |
|---|---:|---:|---:|---:|---:|---:|
| Duk vs MT5 | 81.6754 | 82.1184 | 82.4974 | 82.0777 | 0.6127% | 0.6150% |
| Duk vs DBN | 334.0925 | 334.1536 | 334.0390 | 334.0856 | 5.4830% | 5.4815% |
| MT5 vs DBN | 350.5900 | 349.7092 | 350.0348 | 350.4848 | 5.4892% | 5.4907% |
