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
