"""Trade-ups realisables avec les skins qu'on possede deja.

Deux differences avec `scan` et `plan`, et la premiere est economique.

QUEL COUT DONNER A UN OBJET DEJA POSSEDE
----------------------------------------
La reponse intuitive -- ce qu'on a paye -- est fausse. C'est un cout irrecuperable :
il est deja sorti du portefeuille quoi qu'on decide maintenant, donc il ne doit
peser sur aucune decision. La reponse tentante -- zero, "je l'ai deja" -- est
pire encore : elle rend tout contrat rentable et pousse a fondre des skins qui
valaient mieux que leur sortie.

Le bon cout est le cout d'OPPORTUNITE : ce qu'on encaisserait en revendant
l'objet aujourd'hui. Utiliser un skin dans un contrat, c'est renoncer a cette
vente. Un contrat n'ajoute de la valeur que si sa sortie esperee depasse ce que
les dix entrees rapporteraient vendues telles quelles.

Consequence a assumer : beaucoup de contrats "gratuits" apparaissent perdants.
C'est le resultat correct -- ils l'etaient deja, le cout etait simplement
invisible.

DES OBJETS UNIQUES, AU FLOAT CONNU
----------------------------------
On possede trois exemplaires, pas un stock infini : la selection est 0/1, comme
pour les annonces reelles (`cheapest_unique_selection`). Et le float de chaque
objet est LU, pas suppose : aucun alea a modeliser, donc aucune marge de
securite a prendre.
"""

from __future__ import annotations

import csv
import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .db import SkinDatabase
from .ev import InputItem, PriceLookup, TradeUpResult, evaluate
from .generator import InputOption, cheapest_unique_selection
from .models import Collection, Rarity, Skin
from .scoring import Ranking, sort_key
from .wear import TRADEUP_INPUT_COUNT, wear_breakpoints, wear_of

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OwnedItem:
    """Un skin reellement possede."""

    market_hash_name: str
    float_value: float | None
    asset_id: str = ""
    stattrak: bool = False
    tradable: bool = True
    stickers: int = 0
    keychains: int = 0
    listed: bool = False  # deja mis en vente sur CSFloat
    paid: float | None = None  # prix d'achat, pour information seulement

    @property
    def souvenir(self) -> bool:
        """Cet objet est-il de qualite Souvenir ?

        Ils ETAIENT interdits en contrat ; ils ne le sont plus depuis la mise a
        jour du 21 mai 2026 : "Souvenir quality items can now be selected in
        Trade Up Contract alongside normal quality items. All Souvenir
        attributes will be removed from any souvenir items selected."

        Le prefixe du nom de marche fait foi. Le champ `souvenir` de la base
        statique, lui, vaut True sur les 1451 skins : il ne distingue rien et ne
        doit pas servir a decider.

        Consequence a garder en tete : la sortie perd les attributs Souvenir,
        donc elle se valorise comme un skin NORMAL -- ce que le moteur fait
        deja, puisqu'il ne produit jamais de Souvenir.
        """
        return self.market_hash_name.startswith("Souvenir ")

    @property
    def usable(self) -> bool:
        """Cet objet peut-il entrer dans un contrat aujourd'hui ?

        Le float est indispensable : sans lui on ne sait pas calculer l'usure de
        sortie. `tradable` porte le verrou de 7 jours -- un objet recu par
        echange n'est pas utilisable avant, et l'API le dit.

        Les Souvenir ne sont plus exclus (voir `souvenir`).
        """
        return self.float_value is not None and self.tradable

    @property
    def decorated(self) -> bool:
        """Porte-t-il des stickers ou une breloque ?

        Fondre un objet stické detruit les stickers. Ce n'est pas interdit, mais
        c'est une perte que le prix de marche du skin nu ne reflete pas.
        """
        return bool(self.stickers or self.keychains)


