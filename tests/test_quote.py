"""Tests du prix retenu pour decider.

Motivation mesuree le 20 septembre 2026. Un balayage a sorti The Nuke
Collection a 191 % de profitabilite, sortie M4A4 | Radiation Hazard (FT)
valorisee 136 EUR. TradeUpSpy donnait 42 % et l'objet valait 35,65 EUR.

La cause tenait dans une seule ligne du cache :

    low=35.65   med=165.04   vol=1

Une vente isolee -- exemplaire sticke, motif rare ou erreur de prix -- servait
de valorisation, parce que `realised_reference()` retenait le median sans
condition.
"""

from __future__ import annotations

import pytest

from tradeupfinder.pricing.base import MIN_SALES_FOR_MEDIAN, Quote


def q(low, med, vol=50):
    return Quote(market_hash_name="x", source="steam", lowest_price=low,
                 median_price=med, volume=vol, currency="EUR")


def test_un_median_sur_une_seule_vente_est_ignore():
    """Le bug exact, verrouille avec ses vrais chiffres."""
    assert q(35.65, 165.04, vol=1).realised_reference() == 35.65


def test_le_prix_retenu_ne_depasse_jamais_la_plus_basse_annonce():
    """On ne vend pas a 165 ce dont un exemplaire est affiche a 35.

    Meme avec un volume credible : personne n'achete le second exemplaire
    quand le premier est moins cher. Et a l'achat, placer un ordre au-dessus
    du prix demande n'a aucun sens.
    """
    assert q(35.65, 165.04, vol=500).realised_reference() == 35.65
    assert q(0.88, 0.91, vol=148).realised_reference() == 0.88


def test_un_median_credible_et_plus_bas_est_retenu():
    """C'est tout l'interet de la base "ventes" : le G3SG1 Green Apple se
    vendait 0,09 alors qu'il s'affichait 0,11."""
    assert q(0.11, 0.09, vol=83).realised_reference() == 0.09


def test_le_seuil_de_volume_est_respecte_des_qu_il_est_atteint():
    assert q(1.0, 0.5, vol=MIN_SALES_FOR_MEDIAN).realised_reference() == 0.5
    assert q(1.0, 0.5, vol=MIN_SALES_FOR_MEDIAN - 1).realised_reference() == 1.0


def test_sans_median_on_retombe_sur_l_annonce():
    assert q(35.65, None, vol=None).realised_reference() == 35.65


def test_sans_annonce_le_median_sert_quand_meme():
    """Un objet sans annonce en cours mais avec des ventes reste evaluable."""
    assert q(None, 12.0, vol=40).realised_reference() == 12.0


def test_un_volume_inconnu_ne_bloque_pas():
    """CSFloat ne publie aucun volume : exiger un volume l'ecarterait tout
    entier, ce qui serait pire que le risque qu'on evite."""
    assert q(2.0, 1.5, vol=None).realised_reference() == 1.5


def test_une_cotation_vide_ne_vaut_rien():
    assert q(None, None, vol=None).realised_reference() is None
