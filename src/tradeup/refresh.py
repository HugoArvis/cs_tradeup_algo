"""Re-cotation d'un lot d'objets, et mesure de ce qui a bouge depuis.

Un scan travaille sur un jeu de prix fige, telecharge une fois puis reutilise
pendant des heures. C'est indispensable pour que le classement soit coherent,
mais cela veut dire qu'au moment d'EXECUTER, les prix affiches ont l'age du
cache -- pas celui du marche.

Ce module repond a la seule question qui compte a cet instant : entre le releve
sur lequel la decision a ete prise et maintenant, qu'est-ce qui a change ?

La derive est signee du point de vue du PORTEFEUILLE, pas du prix :
une entree qui rencherit et une sortie qui se deprecie sont toutes deux
defavorables, et c'est ainsi qu'elles sont comptees.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .pricing.base import PriceSource, Quote
from .pricing.cache import QuoteCache

if TYPE_CHECKING:
    from .models import Collection, Rarity

log = logging.getLogger(__name__)

#: Au-dela de cette derive defavorable, un contrat merite d'etre recalcule.
#: Mesure sur les ecarts constates entre un scan et son execution : en dessous
#: de 5 % le bruit domine, au-dela la marge d'un trade-up typique est mangee.
DEFAULT_DRIFT_THRESHOLD = 0.05


@dataclass(frozen=True, slots=True)
class Drift:
    """Ce qu'un objet valait au dernier releve, et ce qu'il vaut maintenant."""

    market_hash_name: str
    source: str
    role: str  # "entree" (on achete) ou "sortie" (on revend)
    ancien: float | None
    nouveau: float | None
    age_ancien: float | None  # secondes ecoulees depuis l'ancien releve

    @property
    def inconnu(self) -> bool:
        """Aucune reference anterieure : rien a comparer."""
        return self.ancien is None

    @property
    def disparu(self) -> bool:
        """L'objet n'a plus de cotation du tout.

        Sur une entree, c'est bloquant : on ne peut plus l'acheter au prix
        prevu. Ce n'est donc pas une derive de 0 %, c'est un panier a refaire.
        """
        return self.nouveau is None and self.ancien is not None

    @property
    def variation(self) -> float | None:
        """Variation relative du PRIX, signee comme le marche l'affiche."""
        if self.ancien is None or self.nouveau is None or not self.ancien:
            return None
        return (self.nouveau - self.ancien) / self.ancien

    @property
    def impact(self) -> float | None:
        """Variation vue du portefeuille : negative = defavorable.

        Une entree qui monte coute plus cher, une sortie qui baisse rapporte
        moins. Les deux doivent alerter, alors qu'elles ont des signes opposes
        en variation de prix brute.
        """
        v = self.variation
        if v is None:
            return None
        return -v if self.role == "entree" else v

    def defavorable(self, seuil: float = DEFAULT_DRIFT_THRESHOLD) -> bool:
        if self.disparu:
            return True
        i = self.impact
        return i is not None and i <= -seuil

    def ligne(self) -> str:
        """Une ligne lisible, alignee sur 78 colonnes."""
        nom = self.market_hash_name[:46]
        if self.disparu:
            return f"  {nom:<46} {self.ancien:>8.2f} -> introuvable"
        if self.nouveau is None:
            return f"  {nom:<46} {'?':>8} -> {'?':>8}  (jamais cote)"
        if self.ancien is None:
            return f"  {nom:<46} {'-':>8} -> {self.nouveau:>8.2f}  (nouveau)"
        v = self.variation or 0.0
        marque = "  <<" if self.defavorable() else ""
        return (
            f"  {nom:<46} {self.ancien:>8.2f} -> {self.nouveau:>8.2f} "
            f"{v:>+7.1%}{marque}"
        )


