# Guide — ce qu'il faut faire

Toutes les commandes se tapent dans PowerShell, testées sur cette machine.

> **La conclusion de toute l'analyse : travaillez uniquement sur CSFloat.**
> Steam coûte ~70 % plus cher à l'achat, ne permet pas de choisir les floats, et
> son porte-monnaie n'est pas retirable. Les commandes Steam existent encore
> dans l'outil, mais vous n'en avez pas besoin. Elles sont en annexe.

---

## Une seule fois : installer

```powershell
cd "c:\dossier temporaire pour contourne O le maudit\Bureau temporaire\projets vscode\cs_tradeup_algo"
python -m pip install -e .
python -m scripts.build_db
```

> `python -m pip`, **pas** `pip` : sur cette machine `pip` appartient à Python 3.9
> et refuse d'installer.

Votre clé CSFloat est déjà enregistrée dans le fichier `.env`. Rien à faire.

Vérifiez que tout marche :

```powershell
python -m tradeup.cli db
```

Vous devez voir `Base : 94 collections, 1451 skins`.

---

## L'application

Une seule commande, puis tout se fait à la souris :

```powershell
python -m tradeup.web
```

Votre navigateur s'ouvre sur la liste des collections. Vous choisissez la
rareté, filtrez par nombre de sorties, cliquez sur **Calculer** — et le plan
s'affiche avec les 10 annonces et leurs boutons d'achat.

Les collections à **une seule sortie** (résultat certain) sont en tête de liste
et marquées d'un badge. La colonne **Requêtes** indique ce que le calcul coûtera
en appels CSFloat, avant de le lancer : c'est ce qui vous évite d'épuiser le
quota en enchaînant les essais.

Le calcul prend quelques minutes ; la page affiche un chronomètre et reste
utilisable. `Ctrl+C` dans le terminal arrête le serveur.

> L'application n'écoute que sur `127.0.0.1` : elle détient votre clé API, elle
> n'a rien à faire sur le réseau. Personne d'autre ne peut y accéder.

### Trois onglets

**Calculer** — la liste des collections et le lancement des plans.

**Historique des plans** — tout plan calculé y est conservé automatiquement. Il
a coûté des requêtes CSFloat, autant pouvoir le retrouver. Le bouton **Suivre**
en fait un contrat.

**Mes contrats** — le suivi de ce que vous avez réellement acheté.

### Suivre un contrat

C'est ce qui répond au problème de plusieurs trade-ups menés de front.

Chaque contrat liste ses 10 objets. Quand vous en achetez un, cliquez
**Acheté** : on vous demande le prix et le float **réels**, qui peuvent différer
du plan si vous avez pris un substitut.

L'application affiche alors :

