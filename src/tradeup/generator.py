"""Generation et optimisation des contrats candidats.

Deux sous-problemes, resolus separement :

1. QUELLES collections melanger (la "recette") et dans quelles proportions.
2. Pour une recette donnee, QUELS objets acheter concretement, au meilleur cout,
   sous contrainte de moyenne de float.

Le point qui rend le probleme traitable : a recette fixee, l'EV ne depend du
float que par les paliers d'usure des skins de SORTIE. Elle est donc constante
par morceaux, et il suffit d'evaluer un point par palier au lieu d'echantillonner
l'intervalle [0, 1]. Sur chaque palier, la meilleure moyenne est la PLUS HAUTE
admissible : elle donne la meme sortie pour des entrees moins cheres.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from itertools import combinations

from .db import SkinDatabase
from .ev import InputItem, PriceLookup, TradeUpResult, evaluate
from .models import Collection, Rarity, Skin, Wear
from .wear import TRADEUP_INPUT_COUNT, wear_breakpoints

log = logging.getLogger(__name__)

EPS = 1e-9


@dataclass(frozen=True, slots=True)
class InputOption:
    """Un objet achetable, avec le float qu'on suppose pouvoir obtenir.

    `listing_id` n'est rempli que pour les offres REELLES (CSFloat). Il permet
    de pointer directement l'annonce : sans lui, retrouver a la main dix objets
    par leur float prend de longues minutes -- pendant lesquelles les annonces
    partent, ce qui invalide le panier calcule.
    """

    skin: Skin
    wear: Wear
    unit_cost: float
    float_value: float
    listing_id: str | None = None

    @property
    def name(self) -> str:
        return self.skin.market_hash_name(self.wear)

    @property
    def normalized(self) -> float:
        """Position du float dans le range du skin : la valeur qui compte.

        La DP contraint la somme des NORMALISES, pas celle des floats affiches.
        Deux objets au meme float mais de ranges differents ne pesent pas pareil
        dans un contrat.
        """
        return self.skin.normalized(self.float_value)

    @property
    def url(self) -> str | None:
        """Lien direct vers l'annonce, quand elle est identifiee."""
        return f"https://csfloat.com/item/{self.listing_id}" if self.listing_id else None


@dataclass(frozen=True, slots=True)
class Recipe:
    """Repartition des 10 entrees entre collections."""

    counts: tuple[tuple[str, int], ...]  # (collection_id, nombre d'entrees)
    rarity: Rarity

    @property
    def collection_ids(self) -> tuple[str, ...]:
        return tuple(cid for cid, _ in self.counts)

    @property
    def total(self) -> int:
        return sum(n for _, n in self.counts)

    def as_dict(self) -> dict[str, int]:
        return dict(self.counts)

    def label(self, db: SkinDatabase) -> str:
        return " + ".join(
            f"{n}x {db.collection(cid).name}" for cid, n in self.counts
        )


def build_options(
    collection: Collection,
    rarity: Rarity,
    prices: PriceLookup,
    *,
    float_percentile: float = 0.15,
    max_unit_cost: float | None = None,
) -> list[InputOption]:
    """Objets d'entree achetables dans cette collection, a cette rarete.

    `float_percentile` modelise le float que l'on arrive reellement a sourcer
    dans un palier d'usure : 0.15 = "je trouve des objets a 15 % du bas du
    palier". Sur CSFloat avec filtrage par float c'est realiste ; sur Steam,
    ou l'on ne choisit pas le float, il faut monter vers 0.5.
    """
    options: list[InputOption] = []
    for skin in collection.by_rarity(rarity):
        for wear in skin.available_wears():
            cost = prices.buy_cost(skin, wear, False)
            if cost is None or cost <= 0:
                continue
            if max_unit_cost is not None and cost > max_unit_cost:
                continue
            lo = max(wear.lo, skin.min_float)
            hi = min(wear.hi, skin.max_float)
            options.append(
                InputOption(
                    skin=skin,
                    wear=wear,
                    unit_cost=cost,
                    float_value=lo + float_percentile * (hi - lo),
                )
            )
    return options


def cheapest_selection(
    options: Sequence[InputOption], count: int, float_budget: float
) -> list[InputOption] | None:
    """Selection de `count` objets de cout minimal, somme des floats <= budget.

    Programmation dynamique sur une frontiere de Pareto (float cumule, cout
    cumule). Les etats domines sont elimines a chaque slot, ce qui garde la
    frontiere de l'ordre de quelques dizaines d'elements meme avec beaucoup
    d'options. Exact, et assez rapide pour etre appele des milliers de fois.
    """
    if count <= 0:
        return []
    if not options:
        return None

    frontier = _extend_frontier(_INITIAL_FRONTIER, options, count, float_budget)
    if frontier is None:
        return None
    return list(min(frontier, key=lambda s: s[1])[2])


# Etat de depart de la DP : rien de choisi, cout et float nuls.
_State = tuple[float, float, tuple[InputOption, ...]]
_INITIAL_FRONTIER: list[_State] = [(0.0, 0.0, ())]


