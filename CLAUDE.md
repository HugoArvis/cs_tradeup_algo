# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Code, commentaires et commits en français : sans accents dans le code Python,
avec accents dans la documentation.

Ce fichier ne porte que ce qui **ne se déduit pas** du code : pièges
d'environnement, règles du domaine issues de bugs mesurés, contraintes des API.
Le reste vit ailleurs et n'a pas à être dupliqué ici — objectif, installation,
état d'avancement et limites de modélisation dans `README.md`, mode d'emploi pas
à pas dans `GUIDE.md`, travail restant dans le ClickUp du projet.

## Début de session

Avant toute autre chose, consulter le ClickUp du projet et relever les tâches
ajoutées depuis la dernière session.

Les traiter **une par une**, jamais en parallèle.

Après chaque tâche terminée, faire un retour dans la conversation : ce qui a été
fait, comment, et ce qui a été vérifié.

Si le serveur MCP ClickUp est injoignable, le dire et demander quoi faire — ne
jamais sauter la vérification en silence.

## Commandes

```bash
python -m pip install -e ".[dev]"     # `pip` seul vise Python 3.9, pas 3.11
python -m scripts.build_db            # (re)construit data/collections.json
python -m pytest -q                   # 150 tests, < 15 s
python -m pytest tests/test_core.py -k probabilites
python scripts/check_js.py            # apres toute retouche du JS de web.py
python -m tradeup.web                 # application locale, port 8765
python -m tradeup.cli plan "The Bank Collection" --rarity industrial --html
python -m tradeup.cli verify --collection "The Bank Collection" --rarity industrial
```

Le raccourci `tradeup` n'est pas dans le `PATH` : passer par `python -m tradeup.cli`.

## Environnement

- Les noms de skins contiennent `|`, ce qui casse `python -c` sous PowerShell.
  Passer par un fichier de script.
- Les heredocs bash transforment les `\n` des chaînes Python en vrais sauts de
  ligne (`SyntaxError: unterminated string literal`). Utiliser l'outil Edit pour
  du code contenant des échappements.
- `/tmp` ne désigne pas le même dossier pour Python (Windows) et bash (Git Bash).
- Un script de patch doit vérifier `assert motif in source` avant de remplacer :
  `str.replace` ne signale pas un motif absent.

## Architecture

`ev.evaluate()` ne connaît aucune marketplace : il dépend du protocole
`PriceLookup`. D'où deux chaînes distinctes qu'il ne faut pas confondre.

| | `scan` | `plan` |
|---|---|---|
| Source | Steam ou CSFloat (`MarketPricer`) | CSFloat (`plan.CSFloatPricer`) |
| Prix | un par palier d'usure | annonces réelles |
| Float | supposé (`--float-pct`) | exact |
| Sélection | avec répétition | 0/1, une annonce est unique |
| Usage | explorer sans clé API | **exécuter** |

Un panier issu de `scan` n'est pas achetable tel quel.

`scan` choisit indépendamment son marché d'achat (`--buy-market`) et de revente
(`--sell-market`). Les deux exigent `--currency USD` côté CSFloat, et une seule
instance `CSFloat` est partagée : deux rate-limiters doubleraient le débit réel.
Un `scan` entièrement CSFloat coûte une cotation par objet et par palier —
réservé à quelques collections, `plan` ne cote que ce qu'il achète.

Si une source expose `sell_net_at_float(...)`, `evaluate()` l'utilise au lieu de
`sell_net()` — point d'extension pour une valorisation sensible au float.

**L'EV est constante par morceaux** en fonction de la moyenne des floats
d'entrée : elle ne saute qu'aux frontières d'usure. `wear.wear_breakpoints()`
les calcule, l'optimiseur évalue un point par palier au lieu d'échantillonner.
La sélection sous contrainte de float est une DP sur frontière de Pareto
(`generator._extend_frontier`, `cheapest_unique_selection`).

## Règles du domaine

Chacune vient d'un bug mesuré ; les tests les verrouillent.

**Probabilité d'une sortie** : `P(s ∈ C) = n_C / Σ n_C' × k_C'`. Une collection
à peu de sorties est globalement *moins* probable, pas plus.

