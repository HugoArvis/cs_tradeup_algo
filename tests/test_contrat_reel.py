"""Le moteur confronte a un contrat REELLEMENT execute.

Tous les autres tests verifient que le code fait ce que le modele dit. Celui-ci
verifie que le MODELE dit ce que le jeu fait -- la seule question qui compte
vraiment, et la seule qu'aucune simulation ne peut trancher.

Donnees : contrat passe le 05/08/2026 sur The Bank Collection, dix entrees
Industrial dont les floats et les prix sont ceux reellement payes. Le jeu a
produit un Desert Eagle | Meteorite (Minimal Wear) a float 0.07221613824367523,
toujours identifiable dans l'inventaire.

Les valeurs sont recopiees ici plutot que lues dans `data/journal.db` : ce
journal est une donnee utilisateur, il n'est pas versionne, et un test qui
disparait avec le poste de son auteur ne protege personne.

PRECISION DES DONNEES : les floats d'entree sont enregistres a cinq decimales.
L'ecart resultant sur le float de sortie est de l'ordre de 1e-5 -- environ
0.015 % de la largeur du palier Minimal Wear. Sans incidence sur le palier
predit, donc sans incidence sur une decision.
"""

from __future__ import annotations

import pytest

from tradeupfinder.db import SkinDatabase
from tradeupfinder.ev import InputItem, evaluate
from tradeupfinder.models import Rarity, Wear
from tradeupfinder.pricing.repository import StaticPricer
from tradeupfinder.wear import average_normalized, output_float, wear_of

#: (nom du skin, float paye, prix paye)
ENTREES_REELLES = [
    ("Nova | Caged Steel", 0.07500, 0.08),
    ("Nova | Caged Steel", 0.07850, 0.08),
    ("Nova | Caged Steel", 0.08010, 0.08),
    ("Nova | Caged Steel", 0.08350, 0.07),
    ("Nova | Caged Steel", 0.08350, 0.07),
    ("Nova | Caged Steel", 0.08450, 0.07),
    ("Nova | Caged Steel", 0.08730, 0.07),
    ("Nova | Caged Steel", 0.08910, 0.07),
    ("Nova | Caged Steel", 0.10760, 0.07),
    ("UMP-45 | Carbon Fiber", 0.01990, 0.10),
]

SORTIE_OBTENUE = "Desert Eagle | Meteorite"
FLOAT_OBTENU = 0.07221613824367523

#: Tolerance justifiee par l'arrondi des floats enregistres (voir en-tete).
TOLERANCE = 1e-4


@pytest.fixture(scope="module")
def db():
    return SkinDatabase.load()


@pytest.fixture(scope="module")
def entrees(db):
    return [(db.find(nom), flt, prix) for nom, flt, prix in ENTREES_REELLES]


def test_les_entrees_du_contrat_sont_reconnues(entrees):
    assert len(entrees) == 10
    assert all(skin is not None for skin, _, _ in entrees)
    assert all(skin.rarity is Rarity.INDUSTRIAL for skin, _, _ in entrees)


def test_le_float_de_sortie_predit_correspond_a_celui_obtenu(db, entrees):
    cible = db.find(SORTIE_OBTENUE)
    moyenne = average_normalized([(s, f) for s, f, _ in entrees])
    predit = output_float(moyenne, cible)
    assert predit == pytest.approx(FLOAT_OBTENU, abs=TOLERANCE)


def test_le_palier_dusure_predit_est_celui_obtenu(db, entrees):
    """Le palier decide du prix : c'est lui qu'il faut avoir juste."""
    cible = db.find(SORTIE_OBTENUE)
    moyenne = average_normalized([(s, f) for s, f, _ in entrees])
    assert wear_of(output_float(moyenne, cible)) is Wear.MINIMAL_WEAR
    assert wear_of(FLOAT_OBTENU) is Wear.MINIMAL_WEAR


def test_moyenner_les_floats_affiches_aurait_donne_le_mauvais_palier(db, entrees):
    """La preuve par l'erreur, sur des donnees reelles.

    Ce contrat est l'origine de la regle : moyenner les floats AFFICHES plutot
    que les normalises predisait Factory New la ou le jeu a produit du Minimal
    Wear -- une sortie valorisee 1.16 EUR au lieu de 0.73 reels.
    """
    cible = db.find(SORTIE_OBTENUE)
    affichee = sum(f for _, f, _ in entrees) / len(entrees)
    normalisee = average_normalized([(s, f) for s, f, _ in entrees])

    assert affichee == pytest.approx(0.0789, abs=1e-4)
    assert normalisee == pytest.approx(0.4011, abs=1e-4)

    faux = output_float(affichee, cible)
    assert wear_of(faux) is Wear.FACTORY_NEW  # l'erreur d'origine
    assert wear_of(faux) is not wear_of(FLOAT_OBTENU)


def test_la_sortie_obtenue_faisait_partie_des_issues_predites(db, entrees):
    col = db.find_collection("The Bank Collection")
    prix = {}
    for rarete in (Rarity.INDUSTRIAL, Rarity.MIL_SPEC):
        for skin in col.by_rarity(rarete):
            for wear in skin.available_wears():
                prix[skin.market_hash_name(wear)] = 1.0

    items = [
        InputItem(skin=s, float_value=f, unit_cost=p) for s, f, p in entrees
    ]
    resultat = evaluate(
        items, db.outcomes_map([col], Rarity.INDUSTRIAL), StaticPricer(prix)
    )

    assert sum(o.probability for o in resultat.outcomes) == pytest.approx(1.0)
    obtenue = [o for o in resultat.outcomes if o.skin.name == SORTIE_OBTENUE]
    assert len(obtenue) == 1
    assert obtenue[0].wear is Wear.MINIMAL_WEAR
    assert obtenue[0].float_value == pytest.approx(FLOAT_OBTENU, abs=TOLERANCE)
    # Trois sorties equiprobables dans cette collection a cette rarete.
    assert obtenue[0].probability == pytest.approx(1 / 3)


def test_le_cout_evalue_est_la_somme_de_ce_qui_a_ete_paye(db, entrees):
    """Le moteur ne doit rien reestimer : on a paye, c'est le cout.

    Le plan d'origine annoncait 0.71 et le panier a coute 0.76 -- les annonces
    partent pendant qu'on achete, et on prend des substituts. C'est precisement
    pour cet ecart que le journal enregistre le reel plutot que le prevu.
    """
    col = db.find_collection("The Bank Collection")
    prix = {
        skin.market_hash_name(wear): 1.0
        for rarete in (Rarity.INDUSTRIAL, Rarity.MIL_SPEC)
        for skin in col.by_rarity(rarete)
        for wear in skin.available_wears()
    }
    items = [InputItem(skin=s, float_value=f, unit_cost=p) for s, f, p in entrees]
    resultat = evaluate(
        items, db.outcomes_map([col], Rarity.INDUSTRIAL), StaticPricer(prix)
    )
    assert resultat.cost == pytest.approx(0.76)
