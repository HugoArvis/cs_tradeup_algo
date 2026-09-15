"""Capacite d'execution : combien de fois peut-on VRAIMENT repeter un contrat.

Le moteur d'EV suppose une liquidite infinie : il valorise chaque objet au prix
de la meilleure offre, comme si on pouvait en acheter mille a ce prix. C'est
faux, et c'est faux dans le sens dangereux.

Repeter un contrat rentable exige d'acheter 10 x N objets et d'en revendre N.
Or un carnet d'ordres a une profondeur finie : les premieres unites partent au
prix affiche, les suivantes coutent plus cher. Symetriquement, revendre N
exemplaires du meme skin fait baisser le prix. Ce glissement mange l'avantage,
et il le mange d'autant plus vite que l'avantage etait mince.

Ce module estime le plafond de repetition a partir du volume de ventes 24 h.

LIMITE A CONNAITRE : le volume Steam est un flux (ventes par jour), pas une
profondeur de carnet. C'est un indicateur, pas une mesure. La vraie profondeur
demande la liste des offres, que `priceoverview` ne donne pas -- CSFloat, si.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .ev import PriceLookup, TradeUpResult

# Fraction du flux quotidien qu'on peut absorber sans deplacer le marche.
# Au-dela, on achete sa propre hausse de prix.
DEFAULT_PARTICIPATION = 0.20


@dataclass(frozen=True, slots=True)
class Constraint:
    """Un objet qui limite le rythme d'execution."""

    name: str
    side: str  # "achat" ou "revente"
    units_per_contract: float
    daily_volume: int
    contracts_per_day: float


@dataclass(frozen=True, slots=True)
class CapacityReport:
    """Rythme d'execution soutenable et goulot d'etranglement."""

    contracts_per_day: float | None  # None si aucun volume connu
    binding: Constraint | None
    constraints: tuple[Constraint, ...] = ()
    unknown_volume: tuple[str, ...] = field(default=())
    participation: float = DEFAULT_PARTICIPATION

    def days_for(self, n_contracts: int) -> float | None:
        """Jours necessaires pour executer `n_contracts` a ce rythme."""
        if not self.contracts_per_day:
            return None
        return n_contracts / self.contracts_per_day

    @property
    def input_constraints(self) -> tuple[Constraint, ...]:
        """Contraintes venant des ENTREES, les plus serrees d'abord."""
        return tuple(c for c in self.constraints if c.side == "achat")

    @property
    def sourcing_days(self) -> float | None:
        """Jours pour reunir les entrees d'UN SEUL contrat.

        Question distincte du rythme de repetition : avant de savoir combien de
        fois on peut refaire le contrat, il faut savoir si on peut le faire une
        fois. Dix exemplaires d'un skin qui s'en vend trois par jour, ce n'est
        pas un contrat rentable, c'est une semaine d'achats a prix mouvant.
        """
        entrees = self.input_constraints
        if not entrees:
            return None
        return max(1.0 / c.contracts_per_day for c in entrees)

    @property
    def sourcing_bottleneck(self) -> Constraint | None:
        """L'entree la plus difficile a reunir."""
        entrees = self.input_constraints
        return min(entrees, key=lambda c: c.contracts_per_day) if entrees else None

    def report(self, n_contracts: int | None = None) -> str:
        if self.contracts_per_day is None:
            return (
                "Capacite d'execution : INCONNUE (aucun volume de vente "
                "disponible pour ces objets)."
            )
        lines = [
            f"Capacite d'execution : ~{self.contracts_per_day:.1f} contrats/jour "
            f"(en absorbant {self.participation:.0%} du flux quotidien)"
        ]
        if self.binding:
            b = self.binding
            lines.append(
                f"  goulot : {b.name} -- {b.daily_volume} ventes/jour, "
                f"{b.units_per_contract:.1f} unites par contrat ({b.side})"
            )

        # Avant "combien de fois", la question est "une seule fois, est-ce
        # faisable ?". Un contrat dont les entrees demandent une semaine de
        # collecte n'est pas executable au prix affiche aujourd'hui.
        jours = self.sourcing_days
        if jours is not None and jours > 1.0:
            g = self.sourcing_bottleneck
            lines.append(
                f"  ENTREES DIFFICILES A REUNIR : ~{jours:.1f} jours pour un "
                f"seul contrat"
            )
            if g is not None:
                lines.append(
                    f"    le plus rare : {g.name} -- {g.units_per_contract:.0f} "
                    f"exemplaires a trouver, {g.daily_volume} ventes/jour"
                )
            lines.append(
                "    le prix affiche vaut pour la premiere unite, pas pour les "
                "dix."
            )
        if n_contracts is not None:
            days = self.days_for(n_contracts)
            if days is not None:
                lines.append(
                    f"  atteindre {n_contracts} contrats prendrait "
                    f"~{days:.0f} jours a ce rythme"
                )
        if self.unknown_volume:
            lines.append(
                f"  volume inconnu pour {len(self.unknown_volume)} objet(s) : "
                + ", ".join(self.unknown_volume[:3])
            )
        return "\n".join(lines)


def execution_capacity(
    result: TradeUpResult,
    prices: PriceLookup,
    *,
    participation: float = DEFAULT_PARTICIPATION,
) -> CapacityReport:
    """Estime combien de fois par jour ce contrat peut etre repete.

    Cote ACHAT : chaque ligne de la liste d'achat consomme `qty` unites par
    contrat. Cote REVENTE : chaque sortie n'apparait qu'avec sa probabilite, on
    revend donc `probabilite` unite par contrat en moyenne.
    """
    constraints: list[Constraint] = []
    unknown: list[str] = []

    # --- Cote achat ---
    grouped: dict[tuple, int] = defaultdict(int)
    for item in result.inputs:
        grouped[(item.skin, item.wear)] += 1

    # Le flux qui limite l'APPROVISIONNEMENT est celui du marche ou l'on achete,
    # pas celui ou l'on revend. Une source qui sait les distinguer le dit.
    volume_achat = getattr(prices, "buy_volume", None)
    if not callable(volume_achat):
        volume_achat = prices.volume

    for (skin, wear), qty in grouped.items():
        name = skin.market_hash_name(wear, result.stattrak)
        vol = volume_achat(skin, wear, result.stattrak)
        if not vol:
            unknown.append(name)
            continue
        constraints.append(
            Constraint(
                name=name,
                side="achat",
                units_per_contract=qty,
                daily_volume=vol,
                contracts_per_day=vol * participation / qty,
            )
        )

    # --- Cote revente ---
    for outcome in result.outcomes:
        if outcome.probability <= 0:
            continue
        vol = prices.volume(outcome.skin, outcome.wear, result.stattrak)
        if not vol:
            unknown.append(outcome.name)
            continue
        constraints.append(
            Constraint(
                name=outcome.name,
                side="revente",
                units_per_contract=outcome.probability,
                daily_volume=vol,
                contracts_per_day=vol * participation / outcome.probability,
            )
        )

    if not constraints:
        return CapacityReport(
            contracts_per_day=None,
            binding=None,
            unknown_volume=tuple(unknown),
            participation=participation,
        )

    binding = min(constraints, key=lambda c: c.contracts_per_day)
    return CapacityReport(
        contracts_per_day=binding.contracts_per_day,
        binding=binding,
        constraints=tuple(sorted(constraints, key=lambda c: c.contracts_per_day)),
        unknown_volume=tuple(unknown),
        participation=participation,
    )
