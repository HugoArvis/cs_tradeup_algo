"""Tests du contrat vers un gold : cinq Covert d'une caisse, un couteau.

Le piege de ce contrat, et il a deja fait mentir un calcul : les golds d'une
meme caisse n'ont PAS le meme range de float. Le Kukri Fade va de 0 a 0.08, le
Safari Mesh de 0.06 a 0.80. Pour une meme moyenne d'entree, le premier sort
Factory New a 148 EUR et le second Field-Tested a 37. Supposer un range commun
faisait annoncer +8.5 % un contrat qui perd 58 %.
"""

from __future__ import annotations

import pytest

from tradeupfinder.db import SkinDatabase
from tradeupfinder.gold import (
    GOLD_INPUT_COUNT,
    Crate,
    best_for_crate,
    evaluate_crate,
    load_crates,
    scan_crates,
)
from tradeupfinder.models import Rarity, Skin, Wear
from tradeupfinder.pricing.repository import StaticPricer


@pytest.fixture(scope="module")
def db():
    return SkinDatabase.load()


@pytest.fixture(scope="module")
def kilowatt(db):
    return next(c for c in db.crates() if "Kilowatt" in c.name)


# --- Structure ---------------------------------------------------------------


def test_le_contrat_prend_cinq_entrees():
    """Et non dix, comme les contrats d'armes."""
    assert GOLD_INPUT_COUNT == 5


def test_les_caisses_ont_entrees_et_golds(db):
    caisses = db.crates()
    assert caisses, "aucune caisse chargee"
    for c in caisses:
        assert c.inputs and c.golds
        assert c.gold_probability == pytest.approx(1 / len(c.golds))


def test_les_golds_ont_des_ranges_differents(kilowatt):
    """Le fait qui invalide tout calcul supposant un range commun."""
    ranges = {(g.min_float, g.max_float) for g in kilowatt.golds}
    assert len(ranges) > 1, "les golds devraient avoir des ranges heterogenes"


def test_une_caisse_sans_gold_est_ignoree():
    assert load_crates({"crates": [
        {"id": "x", "name": "X", "inputs": ["A"], "golds": []},
        {"id": "y", "name": "Y", "inputs": [], "golds": [
            {"key": "k", "name": "K", "min_float": 0.0, "max_float": 1.0}]},
    ]}) == []


# --- Le coeur : un palier par gold -------------------------------------------


def test_chaque_gold_recoit_son_propre_palier():
    """Trois ranges, trois paliers, pour la MEME moyenne d'entree."""
    golds = [
        Skin("f", "Fade", "c", Rarity.COVERT, 0.00, 0.08),
        Skin("s", "Safari", "c", Rarity.COVERT, 0.06, 0.80),
        Skin("l", "Slaughter", "c", Rarity.COVERT, 0.01, 0.26),
    ]
    crate = Crate(id="c", name="C", inputs=("Entree",), golds=tuple(golds))
    entree = Skin("e", "Entree", "col", Rarity.COVERT, 0.0, 1.0)

    prix = {g.market_hash_name(w): 100.0 for g in golds
            for w in g.available_wears()}
    plan = evaluate_crate(crate, entree, Wear.FIELD_TESTED, 10.0,
                          StaticPricer(prix))
    assert plan is not None
    paliers = {o.skin.name: o.wear for o in plan.outcomes}
    assert paliers["Fade"] is Wear.FACTORY_NEW
    assert paliers["Safari"] is Wear.FIELD_TESTED
    assert paliers["Slaughter"] is Wear.MINIMAL_WEAR


def test_le_cout_est_cinq_fois_lunite():
    crate = Crate(id="c", name="C", inputs=("E",), golds=(
        Skin("g", "G", "c", Rarity.COVERT, 0.0, 1.0),))
    entree = Skin("e", "E", "col", Rarity.COVERT, 0.0, 1.0)
    prix = {"G (Field-Tested)": 500.0}
    plan = evaluate_crate(crate, entree, Wear.FIELD_TESTED, 31.0,
                          StaticPricer(prix))
    assert plan.cost == pytest.approx(5 * 31.0)


def test_la_probabilite_est_uniforme_et_somme_a_un(kilowatt, db):
    prix = {g.market_hash_name(w): 100.0 for g in kilowatt.golds
            for w in g.available_wears()}
    for nom in kilowatt.inputs:
        skin = db.find(nom)
        if skin:
            prix.update({skin.market_hash_name(w): 30.0
                         for w in skin.available_wears()})
    plan = best_for_crate(kilowatt, db, StaticPricer(prix))
    assert plan is not None
    assert sum(o.probability for o in plan.outcomes) == pytest.approx(1.0)
    assert len({round(o.probability, 9) for o in plan.outcomes}) == 1


# --- Rentabilite -------------------------------------------------------------


def test_un_contrat_perdant_est_rapporte_comme_tel(kilowatt, db):
    """Golds sans valeur, entrees cheres : le verdict doit etre negatif."""
    prix = {g.market_hash_name(w): 5.0 for g in kilowatt.golds
            for w in g.available_wears()}
    for nom in kilowatt.inputs:
        skin = db.find(nom)
        if skin:
            prix.update({skin.market_hash_name(w): 50.0
                         for w in skin.available_wears()})
    plan = best_for_crate(kilowatt, db, StaticPricer(prix))
    assert plan.ev_profit < 0
    assert plan.win_probability == 0.0


def test_un_gold_sans_prix_est_signale(kilowatt, db):
    prix = {}
    for nom in kilowatt.inputs:
        skin = db.find(nom)
        if skin:
            prix.update({skin.market_hash_name(w): 30.0
                         for w in skin.available_wears()})
    plan = best_for_crate(kilowatt, db, StaticPricer(prix))
    assert plan is not None
    assert plan.unpriced_probability == pytest.approx(1.0)
    assert "sans prix connu" in plan.report()


def test_le_scan_classe_par_profit(db):
    caisses = db.crates()[:5]
    prix = {}
    for i, c in enumerate(caisses):
        for g in c.golds:
            for w in g.available_wears():
                prix[g.market_hash_name(w)] = 100.0 + 50 * i
        for nom in c.inputs:
            skin = db.find(nom)
            if skin:
                for w in skin.available_wears():
                    prix[skin.market_hash_name(w)] = 20.0
    plans = scan_crates(caisses, db, StaticPricer(prix))
    profits = [p.ev_profit for p in plans]
    assert profits == sorted(profits, reverse=True)
