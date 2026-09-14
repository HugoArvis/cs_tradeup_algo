# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Le code, les commentaires et les commits sont en français (sans accents dans le
code Python, avec accents dans la documentation). Garder cette convention.

## Commandes

```bash
python -m pip install -e ".[dev]"    # PAS `pip install` : voir Environnement
python -m scripts.build_db           # (re)construit data/collections.json
python -m pytest -q                  # 143 tests, < 15 s
python -m pytest tests/test_core.py -q -k probabilites   # un seul test
```

Les deux interfaces :

```bash
python -m tradeup.web                        # application locale, port 8765
python -m tradeup.cli plan "The Bank Collection" --rarity industrial --html
python -m tradeup.cli collections --rarity mil-spec   # hors ligne, gratuit
```

Après avoir modifié du JavaScript dans `web.py` ou `report.py`, vérifier la
syntaxe : le JS est une chaîne Python, aucun test ne l'exécute.

```bash
python - <<'PY'
import pathlib, re
from tradeup.web import PAGE
js = re.search(r"<script>(.*)</script>", PAGE, re.S).group(1)
pathlib.Path("rapports").mkdir(exist_ok=True)
pathlib.Path("rapports/_check.js").write_text(js, encoding="utf-8")
PY
node --check rapports/_check.js && rm rapports/_check.js
```

Ne pas viser `/tmp` : Python (Windows) et bash (Git Bash) ne le résolvent pas
vers le même dossier, le fichier écrit par l'un est introuvable pour l'autre.

## Environnement

- `pip` appartient à Python 3.9 sur cette machine, `python` à 3.11. Toujours
  `python -m pip`.
- Le raccourci `tradeup` n'est pas dans le `PATH` ; utiliser `python -m tradeup.cli`.
- PowerShell est le shell principal. Les noms de skins contiennent `|`, ce qui
  casse `python -c` en PowerShell : passer par un fichier de script.
- Les heredocs bash mangent les `\n` dans les chaînes Python et produisent des
  `SyntaxError: unterminated string literal`. Préférer l'outil Edit pour du code
  contenant des échappements.
- `str.replace` ne signale pas un motif absent. Tout script de patch doit
  `assert motif in source` avant de remplacer — un patch silencieusement inopérant
  a déjà provoqué une 500 côté serveur.

## Architecture

### La couture centrale : `PriceLookup`

`ev.evaluate()` ne connaît aucune marketplace. Il dépend du protocole
`PriceLookup` (`ev.py`), ce qui permet deux chaînes complètement différentes :

| | `scan` (Steam) | `plan` (CSFloat) |
|---|---|---|
| Source | `pricing/repository.MarketPricer` | `plan.CSFloatPricer` |
| Prix | un par palier d'usure | annonces réelles |
| Float | **supposé** (`--float-pct`) | **exact**, celui de l'annonce |
| Sélection | avec répétition | **0/1** (une annonce est unique) |
| Rôle | pré-filtrage, exploration | exécution réelle |

`scan` sert à explorer sans clé API. `plan` est ce qu'on exécute. Ne pas les
confondre : un panier `scan` n'est pas achetable tel quel.

Si une source expose `sell_net_at_float(skin, wear, stattrak, float)`,
`evaluate()` l'utilise à la place de `sell_net()`. C'est le point d'extension
pour une valorisation sensible au float.

### Ce qui rend le problème traitable

À recette fixée, l'EV est **constante par morceaux** en fonction de la moyenne
des floats d'entrée : elle ne saute qu'aux frontières d'usure des skins de
sortie. `wear.wear_breakpoints()` les calcule exactement, donc l'optimiseur
évalue un point par palier au lieu d'échantillonner `[0, 1]`.

Le choix des entrées sous contrainte de float est un coût minimal résolu par
programmation dynamique sur frontière de Pareto (`generator._extend_frontier`
avec répétition, `cheapest_unique_selection` en 0/1).

`plan._breakpoints_avec_annonces()` étend les points de rupture aux floats des
annonces de sortie, car le prix y varie aussi.

## Règles du domaine à ne jamais casser

Chacune a coûté un bug mesuré. Les tests les verrouillent.

**Probabilité d'une sortie** — chaque entrée dépose un ticket pour *sa*
collection ; tirage uniforme sur les paires (ticket, sortie) :
`P(s ∈ C) = n_C / Σ n_C' × k_C'`. Une collection à peu de sorties est
globalement *moins* probable, pas plus.

**Float de sortie** — moyenne des floats **absolus** des entrées, remappée sur
le range du skin de **sortie** : `avg × (max_cible − min_cible) + min_cible`.
Un skin à `min_float = 0.10` ne peut jamais sortir en Factory New.

**Objets stickés exclus de la valorisation** (`plan.CSFloatPricer._listings`) —
une sortie de contrat naît nue. Les compter a valorisé une Five-SeveN Candy
Apple 582 USD contre 85 réels. Un plafond à 3× la médiane écarte les autres
aberrations.

**Aucune prime de bas float dans le calcul** (`plan.price_at_float`) — la prime
existe (+40 % mesuré) mais ce sont des prix demandés, sans volume publié. Trois
tentatives de la modéliser ont produit 582, puis 181, puis 121 là où le marché
affichait 85. On retient la moins chère annonce nue du palier.

**`all_outcomes_profitable` prime sur le nombre de sorties** — le nombre de
sorties est une approximation *structurelle*, utile avant d'avoir les prix,
trompeuse comme verdict. Un contrat à 3 issues toutes rentables bat un contrat
à issue unique au gain marginal.

**Devise** — l'API CSFloat cote en USD, le site facture dans la devise du
profil. `web.App.conv()` convertit via `/meta/exchange-rates`. Le cache SQLite
indexe par devise : sans ça un scan mélangeait EUR et USD.

**Verrou d'échange de 7 jours** — un objet reçu par échange (donc tout achat
CSFloat) ne peut PAS entrer dans un contrat avant 7 jours. Un achat sur le
marché Steam le peut. Le compteur part à la réception de *chaque* objet : c'est
le dernier qui commande.

## Contraintes externes

**Quota CSFloat** — porte sur une fenêtre longue, pas seulement par minute. Le
défaut de 10 req/min est prudent. Un 429 persistant ne se réessaie pas, il
s'attend. `web.Batch` met le balayage en pause 10 min sans retirer la
collection de la file.

**Steam** — ~15 req/min. `priceoverview` ne donne aucun float.
`SteamMarket.order_book()` ne fonctionne plus : Steam a réécrit ses pages de
marché, le HTML ne contient plus `item_nameid`.

## Données locales

`data/collections.json` est régénérable (`scripts/build_db.py` ingère
ByMykel/CSGO-API). `data/prices.db` est un cache jetable. `data/journal.db`
contient les achats réels de l'utilisateur — **jamais versionné, jamais purgé**.
`.env` porte `CSFLOAT_API_KEY`.

## Pièges d'interface

Le serveur ne recharge pas le code : après modification, redémarrer *et* faire
un rechargement forcé du navigateur. L'heure de démarrage s'affiche en haut de
page pour rendre ce piège visible.

Un plan ouvert depuis l'historique est une **archive** aux valeurs figées, pas
un calcul frais. Le bandeau le signale.
