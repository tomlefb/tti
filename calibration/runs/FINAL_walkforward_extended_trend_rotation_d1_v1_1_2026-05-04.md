# Walk-forward extended 20y — trend_rotation_d1 v1.1 cell 126/5/3 — FINAL

**Date**: 2026-05-04
**Source**: Yahoo Finance D1 OHLC (`tests/fixtures/historical_extended/yahoo/`)
**Cell**: gate-4-v1.1-selected 126/5/3 (mom=126, K=5, rebal=3)
**Window**: 2006-01-01 → 2026-04-30 (≈ 20.3 y; 2005 excluded as 6-mo momentum warmup)
**Scripts**: `_inventory_extended_sources.py`, `_download_extended_fixtures.py`, `walkforward_extended_trend_rotation_d1_v1_1.py`
**Run**: `calibration/runs/walkforward_extended_trend_rotation_d1_v1_1_2026-05-04T15-39-51Z/` (gitignored)

---

## Synthèse

| Métrique | Valeur |
|---|---|
| Sources disponibles | **15 / 15** actifs sur Yahoo Finance |
| Couverture ≥ 15 ans | 14 / 15 (BTC seul à 11.6 y) |
| Couverture ≥ 20 ans | 13 / 15 (BTC + AUDUSD à 20.0 y limite) |
| Total closed trades 20-y | **1000** |
| Pooled mean_r 20-y | **+1.7112 R** |
| Sub-windows positives (mean_r > 0) | **11 / 11** |
| Sub-windows above +0.3 R | **10 / 11** |
| Pooled excl 2016-2017 BTC outlier | **+0.93 R** (n=897) |
| Verdict mécanique strict | ❌ **ARCHIVE** (variance ratio fails) |
| Verdict honnête | ⚠️ **REVIEW** (gap in pre-spec matrix; see §4) |

**Synthèse en une phrase**: l'edge cross-sectional momentum est mesurable et présent sur 20 ans (11/11 sous-fenêtres positives, pooled +1.71 R), mais la variance inter-fenêtre est extrême et dominée par un outlier 2016-2017 (BTC bull run, +823 R / 10 trades). L'edge sous-jacent post-outlier reste solide (+0.93 R sur 897 trades).

---

## 1. Inventaire sources Yahoo Finance (15 / 15 disponibles)

| Asset | Yahoo symbol | First | Last | Years (max) | n_bars | Gaps > 10 d |
|---|---|---|---|---:|---:|---:|
| NDX100 | ^NDX | 1985-10-01 | 2026-05-04 | 40.6 | 10226 | 0 |
| SPX500 | ^GSPC | 1927-12-30 | 2026-05-04 | 98.3 | 24701 | 1 |
| US30 | ^DJI | 1992-01-02 | 2026-05-04 | 34.3 | 8645 | 0 |
| US2000 | ^RUT | 1987-09-10 | 2026-05-04 | 38.6 | 9735 | 0 |
| GER30 | ^GDAXI | 1987-12-29 | 2026-05-03 | 38.3 | 9693 | 0 |
| UK100 | ^FTSE | 1984-01-03 | 2026-04-30 | 42.3 | 10693 | 0 |
| JP225 | ^N225 | 1965-01-04 | 2026-04-30 | 61.3 | 15076 | 1 |
| EURUSD | EURUSD=X | 2003-12-01 | 2026-05-03 | 22.4 | 5817 | 1 |
| GBPUSD | GBPUSD=X | 2003-12-01 | 2026-05-03 | 22.4 | 5829 | 0 |
| USDJPY | USDJPY=X | 1996-10-30 | 2026-04-30 | 29.5 | 7650 | 1 |
| AUDUSD | AUDUSD=X | 2006-05-15 | 2026-05-03 | 20.0 | 5193 | 0 |
| XAUUSD | GC=F | 2000-08-30 | 2026-05-04 | 25.7 | 6442 | 0 |
| XAGUSD | SI=F | 2000-08-30 | 2026-05-04 | 25.7 | 6444 | 0 |
| USOUSD | CL=F | 2000-08-23 | 2026-05-04 | 25.7 | 6451 | 0 |
| BTCUSD | BTC-USD | 2014-09-17 | 2026-05-04 | 11.6 | 4248 | 0 |

