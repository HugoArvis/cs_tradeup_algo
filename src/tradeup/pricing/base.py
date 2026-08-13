"""Contrat commun aux sources de prix + parsing des montants Steam."""

from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Quote:
    """Un releve de prix pour un `market_hash_name` donne."""

    market_hash_name: str
    source: str
    lowest_price: float | None  # meilleure offre de vente (ce qu'on paie)
    median_price: float | None  # prix median recent (ce qu'on espere encaisser)
    volume: int | None  # ventes sur 24 h, proxy de liquidite
    currency: str = "EUR"
    fetched_at: float = field(default_factory=time.time)

    @property
    def age_seconds(self) -> float:
        return time.time() - self.fetched_at

    @property
    def is_empty(self) -> bool:
        return self.lowest_price is None and self.median_price is None

    def buy_reference(self) -> float | None:
        """Prix retenu pour ACHETER : la meilleure offre disponible."""
        return self.lowest_price if self.lowest_price is not None else self.median_price

    def sell_reference(self, conservative: bool = True) -> float | None:
        """Prix retenu pour VENDRE.

        En mode conservateur on prend le minimum entre lowest et median : pour
        vendre vite il faut s'aligner sur la meilleure offre existante, et le
        median protege des cas ou une seule offre casse le marche.
        """
        candidates = [p for p in (self.lowest_price, self.median_price) if p is not None]
        if not candidates:
            return None
        return min(candidates) if conservative else max(candidates)


class PriceSource(ABC):
    """Interface d'une marketplace."""

    name: str

    @abstractmethod
    def fetch(self, market_hash_name: str) -> Quote | None:
        """Recupere un prix, ou None si l'objet est introuvable."""

    def fetch_many(self, names: list[str]) -> dict[str, Quote]:
        """Version par lot ; surchargee quand l'API supporte le batch."""
        out: dict[str, Quote] = {}
        for n in names:
            q = self.fetch(n)
            if q is not None:
                out[n] = q
        return out


_MONEY_RE = re.compile(r"[-+]?[\d\s .,]+")


def parse_money(text: str | None) -> float | None:
    """Parse un montant Steam quelle que soit la locale.

    Gere ``"1,23€"``, ``"$1.23"``, ``"1.234,56 €"``, ``"1,234.56"``,
    ``"CHF 12.-"``. Renvoie None si rien d'exploitable.
    """
    if text is None:
        return None
    m = _MONEY_RE.search(str(text).replace(" ", " "))
    if not m:
        return None
    s = m.group(0).strip().replace(" ", "")
    if not s:
        return None

    last_dot, last_comma = s.rfind("."), s.rfind(",")
    if last_dot >= 0 and last_comma >= 0:
        # Le separateur le plus a droite est le separateur decimal.
        if last_comma > last_dot:
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif last_comma >= 0:
        # Virgule seule : decimale si <= 2 chiffres apres, sinon millier.
        s = s.replace(",", "." if len(s) - last_comma - 1 <= 2 else "")
    elif last_dot >= 0 and len(s) - last_dot - 1 == 3 and s.count(".") == 1:
        # "1.234" est ambigu ; Steam n'utilise le point millier qu'ainsi.
        pass

    try:
        return float(s.rstrip("."))
    except ValueError:
        return None


def parse_volume(text: str | None) -> int | None:
    if text is None:
        return None
    digits = re.sub(r"[^\d]", "", str(text))
    return int(digits) if digits else None
