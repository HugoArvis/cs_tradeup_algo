"""Simulation Monte-Carlo d'une serie de contrats.

A quoi ca sert : une EV positive ne dit pas si l'avantage est DETECTABLE. Avec
un ecart-type de 3 EUR pour un profit espere de 1 EUR, un contrat unique
n'apprend rien, et meme dix contrats peuvent finir dans le rouge sans que le
modele soit faux.

Ce module repond a deux questions concretes :
  - combien de contrats faut-il pour distinguer l'avantage de zero ?
  - a quoi ressemble la distribution du P&L apres N contrats ?

Limite importante : la simulation tire dans la distribution CALCULEE par le
moteur. Elle valide donc la consequence statistique du modele, jamais le modele
lui-meme. Si les prix sont faux ou le sourcing irrealiste, elle propagera
fidelement l'erreur.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from statistics import NormalDist

from .ev import TradeUpResult


@dataclass(frozen=True, slots=True)
class SeriesOutcome:
    """Distribution du P&L cumule apres une serie de contrats."""

    n_contracts: int
    trials: int
    mean: float
    median: float
    p05: float
    p25: float
    p75: float
    p95: float
    loss_probability: float  # P(P&L cumule < 0)
    worst: float
    best: float

    def report(self) -> str:
        return "\n".join(
            [
                f"Apres {self.n_contracts} contrats ({self.trials} simulations) :",
                f"  P&L moyen        {self.mean:+9.2f}",
                f"  median           {self.median:+9.2f}",
                f"  intervalle 90 %  {self.p05:+9.2f}  ...  {self.p95:+9.2f}",
                f"  intervalle 50 %  {self.p25:+9.2f}  ...  {self.p75:+9.2f}",
                f"  pire / meilleur  {self.worst:+9.2f}  ...  {self.best:+9.2f}",
                f"  P(finir perdant) {self.loss_probability:>8.1%}",
            ]
        )


def contracts_for_significance(
    result: TradeUpResult, confidence: float = 0.95
) -> int | None:
    """Nombre de contrats pour distinguer l'avantage de zero.

    Taille d'echantillon d'un test bilateral sur la moyenne :
    ``n = (z * ecart-type / profit_espere)^2``.

    Renvoie None si le profit espere est nul ou negatif : il n'y a alors aucun
    avantage a detecter, quel que soit le nombre d'essais.
    """
    if result.ev_profit <= 0:
        return None
    if result.stdev == 0:
        return 1
    z = NormalDist().inv_cdf(1 - (1 - confidence) / 2)
    return math.ceil((z * result.stdev / result.ev_profit) ** 2)


def simulate_series(
    result: TradeUpResult,
    n_contracts: int = 30,
    trials: int = 10_000,
    seed: int | None = None,
) -> SeriesOutcome:
    """Tire `trials` series de `n_contracts` contrats identiques."""
    if n_contracts < 1:
        raise ValueError("Il faut au moins un contrat")
    if not result.outcomes:
        raise ValueError("Ce contrat n'a aucune sortie possible")

    rng = random.Random(seed)
    # Tirage pondere : on prepare les poids une fois pour toutes.
    values = [o.net_value - result.cost for o in result.outcomes]
    weights = [o.probability for o in result.outcomes]

    totals: list[float] = []
    for _ in range(trials):
        draws = rng.choices(values, weights=weights, k=n_contracts)
        totals.append(sum(draws))
    totals.sort()

    return SeriesOutcome(
        n_contracts=n_contracts,
        trials=trials,
        mean=sum(totals) / len(totals),
        median=_quantile(totals, 0.50),
        p05=_quantile(totals, 0.05),
        p25=_quantile(totals, 0.25),
        p75=_quantile(totals, 0.75),
        p95=_quantile(totals, 0.95),
        loss_probability=sum(1 for t in totals if t < 0) / len(totals),
        worst=totals[0],
        best=totals[-1],
    )


def _quantile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, max(0, int(q * len(sorted_values))))
    return sorted_values[idx]


def sanity_check(result: TradeUpResult, tolerance: float = 1e-9) -> list[str]:
    """Verifications internes d'un resultat. Liste vide = tout est coherent.

    Ce sont des invariants du modele, pas une validation face au jeu : ils
    attrapent les bugs de calcul, pas les hypotheses fausses.
    """
    problems: list[str] = []

    total = sum(o.probability for o in result.outcomes)
    if abs(total - 1.0) > tolerance:
        problems.append(f"les probabilites somment a {total!r}, pas a 1")

    if any(o.probability < 0 for o in result.outcomes):
        problems.append("probabilite negative")

    if len(result.inputs) != 10:
        problems.append(f"{len(result.inputs)} entrees au lieu de 10")

    recomputed = sum(i.unit_cost for i in result.inputs)
    if abs(recomputed - result.cost) > 1e-6:
        problems.append(f"cout incoherent : {recomputed} vs {result.cost}")

    avg = sum(i.float_value for i in result.inputs) / max(len(result.inputs), 1)
    if abs(avg - result.avg_input_float) > 1e-6:
        problems.append(f"moyenne de float incoherente : {avg} vs {result.avg_input_float}")

    for o in result.outcomes:
        if not o.skin.min_float <= o.float_value <= o.skin.max_float:
            problems.append(
                f"{o.skin.name} : float {o.float_value} hors de son range "
                f"[{o.skin.min_float}, {o.skin.max_float}]"
            )
        if o.wear not in o.skin.available_wears():
            problems.append(f"{o.skin.name} : usure {o.wear.short} inatteignable")

    # L'EV doit se situer entre la pire et la meilleure sortie.
    if result.outcomes and not (
        result.worst_case - tolerance <= result.ev_net <= result.best_case + tolerance
    ):
        problems.append("EV nette hors de l'intervalle [pire cas, meilleur cas]")

    return problems
