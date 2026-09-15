"""Moteur de calcul : distribution des sorties, EV nette, variance.

Distribution des sorties
------------------------
Chaque objet d'entree depose un "ticket" pour SA collection. Le tirage se fait
uniformement sur l'ensemble des paires (ticket, sortie possible de sa
collection). Donc, pour une collection C fournissant ``n_C`` entrees et
``k_C`` skins de sortie a la rarete cible :

    P(un skin de sortie donne de C) = n_C / somme_sur_C'( n_C' * k_C' )

Consequence contre-intuitive mais correcte : une collection avec PEU de sorties
possibles est globalement MOINS probable qu'une collection avec beaucoup de
sorties, a nombre d'entrees egal. Et une collection sans sortie a la rarete
cible ne contribue rien : ses entrees sont du cout pur.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from .models import Skin, Wear
from .wear import TRADEUP_INPUT_COUNT, average_normalized, output_float, wear_of


class PriceLookup(Protocol):
    """Source de valorisation, injectee dans le moteur."""

    def sell_net(self, skin: Skin, wear: Wear, stattrak: bool) -> float | None:
        """Montant NET encaisse en revendant ce skin, frais deduits."""

    def buy_cost(self, skin: Skin, wear: Wear, stattrak: bool) -> float | None:
        """Cout total pour acquerir ce skin, frais/premium inclus."""

    def volume(self, skin: Skin, wear: Wear, stattrak: bool) -> int | None:
        """Volume de ventes recent (proxy de liquidite), si connu."""


@dataclass(frozen=True, slots=True)
class InputItem:
    """Un objet place dans le contrat."""

    skin: Skin
    float_value: float
    unit_cost: float

    @property
    def wear(self) -> Wear:
        return wear_of(self.float_value)


@dataclass(frozen=True, slots=True)
class Outcome:
    """Une issue possible du contrat."""

    skin: Skin
    float_value: float
    wear: Wear
    probability: float
    net_value: float
    priced: bool  # False si le prix manquait et a ete traite comme 0

    @property
    def name(self) -> str:
        return self.skin.market_hash_name(self.wear)


@dataclass(frozen=True, slots=True)
class TradeUpResult:
    """Evaluation complete d'un contrat."""

    outcomes: tuple[Outcome, ...]
    inputs: tuple[InputItem, ...]  # ce qu'il faut acheter, concretement
    cost: float
    avg_input_float: float  # moyenne des floats AFFICHES, pour l'utilisateur
    avg_normalized: float  # moyenne normalisee : c'est elle qui fait la sortie
    stattrak: bool
    unpriced_probability: float  # masse de proba sans prix connu
    # Marge avant la prochaine frontiere d'usure qui degraderait une sortie.
    # inf = aucune frontiere au-dessus (la sortie ne peut plus empirer).
    cliff_distance: float = float("inf")

    def shopping_list(self) -> list[tuple[str, int, float, float]]:
        """Entrees regroupees : (nom, quantite, prix unitaire, float vise).

        C'est la sortie actionnable : sans elle, un contrat "a +103 % d'EV"
        n'est qu'un nombre. Le float vise est une HYPOTHESE de sourcing (voir
        `generator.build_options`) ; il doit etre verifie offre par offre.
        """
        grouped: dict[tuple[str, float, float], int] = defaultdict(int)
        for item in self.inputs:
            key = (
                item.skin.market_hash_name(item.wear, self.stattrak),
                item.unit_cost,
                item.float_value,
            )
            grouped[key] += 1
        rows = [(name, n, cost, flt) for (name, cost, flt), n in grouped.items()]
        rows.sort(key=lambda r: (-r[1], r[0]))
        return rows

    @property
    def max_input_float(self) -> float:
        """Float le plus eleve qu'une entree doit respecter."""
        return max((i.float_value for i in self.inputs), default=0.0)

    # --- Esperance ---
    @property
    def ev_net(self) -> float:
        """Valeur nette esperee de la sortie (apres frais de revente)."""
        return sum(o.probability * o.net_value for o in self.outcomes)

    @property
    def ev_profit(self) -> float:
        """Profit espere : EV nette moins le cout des 10 entrees."""
        return self.ev_net - self.cost

    @property
    def roi(self) -> float:
        return self.ev_profit / self.cost if self.cost > 0 else 0.0

    # --- Risque ---
    @property
    def variance(self) -> float:
        mu = self.ev_net
        return sum(o.probability * (o.net_value - mu) ** 2 for o in self.outcomes)

    @property
    def stdev(self) -> float:
        return math.sqrt(self.variance)

    @property
    def coefficient_of_variation(self) -> float:
        """Ecart-type rapporte a l'EV : variance normalisee, comparable."""
        return self.stdev / self.ev_net if self.ev_net > 0 else math.inf

    @property
    def profit_probability(self) -> float:
        """Probabilite que le contrat soit rentable ex post."""
        return sum(o.probability for o in self.outcomes if o.net_value >= self.cost)

    @property
    def worst_case(self) -> float:
        return min((o.net_value for o in self.outcomes), default=0.0)

    @property
    def best_case(self) -> float:
        return max((o.net_value for o in self.outcomes), default=0.0)

    def value_at_risk(self, quantile: float = 0.25) -> float:
        """Perte au pire `quantile` des cas (CVaR sur la queue basse).

        Renvoie le profit moyen conditionnel aux `quantile` % de tirages les
        moins favorables. Negatif = perte moyenne dans ce scenario.
        """
        if not self.outcomes:
            return 0.0
        ordered = sorted(self.outcomes, key=lambda o: o.net_value)
        remaining = quantile
        total = 0.0
        for o in ordered:
            take = min(o.probability, remaining)
            if take <= 0:
                break
            total += take * o.net_value
            remaining -= take
        used = quantile - remaining
        return (total / used - self.cost) if used > 0 else 0.0

    @property
    def distinct_outcomes(self) -> int:
        return len(self.outcomes)


