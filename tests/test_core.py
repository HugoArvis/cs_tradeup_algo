"""Tests du noyau : float, probabilites, frais, EV, selection d'entrees."""

from __future__ import annotations

import math

import pytest

from tradeup.ev import InputItem, evaluate, outcome_probabilities
from tradeup.fees import steam_net_proceeds
from tradeup.generator import InputOption, cheapest_selection
from tradeup.models import Rarity, Skin, Wear
from tradeup.pricing.base import parse_money, parse_volume
from tradeup.pricing.repository import StaticPricer
from tradeup.wear import output_float, wear_breakpoints, wear_of


def make_skin(key, rarity=Rarity.MIL_SPEC, lo=0.0, hi=1.0, collection="col_a"):
    return Skin(
        key=key,
        name=key.replace("_", " ").title(),
        collection_id=collection,
        rarity=rarity,
        min_float=lo,
        max_float=hi,
    )


# --- Float -------------------------------------------------------------------


def test_output_float_remappe_sur_le_range_de_la_cible():
    # Range complet : le float de sortie egale la moyenne d'entree.
    full = make_skin("full", lo=0.0, hi=1.0)
    assert output_float(0.25, full) == pytest.approx(0.25)

    # Range restreint : 0.00-0.50 -> une moyenne de 0.25 donne 0.125.
    limited = make_skin("limited", lo=0.0, hi=0.50)
    assert output_float(0.25, limited) == pytest.approx(0.125)

    # Range decale : min > 0 impose un plancher.
    shifted = make_skin("shifted", lo=0.10, hi=0.60)
    assert output_float(0.0, shifted) == pytest.approx(0.10)
    assert output_float(1.0, shifted) == pytest.approx(0.60)
    assert output_float(0.5, shifted) == pytest.approx(0.35)


def test_un_skin_a_min_float_eleve_ne_peut_jamais_sortir_factory_new():
    # Piege classique : viser du FN sur un skin dont le min_float est 0.10.
    skin = make_skin("high_min", lo=0.10, hi=0.80)
    assert output_float(0.0, skin) == pytest.approx(0.10)
    assert wear_of(output_float(0.0, skin)) is Wear.MINIMAL_WEAR
    assert Wear.FACTORY_NEW not in skin.available_wears()


def test_wear_of_aux_frontieres():
    assert wear_of(0.0) is Wear.FACTORY_NEW
    assert wear_of(0.0699) is Wear.FACTORY_NEW
    assert wear_of(0.07) is Wear.MINIMAL_WEAR  # borne basse inclusive
    assert wear_of(0.1499) is Wear.MINIMAL_WEAR
    assert wear_of(0.15) is Wear.FIELD_TESTED
    assert wear_of(0.38) is Wear.WELL_WORN
    assert wear_of(0.45) is Wear.BATTLE_SCARRED
    assert wear_of(1.0) is Wear.BATTLE_SCARRED


def test_wear_breakpoints_couvre_les_frontieres_atteignables():
    skin = make_skin("s", lo=0.0, hi=1.0)
    pts = wear_breakpoints([skin])
    # Range complet : les 4 frontieres internes tombent sur leurs valeurs brutes.
    for boundary in (0.07, 0.15, 0.38, 0.45):
        assert any(math.isclose(p, boundary) for p in pts), boundary
    assert pts[0] == 0.0 and pts[-1] == 1.0


# --- Distribution des sorties ------------------------------------------------


def test_mono_collection_probabilites_uniformes():
    outcomes = [make_skin(f"out{i}", Rarity.RESTRICTED) for i in range(5)]
    probs = outcome_probabilities({"col_a": 10}, {"col_a": outcomes})
    assert len(probs) == 5
    for p in probs.values():
        assert p == pytest.approx(0.20)
    assert sum(probs.values()) == pytest.approx(1.0)


def test_melange_penalise_la_collection_a_peu_de_sorties():
    """5+5 entrees, mais 2 sorties d'un cote et 10 de l'autre.

    Denominateur = 5*2 + 5*10 = 60. Chaque sortie vaut 5/60, donc la petite
    collection ne pese que 2*5/60 = 16.7 % malgre la moitie des entrees.
    """
    petite = [make_skin(f"p{i}", Rarity.RESTRICTED, collection="col_a") for i in range(2)]
    grande = [make_skin(f"g{i}", Rarity.RESTRICTED, collection="col_b") for i in range(10)]

    probs = outcome_probabilities(
        {"col_a": 5, "col_b": 5}, {"col_a": petite, "col_b": grande}
    )
    assert sum(probs.values()) == pytest.approx(1.0)
    assert sum(probs[s.key] for s in petite) == pytest.approx(2 / 12)
    assert sum(probs[s.key] for s in grande) == pytest.approx(10 / 12)
    assert probs[petite[0].key] == pytest.approx(probs[grande[0].key])