@dataclass(frozen=True, slots=True)
class InventoryTradeUp:
    """Un contrat realisable avec les objets possedes."""

    result: TradeUpResult
    options: tuple[InputOption, ...]
    collection: Collection
    rarity: Rarity
    items: tuple[OwnedItem, ...]

    @property
    def opportunity_cost(self) -> float:
        """Ce que les dix entrees rapporteraient vendues telles quelles."""
        return self.result.cost

    @property
    def gain(self) -> float:
        """Valeur ajoutee par le contrat, cout d'opportunite deduit."""
        return self.result.ev_profit

    @property
    def always_profitable(self) -> bool:
        """Toutes les sorties rapportent-elles plus que les entrees ?

        Le tirage ne peut alors pas faire perdre. C'est la propriete qui prime
        sur le nombre de sorties : trois issues toutes rentables valent mieux
        qu'une issue unique au gain marginal.
        """
        return bool(self.result.outcomes) and all(
            o.net_value >= self.result.cost for o in self.result.outcomes
        )

    @property
    def decorated_inputs(self) -> tuple[OwnedItem, ...]:
        return tuple(i for i in self.items if i.decorated)

    @property
    def listed_inputs(self) -> tuple[OwnedItem, ...]:
        return tuple(i for i in self.items if i.listed)

    def inputs_table(self) -> str:
        """Les dix objets a fondre.

        Pas `scoring.shopping_list` : celle-la titre "a acheter" et donne un
        "float max" a viser. Ici il n'y a rien a acheter et aucun float a viser
        -- les objets sont la, leur float est celui qu'il est. Afficher une
        liste de courses pour un inventaire ferait relire une consigne d'achat
        a quelqu'un qui doit juste selectionner dix objets dans son stock.
        """
        lignes = [
            f"{'a fondre':<46} {'float':>9} {'valeur si vendu':>16}",
            "-" * 74,
        ]
        for option, item in zip(self.options, self.items):
            marque = " *" if item.decorated else ""
            lignes.append(
                f"{option.name[:46]:<46} {option.float_value:>9.4f} "
                f"{option.unit_cost:>16.2f}{marque}"
            )
        lignes.append("-" * 74)
        lignes.append(
            f"{'TOTAL':<46} {self.result.avg_input_float:>9.4f} "
            f"{self.opportunity_cost:>16.2f}"
        )
        if self.decorated_inputs:
            lignes.append("* porte des stickers ou une breloque")
        return "\n".join(lignes)

    def report(self) -> str:
        r = self.result
        lignes = [
            f"{self.collection.name} -- {self.rarity.label}",
            f"  10 entrees possedees, valeur de revente {self.opportunity_cost:.2f}",
            f"  EV nette de la sortie {r.ev_net:.2f}  |  "
            f"gain {self.gain:+.2f} ({r.roi:+.1%})",
            # Le gain seul ne dit pas a quelle frequence on l'obtient : un +2
            # une fois sur trois vaut moins qu'un +0.50 a tous les coups.
            f"  {r.profit_probability:.0%} de chances d'y gagner"
            f"  |  {r.distinct_outcomes} sorties possibles"
            + ("  |  TOUTES RENTABLES" if self.always_profitable else ""),
            f"  float moyen d'entree {r.avg_input_float:.4f} "
            f"(exact : aucun tirage)",
        ]
        if self.gain <= 0:
            lignes.append(
                "  PERDANT : ces dix skins valent plus vendus que fondus."
            )
        for item in self.decorated_inputs:
            lignes.append(
                f"  ATTENTION : {item.market_hash_name} porte des stickers ou "
                f"une breloque -- le contrat les detruit, et leur valeur n'est "
                f"pas comptee ici."
            )
        if self.listed_inputs:
            lignes.append(
                f"  {len(self.listed_inputs)} de ces objets sont actuellement "
                f"EN VENTE sur CSFloat : il faudra retirer les annonces."
            )
        return "\n".join(lignes)


# --- Lecture de l'inventaire -------------------------------------------------


