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
python -m tradeup.cli orders --rarity mil-spec   # prix d'ordre d'achat a placer
python -m tradeup.cli daily --rarity industrial  # passage quotidien, code 10 si actionnable
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
| Source | Steam ou CSFloat (`MarketPricer`) | achat CSFloat, revente Steam (`plan.CSFloatPricer`) |
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

**Probabilité d'une sortie** : on tire un ticket (une entrée), ce qui désigne
une collection, puis un skin uniformément dedans.

    P(collection C)       = n_C / N
    P(un skin donné de C) = n_C / (N × k_C)

La part d'une collection ne dépend **que de ses entrées** : cinq entrées de The
Bank Collection donnent 50 % de chances d'une sortie Bank, qu'elle ait deux
sorties ou dix. Son nombre de sorties ne fait que répartir cette part — une
collection à peu de sorties concentre la sienne, chaque skin y vaut donc plus.

*Corrigé en septembre 2026*, sur l'indication d'un guide vidéo. Le projet
pondérait auparavant la part de chaque collection par son nombre de sorties
(`n_C / Σ n_C'×k_C'`) : sur « 9 entrées d'une collection à 2 sorties + 1 entrée
d'une à 1 sortie », cela donnait 94,7 / 5,3 au lieu de **90 / 10** — soit la
moitié de sa vraie part pour la collection intruse, celle qu'on ajoute justement
pour diluer. Les deux formules coïncident exactement en mono-collection (toutes
deux donnent `1/k`), ce qui a rendu l'erreur invisible sur tous les contrats
recommandés jusque-là.

**Une collection sans sortie sort du dénominateur.** Ses entrées sont du coût
pur ; les compter ferait une masse totale inférieure à 1.

**Profitabilité = `EV / coût`, où 1,0 est le point mort** — convention des
guides et calculateurs publics, exposée par `TradeUpResult.profitability` et
`GoldPlan.profitability`. Un contrat annoncé « à 40 % » rend 40 centimes par
euro engagé : il en détruit 60. C'est `1 + roi`, mais comparer un chiffre du
projet à celui d'une vidéo exige la même convention, sans quoi on compare 10
à 110.

**Le float cap de la SORTIE décide de la difficulté**, pas celui de l'entrée.
Une sortie plafonnée à 0,08 passe en Factory New tant que la moyenne d'entrée
reste sous 0,875 — presque n'importe quelle entrée suffit. Une sortie allant
jusqu'à 1,00 exige une moyenne sous 0,07, donc un tri annonce par annonce.
`Collection.factory_new_threshold()` calcule ce seuil **sans aucune cotation**,
et `collections` l'affiche : c'est un critère de sourcing gratuit qui dit où
chercher. Contraste mesuré sur la base réelle — Aztec 0,875 contre Nuke 0,014,
deux collections à sortie unique pourtant.

**Sur Steam, le prix ne dépend pas du float** : il n'apparaît pas dans la liste,
il faut inspecter chaque annonce. Les vendeurs ne le pricent donc pas, et un bas
float s'y obtient au prix du palier — c'est l'inefficience que la stratégie de
tri exploite. Sur **CSFloat**, le float est visible et pricé : la prime y est
réelle (P250 Kintsugi 22,62 € au palier, 40,27 € sous 0,0105). D'où le choix du
modèle de float : `fixed` est légitime si l'on trie les annonces Steam,
`random` si l'on achète sans regarder.

**L'écart entre les deux modèles décide du verdict**, il ne le nuance pas.
Mesuré le 20 septembre 2026 sur le même cache Industrial : Bank **96 % en
`random` contre 158 % en `fixed`**, Italy 93 % contre 120 %, Train 17 % contre
76 %. Deux contrats passent le point mort en changeant ce seul réglage. Choisir
le modèle revient donc à choisir la méthode d'approvisionnement, et se tromper
de modèle rend le classement entier inutilisable. `daily` est par défaut en
`fixed`, la stratégie visée étant le tri d'annonces Steam.

Ce que `fixed` ne facture pas : **la recherche**. Viser `--float-pct 0.15`
suppose de trouver dix annonces dans le premier sixième du palier, donc d'en
inspecter beaucoup plus — le float n'étant pas affiché dans la liste Steam. Ce
coût est en temps, pas en argent, et n'apparaît dans aucun chiffre du modèle.

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

