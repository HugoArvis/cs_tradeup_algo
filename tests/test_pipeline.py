"""Tests de bout en bout du generateur et du scan, hors ligne.

On construit une base synthetique dont on connait la reponse a la main, ce qui
permet de verifier que l'optimiseur trouve bien l'optimum et pas seulement
"quelque chose de positif".
"""

from __future__ import annotations

import pytest

from tradeup.db import SkinDatabase
from tradeup.generator import iter_recipes, optimize_recipe
from tradeup.models import Rarity, Wear
from tradeup.pricing.repository import StaticPricer
from tradeup.scan import required_market_names, scan
from tradeup.scoring import Ranking, ScreenConfig

# Une collection ou le trade-up est rentable, une autre ou il ne l'est pas.
RAW_DB = {
    "version": "test",
    "collections": [
        {
            "id": "col_rentable",
            "name": "Collection Rentable",
            "skins": [
                {"key": "r_in1", "name": "Arme A", "rarity": "Mil-Spec Grade",
                 "min_float": 0.0, "max_float": 1.0},
                {"key": "r_in2", "name": "Arme B", "rarity": "Mil-Spec Grade",
                 "min_float": 0.0, "max_float": 1.0},
                # Deux sorties seulement : faible variance.
                {"key": "r_out1", "name": "Sortie X", "rarity": "Restricted",
                 "min_float": 0.0, "max_float": 1.0},
                {"key": "r_out2", "name": "Sortie Y", "rarity": "Restricted",
                 "min_float": 0.0, "max_float": 1.0},
            ],
        },
        {
            "id": "col_perdante",
            "name": "Collection Perdante",
            "skins": [
                {"key": "p_in1", "name": "Arme C", "rarity": "Mil-Spec Grade",
                 "min_float": 0.0, "max_float": 1.0},
                {"key": "p_out1", "name": "Sortie Z", "rarity": "Restricted",
                 "min_float": 0.0, "max_float": 1.0},
            ],
        },
    ],
}

# Les sorties valent beaucoup plus en Factory New qu'en Field-Tested : c'est ce
# qui doit pousser l'optimiseur a acheter des entrees a bas float.
PRICES = {
    "Arme A (Factory New)": 2.00,
    "Arme A (Field-Tested)": 1.00,
    "Arme B (Factory New)": 3.00,
    "Arme B (Field-Tested)": 0.90,
    "Arme C (Factory New)": 1.00,
    "Arme C (Field-Tested)": 0.50,
    "Sortie X (Factory New)": 60.00,
    "Sortie X (Field-Tested)": 12.00,
    "Sortie Y (Factory New)": 40.00,
    "Sortie Y (Field-Tested)": 8.00,
    "Sortie Z (Factory New)": 1.00,
    "Sortie Z (Field-Tested)": 0.50,
}


@pytest.fixture
def db() -> SkinDatabase:
    return SkinDatabase.from_dict(RAW_DB)


@pytest.fixture
def prices() -> StaticPricer:
    return StaticPricer(PRICES, sell_fees="csfloat", buy_fees="csfloat")


def test_db_charge_et_indexe(db):
    assert len(db) == 2
    assert db.skin_count == 6
    assert db.find("Arme A").rarity is Rarity.MIL_SPEC
    assert db.find_collection("Rentable").id == "col_rentable"
    assert len(db.tradeable_collections(Rarity.MIL_SPEC)) == 2
    # Aucune collection n'a de Classified : rien a faire depuis Restricted.
    assert db.tradeable_collections(Rarity.RESTRICTED) == []


def test_required_market_names_couvre_entrees_et_sorties(db):
    names = required_market_names(db, [db.collection("col_rentable")], Rarity.MIL_SPEC)
    assert "Arme A (Factory New)" in names
    assert "Sortie X (Battle-Scarred)" in names
    # 4 skins x 5 usures atteignables.
    assert len(names) == 20


