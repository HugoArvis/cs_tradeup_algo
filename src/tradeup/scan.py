"""Orchestration d'un scan complet : prechargement des prix puis optimisation.

Le prechargement est separe du calcul a dessein. Sur Steam, recuperer les prix
est le goulot d'etranglement absolu (~15 requetes/minute) : il faut savoir
AVANT de lancer combien de requetes seront necessaires, et travailler ensuite
sur un jeu de prix fige et coherent.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable

from .db import SkinDatabase
from .ev import PriceLookup
from .generator import Recipe, iter_recipes, optimize_recipe
from .models import Collection, Rarity
from .pricing.repository import MarketPricer
from .scoring import Candidate, Ranking, ScreenConfig, rank, score

log = logging.getLogger(__name__)


def required_market_names(
    db: SkinDatabase, collections: Iterable[Collection], rarity: Rarity
) -> list[str]:
    """Tous les `market_hash_name` necessaires pour evaluer ces collections.

    Entrees a `rarity` et sorties a la rarete superieure, dans chaque usure
    reellement atteignable par le range du skin.
    """
    target = rarity.next_up
    names: set[str] = set()
    for c in collections:
        skins = list(c.by_rarity(rarity))
        if target is not None:
            skins += list(c.by_rarity(target))
        for skin in skins:
            for wear in skin.available_wears():
                names.add(skin.market_hash_name(wear))
    return sorted(names)


def prefetch(
    pricer: MarketPricer,
    names: list[str],
    *,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, int]:
    """Charge toutes les cotations necessaires. Renvoie un bilan."""
    found = 0
    started = time.monotonic()
    for i, name in enumerate(names, 1):
        if pricer.warm([name]):
            found += 1
        if progress:
            progress(i, len(names))
    return {
        "demandes": len(names),
        "trouves": found,
        "manquants": len(names) - found,
        "secondes": round(time.monotonic() - started, 1),
    }


def scan(
    db: SkinDatabase,
    prices: PriceLookup,
    rarity: Rarity,
    *,
    screen: ScreenConfig | None = None,
    ranking: Ranking = Ranking.RISK_ADJUSTED,
    max_collections: int = 1,
    collection_filter: Iterable[str] | None = None,
    float_percentile: float = 0.15,
    float_safety: float = 0.02,
    max_unit_cost: float | None = None,
    limit: int | None = 20,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[list[Candidate], dict[str, int]]:
    """Evalue toutes les recettes et renvoie les meilleures.

    Returns:
        (candidats classes, statistiques du scan)
    """
    screen = screen or ScreenConfig()
    recipes: list[Recipe] = list(
        iter_recipes(
            db,
            rarity,
            max_collections=max_collections,
            collection_filter=collection_filter,
        )
    )
    log.info("%d recettes a evaluer a la rarete %s", len(recipes), rarity.label)

    kept: list[Candidate] = []
    stats = {"recettes": len(recipes), "evaluees": 0, "sans_prix": 0, "rejetees": 0}

    for i, recipe in enumerate(recipes, 1):
        if progress:
            progress(i, len(recipes))
        result = optimize_recipe(
            recipe,
            db,
            prices,
            float_percentile=float_percentile,
            float_safety=float_safety,
            max_unit_cost=max_unit_cost,
        )
        if result is None:
            stats["sans_prix"] += 1
            continue
        stats["evaluees"] += 1
        if not screen.passes(result):
            stats["rejetees"] += 1
            continue
        kept.append(
            Candidate(
                result=result,
                label=recipe.label(db),
                score=score(result, ranking),
            )
        )

    stats["retenues"] = len(kept)
    return rank(kept, ranking, limit), stats