def from_csfloat_rows(rows: Iterable[dict]) -> list[OwnedItem]:
    """Convertit les lignes de `/me/inventory` en objets possedes.

    Accepte aussi `paid`, qui n'existe pas dans l'API : il ne peut venir que
    d'un fichier tenu a la main, et ne sert qu'a informer.
    """
    out: list[OwnedItem] = []
    for row in rows:
        nom = row.get("market_hash_name")
        if not nom:
            continue
        flt = row.get("float_value")
        paye = row.get("paid")
        out.append(
            OwnedItem(
                market_hash_name=str(nom),
                float_value=float(flt) if flt is not None else None,
                asset_id=str(row.get("asset_id", "")),
                stattrak=bool(row.get("is_stattrak") or row.get("stattrak")),
                # L'API renvoie 1/0 ; absent = on suppose echangeable plutot
                # que de masquer tout l'inventaire sur un champ manquant.
                tradable=bool(row.get("tradable", 1)),
                stickers=_compte(row.get("stickers")),
                keychains=_compte(row.get("keychains")),
                listed=bool(row.get("listing_id") or row.get("listed")),
                paid=float(paye) if paye is not None else None,
            )
        )
    return out


def _compte(valeur) -> int:
    """Nombre de stickers / breloques, que la source donne une liste ou un nombre."""
    if valeur is None:
        return 0
    if isinstance(valeur, (int, float)):
        return int(valeur)
    try:
        return len(valeur)
    except TypeError:
        return 0