def test_optimiseur_choisit_le_palier_factory_new(db, prices):
    recipe = next(iter_recipes(db, Rarity.MIL_SPEC, max_collections=1,
                               collection_filter=["col_rentable"]))
    # float_safety=0 pour isoler le choix du palier de la marge de securite,
    # qui est testee separement plus bas.
    r = optimize_recipe(recipe, db, prices, float_percentile=0.15, float_safety=0.0)

    assert r is not None
    # La sortie FN vaut 5x la sortie FT : l'optimiseur doit viser FN malgre
    # des entrees deux fois plus cheres.
    assert all(o.wear is Wear.FACTORY_NEW for o in r.outcomes)
    assert r.avg_input_float < 0.07

    # EV : 50 % a 60 et 50 % a 40, moins 2 % de frais.
    assert r.ev_net == pytest.approx(49.0)
    assert r.ev_profit == pytest.approx(32.3)
    assert r.profit_probability == pytest.approx(1.0)  # les deux sorties couvrent le cout


def test_optimiseur_melange_les_usures_pour_viser_la_moyenne(db, prices):
    """Seule la MOYENNE des floats compte : c'est le levier principal.

    Acheter 10 entrees Factory New couterait 20.00. En melangeant 7 FN a 2.00
    et 3 Field-Tested a 0.90 la moyenne reste sous 0.07 -- meme sortie FN, mais
    16.70 de cout. Un optimiseur qui n'achete que du FN laisse 17 % sur la table.
    """
    recipe = next(iter_recipes(db, Rarity.MIL_SPEC, max_collections=1,
                               collection_filter=["col_rentable"]))
    r = optimize_recipe(recipe, db, prices, float_percentile=0.15, float_safety=0.0)

    assert r is not None
    assert r.cost == pytest.approx(16.7)
    assert r.cost < 20.0  # strictement mieux que "tout en Factory New"
    # La moyenne reste sous la frontiere FN malgre des entrees Field-Tested.
    assert r.avg_input_float < Wear.FACTORY_NEW.hi
    assert all(o.wear is Wear.FACTORY_NEW for o in r.outcomes)


def test_optimiseur_rejette_la_collection_perdante(db, prices):
    recipe = next(iter_recipes(db, Rarity.MIL_SPEC, max_collections=1,
                               collection_filter=["col_perdante"]))
    r = optimize_recipe(recipe, db, prices, float_percentile=0.15)
    assert r is not None
    assert r.ev_profit < 0  # 10 entrees a 1.00 pour une sortie a 1.00 max


def test_scan_classe_la_rentable_devant_et_filtre_la_perdante(db, prices):
    candidates, stats = scan(
        db, prices, Rarity.MIL_SPEC,
        screen=ScreenConfig(min_roi=0.03, max_unpriced_probability=0.0),
        max_collections=1,
        float_safety=0.0,
    )
    assert stats["recettes"] == 2
    assert stats["evaluees"] == 2
    assert len(candidates) == 1
    assert "Rentable" in candidates[0].label
    assert candidates[0].result.ev_profit == pytest.approx(32.3)


def test_scan_avec_melanges_explore_plus_de_recettes(db, prices):
    _, mono = scan(db, prices, Rarity.MIL_SPEC, max_collections=1)
    _, duo = scan(db, prices, Rarity.MIL_SPEC, max_collections=2)
    # 2 mono + 9 repartitions (1..9) de la paire.
    assert mono["recettes"] == 2
    assert duo["recettes"] == 11


def test_diluer_avec_une_collection_perdante_degrade_le_contrat(db, prices):
    """Verifie la formule de probabilite sur un melange reel.

    5 entrees "rentable" (2 sorties) + 5 "perdante" (1 sortie) :
    denominateur = 5*2 + 5*1 = 15, donc la collection rentable ne pese que
    10/15 = 66.7 % alors qu'elle fournit la moitie des entrees.
    """
    recipes = [
        r for r in iter_recipes(db, Rarity.MIL_SPEC, max_collections=2)
        if len(r.counts) == 2 and dict(r.counts).get("col_rentable") == 5
    ]
    assert len(recipes) == 1
    r = optimize_recipe(recipes[0], db, prices, float_percentile=0.15)

    assert r is not None
    proba_rentable = sum(
        o.probability for o in r.outcomes if o.skin.collection_id == "col_rentable"
    )
    assert proba_rentable == pytest.approx(10 / 15)
    # Le melange reste positif ici, mais nettement moins bon que le mono.
    assert r.ev_profit < 29.0


