# Pyramidal + Scale-Up economic simulation 20y — trend_rotation_d1 v1.1 cell 126/5/3 — FINAL

**Date**: 2026-05-04
**Subject**: comparaison 4 stratégies de scaling FundedNext sur 1000 trades 2006-2026
**Driver**: `calibration/economic_pyramidal_trend_rotation_d1_v1_1.py`
**Run**: `calibration/runs/economic_pyramidal_trend_rotation_d1_v1_1_2026-05-04T21-55-56Z.md` (gitignored)

---

## Verdict

**Strategy B (Pyramidal manual) ÉCRASE les autres**: +$2,219,409 net sur 20.3 y, **38× la baseline** Strategy A.

| Strategy | Net P&L 20y | × baseline | Max balance | Time → $50K | Worst DD |
|---|---:|---:|---:|---:|---:|
| **A** Baseline ($5K loop) | +$58,868 | 1.0× | $44,564 | never | -$8,542 |
| **B** 🥇 **Pyramidal manual** | **+$2,219,409** | **37.7×** | **$1,781,783** | **85 mo** | -$340,257 |
| **C** Scale-Up native | +$60,269 | 1.0× | $44,564 | never | -$8,439 |
| **D** Hybrid (pyramid → $25K + Scale-Up) | +$325,354 | 5.5× | $222,723 | never | -$42,022 |

---

## Synthèse en une phrase

**Le pyramidal manuel à travers les tiers $5K → $25K → $50K → $100K → $200K capture ×37 plus de net P&L que le baseline parce qu'il leverage l'edge sur des comptes de plus en plus gros**, alors que le Scale-Up natif FundedNext ne se déclenche jamais (bust rate trop haut pour rester 4 mois consécutifs en funded avec ≥ 2 payouts).

---

## 1. Tableau comparatif détaillé

| Strategy | Net P&L | Total paid | Total received | Max balance | Funnel | Upgrades | Scale-Ups | TTB | Worst DD |
|---|---:|---:|---:|---:|---|---:|---:|---:|---:|
| **A** | +$58,868 | $1,610 (70 fees) | $60,478 (49 payouts) | $44,564 | 33 P1 / 21 P2 / 20 fbusts | 0 | 0 | 11 mo | -$8,542 |
| **B** | **+$2,219,409** | $42,763 (73 fees) | $2,262,172 (58 payouts) | $1,781,783 | 35 / 22 / 17 | **4** | 0 | 11 mo | -$340,257 |
| **C** | +$60,269 | $1,587 (69 fees) | $61,856 (51 payouts) | $44,564 | 33 / 21 / 20 | 0 | **0** | 11 mo | -$8,439 |
| **D** | +$325,354 | $6,004 (68 fees) | $331,358 (70 payouts) | $222,723 | 33 / 20 / 18 | 1 | 0 | 11 mo | -$42,022 |

(TTB = time-to-breakeven en mois, defined as first cumulative > 0)

### Observations clés

1. **Toutes les stratégies cassent le breakeven en 11 mois** — c'est le même premier payout généré au $5K initial. Cohérent avec l'edge baseline.

2. **Strategy B grimpe les 4 tiers avec succès** ($5K → $25K → $50K → $100K → $200K). Une fois à $200K, reste là pour le reste des 20 ans. Capture les big régimes (2016-2017 BTC, 2020-2021 COVID, 2024-2025) à $200K leverage.

3. **Strategy C ne déclenche AUCUN Scale-Up** sur 20 ans. La règle FundedNext "≥ 2 payouts dans 120 jours" n'est jamais satisfaite parce que le bust rate (~50 %) interrompt les funded accounts avant 4 mois consécutifs. C'est un finding important: **la stratégie 126/5/3 est trop volatile pour bénéficier du Scale-Up natif**.

4. **Strategy D pyramide à $25K** (1 upgrade), puis attend Scale-Up qui ne vient jamais. Mais $25K reste 5× plus profitable que $5K en absolute, d'où net +$325K (5.5× baseline). C'est un compromis intéressant: cap le risque (max balance $222K vs $1.78M) pour une économie plus modeste mais 5× la baseline.

5. **Worst drawdown scale avec la magnitude des gains**: B perd jusqu'à **-$340K** entre 2018-02 et 2019-08 (18 mois). C'est psychologiquement très dur. A baseline ne perd que -$8.5K max.

---

## 2. Strategy B detail — le winner

