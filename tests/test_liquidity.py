"""Tests de la capacite d'execution."""

from __future__ import annotations

import pytest

from tradeup.ev import InputItem, Outcome, TradeUpResult
from tradeup.liquidity import execution_capacity
from tradeup.models import Rarity, Skin, Wear


def skin(key, rarity=Rarity.MIL_SPEC):
    return Skin(key=key, name=key, collection_id="c", rarity=rarity,
                min_float=0.0, max_float=1.0)


ENTREE = skin("entree")
SORTIE = skin("sortie", Rarity.RESTRICTED)


class Volumes:
    """PriceLookup minimal : seuls les volumes comptent ici."""

    def __init__(self, volumes: dict[str, int | None]):
        self.volumes = volumes

    def sell_net(self, s, w, st=False):
        return 10.0

    def buy_cost(self, s, w, st=False):
        return 1.0

    def volume(self, s, w, st=False):
        return self.volumes.get(s.market_hash_name(w, st))


def make_result(inputs=None, outcomes=None, cost=10.0):
    inputs = inputs or tuple(InputItem(ENTREE, 0.05, 1.0) for _ in range(10))
    outcomes = outcomes or (
        Outcome(skin=SORTIE, float_value=0.05, wear=Wear.FACTORY_NEW,
                probability=1.0, net_value=15.0, priced=True),
    )
    return TradeUpResult(outcomes=outcomes, inputs=inputs, cost=cost,
                         avg_input_float=0.05, stattrak=False,
                         unpriced_probability=0.0)


def test_le_volume_limite_le_rythme_de_repetition():
    """200 ventes/jour, 10 unites par contrat, 20 % du flux -> 4 contrats/jour."""
    prices = Volumes({
        "entree (Factory New)": 200,
        "sortie (Factory New)": 10_000,  # revente non contraignante
    })
    cap = execution_capacity(make_result(), prices, participation=0.2)
    assert cap.contracts_per_day == pytest.approx(4.0)
    assert cap.binding is not None
    assert cap.binding.side == "achat"
    assert cap.days_for(40) == pytest.approx(10.0)


def test_la_revente_peut_etre_le_goulot():
    prices = Volumes({
        "entree (Factory New)": 100_000,
        "sortie (Factory New)": 5,  # tres illiquide a la revente
    })
    cap = execution_capacity(make_result(), prices, participation=0.2)
    assert cap.binding is not None
    assert cap.binding.side == "revente"
    assert cap.contracts_per_day == pytest.approx(1.0)


def test_une_sortie_rare_pese_moins_sur_la_revente():
    """On ne revend une sortie qu'a hauteur de sa probabilite."""
    rare = Outcome(skin=SORTIE, float_value=0.05, wear=Wear.FACTORY_NEW,
                   probability=0.1, net_value=100.0, priced=True)
    frequent = Outcome(skin=skin("frequent", Rarity.RESTRICTED), float_value=0.05,
                       wear=Wear.FACTORY_NEW, probability=0.9, net_value=5.0,
                       priced=True)
    prices = Volumes({
        "entree (Factory New)": 100_000,
        "sortie (Factory New)": 10,
        "frequent (Factory New)": 10,
    })
    cap = execution_capacity(make_result(outcomes=(rare, frequent)), prices,
                             participation=0.2)
    # A volume egal, c'est la sortie FREQUENTE qui contraint.
    assert cap.binding is not None
    assert cap.binding.name == "frequent (Factory New)"


def test_volume_inconnu_signale_et_pas_devine():
    prices = Volumes({"entree (Factory New)": None, "sortie (Factory New)": 100})
    cap = execution_capacity(make_result(), prices)
    assert "entree (Factory New)" in cap.unknown_volume
    # Le calcul continue sur ce qui est connu, sans inventer le reste.
    assert cap.contracts_per_day is not None


def test_aucun_volume_connu_renvoie_une_capacite_indeterminee():
    cap = execution_capacity(make_result(), Volumes({}))
    assert cap.contracts_per_day is None
    assert cap.days_for(10) is None
    assert "INCONNUE" in cap.report()


def test_participation_plus_prudente_reduit_le_rythme():
    prices = Volumes({"entree (Factory New)": 200, "sortie (Factory New)": 10_000})
    large = execution_capacity(make_result(), prices, participation=0.5)
    prudent = execution_capacity(make_result(), prices, participation=0.05)
    assert large.contracts_per_day > prudent.contracts_per_day


def test_le_rapport_mentionne_le_goulot_et_lhorizon():
    prices = Volumes({"entree (Factory New)": 200, "sortie (Factory New)": 10_000})
    texte = execution_capacity(make_result(), prices).report(n_contracts=40)
    assert "contrats/jour" in texte
    assert "goulot" in texte
    assert "40 contrats" in texte
