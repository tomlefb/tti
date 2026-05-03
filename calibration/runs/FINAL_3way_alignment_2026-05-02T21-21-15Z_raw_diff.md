# 3-way alignment — raw diff per instrument (2026-05-02T21-21-15Z)

Pair labels: **Duk** = Dukascopy bid M5 (parquet cache), **MT5** = MetaTrader 5 broker CFD export (`tests/fixtures/historical/`), **DBN** = Databento back-adjusted futures, Panama processing (`tests/fixtures/historical_extended/processed_adjusted/`).

Sample: 30 weekday dates drawn uniformly from each instrument's 3-source common window (numpy seed=42). All M5 timestamps where all three sources have a bar are kept; metrics are computed on that intersection.

## XAUUSD

- Common window: `2019-12-23 00:00:00+00:00` → `2026-04-29 23:55:00+00:00` (2319 days span)
- Sampled days (n=30): 2020-07-06, 2020-07-13, 2020-07-24, 2020-10-12, 2021-02-16, 2021-03-26, 2022-04-26, 2022-07-11, 2022-09-07, 2022-09-20, 2022-10-17, 2022-10-26, 2023-02-20, 2023-03-17, 2023-04-12, 2023-06-08, 2024-01-19, 2024-01-24, 2024-05-06, 2024-06-27, 2024-08-07, 2024-10-04, 2024-10-22, 2024-12-03, 2024-12-04, 2025-03-12, 2025-04-10, 2025-05-07, 2025-11-04, 2026-02-09
- Common M5 bars after intersection: **1544**

| Pair | N bars | Close MAD abs | Close MAD rel | Close p95 abs | Return Pearson | Body sign agree |
|---|---:|---:|---:|---:|---:|---:|
| Duk vs MT5 | 1544 | 0.4829 | 0.0240% | 2.6165 | 0.9994 | 0.8867 |
| Duk vs DBN | 1544 | 53.1956 | 2.7188% | 74.3350 | 0.9982 | 0.6010 |
| MT5 vs DBN | 1544 | 53.2925 | 2.7228% | 75.2770 | 0.9976 | 0.5356 |

Detailed OHLC quantiles (absolute / relative %):

| Pair | open p99 abs | high p99 abs | low p99 abs | close p99 abs | open p95 rel% | close p95 rel% |
|---|---:|---:|---:|---:|---:|---:|
| Duk vs MT5 | 7.0362 | 7.3621 | 6.6242 | 8.1393 | 0.1455% | 0.1498% |
| Duk vs DBN | 74.6937 | 74.7037 | 74.7120 | 74.6920 | 4.2067% | 4.2061% |
| MT5 vs DBN | 77.8354 | 77.7184 | 77.5870 | 77.6168 | 4.2650% | 4.2636% |

## NDX100

- Common window: `2022-10-20 09:00:00+00:00` → `2026-04-29 23:55:00+00:00` (1287 days span)
- Sampled days (n=30): 2023-01-24, 2023-02-22, 2023-04-03, 2023-05-12, 2023-06-16, 2023-08-04, 2023-11-27, 2023-12-04, 2023-12-13, 2024-01-22, 2024-01-30, 2024-03-19, 2024-05-01, 2024-05-28, 2024-06-04, 2024-06-14, 2024-10-08, 2025-01-03, 2025-02-18, 2025-02-20, 2025-03-07, 2025-04-07, 2025-05-08, 2025-07-04, 2025-07-21, 2025-09-19, 2025-12-02, 2025-12-15, 2026-02-02, 2026-02-12
- Common M5 bars after intersection: **7053**

| Pair | N bars | Close MAD abs | Close MAD rel | Close p95 abs | Return Pearson | Body sign agree |
|---|---:|---:|---:|---:|---:|---:|
| Duk vs MT5 | 7053 | 7.8086 | 0.0388% | 30.9948 | 0.9967 | 0.9553 |
| Duk vs DBN | 7053 | 1242.4932 | 6.8475% | 2369.6610 | 0.9926 | 0.9619 |
| MT5 vs DBN | 7053 | 1245.7862 | 6.8612% | 2371.4700 | 0.9897 | 0.9387 |

Detailed OHLC quantiles (absolute / relative %):

| Pair | open p99 abs | high p99 abs | low p99 abs | close p99 abs | open p95 rel% | close p95 rel% |
|---|---:|---:|---:|---:|---:|---:|
| Duk vs MT5 | 60.8494 | 60.8519 | 60.7470 | 60.7774 | 0.1604% | 0.1769% |
| Duk vs DBN | 2445.6798 | 2446.0152 | 2445.2650 | 2445.4790 | 14.5066% | 14.5061% |
| MT5 vs DBN | 2444.1240 | 2444.4600 | 2443.7720 | 2443.8980 | 14.5071% | 14.5081% |

## SPX500

- Common window: `2022-10-20 09:00:00+00:00` → `2026-04-29 23:55:00+00:00` (1287 days span)
- Sampled days (n=30): 2022-11-29, 2022-12-02, 2023-01-27, 2023-02-10, 2023-03-13, 2023-07-21, 2023-08-17, 2023-08-22, 2023-10-10, 2023-11-09, 2024-01-05, 2024-03-27, 2024-04-22, 2024-04-30, 2024-06-03, 2024-09-19, 2024-09-27, 2024-09-30, 2024-10-03, 2024-10-14, 2024-11-25, 2024-12-30, 2025-01-16, 2025-02-03, 2025-06-10, 2025-07-24, 2025-09-29, 2025-10-21, 2026-03-30, 2026-04-16
- Common M5 bars after intersection: **6450**

| Pair | N bars | Close MAD abs | Close MAD rel | Close p95 abs | Return Pearson | Body sign agree |
|---|---:|---:|---:|---:|---:|---:|
| Duk vs MT5 | 6450 | 0.3603 | 0.0071% | 1.5067 | 0.9928 | 0.9295 |
| Duk vs DBN | 6450 | 428.6545 | 8.0071% | 699.6000 | 0.9905 | 0.8670 |
| MT5 vs DBN | 6450 | 428.7483 | 8.0087% | 699.6500 | 0.9833 | 0.8504 |

Detailed OHLC quantiles (absolute / relative %):

| Pair | open p99 abs | high p99 abs | low p99 abs | close p99 abs | open p95 rel% | close p95 rel% |
|---|---:|---:|---:|---:|---:|---:|
| Duk vs MT5 | 4.6969 | 5.4945 | 5.5618 | 6.2356 | 0.0232% | 0.0284% |
| Duk vs DBN | 751.9637 | 752.2792 | 751.9832 | 752.2331 | 14.4428% | 14.4416% |
| MT5 vs DBN | 752.5255 | 748.8295 | 754.8895 | 752.0785 | 14.4436% | 14.4426% |