**Une sortie se valorise sur le marche ou on la VEND, nette de ses frais.**
`plan` achète sur CSFloat et revend sur Steam : `CSFloatPricer` reçoit un
`sell_source` et son `FeeModel`. Le valoriser au prix CSFloat moins 2 %
supposait qu'on revend là où l'on a acheté — mesuré sur les 383 sorties
Mil-Spec cotées, Steam prélève **13,2 %** en moyenne et **23,2 %** entre 0,05
et 0,10 €. Deux conséquences que le code tire de lui-même : `sell_net_at_float`
cesse d'être sensible au float (Steam ne le price pas, viser 0,001 plutôt que
0,06 n'achète rien), et les paliers de prix CSFloat des sorties sortent de
`_breakpoints_avec_annonces` — les coter devenait une dépense de quota sans
objet. Les deux côtés sont cotés en **USD** : mélanger les devises donne un
nombre qui ressemble à un profit sans en être un.

**Le prix DEMANDE n'est pas le prix NEGOCIE.** `lowest_price` est la plus
basse annonce en cours, `median_price` la mediane des ventes recentes.
`--price-basis sales` (et `sell_basis` cote `plan`, ou c'est le defaut)
raisonne sur le second, via `Quote.realised_reference()`. L'ecart coupe dans
les deux sens — mesuré sur le contrat Bank le 19 septembre 2026 : entrée
G3SG1 Green Apple (MW) **0,09 vendu contre 0,11 demandé** (−18 %), sortie
CZ75-Auto Tuxedo (FT) 0,24 contre 0,28, mais Galil Tuxedo (FT) 0,91 contre
0,88. Le contrat passe de **81,5 % à 100 %** de profitabilité, tout l'écart
venant de l'entrée.

Deux limites qui interdisent d'en faire le défaut partout :
- viser le prix négocié à l'ACHAT suppose un **ordre d'achat** qui attend
  d'être servi. On ne clique pas sur une annonce à 0,09 quand la moins chère
  est à 0,11 — c'est ce que la méthode échange contre son gain ;
- c'est **instable** sur les petits montants. Le même contrat donnait 100 %
  sur le cache de la veille et 87,3 % re-coté le lendemain, l'écart de 18 %
  sur l'entrée ayant disparu. À 1 € de mise, un centime vaut 10 points.

`market/pricehistory/` (la vraie moyenne sur 7 jours) répond **HTTP 400 sans
session Steam connectée** : la médiane des ventes récentes est le meilleur
substitut automatisable.

**Aucune prime de bas float dans le calcul** — elle existe (+40 %) mais ce sont
des prix demandés, rien ne dit qu'ils se concluent. Retenir la moins chère
annonce nue du palier.

**Mais l'ignorer à l'ACHAT rend `scan` systématiquement optimiste**, et cette
asymétrie est dangereuse : l'ignorer à la revente est prudent, l'ignorer à
l'achat ne l'est pas. `build_options` retient le prix du palier tout en visant
un float à `--float-pct` du bas — or un bas float coûte plus cher que le palier,
quand il existe. Mesuré sur The Dead Hand Collection : `scan` proposait 5×
P250 Kintsugi (FN) à 22,62 € sous 0,0105 de float ; sur CSFloat il n'en existait
**qu'une seule** parmi 49 annonces nues, à **40,27 €** — 78 % de plus. Le contrat
passait de +20 € annoncés à ~−93 € réels.

Conséquence : `scan` sert à **pré-filtrer**, jamais à décider. Seul `plan`, qui
lit des annonces réelles avec leurs floats et leurs prix, donne un chiffre
opposable — et plus `--float-pct` est bas, plus l'écart se creuse.

**Le prix affiché n'est pas ce qu'on encaisse.** Un contrat annoncé à +0,37 € a
fini à −0,02 € : la valorisation était juste à 3 % près, mais la revente s'est
faite 33 % sous le marché. `/history/{nom}/graph` donne les ventes par jour,
`/history/{nom}/sales` les transactions réelles — `CSFloatPricer.sales_stats()`
les expose pour dire combien de temps une revente prendra.

C'est le facteur **dominant**, devant le choix de stratégie. Mesuré sur quatre
collections Mil-Spec aux prix d'ordre : le gain s'annule autour de **18 % de
décote de revente**, que le contrat gagne 100 % du temps ou 51 % du temps — le
seuil est le même (18,8 % pour Bank, 18,1 % pour Fracture). Or le seul contrat
réellement exécuté du projet a subi 33 %. Tant que sa propre décote n'est pas
mesurée, arbitrer entre un contrat sûr et un contrat répété revient à comparer
deux nombres dont le troisième décide.

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

**Un ordre d'achat ne subit pas le prix, il le fixe** — d'où la commande
`orders`, qui renverse le calcul : au lieu de `profit = EV − coût(marché)`, elle
résout `budget = EV / (1 + rendement visé)` et en déduit le prix maximal de
chaque entrée. La valeur de la sortie ne dépend pas de ce qu'on a payé les
entrées, donc elle se calcule une fois et le reste suit.

Trois contraintes que ce mode ne doit pas perdre de vue :
- le float est **subi** (`float_model="random"` imposé) : un ordre ne filtre
  aucune annonce, viser un bas float serait mentir ;
- la probabilité de gain se calcule **au prix d'ordre**, pas au prix du marché.
  Mesuré sur The Bank Collection en Mil-Spec : 0 % de chances de gagner au prix
  demandé, **100 % avec 32 % de rabais**. Afficher la probabilité du marché à
  côté d'un budget réduit donnait deux lignes contradictoires ;
- un ordre trop bas n'est jamais servi. `max_discount` (35 %) écarte ces cas au
  lieu de les présenter comme des occasions, et `STEAM_MIN_PRICE` (0,03) marque
  ceux qu'aucun rabais ne peut sauver.

Les frais Steam ont un **plancher de 0,01 par frais**, donc ils écrasent les
petits montants à la vente : 66 % sur un objet à 0,03 €, 28,6 % à 0,07 €, contre
12–13 % au-delà de 0,25 €. À l'achat, le prix affiché est ce qu'on paie.

**Un skin possédé coûte ce qu'il vaut à la revente, pas ce qu'on l'a payé.** Le
prix d'achat est irrécupérable et ne doit peser sur aucune décision ; le compter
à zéro (« je l'ai déjà ») rend tout contrat rentable et pousse à fondre des skins
qui valaient mieux vendus. `inventory.best_tradeups` valorise donc les entrées à
`sell_net` — fondre, c'est renoncer à vendre. Beaucoup de contrats « gratuits »
apparaissent alors perdants : c'est le résultat correct, le coût était seulement
invisible.

Un objet possédé a un float **exact** (σ = 0) et est **unique** (sélection 0/1).
`/me/inventory` sur CSFloat est la seule source qui donne ces floats — Steam ne
renvoie que des noms. `tradable` y porte le verrou de 7 jours.

**Les Souvenir sont admis depuis le 21 mai 2026** — « Souvenir quality items can
now be selected in Trade Up Contract alongside normal quality items. All Souvenir
attributes will be removed ». La sortie est donc un skin **normal**, ce que le
moteur produit déjà. Deux pièges :
- le préfixe du nom de marché fait foi, et il doit être retiré pour retrouver le
  skin dans la base, sinon l'objet est ignoré sans bruit ;
- le champ `Skin.souvenir` de la base statique vaut **True sur les 1451 skins**
  (`stattrak`, lui, est correctement réparti 754/697). Il ne distingue rien et ne
  doit servir à aucune décision — il vient tel quel de l'API source.

**Le contrat vers un GOLD est une autre mécanique** (module `gold`, commande
`knife`) : **cinq** Covert d'une même **caisse** — pas dix, pas une collection —
donnent un couteau ou des gants de son pool, à probabilité **uniforme**
(vérifié contre un calculateur de référence : 13 Kukri, même chance chacun).
Les golds n'ont aucune `collections` dans la source, seulement des `crates`,
d'où la structure séparée ; `build_db` les excluait jusqu'ici.

**Les golds d'une même caisse n'ont pas le même range de float**, et c'est le
piège : Kukri Fade 0–0,08, Safari Mesh 0,06–0,80, Slaughter 0,01–0,26. Pour une
seule moyenne d'entrée, le premier sort Factory New à 148 €, le deuxième
Field-Tested à 37 €. Supposer un range commun faisait annoncer +8,5 % un contrat
qui perd 58 %. `ev.evaluate()` gérait déjà ce cas — `output_float` lit le range
de chaque sortie ; c'est un script d'analyse qui avait triché, pas le moteur.

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

**Un classement n'a pas un âge moyen, il a l'âge de son plus vieux prix.**
D'où `daily` (module `daily`), qui n'est pas un scan de plus : il classe
d'abord sur le cache — gratuit — pour savoir **où** dépenser les requêtes,
recote les objets des `--top` meilleurs candidats du plus vieux prix au plus
récent, puis reclasse. Recoter avant de savoir quoi recoter dépenserait le
budget au hasard, et rafraîchir le 40e du classement ne change aucune décision.

`Ligne.actionnable` exige **rentable ET prix de moins de 24 h**, les deux ou
rien. Mesuré le 19 septembre 2026 : The Italy Collection ressortait à 94 % sur
un cache de quelques jours et à **84 %** re-cotée en direct, le MP7 | Anodized
Navy (FN) — un tiers des issues — ayant perdu **31 %** entre-temps. Le calcul
était juste, les prix étaient morts. Un refus de Steam en cours de route
n'interrompt pas le passage : il s'arrête avec ce qu'il a, et la colonne d'âge
dit lesquelles des lignes sont fraîches. Code de sortie **10** si quelque chose
est actionnable, de quoi alerter un planificateur sans relire la sortie.

**Une dérive se lit du côté du portefeuille, pas du prix.** Une entrée qui
renchérit et une sortie qui se déprécie sont toutes deux défavorables, avec des
signes opposés en variation brute. `refresh.Drift.impact` porte ce signe ;
`verify` sort en code 1 dès qu'une dérive défavorable dépasse le seuil, pour
qu'un script s'arrête avant d'exécuter.

**Revendre sur Steam rapporte plus que sur CSFloat, malgré des frais 6× plus
élevés.** Contre-intuitif, donc mesuré en direct sur trois sorties Mil-Spec :
net Steam 4,01 / 2,09 / 1,00 € contre 3,42 / 1,52 / 0,75 € sur CSFloat, soit
+17 à +37 % pour Steam. Les prix bruts CSFloat sont structurellement plus bas —
c'est bien pour cela que `plan` y **achète**. La liquidité va dans le même sens
sur ce segment : 98 et 83 ventes/jour sur Steam contre 8 et 10 sur CSFloat.
CSFloat ne l'emporte que sur un point, décisif si l'objectif est de sortir de
l'argent : son porte-monnaie est retirable, pas celui de Steam.

**Le TTL se decide dans la SOURCE, pas dans le cache.** `QuoteCache`
conserve tout ; a chaque lecture `SteamMarket`/`CSFloat` lui impose son propre
`ttl_seconds` (6 h et 3 h par defaut). Tant que `--ttl` n'arrivait qu'au cache,
la source gardait son defaut et redemandait des prix deja acquis : un scan
Industrial a passe des heures a recoter les memes 813 noms sans jamais
atteindre les 865 manquants, en affichant une progression parfaitement
normale. Le budget Steam etant la ressource rare, le scan ne pouvait pas
aboutir — jamais par erreur visible, toujours par epuisement.

**Hors ligne, le TTL du cache est ignoré : un prix de six semaines est servi
sans un mot** et donne un scan d'apparence normale, entièrement faux. Mesuré :
un Tec-9 | Brother (Factory New) affiché à 12,97 € par un cache de 42 jours en
valait 4,61 € au marché, soit −64 %. `MarketPricer.quote_age()` expose l'âge
médian et maximal ; `scan` et `orders` alertent au-delà de 24 h.

**L'API CSFloat cote en USD**, le site facture dans la devise du profil.
`web.App.conv()` convertit ; le cache SQLite indexe par devise.

**Verrou de 7 jours** : un objet reçu par échange (tout achat CSFloat) ne peut
pas entrer dans un contrat avant 7 jours ; un achat sur le marché Steam le peut.
Le compteur part à la réception de chaque objet.

## Contraintes externes

Le quota CSFloat porte sur une fenêtre longue, pas seulement par minute. Un 429
persistant ne se réessaie pas, il s'attend — `web.Batch` met en pause 10 min
sans retirer la collection de la file.

**Steam refuse tout `User-Agent` qu'il ne reconnaît pas** — 429 dès la
*première* requête, sans rapport avec le débit. Mesuré le 19 septembre 2026 sur
cinq chaînes (`cs-tradeup-algo/0.1`, la forme `Mozilla/5.0 (compatible; ...)`,
une chaîne quelconque, un mot isolé, la chaîne vide) : toutes refusées à
l'instant où `Python-urllib/3.x` passait. `HttpClient` n'en envoie donc plus
aucun par défaut. Le piège est qu'un tel 429 **ressemble à un quota** : deux
heures d'attente et six reprises automatiques n'y ont rien changé, et chaque
tentative mourait en 80 secondes sur la première cotation non mise en cache.

Steam renvoie par ailleurs `success=true` **sans aucun champ de prix** pour un
objet sans annonce : `Quote.is_empty` distingue ce cas d'une erreur.

Steam limite à ~15 req/min et ne donne aucun float. `SteamMarket.order_book()`
est mort, mais pas pour la raison qu'on croit : **l'endpoint
`itemordershistogram` répond toujours** (`success=1`, `highest_buy_order`
renseigné, vérifié en septembre 2026). C'est la table nom → `item_nameid` qui
est perdue — les pages marché sont devenues une application cliente qui adresse
les objets par un identifiant opaque (`G182320AB023004`), et le HTML de 2,9 Mo
ne contient plus aucun entier exploitable. Rouvrir le carnet demande donc une
source d'identifiants, pas un nouvel endpoint.

## Données locales

`data/collections.json` régénérable, `data/prices.db` cache jetable,
`data/journal.db` achats réels de l'utilisateur (jamais versionné ni purgé),
`.env` porte `CSFLOAT_API_KEY`.