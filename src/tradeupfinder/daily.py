"""Passage quotidien : re-coter ce qui decide, classer, garder la trace.

Un classement n'a pas d'age moyen, il a l'age de ses prix. Mesure le
19 septembre 2026 : The Italy Collection ressortait a 94 % de profitabilite
sur un cache de quelques jours ; re-cotee en direct, elle tombait a 84 %
parce que le MP7 | Anodized Navy (FN) -- un tiers des issues -- avait perdu
31 % entre-temps. Le calcul etait juste, les prix etaient morts.

D'ou ce module. Il ne refait pas le scan : il repond a "sur quoi depenser les
requetes d'aujourd'hui pour que la decision d'aujourd'hui tienne ?".

La ressource rare n'est pas le temps de calcul, c'est le budget Steam :
environ 200 cotations par fenetre ouverte, et des fenetres qui se referment
pour des heures. Le budget va donc d'abord aux objets des MEILLEURS candidats
-- ceux sur lesquels on pourrait agir -- et seulement ensuite a l'extension de
la couverture. Rafraichir le 40e du classement ne change aucune decision.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .models import Collection, Rarity
from .pricing.base import PriceSource
from .pricing.cache import QuoteCache
from .scoring import Candidate

log = logging.getLogger(__name__)

#: Nombre de candidats dont les prix doivent etre frais a tout prix. Au-dela,
#: un contrat n'est plus un candidat : c'est une ligne de classement.
DEFAULT_TOP = 5

#: Budget de cotations par passage. Cale sur ce qu'une fenetre Steam ouverte
#: laisse effectivement passer avant de se refermer.
DEFAULT_BUDGET = 200

#: Age au-dela duquel un prix ne doit plus porter une decision. Sept jours
#: conviennent a une exploration, pas a un achat : sur un contrat a 1 EUR, un
#: skin peut perdre un tiers de sa valeur en une journee.
DECISION_MAX_AGE = 24 * 3600


@dataclass(frozen=True, slots=True)
class Ligne:
    """Un candidat, son verdict et l'age des prix qui le portent."""

    collection: str
    cost: float
    ev_net: float
    profitability: float
    profit_probability: float
    age_max: float | None  # secondes, None si aucun prix connu
    # Profitabilite recalculee sur des ANNONCES REELLES (`plan`), avec leurs
    # floats et leurs prix. None = pas encore confirme.
    confirmee: float | None = None
    motif_echec: str = ""

    @property
    def frais(self) -> bool:
        """Les prix sont-ils assez recents pour decider ?"""
        return self.age_max is not None and self.age_max <= DECISION_MAX_AGE

    @property
    def actionnable(self) -> bool:
        """Rentable SUR ANNONCES REELLES, et sur des prix frais.

        Trois conditions, et la premiere est la plus dure a satisfaire.

        `scan` suppose qu'un bas float s'obtient au prix du palier. C'etait
        vrai sur Steam tant que le float n'y etait pas visible ; les
        extensions de lecture de float l'ont rendu faux. Mesure le 20
        septembre 2026 sur la G3SG1 Green Apple (FN) : 0,19 au palier, 0,48
        sous 0,026 de float, soit +153 %. Le meme jour, `scan` annoncait le
        contrat Bank a 148 % et `plan`, sur 356 annonces reelles, le donnait
        a -0,3 %.

        Un chiffre de `scan` n'est donc PAS une occasion, c'est une piste. On
        n'agit que sur un chiffre de `plan`.
        """
        return (self.confirmee is not None
                and self.confirmee >= 1.0
                and self.frais)

    @property
    def ecart_confirmation(self) -> float | None:
        """De combien `scan` s'est-il trompe ? Negatif = il etait optimiste."""
        if self.confirmee is None:
            return None
        return self.confirmee - self.profitability


@dataclass
class Passage:
    """Ce qu'un passage quotidien a fait et trouve."""

    rarity: str
    lignes: list[Ligne] = field(default_factory=list)
    recotes: int = 0
    budget: int = 0
    epuise: bool = False  # le marche a ferme avant la fin
    horodatage: float = field(default_factory=time.time)

    @property
    def actionnables(self) -> list[Ligne]:
        return [x for x in self.lignes if x.actionnable]

    def resume(self) -> str:
        n = len(self.actionnables)
        etat = "budget epuise" if self.epuise else f"{self.recotes} recotees"
        if n:
            return f"{n} contrat(s) au-dessus du point mort sur prix frais ({etat})"
        return f"aucun contrat actionnable ({etat})"


