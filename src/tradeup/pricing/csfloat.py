"""Source de prix CSFloat : prix ET floats reels des offres en vente.

C'est la seule des trois sources qui expose le float des objets listes, donc la
seule qui permette de VERIFIER qu'un lot d'entrees a la moyenne de float visee
est effectivement achetable aujourd'hui.

Une cle API est requise (profil CSFloat -> Developer). Sans cle, cette source
est simplement absente du repository.

RATE LIMIT : mesure sur l'API reelle, CSFloat applique un quota sur une fenetre
LONGUE (pas seulement par minute). Un balayage de plusieurs collections epuise
le quota et provoque des 429 en rafale que le backoff ne rattrape pas. Le defaut
de 10/min est prudent ; en cas de 429 persistant, la seule reponse est
d'attendre, pas de reessayer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .base import PriceSource, Quote
from .cache import QuoteCache
from .http import HttpClient, RateLimiter

log = logging.getLogger(__name__)

API_ROOT = "https://csfloat.com/api/v1"
LISTINGS_URL = f"{API_ROOT}/listings"


@dataclass(frozen=True, slots=True)
class Listing:
    """Une offre concrete, avec son float."""

    listing_id: str
    market_hash_name: str
    price: float  # USD, converti depuis les centimes renvoyes par l'API
    float_value: float | None
    paint_seed: int | None
    stickers: int = 0
    keychains: int = 0

    @property
    def has_float(self) -> bool:
        return self.float_value is not None

    @property
    def plain(self) -> bool:
        """Objet nu : ni sticker ni breloque.

        Determinant pour valoriser une SORTIE de trade-up. Un skin cree par un
        contrat sort toujours nu. Or une meme reference stickee peut valoir des
        centaines d'euros de plus -- un Katowice 2014 pese souvent plus que
        l'arme. Melanger les deux dans une courbe de prix produit des valeurs
        de sortie sans rapport avec ce qu'on pourra reellement encaisser.
        """
        return not self.stickers and not self.keychains


class CSFloat(PriceSource):
    name = "csfloat"

    def __init__(
        self,
        api_key: str,
        *,
        cache: QuoteCache | None = None,
        calls_per_minute: int = 10,
        currency: str = "USD",
        client: HttpClient | None = None,
        ttl_seconds: float = 3 * 3600,
    ):
        if not api_key:
            raise ValueError("Cle API CSFloat requise")
        self.currency = currency
        self.cache = cache
        self.ttl = ttl_seconds
        self.client = client or HttpClient(
            rate_limiter=RateLimiter(max_calls=calls_per_minute, period=60.0),
            headers={"Authorization": api_key},
        )

    def fetch(self, market_hash_name: str, *, use_cache: bool = True) -> Quote | None:
        if use_cache and self.cache:
            cached = self.cache.get(
                market_hash_name, self.name, ttl=self.ttl, currency=self.currency
            )
            if cached is not None:
                return cached

        listings = self.listings(market_hash_name, limit=10)
        if not listings:
            return None

        prices = sorted(l.price for l in listings)
        quote = Quote(
            market_hash_name=market_hash_name,
            source=self.name,
            lowest_price=prices[0],
            median_price=prices[len(prices) // 2],
            volume=None,  # l'endpoint listings ne donne pas de volume de ventes
            currency=self.currency,
        )
        if self.cache:
            self.cache.put(quote)
        return quote

    def listings(
        self,
        market_hash_name: str,
        *,
        limit: int = 20,
        max_float: float | None = None,
        min_float: float | None = None,
    ) -> list[Listing]:
        """Offres en vente, triees par prix croissant.

        `min_float` / `max_float` filtrent cote serveur : c'est ce qui permet de
        chercher directement les entrees a bas float d'un trade-up.
        """
        params: dict[str, object] = {
            "limit": min(limit, 50),
            "sort_by": "lowest_price",
            "market_hash_name": market_hash_name,
            "type": "buy_now",
        }
        if max_float is not None:
            params["max_float"] = max_float
        if min_float is not None:
            params["min_float"] = min_float

        data = self.client.get_json(LISTINGS_URL, params)
        rows = data.get("data", []) if isinstance(data, dict) else (data or [])

        out: list[Listing] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = row.get("item") or {}
            raw_price = row.get("price")
            if raw_price is None:
                continue
            out.append(
                Listing(
                    listing_id=str(row.get("id", "")),
                    market_hash_name=item.get("market_hash_name", market_hash_name),
                    price=float(raw_price) / 100.0,  # l'API repond en centimes
                    float_value=(
                        float(item["float_value"])
                        if item.get("float_value") is not None
                        else None
                    ),
                    paint_seed=item.get("paint_seed"),
                    stickers=len(item.get("stickers") or ()),
                    keychains=len(item.get("keychains") or ()),
                )
            )
        return out

    def account_currency(self) -> str | None:
        """Devise d'affichage du compte CSFloat.

        L'API cote TOUJOURS en USD, mais le site convertit vers la devise
        choisie dans le profil. Un utilisateur reglé en EUR paie donc des euros
        pour un prix que l'API annonce en dollars : afficher le brut donne des
        montants qui ne correspondent a rien de ce qu'il voit.
        """
        data = self.client.get_json(f"{API_ROOT}/me")
        if not isinstance(data, dict):
            return None
        user = data.get("user", data)
        prefs = user.get("preferences") or {}
        devise = prefs.get("currency")
        return str(devise).upper() if devise else None

    def exchange_rates(self) -> dict[str, float]:
        """Taux de change par rapport au dollar (1 USD = x devise)."""
        data = self.client.get_json(f"{API_ROOT}/meta/exchange-rates")
        brut = data.get("data", {}) if isinstance(data, dict) else {}
        return {str(k).upper(): float(v) for k, v in brut.items()
                if isinstance(v, (int, float))}

    def usd_rate(self, currency: str) -> float:
        """Combien vaut 1 USD dans `currency`. 1.0 pour USD lui-meme."""
        if currency.upper() == "USD":
            return 1.0
        taux = self.exchange_rates().get(currency.upper())
        if not taux:
            raise LookupError(f"taux introuvable pour {currency}")
        return taux

    def cheapest_at_float(
        self, market_hash_name: str, max_float: float, limit: int = 20
    ) -> Listing | None:
        """Offre la moins chere dont le float ne depasse pas `max_float`."""
        found = self.listings(market_hash_name, limit=limit, max_float=max_float)
        candidates = [l for l in found if l.has_float and l.float_value <= max_float]
        return min(candidates, key=lambda l: l.price) if candidates else None