def _extend_frontier(
    frontier: list[_State],
    options: Sequence[InputOption],
    count: int,
    float_budget: float,
) -> list[_State] | None:
    """Ajoute `count` objets a chaque etat, en ne gardant que le front de Pareto.

    Renvoie None si le budget de float rend la selection impossible.
    """
    # Un objet est inutile s'il est a la fois plus cher et de float superieur.
    pool = _prune_dominated(options)
    if not pool:
        return None

    current = frontier
    for _ in range(count):
        nxt: list[_State] = []
        for f_sum, c_sum, sel in current:
            for opt in pool:
                nf = f_sum + opt.normalized
                if nf > float_budget + EPS:
                    continue
                nxt.append((nf, c_sum + opt.unit_cost, sel + (opt,)))
        if not nxt:
            return None
        current = _pareto_filter(nxt)
    return current


def _prune_dominated(options: Sequence[InputOption]) -> list[InputOption]:
    ordered = sorted(options, key=lambda o: (o.normalized, o.unit_cost))
    kept: list[InputOption] = []
    best_cost = float("inf")
    for opt in ordered:
        if opt.unit_cost < best_cost - EPS:
            kept.append(opt)
            best_cost = opt.unit_cost
    return kept


def _pareto_filter(
    states: list[tuple[float, float, tuple[InputOption, ...]]],
) -> list[tuple[float, float, tuple[InputOption, ...]]]:
    states.sort(key=lambda s: (s[0], s[1]))
    kept = []
    best_cost = float("inf")
    for st in states:
        if st[1] < best_cost - EPS:
            kept.append(st)
            best_cost = st[1]
    return kept


def cheapest_unique_selection(
    options: Sequence[InputOption], count: int, float_budget: float
) -> list[InputOption] | None:
    """Comme `cheapest_selection`, mais chaque option ne sert QU'UNE FOIS.

    Indispensable des qu'on travaille sur des offres reelles : une annonce
    CSFloat est un objet unique, on ne peut pas l'acheter dix fois. Le
    selecteur a repetition, lui, suppose un stock infini au meilleur prix --
    hypothese acceptable pour un pre-scan par paliers d'usure, fausse pour un
    panier qu'on va reellement passer en caisse.

    DP 0/1 : on parcourt les offres une fois, l'etat est (nombre choisi, float
    cumule) et on ne garde que le front de Pareto par nombre d'objets.
    """
    if count <= 0:
        return []
    if len(options) < count:
        return None

    # frontier[k] = etats ayant deja retenu k objets.
    frontier: list[list[_State]] = [[] for _ in range(count + 1)]
    frontier[0] = [(0.0, 0.0, ())]

    for opt in options:
        # Parcours descendant : une offre ne peut pas se reutiliser dans le
        # meme passage.
        for k in range(min(count - 1, count), -1, -1):
            if not frontier[k]:
                continue
            promoted: list[_State] = []
            for f_sum, c_sum, sel in frontier[k]:
                nf = f_sum + opt.normalized
                if nf > float_budget + EPS:
                    continue
                promoted.append((nf, c_sum + opt.unit_cost, sel + (opt,)))
            if promoted:
                frontier[k + 1] = _pareto_filter(frontier[k + 1] + promoted)

    if not frontier[count]:
        return None
    return list(min(frontier[count], key=lambda s: s[1])[2])


def options_from_listings(
    skin: Skin, listings: Sequence[object], *, max_per_skin: int | None = None
) -> list[InputOption]:
    """Transforme des offres reelles (CSFloat) en options d'achat.

    Contrairement a `build_options`, le float n'est plus une hypothese : c'est
    celui de l'objet mis en vente. C'est ce qui permet de supprimer la marge de
    securite anti-falaise -- il n'y a plus de tirage.
    """
    from .wear import wear_of  # import local : evite un cycle a l'import

    out: list[InputOption] = []
    for listing in listings:
        float_value = getattr(listing, "float_value", None)
        price = getattr(listing, "price", None)
        if float_value is None or price is None or price <= 0:
            continue
        if not skin.min_float <= float_value <= skin.max_float:
            continue  # incoherent avec la base : on ne devine pas
        out.append(
            InputOption(
                skin=skin,
                wear=wear_of(float_value),
                unit_cost=float(price),
                float_value=float(float_value),
                listing_id=(str(lid) if (lid := getattr(listing, "listing_id", None)) else None),
            )
        )
    out.sort(key=lambda o: o.unit_cost)
    return out[:max_per_skin] if max_per_skin else out