def noms_prioritaires(
    candidats: Iterable[Candidate],
    collections: dict[str, Collection],
    rarity: Rarity,
    cache: QuoteCache,
    *,
    top: int = DEFAULT_TOP,
    budget: int = DEFAULT_BUDGET,
) -> list[str]:
    """Les cotations a refaire aujourd'hui, par ordre d'utilite decroissante.

    Deux etages, et l'ordre compte plus que le contenu :

    1. les objets des `top` meilleurs candidats, du plus vieux prix au plus
       recent. C'est la que le budget change une decision ;
    2. le reste du classement, meme regle.

    Un nom jamais cote passe avant tout le monde a l'interieur de son etage :
    une sortie sans prix ne fait pas baisser un candidat, elle l'empeche
    d'exister.
    """
    from .refresh import collection_roles

    vus: set[str] = set()
    etages: list[list[str]] = [[], []]

    for rang, cand in enumerate(candidats):
        etage = 0 if rang < top else 1
        for cid in _collections_du_candidat(cand, collections):
            col = collections.get(cid)
            if col is None:
                continue
            for nom in collection_roles(col, rarity):
                if nom not in vus:
                    vus.add(nom)
                    etages[etage].append(nom)

    def age(nom: str) -> float:
        q = cache.get(nom, "steam", ttl=float("inf"))
        # Jamais cote : age infini, donc prioritaire.
        return q.age_seconds if q else float("inf")

    ordonnes: list[str] = []
    for etage in etages:
        ordonnes.extend(sorted(etage, key=age, reverse=True))
    return ordonnes[:budget]


def _collections_du_candidat(cand: Candidate, connues: dict[str, Collection]) -> list[str]:
    """Identifiants de collection portes par un candidat.

    Le libelle est la seule information stable d'un `Candidate` : il porte le
    ou les noms de collection, eventuellement melangees.
    """
    return [cid for cid, col in connues.items() if col.name in cand.label]


def recoter(
    source: PriceSource,
    noms: list[str],
    *,
    cache: QuoteCache | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[int, bool]:
    """Re-cote la liste. Renvoie (nombre obtenu, marche ferme avant la fin).

    Un refus de Steam n'est pas une erreur a propager : le passage s'arrete
    avec ce qu'il a. Mieux vaut un classement partiellement rafraichi, qui dit
    l'age de chaque ligne, qu'un echec qui ne rafraichit rien.

    Une coupure reseau est traitee de la meme facon. La distinction entre
    "le marche refuse" et "la machine n'a plus de reseau" n'interesse personne
    a cet instant : dans les deux cas le passage s'arrete, garde ses
    cotations, et la colonne d'age dit lesquelles sont fraiches. Laisser
    remonter l'erreur ferait perdre tout le travail deja paye.
    """
    import urllib.error

    from .pricing.http import RateLimited

    obtenus = 0
    for i, nom in enumerate(noms, 1):
        try:
            q = source.refresh(nom)
        except (RateLimited, urllib.error.URLError) as exc:
            cause = "Marche ferme" if isinstance(exc, RateLimited) else "Reseau coupe"
            log.info("%s apres %d cotations sur %d", cause, obtenus, len(noms))
            return obtenus, True
        if q is not None:
            obtenus += 1
            if cache:
                cache.put(q)
        if progress:
            progress(i, len(noms))
    return obtenus, False


def age_max(
    collection: Collection, rarity: Rarity, cache: QuoteCache
) -> float | None:
    """Age du plus vieux prix dont depend ce contrat.

    Le maximum, pas la moyenne : c'est la ligne la plus perimee qui peut
    retourner le verdict, et une moyenne la dilue dans les fraiches.
    """
    from .refresh import collection_roles

    ages = []
    for nom in collection_roles(collection, rarity):
        q = cache.get(nom, "steam", ttl=float("inf"))
        if q is not None:
            ages.append(q.age_seconds)
    return max(ages) if ages else None


def journaliser(passage: Passage, chemin: Path) -> None:
    """Ajoute une ligne par passage, en JSON par ligne.

    Le but n'est pas l'archive : c'est de rendre la DERIVE visible. Un contrat
    a 99 % un jour et 84 % le lendemain n'est pas un contrat a 90 %, c'est un
    contrat qu'on ne sait pas evaluer -- et cela ne se voit que sur la serie.
    """
    chemin.parent.mkdir(parents=True, exist_ok=True)
    entree = {
        "horodatage": round(passage.horodatage),
        "rarete": passage.rarity,
        "recotes": passage.recotes,
        "epuise": passage.epuise,
        "candidats": [
            {
                "collection": x.collection,
                "cout": round(x.cost, 4),
                "ev": round(x.ev_net, 4),
                "profitabilite": round(x.profitability, 4),
                "p_gain": round(x.profit_probability, 4),
                "age_h": round(x.age_max / 3600, 1) if x.age_max else None,
            }
            for x in passage.lignes
        ],
    }
    with chemin.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entree, ensure_ascii=False) + "\n")