def test_collection_sans_sortie_ne_contribue_rien():
    outcomes = [make_skin("o1", Rarity.RESTRICTED), make_skin("o2", Rarity.RESTRICTED)]
    probs = outcome_probabilities(
        {"col_a": 7, "col_vide": 3}, {"col_a": outcomes, "col_vide": []}
    )
    assert sum(probs.values()) == pytest.approx(1.0)
    assert len(probs) == 2  # les 3 entrees "col_vide" sont du cout pur


def test_denominateur_nul_renvoie_une_distribution_vide():
    assert outcome_probabilities({"col_a": 10}, {"col_a": []}) == {}


# --- Frais -------------------------------------------------------------------


def test_steam_net_proceeds_retire_environ_15_pourcent():
    assert steam_net_proceeds(11.5) == pytest.approx(10.0, abs=0.02)
    assert steam_net_proceeds(1.15) == pytest.approx(1.0, abs=0.02)
    # Le net est toujours strictement inferieur au prix acheteur.
    for p in (0.03, 0.50, 2.35, 17.99, 250.0):
        assert 0 < steam_net_proceeds(p) < p


def test_parse_money_multi_locale():
    assert parse_money("1,23€") == pytest.approx(1.23)
    assert parse_money("$1.23") == pytest.approx(1.23)
    assert parse_money("1.234,56 €") == pytest.approx(1234.56)
    assert parse_money("1,234.56") == pytest.approx(1234.56)
    assert parse_money("12,50 pуб.") == pytest.approx(12.50)
    assert parse_money(None) is None
    assert parse_money("") is None
    assert parse_volume("1,234") == 1234
    assert parse_volume(None) is None


# --- EV de bout en bout ------------------------------------------------------


def test_evaluate_ev_variance_et_probabilite_de_profit():
    entree = make_skin("in", Rarity.MIL_SPEC, lo=0.0, hi=1.0)
    gagnant = make_skin("win", Rarity.RESTRICTED, lo=0.0, hi=1.0)
    perdant = make_skin("lose", Rarity.RESTRICTED, lo=0.0, hi=1.0)

    prices = StaticPricer(
        {
            gagnant.market_hash_name(Wear.FACTORY_NEW): 100.0,
            perdant.market_hash_name(Wear.FACTORY_NEW): 10.0,
        },
        sell_fees="csfloat",  # 2 % : arithmetique simple et verifiable
    )
    items = [InputItem(entree, 0.01, 2.0) for _ in range(10)]

    r = evaluate(items, {"col_a": [gagnant, perdant]}, prices)

    assert r.cost == pytest.approx(20.0)
    assert r.avg_input_float == pytest.approx(0.01)
    assert r.unpriced_probability == 0.0
    # 50 % a 98.0 net, 50 % a 9.8 net.
    assert r.ev_net == pytest.approx(53.9)
    assert r.ev_profit == pytest.approx(33.9)
    assert r.stdev == pytest.approx(44.1)
    assert r.profit_probability == pytest.approx(0.5)  # seul le gagnant couvre 20
    assert r.worst_case == pytest.approx(9.8)
    assert r.best_case == pytest.approx(98.0)
    # CVaR sur la moitie basse : on ne touche que le perdant.
    assert r.value_at_risk(0.5) == pytest.approx(9.8 - 20.0)


def test_evaluate_signale_les_prix_manquants():
    entree = make_skin("in", Rarity.MIL_SPEC)
    connu = make_skin("connu", Rarity.RESTRICTED)
    inconnu = make_skin("inconnu", Rarity.RESTRICTED)
    prices = StaticPricer({connu.market_hash_name(Wear.FACTORY_NEW): 50.0})
    items = [InputItem(entree, 0.01, 1.0) for _ in range(10)]

    r = evaluate(items, {"col_a": [connu, inconnu]}, prices)
    assert r.unpriced_probability == pytest.approx(0.5)
    assert any(not o.priced for o in r.outcomes)

    with pytest.raises(LookupError):
        evaluate(items, {"col_a": [connu, inconnu]}, prices, treat_missing_as_zero=False)


