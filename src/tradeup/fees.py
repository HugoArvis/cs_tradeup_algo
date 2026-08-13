"""Frais de marketplace : ce que l'on paie a l'achat, ce que l'on touche a la vente.

Steam applique 15 % au total (5 % Steam + 10 % editeur) calcules sur le montant
NET du vendeur, pas sur le prix affiche. Autrement dit, pour un prix acheteur P
le vendeur recoit environ P / 1.15, avec un minimum de 0.01 par frais.
"""

from __future__ import annotations

from dataclasses import dataclass

STEAM_FEE_RATE = 0.05
STEAM_PUBLISHER_FEE_RATE = 0.10  # CS2
STEAM_TOTAL_RATE = STEAM_FEE_RATE + STEAM_PUBLISHER_FEE_RATE
MIN_FEE = 0.01


def steam_net_proceeds(buyer_price: float, currency_step: float = 0.01) -> float:
    """Montant net recu par le vendeur pour un prix acheteur donne.

    Reproduit la logique de Steam : le net est le plus grand montant `net` tel
    que ``net + frais(net) <= buyer_price``, ou chaque frais est arrondi vers le
    bas et plafonne par le bas a 0.01.
    """
    if buyer_price <= 0:
        return 0.0

    def buyer_price_for(net: float) -> float:
        steam_fee = max(MIN_FEE, _floor_step(net * STEAM_FEE_RATE, currency_step))
        pub_fee = max(MIN_FEE, _floor_step(net * STEAM_PUBLISHER_FEE_RATE, currency_step))
        return net + steam_fee + pub_fee

    # Estimation puis ajustement : l'estimation est toujours a +/- quelques pas.
    net = _floor_step(buyer_price / (1.0 + STEAM_TOTAL_RATE), currency_step)
    net = max(net, currency_step)
    while buyer_price_for(net + currency_step) <= buyer_price + 1e-9:
        net += currency_step
    while net > currency_step and buyer_price_for(net) > buyer_price + 1e-9:
        net -= currency_step
    return round(net, 2)


def _floor_step(value: float, step: float) -> float:
    return int(value / step + 1e-9) * step


@dataclass(frozen=True, slots=True)
class FeeModel:
    """Modele de frais d'une marketplace.

    `sell_rate` est la commission vendeur en fraction du prix affiche.
    `buy_premium` modelise le surcout eventuel a l'achat (0 sur Steam : le prix
    affiche EST ce que l'acheteur paie).
    """

    name: str
    sell_rate: float
    buy_premium: float = 0.0
    exact_steam_rounding: bool = False

    def net_from_sale(self, listed_price: float) -> float:
        if self.exact_steam_rounding:
            return steam_net_proceeds(listed_price)
        return listed_price * (1.0 - self.sell_rate)

    def cost_to_buy(self, listed_price: float) -> float:
        return listed_price * (1.0 + self.buy_premium)


# Valeurs par defaut ; a ajuster dans la config selon le compte / le volume.
STEAM = FeeModel("steam", STEAM_TOTAL_RATE / (1 + STEAM_TOTAL_RATE), exact_steam_rounding=True)
CSFLOAT = FeeModel("csfloat", 0.02)
BUFF163 = FeeModel("buff163", 0.025)

FEE_MODELS = {m.name: m for m in (STEAM, CSFLOAT, BUFF163)}