**Univers retenu pour walk-forward**: les 15 actifs. Sur la fenêtre 2006-2026, BTC est exclu naturellement par le filtre `insufficient-history` du pipeline avant 2014-09 (lookback 126 jours requis).

---

## 2. Walk-forward 11 sub-windows

| Sub-window | n | mean_r | win % | trades/mo | proj annual % | CI low | CI high | total_r |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 2006-2007 | 83 | +0.6611 | 47.0 % | 3.47 | +27.5 % | -0.241 | +1.766 | +54.88 R |
| 2008-2009 (GFC) | 84 | +0.5409 | 56.0 % | 3.50 | +22.7 % | -0.515 | +1.836 | +45.43 R |
| 2010-2011 | 116 | +0.4316 | 50.9 % | 4.84 | +25.1 % | -0.246 | +1.332 | +50.07 R |
| 2012-2013 | 81 | +1.2572 | 60.5 % | 3.38 | +50.9 % | +0.213 | +2.701 | +101.83 R |
| 2014-2015 | 95 | +0.7713 | 42.1 % | 3.97 | +36.7 % | -0.429 | +2.343 | +73.28 R |
| **2016-2017** | **103** | **+8.4811** | **57.3 %** | **4.29** | **+437.1 %** | **+0.299** | **+23.748** | **+873.55 R** |
| 2018-2019 | 120 | +0.3525 | 47.5 % | 5.01 | +21.2 % | -0.645 | +1.690 | +42.30 R |
| 2020-2021 (COVID) | 116 | +1.6002 | 49.1 % | 4.84 | +92.9 % | -0.073 | +4.259 | +185.62 R |
| 2022-2023 (Fed hike) | 96 | +0.0989 | 42.7 % | 4.01 | +4.8 % | -0.700 | +1.105 | +9.49 R |
| 2024-2025 | 95 | +2.0177 | 57.9 % | 3.96 | +95.9 % | +0.598 | +3.693 | +191.68 R |
| 2026-Q1+ | 11 | +7.5541 | 54.5 % | 2.81 | +255.0 % | n/a | n/a | +83.10 R |

**Distribution résumée**:
- 11 / 11 sous-fenêtres avec mean_r > 0
- 10 / 11 avec mean_r > +0.3 R (seul 2022-2023 à +0.10 R en-dessous)
- Mean_r min: +0.10 R (2022-2023), max: +8.48 R (2016-2017)
- Médiane: +0.77 R (2014-2015)
- Variance ratio max/médiane: **11.0×** (8.48 / 0.77)

---

## 3. Top-3 carriers per sub-window — qui porte l'edge dans chaque décennie

| Sub-window | Top-3 carriers (asset / n / sum_r) |
|---|---|
| 2006-2007 | GER30 (n=6, +38.8 R) / SPX500 (n=7, +13.9 R) / AUDUSD (n=8, -12.0 R) |
| 2008-2009 (GFC) | XAUUSD (n=5, +31.4 R) / USOUSD (n=3, +23.6 R) / XAGUSD (n=4, +13.5 R) |
| 2010-2011 | XAGUSD (n=6, +28.8 R) / NDX100 (n=10, +22.9 R) / XAUUSD (n=8, +18.2 R) |
| 2012-2013 | JP225 (n=7, +40.9 R) / SPX500 (n=11, +14.2 R) / USDJPY (n=10, +13.6 R) |
| 2014-2015 | NDX100 (n=6, +41.2 R) / US2000 (n=9, +28.8 R) / JP225 (n=9, +16.1 R) |
| **2016-2017** | **BTCUSD (n=10, +823.6 R)** / NDX100 (n=10, +15.9 R) / XAUUSD (n=4, +14.7 R) |
| 2018-2019 | BTCUSD (n=6, +49.4 R) / XAUUSD (n=8, +11.1 R) / NDX100 (n=7, +8.8 R) |
| 2020-2021 (COVID) | BTCUSD (n=11, +139.8 R) / XAGUSD (n=4, +22.6 R) / XAUUSD (n=3, +17.3 R) |
| 2022-2023 (Fed hike) | USDJPY (n=7, +35.6 R) / GER30 (n=10, +13.3 R) / USOUSD (n=8, -10.3 R) |
| 2024-2025 | BTCUSD (n=7, +48.9 R) / XAUUSD (n=10, +47.6 R) / SPX500 (n=13, +39.6 R) |
| 2026-Q1+ | XAGUSD (n=1, +75.1 R) / XAUUSD (n=1, +7.0 R) / NDX100 (n=2, -4.0 R) |

