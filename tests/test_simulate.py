"""Tests de la simulation et des verifications de coherence."""

from __future__ import annotations

import pytest

from tradeup.ev import InputItem, Outcome, TradeUpResult
from tradeup.models import Rarity, Skin, Wear
from tradeup.simulate import contracts_for_significance, sanity_check, simulate_series


def make_skin(key, lo=0.0, hi=1.0):
    return Skin(key=key, name=key, collection_id="c", rarity=Rarity.RESTRICTED,
                min_float=lo, max_float=hi)


def make_result(outcomes, cost=20.0, inputs=None):
    entree = Skin(key="in", name="in", collection_id="c", rarity=Rarity.MIL_SPEC,
                  min_float=0.0, max_float=1.0)
    inputs = inputs or tuple(InputItem(entree, 0.05, cost / 10) for _ in range(10))
    return TradeUpResult(
        outcomes=tuple(outcomes),
        inputs=inputs,
        cost=cost,
        avg_input_float=0.05,
        stattrak=False,
        unpriced_probability=0.0,
    )


def outcome(name, proba, net, float_value=0.05, wear=Wear.FACTORY_NEW, skin=None):
    return Outcome(skin=skin or make_skin(name), float_value=float_value, wear=wear,
                   probability=proba, net_value=net, priced=True)


# --- Simulation --------------------------------------------------------------


def test_la_moyenne_simulee_converge_vers_lev():
    r = make_result([outcome("gagne", 0.5, 100.0), outcome("perd", 0.5, 10.0)])
    # EV nette 55, cout 20 -> profit espere 35 par contrat.
    assert r.ev_profit == pytest.approx(35.0)

    s = simulate_series(r, n_contracts=50, trials=4000, seed=7)
    assert s.mean == pytest.approx(50 * 35.0, rel=0.05)


def test_la_serie_reduit_le_risque_de_finir_perdant():
    """Le coeur du raisonnement en esperance : la duree fait le travail."""
    r = make_result([outcome("gagne", 0.4, 80.0), outcome("perd", 0.6, 5.0)])
    assert r.ev_profit > 0
    # Sur un contrat, on perd 6 fois sur 10.
    assert r.profit_probability == pytest.approx(0.4)

    court = simulate_series(r, n_contracts=1, trials=4000, seed=3)
    long = simulate_series(r, n_contracts=40, trials=4000, seed=3)
    assert court.loss_probability > 0.5
    assert long.loss_probability < court.loss_probability


def test_simulation_reproductible_avec_une_graine():
    r = make_result([outcome("a", 0.5, 60.0), outcome("b", 0.5, 10.0)])
    a = simulate_series(r, n_contracts=10, trials=500, seed=42)
    b = simulate_series(r, n_contracts=10, trials=500, seed=42)
    c = simulate_series(r, n_contracts=10, trials=500, seed=43)
    assert a == b
    assert a.mean != c.mean


def test_les_quantiles_sont_ordonnes():
    r = make_result([outcome("a", 0.5, 60.0), outcome("b", 0.5, 10.0)])
    s = simulate_series(r, n_contracts=20, trials=2000, seed=1)
    assert s.worst <= s.p05 <= s.p25 <= s.median <= s.p75 <= s.p95 <= s.best


def test_simulation_refuse_les_entrees_absurdes():
    r = make_result([outcome("a", 1.0, 30.0)])
    with pytest.raises(ValueError, match="au moins un contrat"):
        simulate_series(r, n_contracts=0)
    with pytest.raises(ValueError, match="aucune sortie"):
        simulate_series(make_result([]), n_contracts=5)


# --- Horizon de detection ----------------------------------------------------


def test_un_avantage_faible_exige_plus_de_contrats():
    fort = make_result([outcome("a", 0.5, 100.0), outcome("b", 0.5, 90.0)], cost=20.0)
    faible = make_result([outcome("a", 0.5, 100.0), outcome("b", 0.5, 0.0)], cost=49.0)

    n_fort = contracts_for_significance(fort)
    n_faible = contracts_for_significance(faible)
    assert n_fort is not None and n_faible is not None
    # Meme EV a peu pres, mais une variance bien plus grande d'un cote.
    assert n_faible > n_fort


def test_pas_davantage_a_detecter_si_lev_est_negative():
    perdant = make_result([outcome("a", 1.0, 5.0)], cost=20.0)
    assert perdant.ev_profit < 0
    assert contracts_for_significance(perdant) is None


# --- Verifications de coherence ----------------------------------------------


def test_sanity_check_valide_un_resultat_correct():
    r = make_result([outcome("a", 0.5, 60.0), outcome("b", 0.5, 10.0)])
    assert sanity_check(r) == []


def test_sanity_check_detecte_des_probabilites_incoherentes():
    r = make_result([outcome("a", 0.5, 60.0), outcome("b", 0.3, 10.0)])
    problems = sanity_check(r)
    assert any("somment" in p for p in problems)


def test_sanity_check_detecte_un_float_hors_range():
    # Skin plafonne a 0.5, mais on pretend en sortir un a 0.9.
    borne = make_skin("borne", lo=0.0, hi=0.5)
    r = make_result([
        outcome("borne", 1.0, 60.0, float_value=0.9, wear=Wear.BATTLE_SCARRED,
                skin=borne),
    ])
    problems = sanity_check(r)
    assert any("hors de son range" in p for p in problems)


def test_sanity_check_detecte_un_cout_incoherent():
    entree = Skin(key="in", name="in", collection_id="c", rarity=Rarity.MIL_SPEC,
                  min_float=0.0, max_float=1.0)
    r = make_result(
        [outcome("a", 1.0, 60.0)],
        cost=999.0,  # ne correspond pas aux entrees ci-dessous
        inputs=tuple(InputItem(entree, 0.05, 1.0) for _ in range(10)),
    )
    problems = sanity_check(r)
    assert any("cout incoherent" in p for p in problems)
