# Operational risk simulation 20y — trend_rotation_d1 v1.1 cell 126/5/3 — FINAL

**Date**: 2026-05-04
**Cell**: 126/5/3 (gate-4-v1.1 selected)
**Model**: sequential Phase-1 attempts on $5K, +8 % target / -8 % total / -4 %-of-init daily limits
**Window**: 2006-01 → 2026-04 (20.3 y, Yahoo D1)
**Scripts**: `calibration/operational_risk_trend_rotation_d1_v1_1.py`
**Run**: `calibration/runs/operational_risk_trend_rotation_d1_v1_1_2026-05-04T17-52-51Z.md` (gitignored)

---

## Synthèse

**Sequential Phase-1 attempts model**: capital starts at $5K. Each attempt closes on first of: PASS (capital ≥ $5,400 = +8 %), FAIL_TOTAL (capital ≤ $4,600 = -8 % static floor), FAIL_DAILY (single calendar-day net P&L ≤ -$200). On close, reset to $5K and start the next attempt. The 20-y span thus contains a sequence of Phase-1 attempts, each independent.

| Risk | Attempts | PASS | FAIL_TOTAL | FAIL_DAILY | Pass rate | Worst attempt DD |
|---|---:|---:|---:|---:|---:|---:|
| **1.0 %** | **140** | **76** | 29 | 35 | **54.3 %** | -7.9 % |
| **0.5 %** | **66** | **41** | 11 | 14 | **62.1 %** | -8.0 % |

| Verdict |
|---|
| 1 % risk: ⚠️ **RISQUÉ MAIS ACCEPTABLE** (pass 54 %, 64 fails sur 140) |
| 0.5 % risk: ⚠️ **RISQUÉ MAIS ACCEPTABLE** (pass 62 %, 25 fails sur 66) |

Pre-spec verdict bands (from prompt):
- ≥ 80 % pass + ≤ 2 fails → **PHASE-1-COMPATIBLE**
- 50–80 % pass → **RISQUÉ MAIS ACCEPTABLE**
- 20–50 % pass → **NON-DÉPLOYABLE TEL QUEL**
- < 20 % pass → **STRUCTURELLEMENT INCOMPATIBLE**

The strategy sits firmly in the middle band (RISQUÉ MAIS ACCEPTABLE) at both risk levels.

---

## 1. Operational interpretation — combien coûte un Phase 1?

À 1 % risk, l'opérateur passe Phase 1 environ 1 fois sur 2 :
- Expected attempts to graduate (geometric): 1 / 0.543 ≈ **1.84 attempts**
- À ~$50-100 frais par attempt FundedNext: **expected cost ~$92-184 par graduation Phase 1**
- Phase 2 attendue cost identique → cumulative ~$184-368 par compte funded

À 0.5 % risk, pass rate monte à 62 %:
- Expected attempts: 1 / 0.621 ≈ **1.61**
- Cost ~$80-161 par graduation
- Magnitude proj annual divisée par 2 (puisque risk ÷ 2)

Le trade-off à mesurer:
- 1 % risk: expected_cost = 1.84 × $50 = $92, expected_proj_annual ≈ +30 %/y eq-weight (post H4 corrections)
- 0.5 % risk: expected_cost = 1.61 × $50 = $80, expected_proj_annual ≈ +15 %/y

À 0.5 % risk, projection annuelle tombe sur ou sous le §3 viability seuil 20 %.

---

## 2. Drawdown granularité mensuelle (1 % risk, monthly accounting)

Sur 244 mois (2006-01 → 2026-04):

| Métrique | Valeur |
|---|---:|
| Mois avec net P&L négatif | 98 / 244 (**40.2 %**) |
| Mois avec intra-month DD < -4 % init | 46 / 244 (warning zone) |
| Mois avec intra-month DD < -8 % init | 5 / 244 (would have busted intra-month) |
| **Pire séquence consécutive de mois négatifs** | **6 mois (2019-12 → 2020-05)** |

Worst 12 mois par intra-month DD (1 % risk):

| Month | n_trades | sum_r | net P&L $ | intra-month DD % | flag |
|---|---:|---:|---:|---:|:---:|
| 2010-04 | 6 | -2.71 | -$125 | **-12.7 %** | ❌ |
| 2020-09 | 9 | +14.05 | +$688 | **-12.4 %** | ❌ |
| 2024-12 | 7 | +9.46 | +$513 | **-9.8 %** | ❌ |
| 2011-01 | 5 | +0.15 | +$13 | **-8.8 %** | ❌ |
| 2012-03 | 2 | +2.65 | +$146 | **-8.3 %** | ❌ |
| 2022-12 | 8 | +27.67 | +$1388 | -7.8 % | ⚠️ |
| 2009-12 | 7 | -5.28 | -$286 | -7.8 % | ⚠️ |
| 2006-05 | 6 | -0.72 | -$13 | -7.4 % | ⚠️ |
| 2006-11 | 7 | +0.54 | +$36 | -7.3 % | ⚠️ |
| 2026-02 | 3 | +71.82 | +$3716 | -7.1 % | ⚠️ |
| 2019-05 | 7 | -6.38 | -$326 | -7.0 % | ⚠️ |
| 2014-07 | 6 | -7.02 | -$342 | -6.8 % | ⚠️ |