def outcome_probabilities(
    inputs_per_collection: dict[str, int],
    outcomes_per_collection: dict[str, Sequence[Skin]],
) -> dict[str, float]:
    """Probabilite par skin de sortie (cle = `Skin.key`).

    Args:
        inputs_per_collection: nombre d'entrees par identifiant de collection.
        outcomes_per_collection: skins de sortie possibles par collection.
    """
    denominator = sum(
        count * len(outcomes_per_collection.get(cid, ()))
        for cid, count in inputs_per_collection.items()
    )
    if denominator == 0:
        return {}

    probs: dict[str, float] = {}
    for cid, count in inputs_per_collection.items():
        for skin in outcomes_per_collection.get(cid, ()):
            # Un meme skin ne peut appartenir qu'a une collection : pas de cumul.
            probs[skin.key] = count / denominator
    return probs


def evaluate(
    inputs: Sequence[InputItem],
    outcomes_per_collection: dict[str, Sequence[Skin]],
    prices: PriceLookup,
    *,
    stattrak: bool = False,
    treat_missing_as_zero: bool = True,
    cliff_distance: float = float("inf"),
) -> TradeUpResult:
    """Evalue un contrat a partir de 10 entrees concretes."""
    if len(inputs) != TRADEUP_INPUT_COUNT:
        raise ValueError(
            f"Un contrat exige {TRADEUP_INPUT_COUNT} entrees, {len(inputs)} fournies"
        )

    rarities = {item.skin.rarity for item in inputs}
    if len(rarities) > 1:
        raise ValueError(f"Entrees de raretes melangees : {sorted(r.label for r in rarities)}")

    counts: dict[str, int] = defaultdict(int)
    for item in inputs:
        counts[item.skin.collection_id] += 1

    # Le float de sortie derive de la moyenne NORMALISEE, pas des floats
    # affiches : chaque entree compte pour sa position dans son propre range.
    avg = average_normalized([(item.skin, item.float_value) for item in inputs])
    avg_affiche = sum(i.float_value for i in inputs) / len(inputs)
    cost = sum(item.unit_cost for item in inputs)

    probs = outcome_probabilities(dict(counts), outcomes_per_collection)
    by_key = {s.key: s for skins in outcomes_per_collection.values() for s in skins}

    built: list[Outcome] = []
    unpriced = 0.0
    for key, p in probs.items():
        if p <= 0:
            continue
        skin = by_key[key]
        f_out = output_float(avg, skin)
        w = wear_of(f_out)
        # Certaines sources (CSFloat) savent coter au float pres. A l'interieur
        # d'un meme palier l'ecart depasse 40 % : une Factory New a 0.005 ne
        # vaut pas une Factory New a 0.069. Quand l'information existe, on
        # l'utilise ; sinon on retombe sur le prix du palier.
        cotation = getattr(prices, "sell_net_at_float", None)
        if callable(cotation):
            net = cotation(skin, w, stattrak, f_out)
        else:
            net = prices.sell_net(skin, w, stattrak)
        priced = net is not None
        if not priced:
            unpriced += p
            if not treat_missing_as_zero:
                raise LookupError(f"Prix manquant pour {skin.market_hash_name(w, stattrak)}")
            net = 0.0
        built.append(
            Outcome(
                skin=skin,
                float_value=f_out,
                wear=w,
                probability=p,
                net_value=net,
                priced=priced,
            )
        )

    built.sort(key=lambda o: o.net_value, reverse=True)
    return TradeUpResult(
        outcomes=tuple(built),
        inputs=tuple(inputs),
        cost=cost,
        avg_input_float=avg_affiche,
        avg_normalized=avg,
        stattrak=stattrak,
        unpriced_probability=unpriced,
        cliff_distance=cliff_distance,
    )