**Float de sortie** : moyenne des floats **normalisés** — chaque entrée ramenée
à `(float − min_skin) / (max_skin − min_skin)` — puis remappée sur le range du
skin de **sortie**. Moyenner les floats affichés est faux : sur un contrat réel,
0,0789 de moyenne affichée valait 0,4011 en normalisé et a produit du Minimal
Wear là où le calcul annonçait Factory New.

**Exclure les objets stickés de la valorisation** — une sortie de contrat naît
nue. Les compter valorisait une Five-SeveN Candy Apple 582 USD contre 85 réels.
La règle vaut pour les **deux** sources CSFloat (`plan.CSFloatPricer` et
`pricing.csfloat`), et impose d'examiner 50 annonces, pas 10 : sur l'AK-47
Redline (FT), les dix moins chères sont toutes stickées — 3 nues sur 50. La
fenêtre est un paramètre de requête, elle ne coûte aucun quota.

**CSFloat ne donne le volume que sur demande** (`--csfloat-volume`), via un
appel `/history/{nom}/graph` qui **double** la consommation de quota. Sans lui,
`--min-volume` ne filtre rien et la capacité d'exécution reste « inconnue » —
silencieusement.

**Aucune prime de bas float dans le calcul** — elle existe (+40 %) mais ce sont
des prix demandés, rien ne dit qu'ils se concluent. Retenir la moins chère
annonce nue du palier.

**Le prix affiché n'est pas ce qu'on encaisse.** Un contrat annoncé à +0,37 € a
fini à −0,02 € : la valorisation était juste à 3 % près, mais la revente s'est
faite 33 % sous le marché. `/history/{nom}/graph` donne les ventes par jour,
`/history/{nom}/sales` les transactions réelles — `CSFloatPricer.sales_stats()`
les expose pour dire combien de temps une revente prendra.