**Lecture**: 5 mois ont dépassé le seuil intra-mois -8 % de l'initial — ce sont les mois où une bust event aurait pu fire sans simulation reset. Plusieurs (2020-09, 2024-12, 2026-02) ont un net P&L mensuel POSITIF mais une drawdown intra-mois importante: typique d'une volatile-up-then-down trajectoire dans la même fenêtre.

---

## 3. Pire séquence négative — 2019-12 → 2020-05

6 mois consécutifs avec net P&L mensuel négatif. C'est le plus long stretch over 20y. Coïncide avec:
- Fin 2019 chop pré-COVID
- COVID drawdown Q1-2020
- Début recovery Q2-2020 (mais lag entrée/sortie panier rotation)

Sur cette période, la stratégie a probablement enchaîné plusieurs attempts FAIL au régime où le top-K rotait rapidement entre risk-on / risk-off sans capter la reprise rapide post-COVID.

---

## 4. FAIL pattern analysis (1 % risk)

### Fails by year

| Year | n fails | régime |
|---|---:|---|
| 2006 | 3 | normal |
| 2007 | 4 | pré-GFC |
| **2008** | **5** | **GFC volatility** |
| 2009 | 2 | post-GFC recovery |
| 2010 | 4 | flash-crash + sovereign debt |
| 2011 | 3 | euro crisis |
| 2012 | 1 | calme |
| 2013 | 1 | calme |
| 2014 | 1 | calme |
| **2015** | **7** | **Volatile-China + ECB QE** |
| **2016** | **5** | **Brexit + Trump** |
| 2018 | 5 | Vol-mageddon Feb + Q4 selloff |
| 2019 | 3 | reprise |
| **2020** | **7** | **COVID** |
| 2022 | 4 | Fed hike start |
| 2023 | 4 | continuing hike |
| 2024 | 2 | calme |
| 2025 | 3 | recent |

**Concentration**: 2015 (7), 2020 (7), 2008 (5), 2016 (5), 2018 (5) — les **5 années les plus volatiles** du 20-y window. Pas une concentration unique sur un régime — la stratégie peut buster lors de toute volatilité élevée, pas spécifiquement bull/bear.

### Fails by triggering asset

| Asset | n fails | commentaire |
|---|---:|---|
| **XAGUSD** | **5** | Silver les flash spikes (2011 silver crash, 2020) |
| US2000, US30, SPX500 | 4 each | Equities en volatilité (2008, 2018, 2020) |
| USOUSD | 3 | Oil price shocks (2008, 2014, 2020) |
| XAUUSD, UK100 | 2 each | |
| Autres (AUDUSD, GBPUSD, USDJPY, NDX, JP225) | 1 each | |

**Pas de concentration BTC** dans les triggering assets de fails. BTC contribue PASS (gros gains 2016-2017, 2020-2021, 2024-2025) mais ne déclenche pas de fail. Cohérent avec la mesure excl-BTC: BTC porte l'edge sur les hauts, mais ne porte pas le risque-bust.

---

## 5. Lecture comparée H1 risk vs 0.5 % risk

À 0.5 % risk:
- 66 attempts (vs 140) — chaque attempt prend ~2× plus de temps à atteindre +8 %
- 62.1 % pass rate (vs 54.3 %) — improvement de 8 points
- 25 fails (vs 64) — réduction de 39 fails sur 20 y
- Max attempt DD reste -8 % (la stratégie peut encore busteer même à 0.5 % risk via une séquence de mauvais trades)

**Trade-off magnitude**:
- 1 % risk: expected proj annual ~30 %/y (eq-weight excl-BTC corrigé), pass rate 54 %
- 0.5 % risk: expected proj annual ~15 %/y, pass rate 62 %
- Δ proj annual: -50 % (divisé par 2)
- Δ pass rate: +14 % (relatif)

À 0.5 % risk on échange 50 % de magnitude contre +8 points de pass rate. C'est un mauvais ratio si l'objectif est croissance long terme post-Phase-1.

---

## 6. Verdict opérationnel synthétique

