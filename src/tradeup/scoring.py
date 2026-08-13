"""Filtrage et classement des contrats candidats.

Le classement par defaut est un ratio de type Sharpe : profit espere rapporte a
l'ecart-type de la sortie. Il traduit directement l'objectif "EV positive, mais
en privilegiant la faible variance" — un contrat a +2 EUR d'EV pour 3 EUR
d'ecart-type passe devant un contrat a +5 EUR d'EV pour 40 EUR d'ecart-type.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .ev import TradeUpResult


class Ranking(Enum):
    """Critere de tri des resultats."""

    RISK_ADJUSTED = "risk_adjusted"  # profit / ecart-type (defaut)
    EV = "ev"  # profit espere brut
    ROI = "roi"  # profit / capital engage
    SAFETY = "safety"  # probabilite d'etre rentable


@dataclass(slots=True)
class ScreenConfig:
    """Criteres d'admissibilite d'un contrat.

    Les valeurs par defaut sont deliberement severes : sur un scan complet,
    l'immense majorite des contrats sont perdants, et les quelques "gagnants"
    apparents proviennent souvent de prix aberrants ou illiquides.
    """

    min_ev_profit: float = 0.0
    min_roi: float = 0.03  # 3 % : en dessous, le bruit de prix domine
    min_profit_probability: float = 0.0
    max_coefficient_of_variation: float | None = None
    max_cost: float | None = None
    min_outcome_volume: int | None = None
    max_unpriced_probability: float = 0.02  # au-dela, l'EV n'est pas fiable
    require_positive_worst_case_ratio: float | None = None  # ex. 0.5
    min_cliff_distance: float = 0.0  # marge exigee sous une frontiere d'usure

    # Motifs de rejet accumules lors du dernier `passes()`.
    last_reasons: list[str] = field(default_factory=list)

    def passes(self, r: TradeUpResult) -> bool:
        reasons: list[str] = []

        if r.ev_profit < self.min_ev_profit:
            reasons.append(f"EV {r.ev_profit:+.2f} < {self.min_ev_profit:+.2f}")
        if r.roi < self.min_roi:
            reasons.append(f"ROI {r.roi:.1%} < {self.min_roi:.1%}")
        if r.profit_probability < self.min_profit_probability:
            reasons.append(
                f"P(profit) {r.profit_probability:.1%} < {self.min_profit_probability:.1%}"
            )
        if (
            self.max_coefficient_of_variation is not None
            and r.coefficient_of_variation > self.max_coefficient_of_variation
        ):
            reasons.append(
                f"CV {r.coefficient_of_variation:.2f} > {self.max_coefficient_of_variation:.2f}"
            )
        if self.max_cost is not None and r.cost > self.max_cost:
            reasons.append(f"cout {r.cost:.2f} > {self.max_cost:.2f}")
        if r.unpriced_probability > self.max_unpriced_probability:
            reasons.append(
                f"{r.unpriced_probability:.1%} de la proba sans prix connu"
            )
        if r.cliff_distance < self.min_cliff_distance:
            reasons.append(
                f"marge d'usure {r.cliff_distance:.4f} < {self.min_cliff_distance:.4f}"
            )
        if self.require_positive_worst_case_ratio is not None:
            floor = self.require_positive_worst_case_ratio * r.cost
            if r.worst_case < floor:
                reasons.append(
                    f"pire cas {r.worst_case:.2f} < {floor:.2f} "
                    f"({self.require_positive_worst_case_ratio:.0%} du cout)"
                )

        self.last_reasons = reasons
        return not reasons


def score(result: TradeUpResult, ranking: Ranking = Ranking.RISK_ADJUSTED) -> float:
    """Note un contrat selon le critere demande (plus haut = meilleur)."""
    if ranking is Ranking.EV:
        return result.ev_profit
    if ranking is Ranking.ROI:
        return result.roi
    if ranking is Ranking.SAFETY:
        # A probabilite egale, on departage par le profit espere.
        return result.profit_probability * 1000 + result.ev_profit
    # RISK_ADJUSTED : un contrat sans variance (impossible en pratique) ne doit
    # pas exploser le classement, d'ou le plancher sur l'ecart-type.
    return result.ev_profit / max(result.stdev, 0.01)


def _cliff(distance: float) -> str:
    """Formate la marge avant la prochaine frontiere d'usure."""
    if distance == float("inf"):
        return "aucune (les sorties ne peuvent plus se degrader)"
    verdict = "CONFORTABLE" if distance >= 0.01 else "SERREE"
    return f"{distance:.4f} de moyenne -- {verdict}"