def load_file(path: Path | str) -> list[OwnedItem]:
    """Lit un inventaire depuis un fichier JSON ou CSV.

    Permet de travailler sans cle API, et de simuler un inventaire qu'on
    n'a pas encore. Colonnes attendues : `market_hash_name` et `float_value`,
    le reste est optionnel.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Inventaire introuvable : {p}")

    texte = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".csv":
        lignes = list(csv.DictReader(texte.splitlines()))
    else:
        brut = json.loads(texte)
        # Accepte aussi bien la reponse brute de l'API qu'une liste nue.
        lignes = brut.get("items", brut) if isinstance(brut, dict) else brut

    if not isinstance(lignes, list):
        raise ValueError(f"{p} : une liste d'objets etait attendue")

    normalisees = []
    for row in lignes:
        if not isinstance(row, dict):
            continue
        copie = dict(row)
        # Un CSV ne porte que du texte : "0.07" et "false" doivent redevenir
        # un nombre et un booleen, sinon tout est vrai et rien n'est un float.
        for cle in ("float_value", "paid"):
            if copie.get(cle) in ("", None):
                copie.pop(cle, None)
            else:
                copie[cle] = float(copie[cle])
        for cle in ("tradable", "stattrak", "listed"):
            if isinstance(copie.get(cle), str):
                copie[cle] = copie[cle].strip().lower() not in (
                    "0", "false", "non", "no", ""
                )
        normalisees.append(copie)

    return from_csfloat_rows(normalisees)


# --- Calcul ------------------------------------------------------------------


def resolve(db: SkinDatabase, items: Sequence[OwnedItem]) -> list[tuple[OwnedItem, Skin]]:
    """Associe chaque objet possede au skin de la base, quand il existe.

    Les objets non reconnus (agents, autocollants, caisses, couteaux hors base)
    sont simplement ignores : ils ne peuvent pas servir d'entree.
    """
    apparies: list[tuple[OwnedItem, Skin]] = []
    for item in items:
        if not item.usable:
            continue
        nom = item.market_hash_name
        # Retirer le prefixe StatTrak et le palier d'usure pour retrouver le
        # skin : la base indexe "AK-47 | Redline", pas le nom de marche complet.
        base = (nom.replace("StatTrak™ ", "")
                   .replace("Souvenir ", "")
                   .replace("★ ", ""))
        if "(" in base:
            base = base[: base.rindex("(")].strip()
        skin = db.find(base)
        if skin is None:
            log.debug("objet non reconnu, ignore : %s", nom)
            continue
        if not (skin.min_float <= item.float_value <= skin.max_float):
            # Incoherent avec la base : on ne devine pas.
            log.info("float hors range pour %s : %s", nom, item.float_value)
            continue
        apparies.append((item, skin))
    return apparies


def _options(
    apparies: Sequence[tuple[OwnedItem, Skin]], prices: PriceLookup, stattrak: bool
) -> list[tuple[InputOption, OwnedItem]]:
    """Options d'entree valorisees a leur cout d'OPPORTUNITE.

    Le cout retenu est `sell_net` : ce qu'on toucherait en revendant l'objet,
    frais deduits. Un objet sans prix de revente connu est ecarte -- sans lui on
    ne peut pas dire si le fondre cree ou detruit de la valeur.
    """
    out: list[tuple[InputOption, OwnedItem]] = []
    for item, skin in apparies:
        if item.stattrak != stattrak:
            continue  # le jeu refuse le melange
        wear = wear_of(item.float_value)
        valeur = prices.sell_net(skin, wear, stattrak)
        if valeur is None or valeur <= 0:
            continue
        out.append((
            InputOption(
                skin=skin,
                wear=wear,
                unit_cost=valeur,
                float_value=item.float_value,
                owned=True,
            ),
            item,
        ))
    return out


def best_tradeups(
    db: SkinDatabase,
    items: Sequence[OwnedItem],
    prices: PriceLookup,
    rarity: Rarity,
    *,
    stattrak: bool = False,
    limit: int | None = 10,
    include_losing: bool = False,
    ranking: Ranking = Ranking.RISK_ADJUSTED,
) -> list[InventoryTradeUp]:
    """Meilleurs contrats realisables avec ce qu'on possede, par collection.

    Mono-collection uniquement : melanger deux collections depuis un inventaire
    suppose posseder assez d'objets dans chacune, ce qui est rare, et multiplie
    les combinaisons sans rien apporter tant que le cas simple n'est pas couvert.

    `ranking` reprend les criteres du scan. Le gain brut seul est trompeur ici :
    un contrat a +2 qui ne gagne qu'une fois sur trois vaut moins qu'un contrat
    a +0.50 qui gagne a tous les coups, et c'est la regle du domaine -- trois
    issues toutes rentables valent mieux qu'une issue unique au gain marginal.
    """
    # Regrouper AVANT de valoriser. Un contrat exige dix objets de la meme
    # collection : les collections qui n'en ont pas assez ne produiront rien,
    # et les coter serait du quota depense pour rien -- sur CSFloat, chaque
    # valorisation est une requete.
    candidats: dict[str, list[tuple[OwnedItem, Skin]]] = {}
    for item, skin in resolve(db, items):
        if skin.rarity is not rarity or item.stattrak != stattrak:
            continue
        candidats.setdefault(skin.collection_id, []).append((item, skin))

    plans: list[InventoryTradeUp] = []
    for cid, apparies in candidats.items():
        if len(apparies) < TRADEUP_INPUT_COUNT:
            continue
        groupe = _options(apparies, prices, stattrak)
        if len(groupe) < TRADEUP_INPUT_COUNT:
            continue  # des objets sans prix de revente connu
        collection = db.collection(cid)
        sorties = collection.outcomes_for_input_rarity(rarity, stattrak)
        if not sorties:
            continue

        meilleur = _meilleur_pour_collection(
            collection, rarity, groupe, sorties, prices, stattrak
        )
        if meilleur is not None and (include_losing or meilleur.gain > 0):
            plans.append(meilleur)

    plans.sort(key=lambda p: sort_key(p.result, ranking), reverse=True)
    return plans[:limit] if limit else plans


def _meilleur_pour_collection(
    collection: Collection,
    rarity: Rarity,
    groupe: Sequence[tuple[InputOption, OwnedItem]],
    sorties: Sequence[Skin],
    prices: PriceLookup,
    stattrak: bool,
) -> InventoryTradeUp | None:
    """Balaye les paliers d'usure et retient le meilleur lot de dix.

    Les floats etant connus, aucune marge de securite n'est prise : il n'y a pas
    de tirage a couvrir. Le seul risque restant serait de vendre un des objets
    entre-temps, ce que l'utilisateur maitrise.
    """
    options = [o for o, _ in groupe]
    par_option = {id(o): item for o, item in groupe}
    outcomes_map = {collection.id: tuple(sorties)}

    meilleur: InventoryTradeUp | None = None
    for hi in wear_breakpoints(sorties)[1:]:
        budget = (hi - 1e-9) * TRADEUP_INPUT_COUNT
        selection = cheapest_unique_selection(options, TRADEUP_INPUT_COUNT, budget)
        if selection is None:
            continue

        entrees = [
            InputItem(skin=o.skin, float_value=o.float_value, unit_cost=o.unit_cost)
            for o in selection
        ]
        atteint = sum(o.normalized for o in selection) / TRADEUP_INPUT_COUNT
        resultat = evaluate(
            entrees, outcomes_map, prices, stattrak=stattrak,
            cliff_distance=hi - atteint,
        )
        if meilleur is None or resultat.ev_profit > meilleur.result.ev_profit:
            meilleur = InventoryTradeUp(
                result=resultat,
                options=tuple(selection),
                collection=collection,
                rarity=rarity,
                items=tuple(par_option[id(o)] for o in selection),
            )
    return meilleur


def summary(db: SkinDatabase, items: Sequence[OwnedItem]) -> dict:
    """Ce que contient l'inventaire, du point de vue des trade-ups."""
    apparies = resolve(db, items)
    par_rarete: dict[str, int] = {}
    for _, skin in apparies:
        par_rarete[skin.rarity.label] = par_rarete.get(skin.rarity.label, 0) + 1
    return {
        "objets": len(items),
        "utilisables": len(apparies),
        "sans_float": sum(1 for i in items if i.float_value is None),
        "verrouilles": sum(1 for i in items if not i.tradable),
        "souvenirs": sum(1 for i in items if i.souvenir),
        "en_vente": sum(1 for i in items if i.listed),
        "par_rarete": dict(sorted(par_rarete.items())),
    }


