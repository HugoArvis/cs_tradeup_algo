"""Agregation des sources de prix -> objet `PriceLookup` du moteur d'EV.

Deux marches distincts : celui ou l'on ACHETE les entrees et celui ou l'on
REVEND la sortie. Ce sont rarement les memes (acheter sur CSFloat, revendre sur
Steam, ou l'inverse), et les frais different.
"""

from __future__ import annotations

import logging

from ..fees import FEE_MODELS, FeeModel
from ..models import Skin, Wear
from .base import PriceSource, Quote

log = logging.getLogger(__name__)


class MarketPricer:
    """Implemente le protocole `PriceLookup` a partir de sources concretes.

    Args:
        buy_source: marche d'achat des entrees.
        sell_source: marche de revente (defaut : identique a l'achat).
        buy_fees / sell_fees: modeles de frais associes.
        safety_margin: decote appliquee a la valeur de revente esperee. Les prix
            bougent, le carnet se vide : 0.05 signifie "je ne compte que sur
            95 % du prix affiche".
        stattrak_supported: si False, toute demande StatTrak renvoie None.
    """

    def __init__(
        self,
        buy_source: PriceSource,
        sell_source: PriceSource | None = None,
        *,
        buy_fees: FeeModel | str = "steam",
        sell_fees: FeeModel | str = "steam",
        safety_margin: float = 0.05,
        min_volume: int | None = None,
        conservative_sell: bool = True,
    ):
        self.buy_source = buy_source
        self.sell_source = sell_source or buy_source
        self.buy_fees = _as_fee_model(buy_fees)
        self.sell_fees = _as_fee_model(sell_fees)
        self.safety_margin = safety_margin
        self.min_volume = min_volume
        self.conservative_sell = conservative_sell
        self._quotes: dict[tuple[str, str], Quote | None] = {}
        self._volume_unknown: set[str] = set()

    # --- Protocole PriceLookup ---

    def sell_net(self, skin: Skin, wear: Wear, stattrak: bool = False) -> float | None:
        q = self._quote(self.sell_source, skin, wear, stattrak)
        if q is None:
            return None
        listed = q.sell_reference(conservative=self.conservative_sell)
        if listed is None:
            return None
        if self.min_volume is not None and q.volume is not None:
            if q.volume < self.min_volume:
                # Illiquide : on refuse de compter dessus plutot que de surestimer.
                return None
        elif self.min_volume is not None:
            # Volume inconnu (CSFloat ne publie pas de volume de ventes) : on ne
            # peut ni confirmer ni infirmer la liquidite. On laisse passer, mais
            # on le trace pour que l'appelant puisse le signaler.
            self._volume_unknown.add(q.market_hash_name)
        return self.sell_fees.net_from_sale(listed) * (1.0 - self.safety_margin)

    def buy_cost(self, skin: Skin, wear: Wear, stattrak: bool = False) -> float | None:
        q = self._quote(self.buy_source, skin, wear, stattrak)
        if q is None:
            return None
        listed = q.buy_reference()
        return self.buy_fees.cost_to_buy(listed) if listed is not None else None

    def volume(self, skin: Skin, wear: Wear, stattrak: bool = False) -> int | None:
        q = self._quote(self.sell_source, skin, wear, stattrak)
        return q.volume if q else None

    # --- Prechargement ---

    def warm(self, names: list[str], source: PriceSource | None = None) -> int:
        """Precharge un lot de cotations. Renvoie le nombre de prix trouves.

        Sans `source`, precharge sur les deux marches (achat et revente) s'ils
        different : sinon le scan repartirait en appels reseau au milieu du
        calcul, ce que le prechargement existe justement pour eviter.
        """
        sources = (
            [source]
            if source is not None
            else list(dict.fromkeys([self.buy_source, self.sell_source]))
        )
        found = 0
        for src in sources:
            for name in names:
                key = (src.name, name)
                if key in self._quotes:
                    continue
                q = src.fetch(name)
                self._quotes[key] = q
                if q is not None:
                    found += 1
        return found

    def volume_unknown(self) -> list[str]:
        """Objets dont la liquidite n'a pas pu etre verifiee."""
        return sorted(self._volume_unknown)

    def missing(self) -> list[str]:
        return sorted(name for (_, name), q in self._quotes.items() if q is None)

    # --- Interne ---

    def _quote(
        self, source: PriceSource, skin: Skin, wear: Wear, stattrak: bool
    ) -> Quote | None:
        if stattrak and not skin.stattrak:
            return None
        name = skin.market_hash_name(wear, stattrak)
        key = (source.name, name)
        if key not in self._quotes:
            self._quotes[key] = source.fetch(name)
        return self._quotes[key]


class StaticPricer:
    """`PriceLookup` alimente par un dictionnaire fige.

    Sert aux tests et aux simulations hors ligne : ``{market_hash_name: prix}``
    en prix affiche, les frais etant appliques par le modele.
    """

    def __init__(
        self,
        prices: dict[str, float],
        *,
        sell_fees: FeeModel | str = "steam",
        buy_fees: FeeModel | str = "steam",
        safety_margin: float = 0.0,
        volumes: dict[str, int] | None = None,
    ):
        self.prices = prices
        self.sell_fees = _as_fee_model(sell_fees)
        self.buy_fees = _as_fee_model(buy_fees)
        self.safety_margin = safety_margin
        self.volumes = volumes or {}

    def sell_net(self, skin: Skin, wear: Wear, stattrak: bool = False) -> float | None:
        p = self.prices.get(skin.market_hash_name(wear, stattrak))
        if p is None:
            return None
        return self.sell_fees.net_from_sale(p) * (1.0 - self.safety_margin)

    def buy_cost(self, skin: Skin, wear: Wear, stattrak: bool = False) -> float | None:
        p = self.prices.get(skin.market_hash_name(wear, stattrak))
        return self.buy_fees.cost_to_buy(p) if p is not None else None

    def volume(self, skin: Skin, wear: Wear, stattrak: bool = False) -> int | None:
        return self.volumes.get(skin.market_hash_name(wear, stattrak))


def _as_fee_model(value: FeeModel | str) -> FeeModel:
    if isinstance(value, FeeModel):
        return value
    try:
        return FEE_MODELS[value]
    except KeyError as exc:
        raise ValueError(
            f"Modele de frais inconnu : {value!r} (connus : {sorted(FEE_MODELS)})"
        ) from exc
