# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Code, commentaires et commits en français : sans accents dans le code Python,
avec accents dans la documentation.

## Commandes

```bash
python -m pip install -e ".[dev]"     # `pip` seul vise Python 3.9, pas 3.11
python -m scripts.build_db            # (re)construit data/collections.json
python -m pytest -q                   # 150 tests, < 15 s
python -m pytest tests/test_core.py -k probabilites
python scripts/check_js.py            # apres toute retouche du JS de web.py
python -m tradeup.web                 # application locale, port 8765
python -m tradeup.cli plan "The Bank Collection" --rarity industrial --html
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
| Source | Steam (`MarketPricer`) | CSFloat (`plan.CSFloatPricer`) |
| Prix | un par palier d'usure | annonces réelles |
| Float | supposé (`--float-pct`) | exact |
| Sélection | avec répétition | 0/1, une annonce est unique |
| Usage | explorer sans clé API | **exécuter** |

Un panier issu de `scan` n'est pas achetable tel quel.

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

**Aucune prime de bas float dans le calcul** — elle existe (+40 %) mais ce sont
des prix demandés, rien ne dit qu'ils se concluent. Retenir la moins chère
annonce nue du palier.

**Le prix affiché n'est pas ce qu'on encaisse.** Un contrat annoncé à +0,37 € a
fini à −0,02 € : la valorisation était juste à 3 % près, mais la revente s'est
faite 33 % sous le marché. `/history/{nom}/graph` donne les ventes par jour,
`/history/{nom}/sales` les transactions réelles — `CSFloatPricer.sales_stats()`
les expose pour dire combien de temps une revente prendra.

**`all_outcomes_profitable` prime sur le nombre de sorties** — trois issues
toutes rentables valent mieux qu'une issue unique au gain marginal.

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
