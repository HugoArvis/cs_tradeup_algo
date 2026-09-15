"""Tests de la re-cotation et de la mesure de derive.

L'enjeu n'est pas de verifier qu'on sait soustraire deux nombres, mais que le
SIGNE de la derive est lu du bon cote : une entree qui rencherit et une sortie
qui se deprecie sont toutes deux defavorables, alors qu'elles ont des signes
opposes en variation de prix brute. Se tromper la ferait passer une mauvaise
nouvelle pour une bonne.
"""

from __future__ import annotations

import time

import pytest

from tradeup.pricing.base import PriceSource, Quote
from tradeup.pricing.cache import QuoteCache
from tradeup.refresh import (
    DEFAULT_DRIFT_THRESHOLD,
    Drift,
    collection_roles,
    refresh_quotes,
)


class FauxMarche(PriceSource):
    """Source qui renvoie des prix figes et compte ses appels."""

    name = "faux"
    currency = "EUR"

    def __init__(self, prix: dict[str, float | None]):
        self.prix = prix
        self.appels: list[str] = []

    def fetch(self, market_hash_name: str, *, use_cache: bool = True) -> Quote | None:
        self.appels.append(market_hash_name)
        p = self.prix.get(market_hash_name)
        if p is None:
            return None
        return Quote(market_hash_name, self.name, p, p, 10, currency="EUR")


@pytest.fixture
def cache(tmp_path):
    c = QuoteCache(path=tmp_path / "prices.db")
    yield c
    c.close()


def ancien(cache, nom, prix, age_heures=12.0):
    cache.put(
        Quote(nom, "faux", prix, prix, 10, currency="EUR",
              fetched_at=time.time() - age_heures * 3600)
    )


# --- Sens de la derive -------------------------------------------------------


def test_une_entree_qui_rencherit_est_defavorable():
    d = Drift("x", "faux", "entree", ancien=10.0, nouveau=12.0, age_ancien=3600)
    assert d.variation == pytest.approx(0.20)  # le prix a monte
    assert d.impact == pytest.approx(-0.20)  # mais ca nous coute
    assert d.defavorable()


def test_une_sortie_qui_baisse_est_defavorable():
    d = Drift("x", "faux", "sortie", ancien=10.0, nouveau=8.0, age_ancien=3600)
    assert d.variation == pytest.approx(-0.20)
    assert d.impact == pytest.approx(-0.20)
    assert d.defavorable()


def test_une_entree_qui_baisse_est_favorable():
    d = Drift("x", "faux", "entree", ancien=10.0, nouveau=8.0, age_ancien=3600)
    assert d.impact == pytest.approx(0.20)
    assert not d.defavorable()


def test_un_objet_sans_cotation_est_bloquant():
    # Ce n'est pas une derive de 0 % : le panier n'est plus achetable tel quel.
    d = Drift("x", "faux", "entree", ancien=10.0, nouveau=None, age_ancien=3600)
    assert d.disparu
    assert d.defavorable()
    assert d.variation is None


def test_un_objet_jamais_cote_nalerte_pas():
    d = Drift("x", "faux", "sortie", ancien=None, nouveau=10.0, age_ancien=None)
    assert d.inconnu and not d.disparu
    assert not d.defavorable()


# --- Re-cotation -------------------------------------------------------------


def test_recotation_compare_au_dernier_releve(cache):
    ancien(cache, "A", 10.0)
    source = FauxMarche({"A": 9.0})
    rapport = refresh_quotes(source, ["A"], cache=cache)
    d = rapport.drifts[0]
    assert d.ancien == 10.0 and d.nouveau == 9.0
    assert d.age_ancien > 0


def test_la_recotation_ignore_le_ttl_du_cache(cache):
    """Une cotation perimee reste le chiffre sur lequel l'utilisateur a decide.

    Si le TTL l'ecartait, toute derive ancienne -- justement la plus grande --
    serait signalee comme "nouveau" au lieu d'alerter.
    """
    ancien(cache, "A", 10.0, age_heures=240)  # dix jours
    source = FauxMarche({"A": 5.0})
    rapport = refresh_quotes(source, ["A"], cache=cache)
    assert rapport.drifts[0].ancien == 10.0
    assert rapport.a_recalculer


def test_les_doublons_ne_sont_cotes_quune_fois(cache):
    source = FauxMarche({"A": 1.0})
    refresh_quotes(source, ["A", "A", "A"], cache=cache)
    assert source.appels == ["A"]


def test_sans_cache_il_ny_a_rien_a_comparer():
    source = FauxMarche({"A": 1.0})
    rapport = refresh_quotes(source, ["A"], cache=None)
    assert rapport.drifts[0].inconnu
    assert not rapport.a_recalculer


def test_le_rapport_signale_ce_quil_faut_recalculer(cache):
    ancien(cache, "entree", 10.0)
    ancien(cache, "sortie", 30.0)
    source = FauxMarche({"entree": 12.0, "sortie": 30.0})
    rapport = refresh_quotes(
        source, ["entree", "sortie"], cache=cache,
        roles={"entree": "entree", "sortie": "sortie"},
    )
    assert rapport.a_recalculer
    assert [d.market_hash_name for d in rapport.defavorables] == ["entree"]
    texte = rapport.report()
    assert "Relance le calcul" in texte


def test_pas_dalerte_quand_rien_ne_bouge(cache):
    ancien(cache, "A", 10.0)
    source = FauxMarche({"A": 10.02})
    rapport = refresh_quotes(source, ["A"], cache=cache)
    assert not rapport.a_recalculer
    assert "tiennent encore" in rapport.report()


def test_le_seuil_est_configurable(cache):
    ancien(cache, "A", 10.0)
    source = FauxMarche({"A": 9.7})  # -3 %
    assert not refresh_quotes(source, ["A"], cache=cache).a_recalculer
    source.appels.clear()
    assert refresh_quotes(source, ["A"], cache=cache, seuil=0.02).a_recalculer


def test_seuil_par_defaut_documente():
    assert DEFAULT_DRIFT_THRESHOLD == 0.05


# --- Roles d'une collection --------------------------------------------------


def test_les_entrees_et_les_sorties_ont_des_roles_distincts():
    from tradeup.db import SkinDatabase
    from tradeup.models import Rarity

    db = SkinDatabase.load()
    col = db.find_collection("The Bank Collection")
    roles = collection_roles(col, Rarity.INDUSTRIAL)

    assert set(roles.values()) == {"entree", "sortie"}
    entrees = {n for n, r in roles.items() if r == "entree"}
    sorties = {n for n, r in roles.items() if r == "sortie"}
    assert entrees and sorties
    assert not (entrees & sorties)  # un objet ne peut pas etre les deux