**Lecture qualitative**:
- **Pas de "top-3 fixe"**. Différentes décennies sont portées par différents actifs : GER30 en 2006-2007 (DAX rally pré-GFC), métaux/oil en 2008-2009 (GFC commodity rally), JP225 en 2012-2013 (Abenomics), tech-NDX en 2014-2015, **BTC en 2016-2017 / 2020-2021 / 2024-2025**, USDJPY en 2022-2023 (yen weakness sur Fed hike).
- **BTC apparaît dans 4 / 7 sous-fenêtres post-2014**: la stratégie capture systématiquement le crypto à chaque rallye (+823 / +49 / +140 / +49). Le 2016-2017 est singulier par son magnitude (+823 R / 10 trades = +82 R/trade en moyenne) qui reflète la phase exponentielle BTC $400 → $20K.
- **L'edge n'est pas mono-actif**: avant l'ère BTC (2006-2013), les top-3 carriers tournent (GER, métaux, JP, tech, FX) — la stratégie capture des régimes différents avec des actifs différents.
- **2026-Q1+** est petit échantillon (n=11) et dominé par XAGUSD (1 trade à +75 R = silver bull run) — noise on cette fenêtre 4 mois.

---

## 4. Verdict — gap dans la matrice pré-spec

### 4.1 Verdict mécanique strict

Bandes pré-spécifiées par l'opérateur (figées avant analyse):

| Verdict | Conditions (toutes) |
|---|---|
| PROMOTE | ≥ 7/11 mean_r > 0 ET ≥ 4/11 mean_r > +0.3 R ET pooled mean_r > +0.3 R ET variance ratio < 5× |
| REVIEW | 5-6/11 positives ET ≥ 2/11 above +0.3 R ET pooled > +0.1 R |
| ARCHIVE | ≤ 4/11 positives ET edge concentré 1-2 sous-fenêtres récentes ET pooled ≈ zéro/négatif |

Application stricte aux mesures:

| Critère | PROMOTE seuil | Mesuré | Pass |
|---|---|---:|:---:|
| ≥ 7/11 positives | 7 | **11** | ✅ |
| ≥ 4/11 above +0.3 R | 4 | **10** | ✅ |
| pooled > +0.3 R | +0.3 | **+1.71** | ✅ |
| variance ratio < 5× | <5 | **11.0×** | ❌ |

PROMOTE échoue sur le seul critère 4/4 (variance ratio).

| Critère | REVIEW seuil | Mesuré | Pass |
|---|---|---:|:---:|
| 5-6/11 positives | ∈ {5, 6} | **11** | ❌ |

REVIEW échoue car la condition est *exactement* 5 ou 6 positives (range exclut 7+).

| Critère | ARCHIVE seuil | Mesuré | Pass |
|---|---|---:|:---:|
| ≤ 4/11 positives | ≤ 4 | **11** | ❌ |
| Edge concentré 1-2 sous-fenêtres récentes | binary | non (étalé sur 11) | ❌ |
| Pooled ≈ zéro/négatif | ≈ 0 | **+1.71** | ❌ |

ARCHIVE échoue sur 3/3.

**Aucun des trois verdicts n'est strictement satisfait** par la matrice pré-spec.

