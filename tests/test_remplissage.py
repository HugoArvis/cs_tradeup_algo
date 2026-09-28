"""Tests du debit de remplissage d'un ordre d'achat.

Le seul chiffre qui manque a l'arbitrage "CSFloat ou ordre Steam" est le delai
de remplissage au prix vise. Aucune donnee publique ne le donne -- Steam ne
publie pas son carnet d'ordres -- donc il se mesure a l'usage, et un debit mal
calcule ferait renoncer a la bonne strategie.

Seuil mesure le 28 septembre 2026 sur The Arabesque Collection : en dessous de
3,7 jours de remplissage, l'ordre Steam rapporte plus par jour de capital que
l'achat CSFloat, verrou de 7 jours compris.
"""

from __future__ import annotations

import time

import pytest

from tradeup.journal import Journal

JOUR = 86400.0


@pytest.fixture
def journal(tmp_path):
    j = Journal(tmp_path / "j.db")
    yield j
    j.close()


def contrat_de_test(j, n=10):
    plan = {
        "collection": "The Arabesque Collection", "cost": 4.83, "net": 6.17,
        "profit": 1.34, "roi": 0.28, "avg_float": 0.13,
        "inputs": [{"name": f"Sawed-Off | Lunar Wyrm (Minimal Wear)",
                    "float": 0.13, "price": 0.49, "url": None}
                   for _ in range(n)],
        "outcomes": [],
    }
    pid = j.save_plan(plan, collection_id="arabesque", rarity="mil-spec")
    return j.follow(pid)


def test_un_debit_se_calcule_du_premier_au_dernier_achat(journal):
    """JAMAIS depuis la creation du contrat.

    Un ordre pose une semaine apres avoir suivi le plan paraitrait deux fois
    plus lent, et ferait renoncer a une strategie qui marche.
    """
    cid = contrat_de_test(journal)
    c = journal.contract(cid)
    base = time.time() - 20 * JOUR          # contrat cree il y a 20 jours

    # Quatre achats, mais etales sur 3 jours seulement, et TARDIVEMENT.
    for k, item in enumerate(c.items[:4]):
        journal.mark_purchased(item.id, price=0.58,
                               at=base + (17 + k) * JOUR)

    s = journal.fill_stats(cid)
    assert s["achetes"] == 4 and s["restants"] == 6
    # 3 intervalles sur 3 jours -> 1 par jour, et non 4/20 = 0,2.
    assert s["debit_par_jour"] == pytest.approx(1.0, abs=0.01)
    assert s["jours_ecoules"] == pytest.approx(3.0, abs=0.01)


def test_un_seul_achat_ne_donne_pas_de_debit(journal):
    """"Inconnu" et "rien ne rentre" appellent des reactions opposees."""
    cid = contrat_de_test(journal)
    c = journal.contract(cid)
    journal.mark_purchased(c.items[0].id, price=0.58)

    s = journal.fill_stats(cid)
    assert s["achetes"] == 1
    assert s["debit_par_jour"] is None
    assert s["jours_restants"] is None
    assert s["steam_gagne"] is False


def test_le_delai_restant_porte_sur_ce_qui_MANQUE(journal):
    """Projeter sur les dix donnerait un delai deja en partie ecoule."""
    cid = contrat_de_test(journal)
    c = journal.contract(cid)
    base = time.time() - 5 * JOUR
    for k, item in enumerate(c.items[:5]):      # 5 achetes en 4 jours
        journal.mark_purchased(item.id, price=0.58, at=base + k * JOUR)

    s = journal.fill_stats(cid)
    assert s["debit_par_jour"] == pytest.approx(1.0, abs=0.01)
    # 5 restants a 1/jour -> 5 jours, pas 10.
    assert s["jours_restants"] == pytest.approx(5.0, abs=0.01)


def test_un_remplissage_rapide_donne_l_avantage_a_steam(journal):
    """Sous 3,7 jours, l'ordre Steam rapporte plus par jour que CSFloat."""
    cid = contrat_de_test(journal)
    c = journal.contract(cid)
    base = time.time() - JOUR
    # 8 achetes en 1 jour -> 7/jour, il en reste 2 -> 0,29 jour.
    for k, item in enumerate(c.items[:8]):
        journal.mark_purchased(item.id, price=0.58, at=base + k * JOUR / 7)

    s = journal.fill_stats(cid)
    assert s["jours_restants"] < 3.7
    assert s["steam_gagne"] is True


def test_un_remplissage_lent_laisse_l_avantage_a_csfloat(journal):
    cid = contrat_de_test(journal)
    c = journal.contract(cid)
    base = time.time() - 10 * JOUR
    # 3 achetes en 10 jours -> 0,2/jour, 7 restants -> 35 jours.
    for k, item in enumerate(c.items[:3]):
        journal.mark_purchased(item.id, price=0.58, at=base + k * 5 * JOUR)

    s = journal.fill_stats(cid)
    assert s["jours_restants"] > 3.7
    assert s["steam_gagne"] is False


def test_un_contrat_plein_na_plus_de_delai(journal):
    cid = contrat_de_test(journal)
    c = journal.contract(cid)
    base = time.time() - 2 * JOUR
    for k, item in enumerate(c.items):
        journal.mark_purchased(item.id, price=0.58, at=base + k * JOUR / 9)

    s = journal.fill_stats(cid)
    assert s["restants"] == 0
    assert s["jours_restants"] is None


def test_un_contrat_inconnu_ne_casse_rien(journal):
    assert journal.fill_stats("inexistant") is None
