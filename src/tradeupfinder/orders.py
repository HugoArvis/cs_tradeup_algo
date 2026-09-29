"""Strategie par ORDRES D'ACHAT : on ne subit pas le prix, on le fixe.

Acheter au prix demande, c'est prendre la meilleure offre du carnet. Placer un
ordre d'achat, c'est annoncer son prix et attendre qu'un vendeur vienne le
prendre. On paie moins, on attend plus.

Sur les skins bon marche l'ecart est decisif. Un panier de dix entrees a 0.07
coute 0.70 ; les memes obtenues a 0.05 coutent 0.50. Les 0.20 economises sont
souvent superieurs a la marge du contrat lui-meme -- autrement dit, la
rentabilite ne vient pas du trade-up mais du prix d'achat.

Ce module renverse donc le calcul habituel. Au lieu de

    profit = EV(sortie) - cout(marche)

il resout

    budget = EV(sortie) / (1 + rendement vise)

et en deduit le prix maximal de chaque entree. La valeur de la sortie ne depend
pas de ce qu'on a paye les entrees : elle se calcule une fois, et le reste suit.

DEUX CONSEQUENCES A NE PAS OUBLIER

1. Un ordre d'achat ne choisit pas le float. On recoit un objet quelconque du
   palier, donc `float_model="random"` est le seul modele honnete ici -- viser
   un bas float suppose de filtrer les annonces, ce qu'un ordre ne fait pas.

2. Un ordre tres au-dessous du marche ne se remplit jamais. Calculer qu'il
   "faudrait payer 93 % sous le prix affiche" n'est pas un conseil, c'est un
   refus deguise. `max_discount` ecarte ces cas au lieu de les presenter comme
   des occasions.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .db import SkinDatabase
from .ev import PriceLookup, TradeUpResult
from .generator import Recipe, optimize_recipe
from .models import Collection, Rarity

#: Prix minimal d'un objet sur le marche Steam. Un ordre en dessous est
#: impossible a placer, quel que soit le calcul.
STEAM_MIN_PRICE = 0.03

#: Rabais au-dela duquel un ordre ne se remplit plus en pratique. Un ordre 20 %
#: sous le prix demande finit par etre servi sur un objet liquide ; a 60 % on
#: attend indefiniment.
DEFAULT_MAX_DISCOUNT = 0.35

#: Rendement vise par defaut. A l'equilibre exact, la moindre variation de prix
#: rend le contrat perdant : il faut une marge.
DEFAULT_TARGET_ROI = 0.20


@dataclass(frozen=True, slots=True)
class OrderLine:
    """Un ordre d'achat a placer : un objet, une quantite, un prix maximal."""

    name: str
    quantity: int
    market_price: float  # prix demande actuel, ce qu'on paierait tout de suite
    order_price: float  # prix a ne pas depasser pour que le contrat tienne

    @property
    def discount(self) -> float:
        """Rabais exige par rapport au prix demande."""
        if self.market_price <= 0:
            return 0.0
        return 1.0 - self.order_price / self.market_price

    @property
    def below_floor(self) -> bool:
        """L'ordre tomberait-il sous le prix plancher de Steam ?"""
        return self.order_price < STEAM_MIN_PRICE

    @property
    def total(self) -> float:
        return self.quantity * self.order_price