def buildable_collections(
    db: SkinDatabase,
    items: Sequence[OwnedItem],
    rarity: Rarity,
    *,
    stattrak: bool = False,
) -> list[dict]:
    """Collections ou un contrat est possible, et ce qu'il coutera a evaluer.

    Le cout est en requetes CSFloat : une par objet de marche distinct, entrees
    possedees ET sorties possibles. L'afficher AVANT de lancer est la regle de
    l'application -- le quota se vide vite et sans prevenir.
    """
    par_collection: dict[str, list[tuple[OwnedItem, Skin]]] = {}
    for item, skin in resolve(db, items):
        if skin.rarity is not rarity or item.stattrak != stattrak:
            continue
        par_collection.setdefault(skin.collection_id, []).append((item, skin))

    rows = []
    for cid, apparies in par_collection.items():
        if len(apparies) < TRADEUP_INPUT_COUNT:
            continue
        collection = db.collection(cid)
        sorties = collection.outcomes_for_input_rarity(rarity, stattrak)
        if not sorties:
            continue
        # Un meme nom de marche n'est cote qu'une fois, meme possede en dix
        # exemplaires : c'est le carnet du skin qu'on lit, pas l'objet.
        noms_entrees = {
            skin.market_hash_name(wear_of(item.float_value), stattrak)
            for item, skin in apparies
        }
        noms_sorties = {
            s.market_hash_name(w, stattrak)
            for s in sorties for w in s.available_wears()
        }
        rows.append({
            "id": cid,
            "name": collection.name,
            "owned": len(apparies),
            "outcomes": len(sorties),
            "requests": len(noms_entrees) + len(noms_sorties),
        })
    rows.sort(key=lambda r: (-r["owned"], r["name"]))
    return rows


def closest_gaps(
    db: SkinDatabase,
    items: Sequence[OwnedItem],
    rarity: Rarity,
    *,
    stattrak: bool = False,
    limit: int = 5,
) -> list[tuple[str, int]]:
    """Collections ou l'on est le plus pres des dix objets requis.

    Sans cela, "aucun contrat realisable" est indiscernable d'un bug : on ne
    sait pas si l'inventaire est loin du compte ou si le calcul a echoue. Ici
    la reponse est chiffree -- "4 sur 10 dans The Harlequin Collection".
    """
    compte: dict[str, int] = {}
    for item, skin in resolve(db, items):
        if skin.rarity is not rarity or item.stattrak != stattrak:
            continue
        compte[skin.collection_id] = compte.get(skin.collection_id, 0) + 1

    lignes = [(db.collection(cid).name, n) for cid, n in compte.items()]
    lignes.sort(key=lambda t: (-t[1], t[0]))
    return lignes[:limit]
