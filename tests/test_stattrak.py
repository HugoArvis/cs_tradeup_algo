"""Tests des contrats StatTrak.

La plomberie `stattrak` traversait deja tout le code, mais rien ne l'activait :
`build_options` demandait les prix avec `False` en dur et `required_market_names`
ne generait que des noms normaux. Un `--stattrak` mal cable n'aurait pas plante,
il aurait silencieusement cote les mauvais objets -- d'ou ces tests.
"""

from __future__ import annotations

import pytest

from tradeupfinder.db import SkinDatabase
from tradeupfinder.generator import Recipe, build_options, iter_recipes, optimize_recipe
from tradeupfinder.models import Collection, Rarity, Skin
from tradeupfinder.pricing.repository import StaticPricer
from tradeupfinder.scan import required_market_names


@pytest.fixture(scope="module")
def db():
    return SkinDatabase.load()


def collection_mixte() -> Collection:
    """Collection dont SEULS certains skins ont une variante StatTrak.

    Ce cas n'existe pas dans la base actuelle -- toute collection qui a des
    StatTrak en a partout. Il est construit ici parce que le jour ou il
    apparaitra, la probabilite de chaque sortie changera, et rien d'autre ne le
    signalerait.
    """
    def s(key, rarity, st):
        return Skin(key=key, name=key, collection_id="mix", rarity=rarity,
                    min_float=0.0, max_float=1.0, stattrak=st)

    return Collection(id="mix", name="Mixte", skins=(
        s("entree_st", Rarity.MIL_SPEC, True),
        s("entree_nonst", Rarity.MIL_SPEC, False),
        s("sortie_st_a", Rarity.RESTRICTED, True),
        s("sortie_st_b", Rarity.RESTRICTED, True),
        s("sortie_nonst", Rarity.RESTRICTED, False),
    ))


# --- Filtrage des entrees et des sorties -------------------------------------


def test_un_contrat_stattrak_exclut_les_skins_sans_variante():
    col = collection_mixte()
    assert len(col.inputs_for_rarity(Rarity.MIL_SPEC)) == 2
    assert len(col.inputs_for_rarity(Rarity.MIL_SPEC, True)) == 1
    assert len(col.outcomes_for_input_rarity(Rarity.MIL_SPEC)) == 3
    assert len(col.outcomes_for_input_rarity(Rarity.MIL_SPEC, True)) == 2


def test_retirer_des_sorties_redistribue_la_probabilite():
    """Le point qui coute cher si on l'oublie.

    P(sortie) = n_C / somme(n_C' x k_C'). Retirer des issues ne les met pas a
    zero en laissant les autres inchangees : toute la masse est redistribuee.
    Garder les skins non-StatTrak au denominateur donnerait 33 % la ou la
    reponse est 50 %.
    """
    from tradeupfinder.ev import outcome_probabilities

    col = collection_mixte()
    normales = outcome_probabilities(
        {"mix": 10}, {"mix": col.outcomes_for_input_rarity(Rarity.MIL_SPEC)}
    )
    stattrak = outcome_probabilities(
        {"mix": 10}, {"mix": col.outcomes_for_input_rarity(Rarity.MIL_SPEC, True)}
    )
    assert all(p == pytest.approx(1 / 3) for p in normales.values())
    assert all(p == pytest.approx(1 / 2) for p in stattrak.values())
    assert sum(stattrak.values()) == pytest.approx(1.0)


def test_les_options_dachat_sont_filtrees_et_cotees_en_stattrak():
    col = collection_mixte()
    prix = {}
    for skin in col.by_rarity(Rarity.MIL_SPEC):
        for w in skin.available_wears():
            prix[skin.market_hash_name(w)] = 1.0
            if skin.stattrak:
                prix[skin.market_hash_name(w, True)] = 3.0  # le ST coute plus cher

    pricer = StaticPricer(prix)
    normales = build_options(col, Rarity.MIL_SPEC, pricer)
    stattrak = build_options(col, Rarity.MIL_SPEC, pricer, stattrak=True)

    assert {o.skin.key for o in normales} == {"entree_st", "entree_nonst"}
    assert {o.skin.key for o in stattrak} == {"entree_st"}
    # Le prix retenu doit etre celui de la variante StatTrak, pas du skin nu.
    assert all(o.unit_cost == 3.0 for o in stattrak)


# --- Noms de marche ----------------------------------------------------------


def test_les_noms_a_coter_sont_ceux_des_variantes_stattrak(db):
    """StatTrak(tm) AK-47 ... est un AUTRE objet de marche, avec son prix."""
    col = db.find_collection("The Recoil Collection")
    normaux = required_market_names(db, [col], Rarity.MIL_SPEC)
    stattrak = required_market_names(db, [col], Rarity.MIL_SPEC, True)

    assert normaux and stattrak
    assert not any(n.startswith("StatTrak") for n in normaux)
    assert all(n.startswith("StatTrak") for n in stattrak)
    assert not (set(normaux) & set(stattrak))


# --- Collections exploitables ------------------------------------------------


def test_aucun_stattrak_sous_le_mil_spec(db):
    """Les caisses ne produisent pas de StatTrak a ces raretes."""
    for rarity in (Rarity.CONSUMER, Rarity.INDUSTRIAL):
        assert db.tradeable_collections(rarity, True) == []


def test_le_stattrak_reduit_le_champ_sans_lannuler(db):
    normales = db.tradeable_collections(Rarity.MIL_SPEC)
    stattrak = db.tradeable_collections(Rarity.MIL_SPEC, True)
    assert 0 < len(stattrak) < len(normales)
    assert all(c in normales for c in stattrak)


def test_les_recettes_stattrak_ne_piochent_que_dans_les_bonnes_collections(db):
    recettes = list(iter_recipes(db, Rarity.MIL_SPEC, max_collections=1,
                                 stattrak=True))
    assert recettes
    utilisables = {c.id for c in db.tradeable_collections(Rarity.MIL_SPEC, True)}
    for r in recettes:
        assert set(r.collection_ids) <= utilisables


# --- Bout en bout ------------------------------------------------------------


def test_un_contrat_stattrak_se_calcule_de_bout_en_bout(db):
    col = db.find_collection("The Recoil Collection")
    rarity = Rarity.MIL_SPEC
    prix = {n: 1.0 for n in required_market_names(db, [col], rarity, True)}
    pricer = StaticPricer(prix)

    resultat = optimize_recipe(
        Recipe(counts=((col.id, 10),), rarity=rarity), db, pricer, stattrak=True
    )
    assert resultat is not None
    assert resultat.stattrak
    assert sum(o.probability for o in resultat.outcomes) == pytest.approx(1.0)
    # La liste d'achat doit nommer des objets StatTrak, sinon on achete a cote.
    for nom, _, _, _ in resultat.shopping_list():
        assert nom.startswith("StatTrak")
    for o in resultat.outcomes:
        assert o.skin.stattrak


def test_sans_prix_stattrak_le_contrat_nest_pas_calculable(db):
    """Garde-fou : coter les skins nus ne doit PAS suffire.

    Si ce test passait au vert avec des prix normaux, cela voudrait dire que le
    mode StatTrak valorise les mauvais objets.
    """
    col = db.find_collection("The Recoil Collection")
    rarity = Rarity.MIL_SPEC
    prix = {n: 1.0 for n in required_market_names(db, [col], rarity)}  # nus
    resultat = optimize_recipe(
        Recipe(counts=((col.id, 10),), rarity=rarity), db, StaticPricer(prix),
        stattrak=True,
    )
    assert resultat is None
