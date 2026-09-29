"""Tests de la strategie par ordres d'achat.

Le piege propre a ce module : melanger deux scenarios de prix dans le meme
affichage. La valeur de la sortie se calcule au marche, mais le contrat
s'execute aux prix d'ORDRE -- bien plus bas. Une probabilite de gain calculee
sur le cout du marche, presentee a cote d'un budget reduit, donne des lignes
qui se contredisent : "rendement vise +20 %" et "0 % de chances de gagner" sur
le meme contrat. Plusieurs tests ici ne verifient que cette coherence.
"""

from __future__ import annotations

import pytest

from tradeupfinder.db import SkinDatabase
from tradeupfinder.models import Rarity
from tradeupfinder.orders import (
    DEFAULT_MAX_DISCOUNT,
    STEAM_MIN_PRICE,
    fill_estimate,
    plan_orders,
    scan_orders,
)
from tradeupfinder.pricing.repository import StaticPricer


@pytest.fixture(scope="module")
def db():
    return SkinDatabase.load()


@pytest.fixture(scope="module")
def bank(db):
    return db.find_collection("The Bank Collection")


def pricer(col, rarity=Rarity.INDUSTRIAL, entree=1.0, sortie=20.0):
    valeurs = {}
    for s in col.by_rarity(rarity):
        for w in s.available_wears():
            valeurs[s.market_hash_name(w)] = entree
    for s in col.by_rarity(rarity.next_up):
        for w in s.available_wears():
            valeurs[s.market_hash_name(w)] = sortie
    return StaticPricer(valeurs)


# --- Le budget ---------------------------------------------------------------


def test_le_budget_decoule_de_la_sortie_pas_du_prix_dachat(db, bank):
    """La valeur de la sortie ne depend pas de ce qu'on a paye les entrees.

    Le budget effectif est legerement SOUS la cible theorique : chaque prix
    d'ordre est arrondi au centime inferieur, et dix entrees peuvent donc
    perdre jusqu'a 0.10 au total. Toujours en dessous, jamais au-dessus --
    depasser mangerait la marge visee.
    """
    plan = plan_orders(db, bank, Rarity.INDUSTRIAL, pricer(bank),
                       target_roi=0.20)
    assert plan is not None
    cible = plan.ev_net / 1.20
    perte_arrondi = 0.01 * sum(l.quantity for l in plan.lines)
    assert cible - perte_arrondi <= plan.budget <= cible


def test_un_rendement_plus_exigeant_baisse_le_prix_dordre(db, bank):
    doux = plan_orders(db, bank, Rarity.INDUSTRIAL, pricer(bank), target_roi=0.05)
    dur = plan_orders(db, bank, Rarity.INDUSTRIAL, pricer(bank), target_roi=0.50)
    assert doux.budget > dur.budget
    assert dur.discount > doux.discount


def test_les_prix_dordre_sont_arrondis_vers_le_bas(db, bank):
    """Arrondir au centime superieur depasserait le budget et mangerait la marge."""
    plan = plan_orders(db, bank, Rarity.INDUSTRIAL, pricer(bank))
    for ligne in plan.lines:
        assert ligne.order_price == pytest.approx(
            int(ligne.order_price * 100) / 100
        )
    assert plan.budget <= plan.ev_net / 1.20 + 0.01


def test_le_rabais_est_le_meme_sur_toutes_les_lignes(db, bank):
    """Repartir autrement supposerait de savoir ou les vendeurs cedent le plus."""
    plan = plan_orders(db, bank, Rarity.INDUSTRIAL,
                       pricer(bank, entree=2.0), target_roi=0.20)
    rabais = [l.discount for l in plan.lines]
    assert max(rabais) - min(rabais) < 0.05  # a l'arrondi au centime pres


# --- Coherence des scenarios -------------------------------------------------


def test_la_probabilite_est_calculee_au_prix_dordre(db, bank):
    """Le coeur du module : acheter au rabais fait basculer des sorties.

    Mesure sur donnees reelles : The Bank Collection en Mil-Spec passe de 0 %
    de chances de gagner au prix demande a 100 % avec 32 % de rabais.
    """
    # Entrees cheres, sortie mediocre : perdant au marche.
    p = pricer(bank, entree=3.0, sortie=20.0)
    plan = plan_orders(db, bank, Rarity.INDUSTRIAL, p, target_roi=0.20)
    assert plan is not None
    assert plan.discount > 0  # il faut bien un rabais
    assert plan.win_probability >= plan.result.profit_probability
    assert 0.0 <= plan.win_probability <= 1.0


def test_toutes_rentables_se_juge_aussi_au_prix_dordre(db, bank):
    p = pricer(bank, entree=3.0, sortie=20.0)
    plan = plan_orders(db, bank, Rarity.INDUSTRIAL, p, target_roi=0.20)
    if plan.always_profitable:
        assert plan.win_probability == pytest.approx(1.0)


def test_le_rapport_ne_melange_pas_les_deux_scenarios(db, bank):
    p = pricer(bank, entree=3.0, sortie=20.0)
    texte = plan_orders(db, bank, Rarity.INDUSTRIAL, p).report()
    assert "prix demande" in texte
    assert "ordre max" in texte
    # La ligne de probabilite doit dire explicitement de quel scenario il s'agit.
    assert "Une fois les ordres servis" in texte