**Mélanger deux collections est possible mais gagne rarement**
(`scan --max-collections 2` ; `plan` et `inventory` restent mono-collection).
À paliers de sortie fixes, l'EV brute vaut `(n_A·V_A + n_B·V_B) / (n_A·k_A +
n_B·k_B)` : une homographie de la répartition, donc **monotone** — son maximum
est à une borne, c'est-à-dire en mono-collection. Un mélange ne peut donc gagner
que par le **coût** des entrées, jamais par la valeur des sorties. La monotonie
tombe seulement quand la répartition change le palier d'usure atteignable.

Mesuré sur 10 collections Mil-Spec entièrement cotées : 405 recettes mixtes
évaluées, meilleure à **+1,17** contre **+1,38** en mono — et cette meilleure
mixte n'est qu'un « 9× Fracture + 1× Recoil », soit le mono dilué. Taux de
contrats rentables : 20 % en mono, 5,7 % en mixte.

Le seul mélange gagnant de l'échantillon illustre pourquoi le défaut est
`risk_adjusted` et non l'EV brute — *9× Bank + 1× Lake* contre *10× Bank* :
profit +0,99 contre +0,97 et ROI 6,4 % contre 5,6 %, mais **pire cas 0,82 au
lieu de 18,19** et P(gain) 90 % au lieu de 100 %. Soit 0,02 € d'espérance
achetés au prix d'une perte de 16,40 € une fois sur dix.

**`all_outcomes_profitable` prime sur le nombre de sorties** — trois issues
toutes rentables valent mieux qu'une issue unique au gain marginal. D'où le
classement : `scoring.sort_key` renvoie un **tuple** (probabilité puis gain pour
`safety`), là où `score()` encodait ce départage en `proba × 1000 + profit` —
exact sur les montants du projet, faux au-delà de 1000. Les quatre critères sont
partagés par `scan --rank`, `inventory --rank` et les deux listes de
l'application web, qui reclassent **côté client** : réordonner des contrats déjà
calculés ne doit rien recoûter en quota.

**Un skin possédé coûte ce qu'il vaut à la revente, pas ce qu'on l'a payé.** Le
prix d'achat est irrécupérable et ne doit peser sur aucune décision ; le compter
à zéro (« je l'ai déjà ») rend tout contrat rentable et pousse à fondre des skins
qui valaient mieux vendus. `inventory.best_tradeups` valorise donc les entrées à
`sell_net` — fondre, c'est renoncer à vendre. Beaucoup de contrats « gratuits »
apparaissent alors perdants : c'est le résultat correct, le coût était seulement
invisible.

Un objet possédé a un float **exact** (σ = 0) et est **unique** (sélection 0/1).
`/me/inventory` sur CSFloat est la seule source qui donne ces floats — Steam ne
renvoie que des noms. `tradable` y porte le verrou de 7 jours, et un **Souvenir
ne peut jamais entrer dans un contrat** (règle du jeu, testée explicitement :
sans ça il n'était écarté que faute d'être reconnu, ce qui est un accident).

**Un contrat StatTrak ne mélange rien** : entrées StatTrak, sorties StatTrak, et
`StatTrak™ AK-47 | Redline (FT)` est un **autre objet de marché** avec son prix
et son volume. `--stattrak` filtre entrées et sorties (`inputs_for_rarity`,
`outcomes_for_input_rarity`) — retirer des sorties **redistribue** la masse de
probabilité, ce n'est pas les mettre à zéro. Aucune collection sous le Mil-Spec
n'en a ; 44 sur 88 en Mil-Spec.

**Un contrat exige dix exemplaires du même objet.** Le prix affiché vaut pour la
première annonce, pas pour les neuf suivantes. `--min-input-volume` (défaut 3)
écarte les entrées trop peu vendues — miroir de `--min-volume` côté sortie — et
`CapacityReport.sourcing_days` alerte quand réunir les entrées d'**un seul**
contrat prend plus d'une journée. Question distincte du rythme de répétition :
avant de savoir combien de fois refaire le contrat, il faut savoir si on peut le
faire une fois.

`volume()` cote le marché de **revente**, `buy_volume()` celui d'**achat**. Les
confondre faisait disparaître en silence toute contrainte d'approvisionnement
dès que la revente passait sur CSFloat, qui ne publie aucun volume.

**Le float d'entrée est un tirage, pas une valeur choisie.** Sur Steam on achète
un palier, pas un float. `--float-model random` (module `floatrisk`) chiffre ce
risque au lieu de l'éviter par la marge forfaitaire `--float-safety` : float
normalisé uniforme sur le palier, moyenne des 10 approchée par une normale
**corrigée au 4ᵉ ordre** (Edgeworth — dix tirages ne font pas une gaussienne :
l'erreur passe de 1,9 à 0,3 point sur les probabilités de palier, mesuré contre
Monte-Carlo), puis EV pondérée par segment et aplatie dans la distribution des
sorties, si bien que tout l'aval en profite sans le savoir.

Deux conséquences qu'il ne faut pas défaire :
- ce mode impose `--float-pct 0.5`. L'espérance d'un tirage uniforme tombe au
  milieu du palier ; viser 0,15 sans pouvoir filtrer les floats n'est pas une
  hypothèse prudente, c'est une moyenne fausse (μ décalé de 0,12 en v1) ;
- l'EV intégrée n'est plus constante par morceaux, donc l'optimiseur teste
  plusieurs reculs (0 à 3 σ) sous chaque frontière. La marge de sécurité devient
  une décision prise contrat par contrat, pas un réglage subi.

**Une dérive se lit du côté du portefeuille, pas du prix.** Une entrée qui
renchérit et une sortie qui se déprécie sont toutes deux défavorables, avec des
signes opposés en variation brute. `refresh.Drift.impact` porte ce signe ;
`verify` sort en code 1 dès qu'une dérive défavorable dépasse le seuil, pour
qu'un script s'arrête avant d'exécuter.

**L'API CSFloat cote en USD**, le site facture dans la devise du profil.
`web.App.conv()` convertit ; le cache SQLite indexe par devise.

**Verrou de 7 jours** : un objet reçu par échange (tout achat CSFloat) ne peut
pas entrer dans un contrat avant 7 jours ; un achat sur le marché Steam le peut.
Le compteur part à la réception de chaque objet.

## Contraintes externes

Le quota CSFloat porte sur une fenêtre longue, pas seulement par minute. Un 429
persistant ne se réessaie pas, il s'attend — `web.Batch` met en pause 10 min
sans retirer la collection de la file.

Steam limite à ~15 req/min et ne donne aucun float. `SteamMarket.order_book()`
est mort : le HTML ne contient plus `item_nameid`.

## Données locales

`data/collections.json` régénérable, `data/prices.db` cache jetable,
`data/journal.db` achats réels de l'utilisateur (jamais versionné ni purgé),
`.env` porte `CSFLOAT_API_KEY`.