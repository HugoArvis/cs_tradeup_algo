"""Source de prix Steam Community Market (endpoint `priceoverview`).

API non officielle et agressivement rate-limitee : en pratique une vingtaine
d'appels par minute et par IP avant de recevoir des 429 en rafale. Le defaut
retenu ici (15/min) laisse une marge.

Limite structurelle : `priceoverview` ne donne AUCUNE information de float. Il
sert a valoriser un couple (skin, usure) ; pour choisir des entrees a float
precis il faut passer par CSFloat.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from dataclasses import dataclass

from .base import PriceSource, Quote, parse_money, parse_volume
from .cache import QuoteCache
from .http import HttpClient, RateLimiter

log = logging.getLogger(__name__)

PRICEOVERVIEW_URL = "https://steamcommunity.com/market/priceoverview/"
ITEM_ORDERS_URL = "https://steamcommunity.com/market/itemordershistogram"
CS2_APPID = 730

# Codes de devise Steam les plus courants.
CURRENCIES = {"USD": 1, "GBP": 2, "EUR": 3, "CHF": 4, "RUB": 5, "BRL": 7, "CAD": 20}


@dataclass(frozen=True, slots=True)
class OrderBook:
    """Meilleur ordre d'achat et meilleure offre de vente pour un objet."""

    market_hash_name: str
    highest_buy_order: float | None
    lowest_sell_order: float | None
    currency: str = "EUR"

    @property
    def spread(self) -> float | None:
        """Ecart absolu entre acheter tout de suite et placer un ordre."""
        if self.highest_buy_order is None or self.lowest_sell_order is None:
            return None
        return self.lowest_sell_order - self.highest_buy_order

    @property
    def savings_ratio(self) -> float | None:
        """Fraction economisee en passant par un ordre d'achat."""
        if self.spread is None or not self.lowest_sell_order:
            return None
        return self.spread / self.lowest_sell_order


