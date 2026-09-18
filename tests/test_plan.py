"""Tests de la selection 0/1 et de la construction de panier reel.

La difference avec `cheapest_selection` est subtile mais critique : une annonce
CSFloat est un objet UNIQUE. Un selecteur qui autorise la repetition produirait
un panier impossible a passer en caisse.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from tradeup.generator import (
    InputOption,
    cheapest_selection,
    cheapest_unique_selection,
    options_from_listings,
)
from tradeup.models import Rarity, Skin, Wear

SKIN = Skin(key="s", name="Arme", collection_id="c", rarity=Rarity.MIL_SPEC,
            min_float=0.0, max_float=1.0)
ETROIT = Skin(key="e", name="Etroit", collection_id="c", rarity=Rarity.MIL_SPEC,
              min_float=0.10, max_float=0.50)


def opt(cost, flt, wear=Wear.FACTORY_NEW, skin=SKIN):
    return InputOption(skin=skin, wear=wear, unit_cost=cost, float_value=flt)


@dataclass
class FakeListing:
    price: float
    float_value: float | None


# --- Selection sans repetition -----------------------------------------------

def test_une_annonce_ne_peut_servir_quune_fois():
    """Le coeur du sujet : 3 annonces ne suffisent pas pour 10 entrees."""
    options = [opt(1.0, 0.01), opt(1.0, 0.01), opt(1.0, 0.01)]

    # Le selecteur a repetition accepte : il croit a un stock infini.
    assert cheapest_selection(options, 10, float_budget=1.0) is not None
    # Le selecteur unique refuse : il n'y a que 3 objets reels.
    assert cheapest_unique_selection(options, 10, float_budget=1.0) is None


def test_selectionne_les_moins_cheres_sous_contrainte():
    options = [opt(5.0, 0.01), opt(1.0, 0.30), opt(2.0, 0.02), opt(3.0, 0.03)]
    sel = cheapest_unique_selection(options, 2, float_budget=0.10)

    assert sel is not None
    assert len(sel) == 2
    assert len({id(o) for o in sel}) == 2  # deux objets distincts
    assert sum(o.float_value for o in sel) <= 0.10 + 1e-9
    # 2.00 + 3.00 : la moins chere (1.00) a un float trop eleve.
    assert sum(o.unit_cost for o in sel) == pytest.approx(5.0)


def test_budget_serre_force_les_bas_floats_meme_chers():
    options = [opt(1.0, 0.20), opt(1.0, 0.20), opt(9.0, 0.01), opt(9.0, 0.01)]
    sel = cheapest_unique_selection(options, 2, float_budget=0.05)
    assert sel is not None
    assert sum(o.unit_cost for o in sel) == pytest.approx(18.0)


def test_budget_inatteignable():
    options = [opt(1.0, 0.20) for _ in range(10)]
    assert cheapest_unique_selection(options, 10, float_budget=0.5) is None


def test_cas_limites():
    assert cheapest_unique_selection([], 0, 1.0) == []
    assert cheapest_unique_selection([], 5, 1.0) is None
    # Exactement le nombre requis, budget suffisant.
    options = [opt(1.0, 0.01) for _ in range(10)]
    sel = cheapest_unique_selection(options, 10, float_budget=0.2)
    assert sel is not None and len(sel) == 10


def test_les_dix_objets_sont_bien_distincts():
    options = [opt(1.0 + i * 0.1, 0.01 * (i + 1)) for i in range(20)]
    sel = cheapest_unique_selection(options, 10, float_budget=10.0)
    assert sel is not None
    assert len({id(o) for o in sel}) == 10


# --- Conversion des offres reelles -------------------------------------------

def test_le_float_reel_remplace_lhypothese():
    listings = [FakeListing(price=1.43, float_value=0.0438),
                FakeListing(price=1.47, float_value=0.0296)]
    options = options_from_listings(SKIN, listings)

    assert [o.float_value for o in options] == [0.0438, 0.0296]
    assert [o.unit_cost for o in options] == [1.43, 1.47]  # triees par prix
    # L'usure est deduite du float, pas fournie par l'appelant.
    assert all(o.wear is Wear.FACTORY_NEW for o in options)


def test_offres_inexploitables_ecartees():
    listings = [
        FakeListing(price=1.0, float_value=None),  # float inconnu
        FakeListing(price=0.0, float_value=0.05),  # prix nul
        FakeListing(price=-1.0, float_value=0.05),  # prix negatif
        FakeListing(price=2.0, float_value=0.05),  # valide
    ]
    options = options_from_listings(SKIN, listings)
    assert len(options) == 1
    assert options[0].unit_cost == 2.0


def test_float_incoherent_avec_la_base_est_rejete():
    """Un float hors du range connu du skin signale une donnee douteuse.

    On prefere ignorer l'offre plutot que de calculer une sortie sur une base
    fausse.
    """
    listings = [FakeListing(price=1.0, float_value=0.05),  # < min_float 0.10
                FakeListing(price=1.0, float_value=0.90),  # > max_float 0.50
                FakeListing(price=1.0, float_value=0.30)]  # valide
    options = options_from_listings(ETROIT, listings)
    assert len(options) == 1
    assert options[0].float_value == 0.30


def test_limitation_du_nombre_doffres_par_skin():
    listings = [FakeListing(price=float(i), float_value=0.05) for i in range(1, 21)]
    options = options_from_listings(SKIN, listings, max_per_skin=5)
    assert len(options) == 5
    # Les 5 moins cheres.
    assert [o.unit_cost for o in options] == [1.0, 2.0, 3.0, 4.0, 5.0]


def test_le_plan_porte_la_rarete_dentree():
    """Sans elle, rien en aval ne pouvait l'afficher : ni le rapport, ni le
    nom du fichier, ni l'historique de l'application web."""
    from tradeup.models import Rarity
    from tradeup.plan import Plan

    assert "rarity" in Plan.__dataclass_fields__
    assert Plan.__dataclass_fields__["rarity"].default is Rarity.MIL_SPEC


# --- Frais du marche de REVENTE ----------------------------------------------
# On achete sur CSFloat et on revend sur Steam. Ce n'est pas le prix d'une
# sortie qui decide d'un contrat, c'est ce qu'on encaisse : les frais Steam
# ont un plancher de 0.01 PAR FRAIS, ce qui devore les petits montants.


class FauxMarcheDeVente:
    """Source de prix minimale, pour verifier le chemin de valorisation."""

    name = "faux"

    def __init__(self, prix: dict[str, float], volume: int | None = 50):
        self.prix = prix
        self.volume = volume
        self.appels: list[str] = []

    def fetch(self, market_hash_name: str, *, use_cache: bool = True):
        from tradeup.pricing.base import Quote

        self.appels.append(market_hash_name)
        p = self.prix.get(market_hash_name)
        if p is None:
            return None
        return Quote(market_hash_name=market_hash_name, source="faux",
                     lowest_price=p, median_price=p, volume=self.volume,
                     currency="USD")


def _pricer(prix_steam, **kw):
    from tradeup.fees import STEAM
    from tradeup.plan import CSFloatPricer

    marche = FauxMarcheDeVente(prix_steam, **kw)
    return CSFloatPricer(source=None, safety_margin=0.0,
                         sell_source=marche, sell_fees=STEAM), marche


def test_le_plancher_de_frais_steam_devore_les_petits_montants():
    """Regle du domaine : 0.01 minimum PAR frais, il y en a deux.

    A 0.03 le vendeur touche 0.01 -- 66 % de frais. Un modele a taux fixe
    annoncerait 0.026 et rendrait rentable un contrat qui perd.
    """
    nom = SKIN.market_hash_name(Wear.FACTORY_NEW)
    pricer, _ = _pricer({nom: 0.03})
    assert pricer.sell_net(SKIN, Wear.FACTORY_NEW) == pytest.approx(0.01)

    pricer, _ = _pricer({nom: 0.07})
    # 0.05 net : 28.6 % de frais, tres loin des 13 % du haut de l'echelle.
    assert pricer.sell_net(SKIN, Wear.FACTORY_NEW) == pytest.approx(0.05)

    pricer, _ = _pricer({nom: 10.0})
    net = pricer.sell_net(SKIN, Wear.FACTORY_NEW)
    assert 0.86 < net / 10.0 < 0.88  # regime normal : ~13 %


def test_une_sortie_non_cotee_sur_le_marche_de_vente_ne_vaut_rien():
    """Mieux vaut une sortie sans prix qu'un prix invente."""
    pricer, _ = _pricer({})
    assert pricer.sell_net(SKIN, Wear.FACTORY_NEW) is None