@dataclass(frozen=True, slots=True)
class RefreshReport:
    """Bilan d'une re-cotation."""

    drifts: list[Drift]
    seuil: float = DEFAULT_DRIFT_THRESHOLD
    secondes: float = 0.0

    @property
    def defavorables(self) -> list[Drift]:
        return [d for d in self.drifts if d.defavorable(self.seuil)]

    @property
    def disparus(self) -> list[Drift]:
        return [d for d in self.drifts if d.disparu]

    @property
    def impact_moyen(self) -> float | None:
        """Derive moyenne du portefeuille, en fraction.

        Moyenne simple : sans les quantites du panier, ponderer serait un faux
        raffinement. Elle sert a repondre "ca a bouge ou pas", pas a recalculer
        une EV -- pour ca, il faut relancer le scan.
        """
        valeurs = [d.impact for d in self.drifts if d.impact is not None]
        return sum(valeurs) / len(valeurs) if valeurs else None

    @property
    def a_recalculer(self) -> bool:
        """Faut-il refaire le calcul avant d'executer ?"""
        return bool(self.defavorables)

    def report(self) -> str:
        lignes = [
            f"{len(self.drifts)} objets recotes en {self.secondes:.0f} s "
            f"(seuil d'alerte : {self.seuil:.0%})",
            "",
        ]
        lignes += [d.ligne() for d in self.drifts]

        moyen = self.impact_moyen
        if moyen is not None:
            lignes += ["", f"Derive moyenne du portefeuille : {moyen:+.1%}"]

        if self.disparus:
            lignes += [
                "",
                f"{len(self.disparus)} objets n'ont PLUS de cotation. Un panier "
                "qui en contient n'est plus achetable tel quel.",
            ]
        if self.defavorables:
            lignes += [
                "",
                f"{len(self.defavorables)} objets ont derive defavorablement de "
                f"plus de {self.seuil:.0%}.",
                "Relance le calcul avant d'executer : la marge d'un trade-up "
                "tient rarement un tel ecart.",
            ]
        else:
            lignes += [
                "",
                "Aucune derive defavorable au-dela du seuil. Les chiffres du "
                "dernier calcul tiennent encore.",
            ]
        return "\n".join(lignes)


def refresh_quotes(
    source: PriceSource,
    names: Iterable[str],
    *,
    cache: QuoteCache | None = None,
    roles: dict[str, str] | None = None,
    seuil: float = DEFAULT_DRIFT_THRESHOLD,
    progress: Callable[[int, int], None] | None = None,
) -> RefreshReport:
    """Recote `names` en ignorant le cache, et compare a la derniere valeur connue.

    Args:
        source: marche a interroger.
        names: `market_hash_name` a verifier.
        cache: cache a lire pour l'ancienne valeur. Sans lui, il n'y a rien a
            comparer : le rapport ne fait que relever les prix du moment.
        roles: `{nom: "entree" | "sortie"}`. Par defaut tout est une "sortie",
            c'est-a-dire qu'une baisse alerte -- le cas du `price --fresh`
            classique, ou l'on verifie ce qu'on espere encaisser.
        seuil: derive defavorable a partir de laquelle on alerte.
    """
    noms = list(dict.fromkeys(names))
    roles = roles or {}
    debut = time.monotonic()
    drifts: list[Drift] = []

    for i, name in enumerate(noms, 1):
        role = roles.get(name, "sortie")
        ancien_q = _dernier_releve(cache, name, source)
        # `refresh` et non `fetch` : une cotation de plusieurs heures est
        # exactement ce que cette commande existe pour ne pas relire.
        nouveau_q = source.refresh(name)

        drifts.append(
            Drift(
                market_hash_name=name,
                source=source.name,
                role=role,
                ancien=_reference(ancien_q, role),
                nouveau=_reference(nouveau_q, role),
                age_ancien=ancien_q.age_seconds if ancien_q else None,
            )
        )
        if progress:
            progress(i, len(noms))

    return RefreshReport(
        drifts=drifts, seuil=seuil, secondes=time.monotonic() - debut
    )


def collection_roles(collection: "Collection", rarity: "Rarity") -> dict[str, str]:
    """Role de chaque objet d'une collection pour un contrat a cette rarete.

    Les entrees sont ACHETEES (leur hausse nous coute), les sorties REVENDUES
    (leur baisse nous coute). Confondre les deux ferait passer une hausse du
    prix d'entree pour une bonne nouvelle.
    """
    roles: dict[str, str] = {}
    for skin in collection.by_rarity(rarity):
        for wear in skin.available_wears():
            roles[skin.market_hash_name(wear)] = "entree"
    cible = rarity.next_up
    if cible is not None:
        for skin in collection.by_rarity(cible):
            for wear in skin.available_wears():
                roles[skin.market_hash_name(wear)] = "sortie"
    return roles


def _dernier_releve(
    cache: QuoteCache | None, name: str, source: PriceSource
) -> Quote | None:
    """Derniere cotation connue, quel que soit son age.

    Le TTL est volontairement ignore : l'ancienne valeur sert de POINT DE
    COMPARAISON, pas de prix de decision. Une cotation perimee reste le chiffre
    sur lequel l'utilisateur a decide.
    """
    if cache is None:
        return None
    devise = getattr(source, "currency", None)
    return cache.get(name, source.name, ttl=float("inf"), currency=devise)


def _reference(quote: Quote | None, role: str) -> float | None:
    """Prix a retenir selon qu'on achete ou qu'on revend l'objet."""
    if quote is None:
        return None
    return quote.buy_reference() if role == "entree" else quote.sell_reference()