def iter_recipes(
    db: SkinDatabase,
    rarity: Rarity,
    *,
    max_collections: int = 2,
    collection_filter: Iterable[str] | None = None,
    min_share: int = 1,
) -> Iterator[Recipe]:
    """Enumere les repartitions de 10 entrees entre collections.

    `max_collections=1` ne garde que les contrats mono-collection : ce sont les
    moins variables, et ceux ou l'on controle le mieux la sortie. Les melanges
    a 2 collections servent a diluer vers une collection a forte valeur.
    """
    pool = db.tradeable_collections(rarity)
    if collection_filter is not None:
        wanted = set(collection_filter)
        pool = [c for c in pool if c.id in wanted or c.name in wanted]

    for c in pool:
        yield Recipe(counts=((c.id, TRADEUP_INPUT_COUNT),), rarity=rarity)

    if max_collections >= 2:
        for a, b in combinations(pool, 2):
            for n in range(min_share, TRADEUP_INPUT_COUNT - min_share + 1):
                yield Recipe(
                    counts=((a.id, n), (b.id, TRADEUP_INPUT_COUNT - n)), rarity=rarity
                )


def optimize_recipe(
    recipe: Recipe,
    db: SkinDatabase,
    prices: PriceLookup,
    *,
    float_percentile: float = 0.15,
    max_unit_cost: float | None = None,
    stattrak: bool = False,
    float_safety: float = 0.02,
) -> TradeUpResult | None:
    """Meilleur contrat realisable pour une recette donnee.

    Balaye les paliers d'EV (constants par morceaux) et, sur chacun, cherche le
    lot d'entrees le moins cher qui atteint la moyenne de float requise.

    `float_safety` corrige un biais systematique de l'optimiseur. Le cout etant
    minimal juste sous une frontiere d'usure, l'optimiseur s'y gare toujours --
    or c'est la position la plus dangereuse : les floats d'entree reels sont des
    TIRAGES, pas des valeurs choisies, et quelques millièmes de derive font
    basculer toutes les sorties d'un palier vers le suivant. Sur la collection
    Fracture, passer de 0.0650 a 0.0705 de moyenne fait chuter l'EV de 4.88 a
    1.73. On s'interdit donc de viser dans les `float_safety` sous une frontiere.

    Le defaut de 0.02 vient d'une mesure, pas d'une intuition. Les floats d'un
    lot de 10 objets tires au hasard dans leurs paliers ont un ecart-type de
    moyenne d'environ 0.0066. Une marge de 0.02 represente donc ~3 ecarts-types
    (risque de basculement ~0.1 %), la ou l'ancien defaut de 0.005 n'en couvrait
    que 0.74 -- soit 23 % de chances de rater le palier vise. Sur le contrat
    The Bank, ce 23 % transformait un profit affiche de +3.75 en +0.40 reel.

    Mettre 0 n'a de sens que si les floats d'entree sont verifies un par un
    (via CSFloat), auquel cas le tirage n'est plus aleatoire.
    """
    collections = [db.collection(cid) for cid, _ in recipe.counts]
    outcomes_map = db.outcomes_map(collections, recipe.rarity)
    all_outcomes = [s for skins in outcomes_map.values() for s in skins]
    if not all_outcomes:
        return None

    options_by_collection = {
        c.id: build_options(
            c,
            recipe.rarity,
            prices,
            float_percentile=float_percentile,
            max_unit_cost=max_unit_cost,
        )
        for c in collections
    }
    if any(not opts for opts in options_by_collection.values()):
        return None  # au moins une collection sans prix exploitable

    best: TradeUpResult | None = None
    breakpoints = wear_breakpoints(all_outcomes)

    for i, hi in enumerate(breakpoints[1:], 1):
        # Sur le palier qui finit en `hi`, la moyenne la plus haute est la moins
        # chere -- mais on garde `float_safety` de marge sous la frontiere.
        lo = breakpoints[i - 1]
        target_avg = hi - max(float_safety, 1e-7)
        if target_avg < lo:
            continue  # palier trop etroit pour y tenir en securite
        budget = target_avg * TRADEUP_INPUT_COUNT

        selection = _select_across_collections(recipe, options_by_collection, budget)
        if selection is None:
            continue

        items = [
            InputItem(skin=o.skin, float_value=o.float_value, unit_cost=o.unit_cost)
            for o in selection
        ]
        achieved = sum(o.normalized for o in selection) / TRADEUP_INPUT_COUNT
        result = evaluate(
            items,
            outcomes_map,
            prices,
            stattrak=stattrak,
            cliff_distance=hi - achieved,
        )
        if best is None or result.ev_profit > best.ev_profit:
            best = result

    return best


def _select_across_collections(
    recipe: Recipe,
    options_by_collection: dict[str, list[InputOption]],
    total_budget: float,
) -> list[InputOption] | None:
    """Repartit le budget de float entre collections, au cout minimal.

    Pour une recette mono-collection c'est direct. Pour un melange, on traite
    les slots de toutes les collections dans une meme DP en enchainant les
    contraintes, ce qui reste exact.
    """
    frontier: list[_State] | None = _INITIAL_FRONTIER
    for cid, count in recipe.counts:
        frontier = _extend_frontier(
            frontier, options_by_collection[cid], count, total_budget
        )
        if frontier is None:
            return None
    return list(min(frontier, key=lambda s: s[1])[2])
