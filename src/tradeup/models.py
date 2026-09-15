"""Types du domaine : raretes, usures, skins, collections.

Reference des regles de trade-up modelisees ici :
  - 10 entrees, meme rarete, meme "type" (toutes StatTrak ou toutes normales,
    jamais de Souvenir).
  - La sortie est un skin de la rarete immediatement superieure, tire parmi les
    collections representees en entree.
  - Le float de sortie derive de la moyenne des floats NORMALISES des entrees
    -- chacun rapporte au range de son propre skin (voir `Skin.normalized`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Rarity(Enum):
    """Echelle de rarete des skins d'armes, dans l'ordre du trade-up."""

    CONSUMER = ("Consumer Grade", 0)
    INDUSTRIAL = ("Industrial Grade", 1)
    MIL_SPEC = ("Mil-Spec Grade", 2)
    RESTRICTED = ("Restricted", 3)
    CLASSIFIED = ("Classified", 4)
    COVERT = ("Covert", 5)
    CONTRABAND = ("Contraband", 6)

    def __init__(self, label: str, ladder: int) -> None:
        self.label = label
        self.ladder = ladder

    @property
    def next_up(self) -> "Rarity | None":
        """Rarete de sortie d'un trade-up dont les entrees sont a `self`.

        Covert est le plafond : on ne peut pas trade-up des Covert (les couteaux
        et gants ne sont pas atteignables par contrat). Contraband non plus.
        """
        if self.ladder >= Rarity.COVERT.ladder:
            return None
        return _RARITY_BY_LADDER[self.ladder + 1]

    @classmethod
    def from_label(cls, label: str) -> "Rarity":
        key = label.strip().lower()
        try:
            return _RARITY_BY_LABEL[key]
        except KeyError as exc:
            raise ValueError(f"Rarete inconnue : {label!r}") from exc

    def __repr__(self) -> str:  # pragma: no cover - confort de debug
        return f"Rarity.{self.name}"


_RARITY_BY_LADDER = {r.ladder: r for r in Rarity}
_RARITY_BY_LABEL = {r.label.lower(): r for r in Rarity}
# Alias rencontres dans les sources de donnees.
_RARITY_BY_LABEL.update(
    {
        "mil-spec": Rarity.MIL_SPEC,
        "mil-spec grade": Rarity.MIL_SPEC,
        "consumer": Rarity.CONSUMER,
        "industrial": Rarity.INDUSTRIAL,
        "high grade": Rarity.RESTRICTED,
        "remarkable": Rarity.CLASSIFIED,
        "exotic": Rarity.COVERT,
    }
)

# Raretes qui peuvent servir d'ENTREE a un trade-up.
TRADEABLE_INPUT_RARITIES = (
    Rarity.CONSUMER,
    Rarity.INDUSTRIAL,
    Rarity.MIL_SPEC,
    Rarity.RESTRICTED,
    Rarity.CLASSIFIED,
)


class Wear(Enum):
    """Paliers d'usure et leurs bornes de float (borne haute exclusive)."""

    FACTORY_NEW = ("Factory New", 0.00, 0.07)
    MINIMAL_WEAR = ("Minimal Wear", 0.07, 0.15)
    FIELD_TESTED = ("Field-Tested", 0.15, 0.38)
    WELL_WORN = ("Well-Worn", 0.38, 0.45)
    BATTLE_SCARRED = ("Battle-Scarred", 0.45, 1.00)

    def __init__(self, label: str, lo: float, hi: float) -> None:
        self.label = label
        self.lo = lo
        self.hi = hi

    @property
    def short(self) -> str:
        return {
            "Factory New": "FN",
            "Minimal Wear": "MW",
            "Field-Tested": "FT",
            "Well-Worn": "WW",
            "Battle-Scarred": "BS",
        }[self.label]

    @classmethod
    def from_label(cls, label: str) -> "Wear":
        key = label.strip().lower()
        for w in cls:
            if w.label.lower() == key or w.short.lower() == key:
                return w
        raise ValueError(f"Usure inconnue : {label!r}")

    def __repr__(self) -> str:  # pragma: no cover - confort de debug
        return f"Wear.{self.short}"


WEARS_ORDERED = tuple(Wear)


@dataclass(frozen=True, slots=True)
class Skin:
    """Un skin d'arme (independant de son usure).

    `min_float` / `max_float` sont les bornes propres au skin (le "range"), qui
    servent a remapper la moyenne des floats d'entree vers le float de sortie.
    """

    key: str  # identifiant stable, ex. "ak-47-redline"
    name: str  # ex. "AK-47 | Redline"
    collection_id: str
    rarity: Rarity
    min_float: float
    max_float: float
    stattrak: bool = False  # une variante StatTrak existe-t-elle
    souvenir: bool = False  # une variante Souvenir existe-t-elle

    def __post_init__(self) -> None:
        if not 0.0 <= self.min_float < self.max_float <= 1.0:
            raise ValueError(
                f"Range de float invalide pour {self.name!r} : "
                f"[{self.min_float}, {self.max_float}]"
            )

    @property
    def float_span(self) -> float:
        return self.max_float - self.min_float

    def normalized(self, float_value: float) -> float:
        """Position du float dans le range PROPRE de ce skin, entre 0 et 1.

        C'est cette valeur, et non le float absolu, qui entre dans la moyenne
        d'un trade-up. Un Nova Caged Steel a 0.075 sur un range 0-0.20 vaut 0.375
        normalise ; le meme float sur un range 0-1 vaudrait 0.075. Confondre les
        deux fait predire une sortie Factory New la ou le jeu produit du Minimal
        Wear -- verifie sur un contrat reel au cinq-millieme pres.
        """
        if self.float_span <= 0:
            return 0.0
        return (float_value - self.min_float) / self.float_span

    def available_wears(self) -> tuple[Wear, ...]:
        """Paliers d'usure reellement atteignables compte tenu du range."""
        return tuple(
            w
            for w in WEARS_ORDERED
            if w.lo < self.max_float and w.hi > self.min_float
        )

    def market_hash_name(self, wear: Wear, stattrak: bool = False) -> str:
        """Nom exact utilise par le Steam Community Market."""
        prefix = "StatTrak™ " if stattrak else ""
        return f"{prefix}{self.name} ({wear.label})"


@dataclass(frozen=True, slots=True)
class Collection:
    """Une collection (caisse ou collection de carte)."""

    id: str  # ex. "set_community_32"
    name: str  # ex. "The Recoil Collection"
    skins: tuple[Skin, ...] = field(default_factory=tuple)

    def by_rarity(self, rarity: Rarity) -> tuple[Skin, ...]:
        return tuple(s for s in self.skins if s.rarity is rarity)

    def outcomes_for_input_rarity(self, rarity: Rarity) -> tuple[Skin, ...]:
        """Skins pouvant sortir d'un trade-up alimente par cette collection."""
        target = rarity.next_up
        if target is None:
            return ()
        return self.by_rarity(target)