def test_marge_de_securite_eloigne_de_la_frontiere_dusure(db, prices):
    """Sans marge, l'optimiseur se gare toujours juste sous une frontiere.

    C'est un biais systematique, pas un alea : le cout est minimal la, donc
    l'optimiseur y va toujours. Or les floats d'entree sont des TIRAGES -- a
    quelques milliemes pres toutes les sorties basculent d'un palier.
    """
    recipe = next(iter_recipes(db, Rarity.MIL_SPEC, max_collections=1,
                               collection_filter=["col_rentable"]))

    colle = optimize_recipe(recipe, db, prices, float_percentile=0.15, float_safety=0.0)
    prudent = optimize_recipe(recipe, db, prices, float_percentile=0.15,
                              float_safety=0.02)

    assert colle is not None and prudent is not None
    # `float_safety` est une borne INFERIEURE garantie sur la marge. Elle peut
    # etre depassee : les options d'achat sont discretes, on ne se pose pas
    # toujours pile sur la cible.
    assert prudent.cliff_distance >= 0.02 - 1e-9
    # Ce qui compte est la relation : plus de marge coute plus cher.
    assert prudent.cliff_distance > colle.cliff_distance
    assert prudent.avg_input_float < colle.avg_input_float
    assert prudent.cost > colle.cost
    # La sortie reste Factory New : on a paye la securite, pas perdu le palier.
    assert all(o.wear is Wear.FACTORY_NEW for o in prudent.outcomes)


def test_filtre_min_cliff_rejette_les_contrats_au_bord(db, prices):
    strict = ScreenConfig(min_roi=-99, min_ev_profit=-999, min_cliff_distance=0.02)
    laxiste = ScreenConfig(min_roi=-99, min_ev_profit=-999, min_cliff_distance=0.0)

    serres, _ = scan(db, prices, Rarity.MIL_SPEC, screen=strict, float_safety=0.001)
    tous, _ = scan(db, prices, Rarity.MIL_SPEC, screen=laxiste, float_safety=0.001)

    assert len(tous) > len(serres)
    assert all(c.result.cliff_distance >= 0.02 for c in serres)


def test_shopping_list_couvre_les_dix_entrees(db, prices):
    recipe = next(iter_recipes(db, Rarity.MIL_SPEC, max_collections=1,
                               collection_filter=["col_rentable"]))
    r = optimize_recipe(recipe, db, prices, float_percentile=0.15)

    assert r is not None
    assert len(r.inputs) == 10
    rows = r.shopping_list()
    assert sum(qty for _, qty, _, _ in rows) == 10
    assert sum(qty * cost for _, qty, cost, _ in rows) == pytest.approx(r.cost)
    # Chaque ligne nomme un objet achetable tel quel sur le marche.
    assert all("(" in name and name.endswith(")") for name, _, _, _ in rows)


def test_scan_sans_aucun_prix_ne_produit_rien(db):
    candidates, stats = scan(db, StaticPricer({}), Rarity.MIL_SPEC)
    assert candidates == []
    assert stats["sans_prix"] == stats["recettes"]


def test_ranking_safety_prefere_la_probabilite_de_gain(db, prices):
    par_ev, _ = scan(db, prices, Rarity.MIL_SPEC, ranking=Ranking.EV,
                     screen=ScreenConfig(min_roi=-99, min_ev_profit=-999))
    par_surete, _ = scan(db, prices, Rarity.MIL_SPEC, ranking=Ranking.SAFETY,
                         screen=ScreenConfig(min_roi=-99, min_ev_profit=-999))
    assert len(par_ev) == len(par_surete) == 2
    assert "Rentable" in par_ev[0].label
    assert "Rentable" in par_surete[0].label