**RISQUÉ MAIS ACCEPTABLE** dans les deux configurations (1 % et 0.5 % risk).

La stratégie 126/5/3:
- **Survit Phase 1 ~1 attempt sur 2** — chaque tentative coûte de l'argent réel
- **Genuine edge confirmé** par walk-forward 20-y stable (commit `93cd60a`) et excl-BTC (commit `1b1c36b`)
- **Mais magnitude réelle proche de la frontière §3 viability** une fois corrigée pour H4 risk-parity et fees
- **Pire DD intra-mois -12.7 %** sur 244 mois — significantly above Phase 1 -8 % limit

L'opérateur peut jouer cette stratégie en Phase 1 mais doit:
1. Anticiper ~2 attempts pour graduer (cost $100-200)
2. Accepter une variance haute mensuelle (40 % mois négatifs)
3. Surveiller les régimes de haute volatilité (2008/2015/2018/2020/2022 sur ce panel) qui amplifient les fail events

---

## 7. Caveats

- **Realised-P&L only**: open positions peuvent avoir unrealised drawdown non capturée. Le DD réel mark-to-market peut être plus profond. **Les bust counts ci-dessus sont des LOWER BOUNDS**.
- **No spread / commission / slippage** modélisés. Investigation H6+H7 (commit `fb374b1`) a montré +0.12 R/trade cost réel — à ajouter, ce qui pousserait DD plus profond et ferait baisser pass rate de 5-10 points.
- **Compounding par exit-timestamp**, pas entry-timestamp — léger biais sur sizing dans les overlapping trades.
- **Yahoo continuous futures** (GC, SI, CL) ont level offset vs FundedNext spot/CFD. Le signal momentum est préservé mais R magnitudes peuvent légèrement différer.

---

## 8. Path forward decision

À ce stade, l'opérateur a 5 lectures cohérentes du même résultat:

1. **Gate 4 v1.1 verdict**: REVIEW (5/9 PASS, drift +1.36 R) (commit `efe599e`)
2. **Investigation systématique**: 0 FAIL, 3 PARTIAL (H3 régime, H4 sizing × 3, H5 concentration) (commit `fb374b1`)
3. **Walk-forward 20y**: 11/11 sub-windows positives, edge multi-décennie (commit `a30e516`)
4. **Walk-forward excl-BTC**: edge structurel confirmé hors crypto (+0.81 R pooled) (commit `1b1c36b`)
5. **Operational risk** (cette analyse): RISQUÉ MAIS ACCEPTABLE, 54-62 % pass rate Phase 1

**Convergence**: edge réel, magnitude 15-30 %/y eq-weight corrigée, mais variance opérationnelle haute (~50 % bust rate sur Phase 1).

### Trois branches opérateur

**(A) PROMOTE gate 6 MT5 sanity à 1 % risk** — argument supporté par walk-forward et excl-BTC, mais opérateur accepte ~50 % bust rate Phase 1. Coûts attempts: $100-200 par graduation. ROI long-terme: positif si plusieurs comptes funded enchainés.

**(B) PROMOTE gate 6 MT5 sanity à 0.5 % risk** — pass rate 62 %, magnitude réduite à ~15 %/y. Sit on §3 viability frontier. Plus conservateur, mais réduit ROI à un niveau peu différenciant vs ETF World 6-8 %/y réel.

**(C) ARCHIVE pour incompatibilité opérationnelle marginale** — les 5 mois sur 244 avec intra-DD < -8 % réelle, plus le 6-mois losing streak 2019-12 → 2020-05, signalent une stratégie marginalement-incompatible Phase 1. L'opérateur peut juger que le bust rate ~50 % est trop élevé pour acceptabilité personnelle. Per spec v1.1 footer, classe non-viable, 5e archive.

**Lecture neutre**: l'analyse ne tranche pas seule. C'est un jugement opérateur sur:
- Tolérance personnelle à $100-200 frais par graduation
- Expected duration: ~3-6 mois pour une graduation à cette cadence (4.5 trades/mo × ~12-20 trades/attempt)
- Comparaison vs autres stratégies du backlog (HTF single-asset wick-sensitive, LTF M5/M15) qui n'ont pas encore été testées

Si l'opérateur veut une décision structurée, je recommande: **option (A) tester en Phase 1 réel** avec budget alloué pour 2-3 attempts max ($150-300). Si après 3 fails la stratégie n'a pas passé, basculer sur archive et explorer d'autres classes du backlog. Le coût d'opportunité est faible et la mesure réelle Phase 1 ferme définitivement le débat.

---

**Pytest count**: 587 (unchanged).
**Wallclock total**: 38 s compute + analyse.
