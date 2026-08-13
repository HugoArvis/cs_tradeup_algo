# cs_tradeup_algo

Aide à la décision pour les **trade-up contracts CS2** : calcule l'espérance de
gain nette après frais et la variance de chaque contrat possible, puis classe
les candidats en privilégiant le risque faible.

> **Aucun trade-up n'est garanti.** La sortie est tirée au hasard parmi
> plusieurs skins possibles. Cet outil optimise une **espérance** sur un grand
> nombre de contrats — sur un seul contrat, le résultat reste binaire. Un
> classement « +12 % d'EV » ne dit rien de ce qui sortira la prochaine fois.

## Installation

```bash
git clone <url-du-depot>
cd cs_tradeup_algo
python -m pip install -e ".[dev]"   # PAS `pip install` : voir ci-dessous
python -m scripts.build_db          # construit data/collections.json (~1450 skins)
```

Aucune dépendance runtime : uniquement la bibliothèque standard.

### Sur une nouvelle machine

Trois choses **ne sont pas** dans le dépôt, volontairement, et doivent être
recréées :

| Fichier | Pourquoi il est absent | Comment le retrouver |
|---|---|---|
| `.env` | contient votre clé API | copiez `.env.example` et renseignez la clé |
| `data/collections.json` | régénérable, ~2 Mo | `python -m scripts.build_db` |
| `data/prices.db` | cache jetable, périmé vite | se remplit tout seul |
| `data/journal.db` | vos achats et contrats réels | reste sur la machine où vous achetez |

Le journal ne se synchronise pas : si vous suivez des contrats depuis les deux
PC, vous aurez deux historiques distincts. Tenez le suivi d'achats sur une seule
machine, ou copiez `data/journal.db` à la main.

> **Utilisez `python -m pip`, pas `pip`.** Sur une machine où plusieurs Python
> cohabitent, `pip` peut appartenir à une autre version que `python` et
> l'installation échoue (`requires a different Python: 3.9 not in '>=3.11'`).
> `python -m pip` installe forcément dans le Python qui exécutera le code.

> **La commande s'invoque `python -m tradeup.cli`.** Le raccourci `tradeup`
> n'existe que si le dossier `Scripts/` de votre Python est dans le `PATH`, ce
> qui n'est pas le cas par défaut sous Windows. `python -m tradeup.cli`
> fonctionne toujours, et depuis n'importe quel dossier une fois installé.

Sans installer du tout, depuis la racine du projet :

```bash
export PYTHONPATH=src        # bash / Git Bash
$env:PYTHONPATH = "src"      # PowerShell
```

## Utilisation

Application locale (recommandé) :

```bash
python -m tradeup.web        # ouvre le navigateur, tout se fait à la souris
```

En ligne de commande :

```bash
python -m tradeup.cli plan "The Bank Collection" --html
python -m tradeup.cli db --list                    # état de la base statique
python -m tradeup.cli inspect "The Recoil Collection" --rarity mil-spec
python -m tradeup.cli price "AK-47 | Redline (Field-Tested)" --fresh
python -m tradeup.cli scan --rarity mil-spec --collections "The Recoil Collection" --detail
python -m tradeup.cli scan --rarity restricted --offline --rank safety
python -m tradeup.cli cache --prune 30
```

Le premier scan en ligne est lent (limite Steam ~15 requêtes/min) ; il remplit
le cache SQLite. Les scans suivants peuvent tourner en `--offline`.

## Les règles modélisées

| Règle | Implémentation |
|---|---|
| 10 entrées de même rareté, même type | `ev.evaluate` (rejette les lots mixtes) |
| Sortie à la rareté immédiatement supérieure | `Rarity.next_up` |
| Probabilité d'une sortie | `ev.outcome_probabilities` |
| Float de sortie | `wear.output_float` |
| Frais Steam (15 % sur le net vendeur) | `fees.steam_net_proceeds` |

### Probabilité d'une sortie

Chaque objet d'entrée dépose un ticket pour **sa** collection ; le tirage est
uniforme sur l'ensemble des paires (ticket, sortie possible de sa collection) :

```
P(sortie s ∈ collection C) = n_C / Σ_C' ( n_C' × k_C' )
```

où `n_C` = entrées venant de C, `k_C` = skins de sortie de C à la rareté cible.

Conséquence contre-intuitive : à nombre d'entrées égal, une collection avec
**peu** de sorties possibles est globalement **moins** probable. Cinq entrées
d'une collection à 2 sorties + cinq d'une collection à 10 sorties donnent
16,7 % / 83,3 %, pas 50/50.

### Float de sortie