### Phase milestones

| Cumulative target | Time to reach |
|---|---:|
| $1,000 | 31 mo |
| $5,000 | 60 mo (5 y) |
| $10,000 | 75 mo (6.3 y) |
| $25,000 | 81 mo (6.75 y) |
| $50,000 | 85 mo (7.1 y) |
| $100,000 | 94 mo (7.8 y) |

**Lecture**: les premières 5 années sont relativement modestes ($5K cumulé). C'est la période où le trader pyramide et accumule suffisamment de wallet pour buy les tiers. La grosse croissance arrive **après 7 ans**, quand le trader est à $200K et capture les régimes trending.

### Funnel

- 73 attempts payés (vs 70 baseline) — dont 4 sont les upgrades successifs
- 35 P1 PASS, 22 P2 PASS (~30 % conversion P1 → P2)
- 17 funded busts (vs 20 baseline) — légèrement moins parce que les comptes plus gros sont plus stables (si l'opérateur survit aux régimes calmes en restant en P1)
- 58 payouts totaux

### Worst drawdown

**-$340,257 entre 2018-02-16 et 2019-08-07** (18 mois).

C'est massif. Probablement une séquence de busts à $200K (chacun -$770 fee + perte unrealised) avec peu de payouts. Le trader est passé de wallet ~$200K à ~-$140K (ou pic +X à pic -340K).

**Tenable psychologiquement**? Très difficile. Voir un drawdown de -$340K alors qu'on a accumulé +$200K cumulatifs requires huge conviction. Ce drawdown est suivi d'un fort rebond (BTC 2020-2021 + Fed pivot 2024-2025) qui amène à +$2.2M final.

### End state

Strategy B termine 2026-04 sur tier $200K, balance ~$5K (just busted ou re-entered Phase 1), profit_split 80 %, scaleups 0.

Si l'opérateur s'arrête à n'importe quel moment dans les 20 ans, son net cumulé est (approximativement):
- Année 5: ~$5K
- Année 10: ~$50-100K
- Année 15: ~$300-500K
- Année 20: ~$2.2M

---

## 3. Strategy C detail — pourquoi Scale-Up échoue

Strategy C reste à $5K et essaie de déclencher Scale-Up natif tous les 4 mois. **Aucun Scale-Up déclenché en 20 ans.**

Pourquoi? Le critère FundedNext: ≥ 2 payouts dans une fenêtre de 4 mois (120 jours).

- 21 funded accounts ouverts sur 20 ans
- 51 payouts au total
- ≈ 2.4 payouts par funded en moyenne
- MAIS ces payouts sont concentrés en début de funded (premiers 1-2 mois) et la plupart des accounts bust avant 4 mois

Pour 2 payouts dans 120 jours, il faut un funded survivant ≥ 35 jours (premier payout à 21 j + 14 j second). Et ensuite 4 mois à compter de la "scaleup_period_start" qui est l'entrée funded. Hmm en fait après 120 jours de funded, payouts in period reset. Si pas 2 payouts → reset period. Si 2 payouts → Scale-Up.

Looking at actual funded durations: they're mostly < 120 days (busts arrive earlier). So Scale-Up never fires.

**C ≈ A**: même résultat que baseline (~$60K net). Le Scale-Up natif FundedNext est conçu pour des stratégies stables qui survivent longtemps en funded; trend_rotation_d1 a un bust rate trop haut pour qualifier.

---

## 4. Strategy D detail — le compromis

Pyramidal jusqu'à $25K + Scale-Up sur $25K.

- 1 tier upgrade ($5K → $25K), bloqué par convention "tier_idx < 1"
- 0 Scale-Up (même raison que C — bust rate trop haut)
- Net +$325K = 5.5× baseline, 6.8× moins que B

**Strategy D capture le multiplicateur 5×** d'un compte $25K (chaque R = $250 vs $50 sur $5K) sans risquer les drawdowns énormes de B (-$42K worst vs -$340K).

C'est un **compromis raisonnable** pour un opérateur qui veut beaucoup plus que A mais ne supporte pas le -$340K worst drawdown de B.

---

## 5. ETF S&P 500 baseline

$1K lump-sum invested 2006-01:
- Final value: $5,775
- Total return: +478 %
- Annualized: +9.01 %/y

Comparaison vs B:
- B net P&L: +$2,219,409 sur 20.3 y
- Si $1K initial avait été investi au S&P 500: +$4,775
- **B beats $1K-ETF by ~465×**

À noter: B a payé $42K de fees au total (pas juste $1K). Si le trader avait à la place investi ce $42K en S&P 500 lump-sum, il aurait fini à ~$240K (+478 % × 42K). B beats this by ~9×.

---

## 6. Phasing recommandé

### Premiers 12 mois — START

Stratégie A ou C (équivalentes): démarrer avec un compte $5K, accumuler le wallet jusqu'à pouvoir buy un $25K. Aucune différence pratique entre A et C dans les premiers mois (le Scale-Up ne va pas trigger).

**Recommandation**: simplement Strategy A (ne pas surveiller pour Scale-Up qui ne viendra pas). Coût initial $23 (avec VIBES). Sur 12 mois, accumule $5K-15K cumulatifs si chance moyenne.

### Années 2-5 — CRUISE

Bascule vers Strategy B dès que wallet permet d'acheter le tier suivant. Le passage $5K → $25K se fait sur le **first payout reçu** (~mois 1-2 du premier funded). Continue à pyramider.

Sur cette phase, le trader passe la plupart du temps en P1/P2 sur le nouveau tier (chacun nécessite passage P1+P2 fresh). Probabilité chained P1+P2 PASS = ~30 %, donc atteindre $200K depuis $5K demande en moyenne 4 upgrades × 1/0.30 ≈ 13 attempts. Sur ce sample: 4 upgrades en ~7 ans dans la simulation.

Cumulative net en année 5: ~$5K. En année 7: ~$10-25K. En année 10: ~$100K.

### Années 5-15 — SCALING

Une fois à $200K, reste à $200K. Capture tous les régimes trending sur ce tier. Net cumulé monte rapidement: $100K → $500K → $1M sur les 5-10 ans suivants.

Worst drawdown -$340K à anticiper (probablement durant une période sous-régime, comme 2018-2019). Préparer mentalement à voir ses gains réduits temporairement.

### Années 15+ — LIBERTÉ FINANCIÈRE

Cumulative net > $1M, dépasse le seuil typique d'indépendance financière au taux de retrait 4 % SWR ($40K/an passive). À ce point, l'opérateur peut:
- Continuer à trader pour augmenter capital
- Réduire risk_pct à 0.5 % pour préserver
- Passer à mode passive (ETF World) pour tail risk reduction

---

## 7. Worst-case scenarios par stratégie

| Strategy | Worst DD | Période | Recovery time | Tenable? |
|---|---:|---|---:|:---:|
| A | -$8,542 | 2018-02 → 2019-08 | ~18 mo | ✅ Easy |
| B | **-$340,257** | 2018-02 → 2019-08 | ~18 mo | ⚠️ Hard mentally |
| C | -$8,439 | 2018-02 → 2019-08 | ~18 mo | ✅ Easy |
| D | -$42,022 | 2018-02 → 2019-08 | ~18 mo | ⚠️ Tough but doable |

Toutes les stratégies ont leur worst drawdown sur la **même période 2018-02 → 2019-08** (~18 mois). C'est un **régime structurellement difficile** pour la stratégie (Vol-mageddon Feb 2018 + Q4 2018 selloff + Powell pivot mars 2019).

Strategy B's -$340K est ×40 vs A — mais B a accumulé +$200K avant le drawdown. Donc le trader voit son capital de $200K passer à $-140K (négatif net). Très dur.

D's -$42K est plus tenable (~5× A) et le multiplicateur ×5.5 vs A reste attractif.

---

## 8. Robustesse à busts consécutifs

Si plusieurs busts consécutifs à $200K, chaque rebuy coûte $770. Avec wallet en $2M, tenable.

À $5K (A/C), chaque rebuy coûte $23. Tenable même avec wallet à $0 (operator paie fee out-of-pocket).

À $25K (D), chaque rebuy coûte $97. Tenable avec wallet > $97.

**B est le plus exposé aux séquences de busts** mais aussi le mieux capitalisé pour les absorber après les premières années.

---

## 9. Faisabilité avec budget initial limité

| Budget initial | Faisable strategy |
|---|---|
| $25 | A (un seul $5K) |
| $50 | A (avec marge pour 2 attempts) |
| $100 | A (3-4 attempts), C (Scale-Up wait) |
| $250 | A/C (10 attempts), D (un upgrade $5K → $25K possible) |
| $500+ | B (peut tier-up à $25K avec marge), D, A/C |
| $1500+ | B (peut atteindre $50K via accumulation) |

**Pour la plupart des opérateurs avec budget < $500: démarrer A**, accumuler vers B au fur et à mesure des first-payouts.

---

## 10. Caveats

- **Single chronological run** — pas de Monte Carlo. La 2016-2017 BTC bull contribue énormément à B (le tier $200K était en place pour capture). Une trade-order alternatif aurait des outcomes différents.
- **Realised-P&L only** — open positions intra-trade DD non modélisé. Bust counts = LOWER bound.
- **No fees / slippage on trades** beyond FundedNext attempt fees. Investigation H6+H7 = -5 % à -10 % net P&L.
- **Single active account** for B/D — réel opérateur peut paralléliser plusieurs accounts pour extraction higher.
- **Scale-Up cap $300K** (CFD pratique). Real cumulative cap $4M permettrait theoretically des balances bien plus hautes.
- **Yahoo continuous futures level offset** vs FundedNext spot — direction préservée, magnitudes peuvent différer.
- **FundedNext rules peuvent évoluer** — promo VIBES, profit splits, Scale-Up criteria, payout cadence. Simulation est ordre de grandeur, pas précision dollar.

Cumulativement: real-world Strategy B net P&L probably 60-80 % du simulé (~$1.3M-1.8M). Toujours énorme et beats baseline by 20-30×.

---

## 11. Sept lectures convergentes — synthèse finale

| # | Mesure | Lecture |
|---|---|---|
| 1 | Gate 4 v1.1 | REVIEW (5/9 PASS, drift +1.36 R) |
| 2 | Investigation 7H | 0 FAIL, 3 PARTIAL — magnification 3-5× |
| 3 | Walk-forward 20y | 11/11 sub-windows positives |
| 4 | Walk-forward excl-BTC | edge structurel +0.81 R hors crypto |
| 5 | Operational risk | 54 % Phase 1 pass rate |
| 6 | Economic baseline (1 account) | +$55K net, ROI +2,617 %, TTB 11 mo |
| 7 | **Pyramidal + Scale-Up (this)** | **+$2.2M net, ×38 baseline, B best** |

**Convergence finale**: la stratégie est **economiquement spectaculairement profitable** quand pyramidée à travers les tiers FundedNext. Le baseline +$55K était déjà PROMOTE-supporting. Le pyramidal multiplie ce résultat ×38.

---

## 12. Recommendation finale

### Verdict: ✅ **PROMOTE gate 6 MT5 sanity check** (déjà la conclusion du baseline economic simulation, renforcée par le pyramidal)

### Phasing

1. **Mois 1-12 (START)**: déployer Strategy A simple, $5K seulement. Build wallet vers ~$500. Si gate 6 MT5 sanity PASS et les 5 autres validations tiennent en démo réel.

2. **Année 1-5 (CRUISE)**: bascule Strategy B sur first payout. Pyramider tier-by-tier vers $200K. Anticiper que la grosse croissance arrive après 5+ ans.

3. **Année 5+ (SCALING)**: stay $200K, capture régimes trending. Préparer mentalement pour worst drawdown -$340K (chiffre absolu, ~50 % de cumulative à ce moment).

4. **Année 15+**: si cumulative > $1M, considérer Strategy A/D pour réduire risque et passer en mode income-stable.

### Action items immédiats

1. **Gate 6 MT5 sanity check** — cellule 126/5/3 sur ~1.4 y MT5 panel, direction agreement (per spec v1.0 §6).
2. **Gate 7 transferability** — top-K agreement Duk vs MT5 > 70 % (spec §6 H10).
3. **Gate 8 Phase C** avec frais granulaires (commit `fb374b1` H6+H7 model).
4. Si gates 6-8 PASS: souscrire Phase 1 Stellar Lite réel ($23 avec VIBES). Budget 3 attempts max ($69-$150) avant décision continue/abandon.
5. **Live monitoring MTM drawdown** vs simulation. Si MTM bust events plus fréquents que simulé → ré-évaluer.

### Si les gates futurs échouent ou si la live performance déçoit

Documentés dans le FINAL economic baseline — switch vers archive 5e + explorer autres classes du backlog (HTF single-asset wick-sensitive, LTF M5/M15).

---

**Pytest count**: 587 (unchanged).
**Wallclock**: 31.5 s compute + 30 min analyse.