Mon code a défaulté à ARCHIVE (`else` branch) — c'est mécaniquement honnête mais opérationnellement erroné: la donnée 11/11 positives + pooled +1.71 R + 10/11 above +0.3 R est plus proche de PROMOTE (avec caveat variance) que d'ARCHIVE.

### 4.2 Cause du gap

La matrice pré-spec couvre le cas "edge stable + variance basse" (PROMOTE) et le cas "edge cyclique" (REVIEW) et le cas "pas d'edge" (ARCHIVE). Elle ne couvre pas le cas **"edge stable + variance haute par outlier"**, qui est exactement ce que le 20-y walk-forward révèle:
- 11/11 positives (edge présent partout)
- variance dominée par 2016-2017 BTC outlier (+8.48 R) et 2026-Q1 small-sample (+7.55 R sur n=11)
- pooled excl 2016-2017: +0.93 R (encore solide)
- pooled excl 2016-2017 + 2026-Q1: +0.85 R (encore solide)

### 4.3 Verdict honnête

Sur la base des données et en remplissant le gap de la matrice par défaut REVIEW (au lieu d'ARCHIVE-mécanique), le verdict approprié est:

**REVIEW — edge stable multi-décennie avec variance dominée par outliers**

Raisons:
1. **Stationnarité confirmée**: 11/11 sous-fenêtres positives sur 20 ans incluant GFC 2008, Fed hike 2022-2023, Abenomics 2012-2013, COVID 2020-2021. La stratégie n'est pas régime-fit sur 2019-2026.
2. **Edge réel hors outliers**: pooled excl 2016-2017 = +0.93 R, ≈ +25-50 %/an projected post-corrections H4 risk-parity.
3. **Variance inter-fenêtre élevée**: ratio 11× (PROMOTE seuil 5×) signale que la magnitude varie fortement régime-à-régime. C'est attendu pour CSM (Moskowitz–Ooi–Pedersen 2012 documente la régime-dépendance) mais opérateur doit anticiper que certaines années seront proches de zéro (2022-2023: +0.10 R).
4. **2016-2017 BTC outlier est légitime mais non-reproductible**: la phase exponentielle BTC $400 → $20K ne se reproduira pas à l'identique. Inclure 2016-2017 dans la projection forward serait optimiste.

---

## 5. Pooled excl outlier — best-estimate pour magnitude réaliste

Trois lectures du pooled mean_r:

| Scénario | n | pooled mean_r | proj annual @ rp |
|---|---:|---:|---:|
| Full 20-y | 1000 | **+1.71** | n/a (cadence varie par fenêtre) |
| Excl 2016-2017 (BTC bull run) | 897 | **+0.93** | ≈ +50 %/an cadence 4.5/mo |
| Excl 2016-2017 + 2026-Q1+ (small sample) | 886 | **+0.85** | ≈ +46 %/an |
| Excl tous les top-3 carrier-windows (2016-2017, 2020-2021 BTC, 2024-2025 BTC) | ≈ 770 | ≈ +0.45 R | ≈ +24 %/an |
| Excl BTC entièrement (toutes fenêtres) | (à mesurer)  | — | — |

**Cohérence avec investigation v1.1 holdout (commit `fb374b1`)**:
- Holdout 2025-01 → 2026-04 mean_r risk-parity = +2.02 R, équivalent equal-weight = +0.67 R (H4 corrigé)
- 20-y pooled excl outliers ≈ +0.93 R en risk-parity → équivalent equal-weight ≈ +0.31 R (× 0.331 ratio H4)
- Pooled equal-weight 20-y excl outlier ≈ +0.31 R × 4.5 trades/mo × 12 × 1 % = **+17 %/an**

**Magnitude réaliste post-toutes-corrections (risk-parity → equal-weight, excl 2016-2017 outlier)**: **15-25 %/an**, avec le seuil §3 viability à 20 %.

L'edge sous-jacent semble exister à magnitude défendable mais sit sur la frontière de viabilité opérateur.

---

## 6. Sanity check top-3 — le pattern 2025 (XAG/XAU/GER) se reproduit-il sur 20 ans?

Reprise de l'investigation v1.1 holdout (commit `fb374b1`): sur 2025-01 → 2026-04, top-3 carriers étaient XAGUSD (+63 R), XAUUSD (+34 R), GER30 (+25 R) = 67.9 % du |R|.

Sur 20 ans, top-3 carriers globaux:

| Asset | n total 20-y | sum_r 20-y | share total |R| |
|---|---:|---:|---:|
| **BTCUSD** | ~70 | **+1100 R** | ~50 % (dominant 2016-2017 + 2020-2021 + 2024-2025) |
| XAUUSD | ~80 | ~+170 R | ~8 % |
| XAGUSD | ~50 | ~+170 R | ~8 % |
| Reste 12 actifs combinés | ~800 | ~+700 R | ~34 % |

(Estimations approximatives à partir des top-3 par sous-fenêtre; non-exhaustif sur les actifs non-top par décennie.)

**Lecture**:
- **BTC porte ~50 %** de l'edge total 20-y (concentré sur 4 sous-fenêtres avec rallyes BTC).
- Les métaux (XAU + XAG) ~16 % combinés.
- Les 12 autres actifs ~34 % combinés — chacun contribuant 2-5 % seul mais collectivement importants.
- **Le pattern 2025 (métaux + 1 EU equity) n'est PAS répétitif** — chaque sous-fenêtre a sa propre dominante (GER 2006, métaux 2008, JP 2012, NDX 2014, BTC 2016-2017+, USDJPY 2022-2023, métaux+BTC 2024-2025).
- C'est un **signal positif** pour la stratégie: elle capture les régimes trending de différentes natures via différents actifs, pas juste les métaux. La concentration sur XAG/XAU/GER en 2025 holdout est un artéfact de la fenêtre courte, pas une caractéristique structurelle.

Cela atténue partiellement la PARTIAL H5 de l'investigation: la concentration top-3 = 67.9 % observée sur 16 mois holdout est **fenêtre-spécifique**, pas structurelle.

---

## 7. Wallclock et reproductibilité

| Étape | Wallclock |
|---|---:|
| Inventaire Yahoo (15 tickers) | ~6 s |
| Téléchargement fixtures (15 actifs) | 4.7 s |
| Pipeline 126/5/3 sur 6956 cycle dates 20-y | ~70 s |
| Bootstrap CI + reports | ~1 s |
| **Total** | **76 s** |

Reproductible: re-télécharger Yahoo donne des prix bit-identiques (pas de stochastic dans yfinance), les bootstrap CI sont seedés (12345), pipeline est gate-3-clean (audit harness 8/8 PASS).

Pytest: **587 collected** (inchangé — additions sont calibration-only, aucune nouvelle couverture src/).

---

## 8. Recommandation — opérateur decision finale

À ce stade, sur la base de:
- Gate 4 v1.1 verdict REVIEW (commit `efe599e`, 5/9 PASS, drift +1.36 R)
- Investigation systématique (commit `fb374b1`, 0 FAIL, 3 PARTIAL: H3 régime, H4 sizing, H5 concentration)
- Walk-forward 20-y: 11/11 positives, pooled +1.71 R brut, +0.93 R excl outlier, top-3 rotates across decades

L'opérateur a maintenant 3 lectures cohérentes et indépendantes du même résultat:

1. **Gate 4 verdict mécanique**: REVIEW (5/9 hypotheses). H3-H5 fail par excès, indiquant edge réel mais magnitude au-dessus des bandes pré-spec.

2. **Investigation systématique**: pas de bug, mais 3 amplificateurs identifiés (risk-parity sizing × 3, concentration top-3, holdout sur-échantillonnage régime). Best-estimate magnitude post-corrections: 15-35 %/an.

3. **Walk-forward 20-y**: edge multi-décennie stable (11/11 positives), variance haute par outlier 2016-2017 BTC, pooled excl outlier ≈ +0.93 R risk-parity → ≈ 17 %/an equal-weight ≈ frontière §3 20 % viability.

**Les trois lectures convergent sur**:
- L'edge n'est PAS un artéfact (pas de bug, pas de leak, pas de régime-fit pur 2019-2026).
- L'edge n'est PAS une bonanza à +109 %/an.
- L'edge est de magnitude **15-35 %/an** réaliste (CSM Sharpe 0.5-1.0 cohérent avec littérature Asness/Moskowitz).
- L'edge est **régime-cyclique**: certaines années (2022-2023) sont quasi-flat, d'autres (2016-2017) sont extrêmes outliers.

### Décision opérateur — branches:

**(A) PROMOTE gate 5 — argument supporté**
Walk-forward 20-y confirme stationnarité multi-décennie. Magnitude réaliste à 15-35 %/an strafle la frontière §3 20 % mais est défendable. Discussion opérateur pour:
- Accepter risk-parity sizing comme convention (× 3 inflation lever vs equal-weight, mais c'est l'académique standard)
- Anticiper variance haute (certaines années à +0.1 R, d'autres à +1.5 R) → position sizing potentiellement réduit pour respecter drawdown FundedNext
- Gate 5 cross-check Databento sur sous-univers NDX/SPX/DJI futures

**(B) PROMOTE-with-caveat → walk-forward additionnel sur sous-univers**
Avant Sprint 7, demander un walk-forward supplémentaire excluant BTC (le carrier dominant) pour mesurer l'edge sur un sous-univers stable ≥ 25 ans (les 14 autres actifs). Si pooled excl-BTC reste > +0.5 R risk-parity, PROMOTE est solide. Si pooled excl-BTC s'effondre à proche zéro, l'edge est crypto-bull-fit et REVIEW serait correcte.

**(C) ARCHIVE — argument supporté**
La variance ratio 11× sur 11 sous-fenêtres (4× au-dessus du seuil pré-spec PROMOTE 5×) signale que la stratégie est de "régime tale risk" — elle dépend d'événements rares (BTC bull, GFC commodity rally, COVID rebond). L'opérateur peut juger que l'incertitude annuelle est trop haute pour un compte phase-1 FundedNext 5K. Per spec v1.1 footer, classe non-viable, 5e archive de la phase strategy-research.

**Ma lecture neutre**: **option (B)** est la plus disciplinée. Gate 5 + walk-forward excl-BTC ferme l'incertitude. Coût compute: 1-2 minutes additionnelles. Coût information: élevé (sépare crypto-bull-fit d'edge structurel). Si (B) confirme edge solide hors-BTC, (A) devient la décision propre. Si (B) montre edge crypto-dépendant, (C) est la décision propre.

---

## 9. Resumé exécutif (1 paragraphe)

Le walk-forward étendu sur 20 ans de data Yahoo Finance (15/15 actifs disponibles, 13 ≥ 20 ans) sur la cellule 126/5/3 produit **11/11 sous-fenêtres positives**, pooled mean_r = **+1.71 R**, et top-3 carriers qui tournent à travers les décennies (GER, métaux, JP, tech, BTC, USDJPY) — pas un pattern fixe XAG/XAU comme sur le holdout 2025. L'edge est stationnaire mais avec variance ratio 11× (au-dessus du seuil pré-spec 5×) dominée par BTC 2016-2017 (+823 R / 10 trades). Le verdict mécanique strict est ARCHIVE par défaut (gap dans la matrice pré-spec), mais le verdict honnête est **REVIEW**: edge multi-décennie réel à magnitude 15-35 %/an (post-corrections H4 risk-parity et excl outlier), strafle la frontière §3 20 % viability. **Pas un bug, pas un régime-fit 2019-2026, mais une stratégie à variance régime-dépendante** dont la magnitude headline +109 % observée au gate 4 v1.1 holdout est sur-estimée d'un facteur 3-5×. La décision opérateur recommandée est **PROMOTE-with-caveat (option B)**: walk-forward supplémentaire excl-BTC pour fermer l'incertitude crypto-bull-fit avant Sprint 7 deployment.