- **la progression** (combien d'objets sur 10) et le dépensé réel face au prévu ;
- **la date d'exécution** — c'est le *dernier* objet acheté qui commande, son
  verrou de 7 jours expirant en dernier. Acheter étalé coûte des jours ;
- **la dérive de float** — si votre moyenne réelle s'écarte de plus de 0,003 de
  celle du plan, un avertissement apparaît. C'est le signal que la sortie a pu
  changer de palier d'usure, donc que le gain calculé ne tient plus.

Vos achats sont enregistrés dans `data/journal.db`, qui survit aux redémarrages.
Ce fichier contient vos données personnelles : il est exclu de git.

---

## En ligne de commande (si vous préférez)

### 1. Obtenir le panier à acheter

```powershell
python -m tradeup.cli plan "The Bank Collection" --html
```

C'est **la** commande. Elle interroge les annonces réellement en vente sur
CSFloat et ouvre dans votre navigateur une page avec les 10 objets précis à
acheter — **chacun avec un bouton menant directement à son annonce**.

C'est le point important : sans ces liens, retrouver dix objets un par un par
leur float prend de longues minutes, pendant lesquelles certaines annonces sont
vendues. Votre moyenne de float change alors, et le contrat ne donne plus le
résultat calculé.

La page affiche son âge en haut. **Au-delà de 15 minutes, elle se signale
elle-même comme périmée** : relancez la commande.

Sans `--html`, tout s'affiche dans le terminal comme avant.

### 2. Vérifier juste avant d'acheter

Relancez la **même commande** au moment d'acheter. Les prix et les annonces
bougent d'heure en heure : le panier de ce matin n'est plus celui de ce soir.

C'est tout. Le reste de ce guide explique comment lire le résultat.

---

## Lire le résultat

```
Achat et revente sur CSFloat. Tous les montants sont en USD.

A ACHETER (offres reelles, floats exacts) :
  Desert Eagle | Meteorite (Factory New)      1.50  float 0.0145
  Desert Eagle | Meteorite (Factory New)      1.47  float 0.0296
  Desert Eagle | Meteorite (Minimal Wear)     1.08  float 0.0729
  ... (10 lignes au total)
  TOTAL                                      11.37  moyenne 0.0698

SORTIE :
  AK-47 | Emerald Pinstripe (Factory New)   100.0%  float 0.0698  net 16.02

Cout 11.37 | EV nette 16.02 | profit +4.65 (+40.9%)

TOLERANCE D'EXECUTION : 0.0505 de float sur la somme des 10 entrees.
    reussite : +4.65   |   palier rate : -7.96
```

**Les 10 lignes sont des annonces précises**, pas des types d'objets. Achetez
celles-là, au float indiqué.

**`100.0%`** signifie que la sortie est certaine : vous savez à l'avance que
vous obtiendrez un AK-47 Emerald Pinstripe. Aucun tirage au sort.

**La tolérance d'exécution** est le seul vrai risque. Si une annonce est vendue
avant vous, remplacez-la par une de float **comparable**. Si la somme des 10
floats dépasse la tolérance, la sortie tombe en Minimal Wear et vous perdez
~8 $ au lieu d'en gagner 4,65.

---

## Le déroulé complet, pas à pas

### Étape 1 — Vérifier le moteur dans le jeu (gratuit, à faire une fois)

Avant de dépenser quoi que ce soit. Lancez CS2, ouvrez un contrat d'échange,
placez-y des Mil-Spec de The Bank Collection — **sans valider**.

Le jeu doit annoncer **AK-47 | Emerald Pinstripe à 100 %**.

Si ça ne correspond pas, arrêtez tout et dites-le-moi : ça voudrait dire que le
calcul de l'outil est faux.

### Étape 2 — Obtenir le panier

```powershell
python -m tradeup.cli plan "The Bank Collection"
```

### Étape 3 — Acheter sur CSFloat

Achetez les 10 annonces listées. Filtrez par float sur le site pour retrouver
les bonnes.

### Étape 4 — Faire le contrat dans CS2

Placez les 10 objets dans un contrat d'échange et validez.

> Les objets achetés sur CSFloat arrivent avec un blocage d'échange de 7 jours.
> **À vérifier** : ce blocage empêche-t-il de les utiliser dans un contrat ?
> Ma compréhension est que non — le verrou bloque l'échange et la revente, pas
> le craft. Testez-le avec un objet bon marché avant de tout acheter.

### Étape 5 — Revendre sur CSFloat

Mettez l'AK-47 en vente. **Notez combien de temps la vente prend** : c'est la
donnée qui manque pour savoir combien de fois répéter l'opération.

---

## Combien de fois répéter ?

**Je ne peux pas encore vous le dire**, et c'est honnête de le préciser.

La sortie étant certaine, il n'y a aucune incertitude statistique : un seul
contrat suffit à réaliser le gain. La limite est le **marché**, pas le hasard.

- Côté achat : au moins 50 Desert Eagle Meteorite en vente. Pas un problème
  immédiat.
- Côté revente : ~50 vendeurs déjà en file sur l'AK-47, et une dizaine de
  ventes par jour. **C'est là que ça coince.**

Faites-en **un seul**, revendez-le, et mesurez le délai de vente. Ce chiffre
vous dira tout.

---

## Si ça ne marche pas

| Message | Cause | Solution |
|---|---|---|
| `No module named tradeup` | mauvais dossier | refaire le `cd` puis l'installation |
| `requires a different Python` | vous avez tapé `pip` | tapez `python -m pip` |
| `Base introuvable` | base non construite | `python -m scripts.build_db` |
| `Cle API CSFloat absente` | `.env` perdu | remettre `CSFLOAT_API_KEY=...` dedans |
| `429 persistant` | quota CSFloat épuisé | **attendre quelques minutes**, ne pas réessayer |
| `Aucun panier realisable` | pas assez d'annonces | réessayer plus tard |

Le `429` est le plus courant si vous enchaînez les commandes. CSFloat limite sur
une fenêtre longue : relancer immédiatement ne fait qu'aggraver. Attendez.

---

## Annexe — explorer d'autres collections

Pour voir quelles collections ont peu de sorties possibles (donc peu de
variance), sans aucune requête réseau :

```powershell
python -m tradeup.cli collections --rarity mil-spec --max-outcomes 2
```

Les 9 collections marquées `<<` ont **une seule sortie possible**. Je les ai
toutes testées sur CSFloat : seule **The Bank** est nettement rentable
(+40,9 %), The Dust 2 l'est marginalement (+16,2 %), les autres sont perdantes.

Pour tester une autre collection :

```powershell
python -m tradeup.cli plan "The Dust 2 Collection"
```

> Allez-y doucement : chaque `plan` consomme une trentaine de requêtes CSFloat
> et le quota est vite atteint.

## Annexe — les commandes Steam

`scan`, `price`, `cache` travaillent sur les prix Steam. Elles ont servi à
explorer le problème, mais Steam n'est pas le marché où exécuter. Vous pouvez
les ignorer. Voir le [README](README.md) si vous y revenez un jour.