def test_un_contrat_deja_rentable_est_signale(db, bank):
    """Inutile d'attendre un ordre quand le prix demande suffit deja."""
    plan = plan_orders(db, bank, Rarity.INDUSTRIAL,
                       pricer(bank, entree=0.05, sortie=50.0))
    assert plan.already_profitable
    assert "DEJA RENTABLE" in plan.report()


# --- Faisabilite -------------------------------------------------------------


def test_un_ordre_sous_le_plancher_steam_est_impossible(db, bank):
    """Steam refuse les ordres sous 0.03 : aucun calcul ne change ca."""
    # Sortie quasi sans valeur : le budget tombe sous le plancher.
    plan = plan_orders(db, bank, Rarity.INDUSTRIAL,
                       pricer(bank, entree=1.0, sortie=0.10))
    assert plan is not None
    assert plan.blocked_by_floor
    assert not plan.feasible()
    assert "plancher" in plan.report()
    assert all(l.order_price < STEAM_MIN_PRICE for l in plan.blocked_by_floor)


def test_un_rabais_irrealiste_est_ecarte(db, bank):
    """Calculer qu'il faudrait 93 % de rabais n'est pas un conseil.

    C'est un refus deguise : un tel ordre n'est jamais servi.
    """
    plan = plan_orders(db, bank, Rarity.INDUSTRIAL,
                       pricer(bank, entree=50.0, sortie=20.0))
    assert plan.discount > DEFAULT_MAX_DISCOUNT
    assert not plan.feasible()
    assert plan.feasible(max_discount=0.99)  # le seuil est bien le seul juge


def test_le_scan_ecarte_les_cas_hors_de_portee(db, bank):
    p = pricer(bank, entree=50.0, sortie=20.0)
    assert scan_orders(db, Rarity.INDUSTRIAL, p, collections=[bank]) == []
    assert scan_orders(db, Rarity.INDUSTRIAL, p, collections=[bank],
                       keep_unfeasible=True) != []


def test_les_plans_sont_classes_par_rabais_croissant(db):
    """Le plus facile a remplir d'abord : c'est le rabais qui decide du rythme."""
    rarity = Rarity.INDUSTRIAL
    cols = db.tradeable_collections(rarity)[:6]
    valeurs = {}
    for i, c in enumerate(cols):
        for s in c.by_rarity(rarity):
            for w in s.available_wears():
                valeurs[s.market_hash_name(w)] = 1.0
        for s in c.by_rarity(rarity.next_up):
            for w in s.available_wears():
                valeurs[s.market_hash_name(w)] = 12.0 + i

    plans = scan_orders(db, rarity, StaticPricer(valeurs), collections=cols,
                        keep_unfeasible=True)
    rabais = [p.discount for p in plans]
    assert rabais == sorted(rabais)


# --- Le float ----------------------------------------------------------------


def test_le_float_est_traite_comme_un_tirage(db, bank):
    """Un ordre d'achat ne filtre pas les floats : viser bas serait mentir.

    Le mode aleatoire impose le milieu du palier ; un plan calcule en mode fixe
    viserait bien plus bas.
    """
    from tradeupfinder.generator import Recipe, optimize_recipe

    p = pricer(bank)
    plan = plan_orders(db, bank, Rarity.INDUSTRIAL, p)
    fixe = optimize_recipe(Recipe(counts=((bank.id, 10),),
                                  rarity=Rarity.INDUSTRIAL), db, p)
    assert plan.result.avg_input_float > fixe.avg_input_float


# --- Delai -------------------------------------------------------------------


def test_le_delai_depend_du_rabais_et_du_volume():
    assert "inconnu" in fill_estimate(None, 0.2)
    assert "immediat" in fill_estimate(100, 0.0)
    assert fill_estimate(100, 0.05) != fill_estimate(100, 0.30)
    assert "jamais" in fill_estimate(100, 0.80)


# --- Age des cotations -------------------------------------------------------


def test_lage_des_cotations_est_mesurable(tmp_path):
    """Hors ligne le TTL est ignore : une cotation de six semaines passe sans
    un mot et donne un scan d'apparence normale, entierement faux."""
    import time

    from tradeupfinder.pricing.base import Quote
    from tradeupfinder.pricing.cache import QuoteCache
    from tradeupfinder.pricing.repository import MarketPricer

    maintenant = time.time()

    class Source:
        name = "steam"
        currency = "EUR"

        def fetch(self, name, *, use_cache=True):
            jours = {"vieux": 42, "moyen": 2, "frais": 0}[name]
            return Quote(name, "steam", 1.0, 1.0, 10, currency="EUR",
                         fetched_at=maintenant - jours * 86400)

    pricer = MarketPricer(Source())
    assert pricer.quote_age() is None  # rien de charge encore

    pricer.warm(["vieux", "moyen", "frais"])
    median, maxi = pricer.quote_age()
    assert 1.5 * 86400 < median < 2.5 * 86400
    assert 41 * 86400 < maxi < 43 * 86400