def historique(chemin: Path, collection: str, limite: int = 10) -> list[tuple[float, float]]:
    """Serie (horodatage, profitabilite) d'une collection, du plus ancien au recent."""
    if not chemin.exists():
        return []
    serie: list[tuple[float, float]] = []
    for ligne in chemin.read_text(encoding="utf-8").splitlines():
        if not ligne.strip():
            continue
        try:
            d = json.loads(ligne)
        except json.JSONDecodeError:
            continue
        for c in d.get("candidats", []):
            if c.get("collection") == collection:
                serie.append((d["horodatage"], c["profitabilite"]))
    return serie[-limite:]


#: Nombre de candidats confirmes par `plan` a chaque passage. Chacun coute
#: quelques centaines de requetes CSFloat -- c'est cher, donc on ne confirme
#: que la tete du classement. Au-dela, on ne deciderait rien de toute facon.
DEFAULT_CONFIRM = 3


def confirmer(
    db,
    lignes: list[Ligne],
    collections: dict[str, Collection],
    rarity: Rarity,
    source,
    *,
    combien: int = DEFAULT_CONFIRM,
    sell_source: PriceSource | None = None,
    sell_to_usd: float = 1.0,
    progress: Callable[[int, int], None] | None = None,
) -> list[Ligne]:
    """Recalcule les meilleurs candidats sur des ANNONCES REELLES.

    C'est l'etape qui separe une piste d'une occasion, et elle n'est pas
    facultative. `scan` raisonne sur un prix par palier d'usure en supposant
    qu'un bas float s'y obtient au meme prix. Cette hypothese a ete vraie sur
    Steam tant que le float n'y etait pas visible ; elle ne l'est plus.

    Mesure du 20 septembre 2026, contrat Bank en Industrial :
    `scan` 148 %, `plan` sur 356 annonces reelles **-0,3 %**. L'ecart n'est
    pas une imprecision, c'est un renversement.

    Les contrats melangeant deux collections ne sont pas confirmables ici --
    `plan` est mono-collection. Ils restent affiches, jamais actionnables.
    """
    from .plan import build_plan

    sortie: list[Ligne] = []
    fait = 0
    for rang, ligne in enumerate(lignes):
        if fait >= combien or "+" in ligne.collection:
            motif = ("melange : plan est mono-collection"
                     if "+" in ligne.collection else "hors du top confirme")
            sortie.append(Ligne(
                ligne.collection, ligne.cost, ligne.ev_net, ligne.profitability,
                ligne.profit_probability, ligne.age_max, None, motif))
            continue

        col = next((c for c in collections.values()
                    if c.name in ligne.collection), None)
        if col is None:
            sortie.append(Ligne(
                ligne.collection, ligne.cost, ligne.ev_net, ligne.profitability,
                ligne.profit_probability, ligne.age_max, None,
                "collection introuvable"))
            continue

        fait += 1
        if progress:
            progress(fait, combien)
        try:
            plan = build_plan(db, col, rarity, source, sell_source=sell_source,
                              sell_to_usd=sell_to_usd)
        except Exception as exc:  # noqa: BLE001 - un echec ne doit pas tout arreter
            log.warning("Confirmation impossible pour %s : %s", col.name, exc)
            sortie.append(Ligne(
                ligne.collection, ligne.cost, ligne.ev_net, ligne.profitability,
                ligne.profit_probability, ligne.age_max, None,
                f"echec : {type(exc).__name__}"))
            continue

        if plan is None:
            # Pas assez d'annonces reelles : ce n'est pas un contrat, c'est une
            # ligne de tableau. Le distinguer d'un contrat perdant importe.
            sortie.append(Ligne(
                ligne.collection, ligne.cost, ligne.ev_net, ligne.profitability,
                ligne.profit_probability, ligne.age_max, None,
                "pas assez d'annonces"))
            continue

        sortie.append(Ligne(
            ligne.collection, plan.result.cost, plan.result.ev_net,
            ligne.profitability, plan.result.profit_probability,
            ligne.age_max, plan.result.profitability, ""))
    return sortie