def test_le_float_ne_change_pas_le_prix_quand_on_revend_sur_steam():
    """Steam n'affiche pas le float, donc ne le price pas.

    Viser 0.001 plutot que 0.06 dans un meme palier ne rapporte rien de plus :
    le modele ne doit pas payer une prime pour un avantage inexistant.
    """
    nom = SKIN.market_hash_name(Wear.FACTORY_NEW)
    pricer, _ = _pricer({nom: 10.0})
    bas = pricer.sell_net_at_float(SKIN, Wear.FACTORY_NEW, False, 0.001)
    haut = pricer.sell_net_at_float(SKIN, Wear.FACTORY_NEW, False, 0.060)
    assert bas == haut == pricer.sell_net(SKIN, Wear.FACTORY_NEW)


def test_la_liquidite_est_lue_sur_le_marche_de_vente_sans_requete_de_plus():
    """Le volume arrive avec la cotation ; l'interroger a part coute un appel."""
    nom = SKIN.market_hash_name(Wear.FACTORY_NEW)
    pricer, marche = _pricer({nom: 10.0}, volume=98)

    pricer.sell_net(SKIN, Wear.FACTORY_NEW)
    stats = pricer.sales_stats(nom)

    assert stats["ventes_jour"] == 98
    assert marche.appels == [nom]  # une seule cotation pour prix ET volume


def test_sans_marche_de_vente_le_comportement_dorigine_est_conserve():
    """Le mode historique -- revendre sur CSFloat -- reste disponible."""
    from tradeup.plan import CSFloatPricer

    pricer = CSFloatPricer(source=None, sell_fee=0.02, safety_margin=0.0)
    pricer._book[SKIN.market_hash_name(Wear.FACTORY_NEW)] = [(0.01, 10.0)]
    assert pricer.sell_net(SKIN, Wear.FACTORY_NEW) == pytest.approx(9.8)