def test_evaluate_refuse_un_lot_invalide():
    entree = make_skin("in", Rarity.MIL_SPEC)
    prices = StaticPricer({})
    with pytest.raises(ValueError, match="10 entrees"):
        evaluate([InputItem(entree, 0.1, 1.0)] * 9, {"col_a": []}, prices)

    autre_rarete = make_skin("autre", Rarity.RESTRICTED)
    lot = [InputItem(entree, 0.1, 1.0)] * 9 + [InputItem(autre_rarete, 0.1, 1.0)]
    with pytest.raises(ValueError, match="raretes melangees"):
        evaluate(lot, {"col_a": []}, prices)


# --- Selection des entrees ---------------------------------------------------


def test_cheapest_selection_respecte_le_budget_de_float():
    skin = make_skin("s")
    cher_bas_float = InputOption(skin, Wear.FACTORY_NEW, unit_cost=5.0, float_value=0.02)
    pas_cher_haut_float = InputOption(skin, Wear.FIELD_TESTED, unit_cost=1.0, float_value=0.20)

    options = [cher_bas_float, pas_cher_haut_float]

    # Budget genereux : que du bon marche.
    sel = cheapest_selection(options, 10, float_budget=10 * 0.20)
    assert sel is not None
    assert sum(o.unit_cost for o in sel) == pytest.approx(10.0)

    # Budget serre : force le passage aux entrees a bas float.
    sel = cheapest_selection(options, 10, float_budget=10 * 0.02)
    assert sel is not None
    assert all(o is cher_bas_float for o in sel)

    # Budget inatteignable.
    assert cheapest_selection(options, 10, float_budget=10 * 0.001) is None


def test_cheapest_selection_melange_pour_atteindre_la_moyenne():
    skin = make_skin("s")
    bas = InputOption(skin, Wear.FACTORY_NEW, unit_cost=10.0, float_value=0.05)
    haut = InputOption(skin, Wear.FIELD_TESTED, unit_cost=1.0, float_value=0.25)

    # Moyenne visee 0.15 -> 5 de chaque est optimal (cout 55).
    sel = cheapest_selection([bas, haut], 10, float_budget=10 * 0.15)
    assert sel is not None
    assert sum(o.float_value for o in sel) <= 10 * 0.15 + 1e-9
    assert sum(o.unit_cost for o in sel) == pytest.approx(55.0)


# --- Cache et devises --------------------------------------------------------


def test_le_cache_ne_melange_pas_les_devises(tmp_path):
    """Bug attrape en production : 16.42 EUR renvoye pour une requete USD.

    Le cache indexait par (nom, source) sans la devise. Changer --currency
    rendait donc les anciens montants, et un calcul achat-Steam/revente-CSFloat
    comparait silencieusement des euros a des dollars.
    """
    from tradeup.pricing.base import Quote
    from tradeup.pricing.cache import QuoteCache

    cache = QuoteCache(tmp_path / "c.db", ttl_seconds=3600)
    cache.put(Quote("AK-47 | Redline (Field-Tested)", "steam", 36.69, 36.38, 125,
                    currency="EUR"))

    en_eur = cache.get("AK-47 | Redline (Field-Tested)", "steam", currency="EUR")
    assert en_eur is not None and en_eur.lowest_price == 36.69

    # La meme cotation ne doit PAS repondre a une demande en dollars.
    assert cache.get("AK-47 | Redline (Field-Tested)", "steam", currency="USD") is None

    # Sans precision de devise, l'ancien comportement reste disponible.
    assert cache.get("AK-47 | Redline (Field-Tested)", "steam") is not None
    cache.close()


def test_deux_devises_coexistent_dans_le_cache(tmp_path):
    from tradeup.pricing.base import Quote
    from tradeup.pricing.cache import QuoteCache

    cache = QuoteCache(tmp_path / "c.db", ttl_seconds=3600)
    cache.put(Quote("X", "steam", 10.0, 10.0, 5, currency="EUR"))
    cache.put(Quote("X", "steam", 11.5, 11.5, 5, currency="USD"))

    assert cache.get("X", "steam", currency="EUR").lowest_price == 10.0
    assert cache.get("X", "steam", currency="USD").lowest_price == 11.5
    cache.close()