@dataclass(frozen=True, slots=True)
class OrderPlan:
    """Les ordres a placer pour qu'un contrat atteigne le rendement vise."""

    collection: Collection
    rarity: Rarity
    result: TradeUpResult
    lines: tuple[OrderLine, ...]
    target_roi: float

    @property
    def ev_net(self) -> float:
        """Valeur nette esperee de la sortie. Independante du prix d'achat."""
        return self.result.ev_net

    @property
    def market_cost(self) -> float:
        """Ce que couterait le panier en achetant tout de suite."""
        return sum(l.quantity * l.market_price for l in self.lines)

    @property
    def budget(self) -> float:
        """Ce qu'on peut payer au total pour tenir le rendement vise."""
        return sum(l.total for l in self.lines)

    @property
    def discount(self) -> float:
        """Rabais moyen exige sur l'ensemble du panier."""
        if self.market_cost <= 0:
            return 0.0
        return 1.0 - self.budget / self.market_cost

    @property
    def already_profitable(self) -> bool:
        """Le contrat tient-il deja au prix demande, sans attendre ?"""
        return self.discount <= 0

    @property
    def win_probability(self) -> float:
        """Probabilite de gagner AUX PRIX D'ORDRE, pas au prix du marche.

        `TradeUpResult.profit_probability` compare les sorties au cout paye au
        marche. Or tout l'interet de la strategie est de payer moins : acheter
        au rabais fait passer des sorties du cote gagnant. Afficher la
        probabilite du marche a cote d'un budget reduit donnait des lignes
        contradictoires -- "rendement vise +20 %" et "0 % de chances de
        gagner" sur le meme contrat.
        """
        budget = self.budget
        return sum(
            o.probability for o in self.result.outcomes if o.net_value >= budget
        )

    @property
    def always_profitable(self) -> bool:
        """Aux prix d'ordre, toutes les sorties rapportent-elles ?"""
        return bool(self.result.outcomes) and all(
            o.net_value >= self.budget for o in self.result.outcomes
        )

    @property
    def blocked_by_floor(self) -> tuple[OrderLine, ...]:
        """Lignes dont l'ordre serait sous le plancher Steam : impossibles."""
        return tuple(l for l in self.lines if l.below_floor)

    def feasible(self, max_discount: float = DEFAULT_MAX_DISCOUNT) -> bool:
        """Cet ensemble d'ordres a-t-il une chance d'etre rempli ?"""
        return not self.blocked_by_floor and self.discount <= max_discount

    def report(self) -> str:
        lignes = [
            f"{self.collection.name} [{self.rarity.label} -> "
            f"{self.rarity.next_up.label}]",
            f"  revente nette esperee {self.ev_net:.2f}  |  "
            f"rendement vise {self.target_roi:+.0%}",
        ]
        if self.already_profitable:
            lignes.append(
                f"  DEJA RENTABLE au prix demande ({self.market_cost:.2f}) : "
                f"inutile d'attendre un ordre."
            )
        else:
            lignes.append(
                f"  panier au prix demande {self.market_cost:.2f}  ->  "
                f"budget maximal {self.budget:.2f}  "
                f"(rabais de {self.discount:.0%} a obtenir)"
            )
        lignes.append("")
        lignes.append(
            f"  {'ordre a placer':<44} {'qte':>4} {'marche':>8} "
            f"{'ordre max':>10} {'rabais':>8}"
        )
        lignes.append("  " + "-" * 78)
        for l in sorted(self.lines, key=lambda x: -x.quantity):
            alerte = "  < plancher Steam" if l.below_floor else ""
            lignes.append(
                f"  {l.name[:44]:<44} {l.quantity:>4} {l.market_price:>8.2f} "
                f"{l.order_price:>10.2f} {l.discount:>7.0%}{alerte}"
            )
        if self.blocked_by_floor:
            lignes.append("")
            lignes.append(
                f"  IMPOSSIBLE : {len(self.blocked_by_floor)} ordre(s) "
                f"tomberaient sous le plancher de {STEAM_MIN_PRICE:.2f} de "
                f"Steam. Ce contrat ne peut pas etre rendu rentable par le prix "
                f"d'achat."
            )
        lignes.append("")
        lignes.append(
            f"  Le float sera TIRE au hasard dans le palier : un ordre d'achat "
            f"ne le choisit pas."
        )
        lignes.append(
            f"  Une fois les ordres servis : {self.win_probability:.0%} de "
            f"chances d'y gagner, {self.result.distinct_outcomes} sorties "
            f"possibles."
            + ("  TOUTES RENTABLES." if self.always_profitable else "")
        )
        if self.win_probability > self.result.profit_probability:
            lignes.append(
                f"  (au prix demande ce serait "
                f"{self.result.profit_probability:.0%} : c'est le rabais qui "
                f"fait basculer des sorties du bon cote)"
            )
        return "\n".join(lignes)