```
moyenne     = (1/10) × Σ floats absolus des entrées
float_sortie = moyenne × (max_cible − min_cible) + min_cible
```

Deux pièges évités : la moyenne porte sur les floats **absolus** (pas
normalisés dans le range de l'entrée), et le remap utilise le range du skin de
**sortie**. C'est pourquoi un skin à `min_float = 0.10` ne peut jamais sortir en
Factory New, quelles que soient les entrées.

## L'astuce qui rend le scan traitable

À recette fixée, l'EV ne dépend du float d'entrée **que** par le palier d'usure
des skins de sortie : elle est **constante par morceaux**. `wear.wear_breakpoints`
calcule les frontières exactes, et l'optimiseur n'évalue qu'un point par palier
— au lieu d'échantillonner `[0, 1]` en aveugle. Sur chaque palier, la meilleure
moyenne est la **plus haute** admissible : même sortie, entrées moins chères.

Le choix des entrées à budget de float donné est ensuite un problème de coût
minimal sous contrainte, résolu exactement par une DP sur frontière de Pareto
(`generator.cheapest_selection`).

### Pourquoi ça compte : seule la *moyenne* est contrainte

C'est le levier d'optimisation principal, et il est facile à manquer. Pour
obtenir une sortie Factory New il ne faut **pas** dix entrées Factory New — il
faut que leur *moyenne* passe sous la frontière. Mélanger des entrées à bas
float (chères) avec des entrées à haut float (bon marché) donne la même sortie
pour moins cher.

Sur le cas de test `test_optimiseur_melange_les_usures_pour_viser_la_moyenne` :
7 entrées FN à 2,00 € + 3 entrées FT à 0,90 € = **16,70 €** au lieu de 20,00 €
en tout-FN, pour une sortie identique. Un optimiseur naïf laisse 17 % sur la
table.

## L'effet de falaise (le piège principal)

Le corollaire de « l'EV est constante par morceaux » est brutal : **aux
frontières, elle est discontinue**. Mesuré sur *The Fracture Collection*, en
faisant passer la moyenne des floats d'entrée de 0,0650 à 0,0705 :

| Sortie | avg = 0,0650 | avg = 0,0705 |
|---|---|---|
| Tec-9 \| Brother | FN — 10,73 € | MW — **0,85 €** |
| MAG-7 \| Monster Call | FN — 4,12 € | MW — 1,00 € |
| MAC-10 \| Allure | FN — 3,70 € | MW — 0,92 € |
| **EV nette** | **4,88 €** | **1,73 €** |
| **Profit** | **+1,08 €** | **−2,07 €** |

**5,5 millièmes de float font basculer le contrat de +28 % à −54 %.**

Pire : le coût des entrées est minimal juste sous une frontière, donc un
optimiseur naïf **s'y gare systématiquement**. Ce n'est pas de la malchance,
c'est un biais structurel — l'optimiseur exploite le fait que le modèle traite
le float comme choisi alors que c'est un *tirage*.

D'où deux garde-fous :

- `--float-safety` (défaut 0,005) interdit de viser dans cette bande sous une
  frontière. Ne le mettez à 0 que si les floats sont vérifiés un par un via
  CSFloat — auquel cas le tirage n'en est plus un.
- `--min-cliff` rejette les contrats dont la marge reste trop faible. Chaque
  candidat affiche sa marge, annotée `SERREE` ou `CONFORTABLE`.

Un contrat à forte EV mais marge `SERREE` n'est pas une opportunité : c'est un
pari sur votre précision de sourcing.

## Ne mélangez pas les marchés

Le porte-monnaie Steam n'est pas retirable : c'est une monnaie captive, qui vaut
donc moins que de l'argent réel. Les prix Steam sont mécaniquement plus élevés
pour cette raison. Mesuré sur *The Bank Collection*, tout en USD :

| Stratégie | Achat | Revente nette | Profit |
|---|---|---|---|
| Steam → Steam | 19,12 | 21,00 | **+1,88 (+9,8 %)** — en crédit Steam captif |
| CSFloat → CSFloat | 11,10 | 16,02 | **+4,92 (+44,3 %)** — argent réel |
| Steam → CSFloat | 19,12 | 16,02 | **−3,10 (−16 %)** |

Acheter sur Steam pour revendre sur CSFloat revient à acheter dans le marché
cher pour vendre dans le marché bon marché : on paie l'écart de change entre
monnaie captive et argent réel. `--sell-market csfloat` permet de mesurer cette
combinaison — avec `--currency USD`, sans quoi les montants mélangent EUR et USD.

## Classement

Par défaut `risk_adjusted` = profit espéré / écart-type. Un contrat à +2 € d'EV
pour 3 € d'écart-type passe devant un contrat à +5 € pour 40 € d'écart-type —
ce qui correspond à l'objectif « EV positive, variance minimale ».

Autres critères : `--rank ev`, `--rank roi`, `--rank safety`
(= P(profit) d'abord).

Chaque candidat affiche aussi le **pire cas** et la **CVaR 25 %** (perte moyenne
sur le quart des tirages les moins favorables) : deux contrats de même EV ne se
valent pas si l'un peut perdre 80 % du capital.

## Hypothèses de modélisation, et leurs limites

| Hypothèse | Réglage | Risque si mal calibrée |
|---|---|---|
| Décote de sécurité sur la revente | `--margin` (5 %) | EV surestimée |
| Float réellement trouvable dans un palier | `--float-pct` (0.15) | Contrats **non réalisables** |
| Prix de vente = min(lowest, median) | `conservative_sell` | EV surestimée |
| Float d'entrée = valeur choisie, pas tirée | `--float-safety` | **EV effondrée** (voir falaise) |
| Frais d'achat/revente | `--buy-fees`, `--sell-fees` | EV surestimée |
| Liquidité de la sortie | `--min-volume` | Skin invendable au prix affiché |

`--float-pct` est le paramètre le plus délicat : il suppose qu'on peut acheter
un objet à 15 % du bas de son palier d'usure. C'est réaliste sur CSFloat, qui
permet de filtrer par float. **Ce n'est pas réaliste sur Steam**, où l'on
n'achète pas un float précis — dans ce cas montez à `--float-pct 0.5`.

## État du projet

| Étape | État |
|---|---|
| Récupération des prix Steam (rate-limit, cache SQLite) | fait |
| Base statique collections / skins / raretés / floats | fait (ingestion CSGO-API) |
| Moteur d'EV : distribution, float de sortie, EV nette, variance | fait |
| Génération des combinaisons à scanner | fait (mono- et bi-collection) |
| Filtrage / scoring / liquidité | fait |
| Vérification des prix avant exécution | partiel — `price --fresh` à la main |
| Liste d'achat actionnable + marge de falaise | fait |
| Source CSFloat — parsing et sélection | testé (14 tests, client HTTP simulé) |
| Source CSFloat — schéma de réponse | **vérifié sur l'API réelle** |
| Commande `plan` : panier depuis les offres réelles | fait |
| Rapport HTML avec liens directs vers les annonces | fait |
| Application web locale (choix de collection, plans à la demande) | fait |
| Buff163 | non commencé |

### Prochaines étapes suggérées

1. **Brancher CSFloat aussi dans `scan`.** La commande `plan` utilise les prix
   et floats réels, mais `scan` reste sur Steam. Un scan CSFloat de bout en bout
   éviterait de passer par un pré-filtrage Steam dont les prix ne sont pas ceux
   du marché d'exécution.
2. **Commande `verify`** : reprendre un candidat, rafraîchir toutes ses
   cotations, et refuser si l'EV a bougé de plus de X %.
3. **Modéliser le float d'entrée comme une variable aléatoire.** `--float-safety`
   est un pansement déterministe. Le traitement correct : tirer les floats,
   propager la distribution jusqu'à l'EV, et reporter P(la sortie tombe dans le
   palier visé). C'est l'amélioration la plus utile après CSFloat.
4. **Vérifier la liquidité des ENTRÉES.** `--min-volume` ne filtre que les
   sorties. Un contrat exigeant 8 exemplaires d'un skin qui s'échange 3 fois par
   jour n'est pas exécutable.
5. **StatTrak** : le moteur accepte le drapeau, mais la base ne distingue pas
   encore les collections sans variante ST.

## Tests

```bash
python -m pytest -q
```

Les tests couvrent la mécanique du float (dont les cas limites de frontières
d'usure), la formule de probabilité en mono- et multi-collection, le parsing
des montants Steam multi-locale, les frais, et la sélection d'entrées sous
contrainte de budget.

## Structure

```
src/tradeup/
  models.py      raretés, usures, skins, collections
  wear.py        float de sortie, paliers, points de rupture d'EV
  fees.py        frais Steam / CSFloat / Buff163
  ev.py          distribution des sorties, EV nette, variance, CVaR
  generator.py   recettes + sélection d'entrées à coût minimal
  scan.py        orchestration (préchargement des prix puis calcul)
  scoring.py     filtres et classement
  cli.py         interface ligne de commande
  pricing/       http (rate-limit), cache SQLite, steam, csfloat, repository
scripts/build_db.py   ingestion de la base statique
```