class SteamMarket(PriceSource):
    name = "steam"

    def __init__(
        self,
        *,
        currency: str = "EUR",
        cache: QuoteCache | None = None,
        calls_per_minute: int = 15,
        client: HttpClient | None = None,
        ttl_seconds: float = 6 * 3600,
        offline: bool = False,
    ):
        if currency not in CURRENCIES:
            raise ValueError(
                f"Devise non supportee : {currency} (connues : {sorted(CURRENCIES)})"
            )
        if offline and cache is None:
            raise ValueError("Le mode hors ligne exige un cache")
        self.currency = currency
        self.cache = cache
        self.ttl = ttl_seconds
        self.offline = offline
        self.client = client or HttpClient(
            rate_limiter=RateLimiter(max_calls=calls_per_minute, period=60.0),
            headers={"Accept": "application/json, text/javascript, */*"},
        )
        self.stats = {"hits_cache": 0, "appels_api": 0, "introuvables": 0}
        self._name_ids: dict[str, str | None] = {}

    def fetch(self, market_hash_name: str, *, use_cache: bool = True) -> Quote | None:
        if use_cache and self.cache:
            # Hors ligne, on accepte n'importe quel age : mieux vaut un prix
            # perime signale comme tel qu'aucun prix du tout.
            ttl = float("inf") if self.offline else self.ttl
            cached = self.cache.get(
                market_hash_name, self.name, ttl=ttl, currency=self.currency
            )
            if cached is not None:
                self.stats["hits_cache"] += 1
                return cached
        if self.offline:
            self.stats["introuvables"] += 1
            return None

        self.stats["appels_api"] += 1
        data = self.client.get_json(
            PRICEOVERVIEW_URL,
            {
                "appid": CS2_APPID,
                "currency": CURRENCIES[self.currency],
                "market_hash_name": market_hash_name,
            },
        )

        if not isinstance(data, dict) or not data.get("success"):
            # Steam renvoie success=false pour un objet jamais vendu.
            self.stats["introuvables"] += 1
            log.debug("Aucun prix Steam pour %r", market_hash_name)
            return None

        quote = Quote(
            market_hash_name=market_hash_name,
            source=self.name,
            lowest_price=parse_money(data.get("lowest_price")),
            median_price=parse_money(data.get("median_price")),
            volume=parse_volume(data.get("volume")),
            currency=self.currency,
        )
        if quote.is_empty:
            self.stats["introuvables"] += 1
            return None
        if self.cache:
            self.cache.put(quote)
        return quote

    def refresh(self, market_hash_name: str) -> Quote | None:
        """Force un appel reseau, en ignorant le cache.

        A utiliser juste avant d'executer un contrat : les prix bougent vite et
        une cotation de plusieurs heures ne vaut rien pour une decision.
        """
        return self.fetch(market_hash_name, use_cache=False)

    def order_book(self, market_hash_name: str) -> "OrderBook | None":
        """Carnet d'ordres Steam : meilleur ordre d'ACHAT et meilleure vente.

        `priceoverview` ne donne que le cote vente. Or placer un ordre d'achat
        au lieu d'acheter au prix demande change le cout d'acquisition, et donc
        toute la rentabilite d'un trade-up. L'ecart entre les deux est ce qu'un
        ordre d'achat permet d'economiser -- au prix d'un delai de remplissage.

        Steam expose ce carnet, mais indexe par un identifiant interne
        (`item_nameid`) qui n'apparaissait que dans le HTML de la page de
        l'objet. On le recupere donc en deux temps.

        ATTENTION -- NE FONCTIONNE PLUS (verifie en aout 2026). Steam a
        reecrit ses pages de marche en application cliente : le HTML servi ne
        contient plus ni `item_nameid` ni `Market_LoadOrderSpread`, et aucune
        fonction `Market_*`. La methode renvoie donc None sur tous les objets.

        Recuperer le carnet demanderait soit une session authentifiee, soit de
        rejouer l'appel que fait le client web. En attendant, le gain d'un
        ordre d'achat doit etre saisi a la main : il se lit directement sur la
        page de l'objet dans un navigateur.
        """
        name_id = self._item_name_id(market_hash_name)
        if name_id is None:
            return None

        data = self.client.get_json(
            ITEM_ORDERS_URL,
            {
                "country": "FR",
                "language": "english",
                "currency": CURRENCIES[self.currency],
                "item_nameid": name_id,
                "two_factor": 0,
            },
        )
        if not isinstance(data, dict) or not data.get("success"):
            return None

        achat = parse_money(data.get("highest_buy_order"))
        vente = parse_money(data.get("lowest_sell_order"))
        # Steam renvoie ces deux champs en centimes bruts, pas en texte formate.
        if achat is not None and achat > 1000:
            achat /= 100.0
        if vente is not None and vente > 1000:
            vente /= 100.0
        if achat is None and vente is None:
            return None
        return OrderBook(
            market_hash_name=market_hash_name,
            highest_buy_order=achat,
            lowest_sell_order=vente,
            currency=self.currency,
        )

    def _item_name_id(self, market_hash_name: str) -> str | None:
        if market_hash_name in self._name_ids:
            return self._name_ids[market_hash_name]
        url = (
            f"https://steamcommunity.com/market/listings/{CS2_APPID}/"
            f"{urllib.parse.quote(market_hash_name)}"
        )
        html = self.client.get_text(url)
        if not html:
            self._name_ids[market_hash_name] = None
            return None
        m = re.search(r"Market_LoadOrderSpread\(\s*(\d+)\s*\)", html)
        found = m.group(1) if m else None
        self._name_ids[market_hash_name] = found
        return found

    def estimated_duration(self, n_names: int) -> float:
        """Duree approximative d'un lot de `n_names` requetes, en secondes."""
        limiter = self.client.limiter
        if not limiter or n_names <= limiter.max_calls:
            return 0.0
        return (n_names / limiter.max_calls - 1) * limiter.period