def plan_orders(
    db: SkinDatabase,
    collection: Collection,
    rarity: Rarity,
    prices: PriceLookup,
    *,
    target_roi: float = DEFAULT_TARGET_ROI,
    stattrak: bool = False,
) -> OrderPlan | None:
    """Prix d'ordre maximal de chaque entree, pour atteindre `target_roi`.

    Le budget est reparti proportionnellement aux prix du marche : on demande
    le meme rabais partout. Repartir autrement supposerait de savoir sur quels
    objets les vendeurs cedent le plus, ce qu'aucune donnee ici ne dit.
    """
    resultat = optimize_recipe(
        Recipe(counts=((collection.id, 10),), rarity=rarity),
        db,
        prices,
        # Un ordre d'achat ne filtre pas les floats : le tirage est subi.
        float_model="random",
        stattrak=stattrak,
    )
    if resultat is None or resultat.cost <= 0:
        return None

    budget = resultat.ev_net / (1.0 + target_roi)
    facteur = budget / resultat.cost

    lignes = [
        OrderLine(
            name=nom,
            quantity=qte,
            market_price=prix_unitaire,
            # Arrondi vers le BAS au centime : un ordre arrondi vers le haut
            # depasserait le budget et mangerait la marge visee.
            order_price=int(prix_unitaire * facteur * 100) / 100,
        )
        for nom, qte, prix_unitaire, _ in resultat.shopping_list()
    ]
    return OrderPlan(
        collection=collection,
        rarity=rarity,
        result=resultat,
        lines=tuple(lignes),
        target_roi=target_roi,
    )


def scan_orders(
    db: SkinDatabase,
    rarity: Rarity,
    prices: PriceLookup,
    *,
    collections: Iterable[Collection] | None = None,
    target_roi: float = DEFAULT_TARGET_ROI,
    max_discount: float = DEFAULT_MAX_DISCOUNT,
    stattrak: bool = False,
    keep_unfeasible: bool = False,
) -> list[OrderPlan]:
    """Collections ou une strategie d'ordres d'achat a une chance de tenir.

    Classees par rabais croissant : le contrat le plus facile a remplir
    d'abord. Un rabais faible se traduit par une attente courte, et c'est ce
    qui decide du rythme -- pas le profit affiche.
    """
    pool = list(collections) if collections is not None else \
        db.tradeable_collections(rarity, stattrak)

    plans: list[OrderPlan] = []
    for col in pool:
        plan = plan_orders(
            db, col, rarity, prices, target_roi=target_roi, stattrak=stattrak
        )
        if plan is None:
            continue
        if keep_unfeasible or plan.feasible(max_discount):
            plans.append(plan)

    plans.sort(key=lambda p: p.discount)
    return plans


def fill_estimate(volume_per_day: int | None, discount: float) -> str:
    """Combien de temps un ordre a ce rabais mettra-t-il a etre servi ?

    Estimation grossiere et assumee comme telle : sans le carnet d'ordres, on
    ne sait pas combien d'acheteurs sont deja places devant. Le volume de
    ventes donne l'ordre de grandeur du flux, le rabais dit a quel point on est
    loin du prix auquel ce flux s'ecoule.
    """
    if not volume_per_day:
        return "delai inconnu (aucun volume publie)"
    if discount <= 0:
        return "immediat (le prix demande suffit)"
    if discount <= 0.10:
        return "quelques heures a un jour"
    if discount <= 0.25:
        return "un a plusieurs jours"
    if discount <= 0.40:
        return "une semaine ou plus"
    return "probablement jamais servi"