@dataclass(slots=True)
class Candidate:
    """Un contrat retenu, avec son contexte de provenance."""

    result: TradeUpResult
    label: str
    score: float

    @property
    def summary(self) -> str:
        r = self.result
        return (
            f"{self.label}\n"
            f"  cout {r.cost:8.2f} | EV nette {r.ev_net:8.2f} | "
            f"profit {r.ev_profit:+7.2f} ({r.roi:+.1%})\n"
            f"  ecart-type {r.stdev:7.2f} | score risque {self.score:6.2f} | "
            f"P(profit) {r.profit_probability:.1%}\n"
            f"  float moyen d'entree {r.avg_input_float:.4f} | "
            f"{r.distinct_outcomes} sorties | "
            f"pire cas {r.worst_case:.2f} | CVaR25 {r.value_at_risk(0.25):+.2f}\n"
            f"  marge avant falaise d'usure : {_cliff(r.cliff_distance)}"
        )


def rank(
    candidates: list[Candidate], ranking: Ranking = Ranking.RISK_ADJUSTED, limit: int | None = None
) -> list[Candidate]:
    ordered = sorted(candidates, key=lambda c: score(c.result, ranking), reverse=True)
    return ordered[:limit] if limit else ordered


def shopping_list(result: TradeUpResult) -> str:
    """Ce qu'il faut acheter, avec la contrainte de float par ligne.

    La colonne `float max` est la contrainte REELLE : au-dela, la moyenne
    derape et la sortie change de palier d'usure. Sur Steam on n'achete pas un
    float precis -- ces lignes ne sont verifiables que sur CSFloat.
    """
    lines = [
        f"{'a acheter':<50} {'qte':>4} {'prix u.':>9} {'float max':>10} {'total':>9}",
        "-" * 86,
    ]
    for name, qty, unit_cost, float_target in result.shopping_list():
        lines.append(
            f"{name:<50} {qty:>4} {unit_cost:>9.2f} {float_target:>10.4f} "
            f"{qty * unit_cost:>9.2f}"
        )
    lines.append("-" * 86)
    lines.append(
        f"{'TOTAL':<50} {len(result.inputs):>4} {'':>9} "
        f"{result.avg_input_float:>10.4f} {result.cost:>9.2f}"
    )
    lines.append(
        f"  moyenne de float a ne pas depasser : {result.avg_input_float:.4f}"
    )
    return "\n".join(lines)


def explain(result: TradeUpResult, max_rows: int = 12) -> str:
    """Detail de la distribution des sorties, pour verification manuelle."""
    lines = [
        f"{'sortie':<52} {'proba':>7} {'float':>8} {'net':>9} {'contrib':>9}",
        "-" * 90,
    ]
    for o in result.outcomes[:max_rows]:
        flag = "" if o.priced else "  [PRIX INCONNU]"
        lines.append(
            f"{o.name:<52} {o.probability:>6.2%} {o.float_value:>8.4f} "
            f"{o.net_value:>9.2f} {o.probability * o.net_value:>9.2f}{flag}"
        )
    if len(result.outcomes) > max_rows:
        rest = result.outcomes[max_rows:]
        lines.append(
            f"{'... ' + str(len(rest)) + ' autres sorties':<52} "
            f"{sum(o.probability for o in rest):>6.2%} {'':>8} {'':>9} "
            f"{sum(o.probability * o.net_value for o in rest):>9.2f}"
        )
    lines.append("-" * 90)
    lines.append(
        f"{'TOTAL':<52} {sum(o.probability for o in result.outcomes):>6.2%} "
        f"{'':>8} {'':>9} {result.ev_net:>9.2f}"
    )
    return "\n".join(lines)
