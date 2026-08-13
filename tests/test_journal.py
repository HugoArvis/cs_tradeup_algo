"""Tests du journal : plans, contrats suivis, objets achetes.

L'interet du journal n'est pas de stocker, c'est de recalculer a partir du
REEL. Un plan dit ce qu'il faudrait acheter ; les annonces partent, on prend
des substituts, et le float moyen derive -- ce qui change l'usure de sortie.
Ces tests portent surtout la-dessus.
"""

from __future__ import annotations

import time

import pytest

from tradeup.journal import TRADE_LOCK_SECONDS, Journal

PLAN = {
    "collection": "The Bank Collection",
    "cost": 11.41,
    "net": 16.02,
    "profit": 4.61,
    "roi": 0.404,
    "avg_float": 0.0648,
    # 2 x 0.03 + 8 x 0.0735 = 0.648, soit une moyenne de 0.0648 : coherent avec
    # `avg_float` ci-dessus, et juste sous la frontiere Factory New (0.07).
    "inputs": [
        {"name": "Desert Eagle | Meteorite (Factory New)", "float": 0.03,
         "price": 1.47, "url": f"https://csfloat.com/item/fn{i}"}
        for i in range(2)
    ] + [
        {"name": "Desert Eagle | Meteorite (Minimal Wear)", "float": 0.0735,
         "price": 1.00, "url": f"https://csfloat.com/item/mw{i}"}
        for i in range(8)
    ],
    "outcomes": [{"name": "AK-47 | Emerald Pinstripe (Factory New)",
                  "probability": 1.0, "float": 0.0648, "net": 16.02}],
}


@pytest.fixture
def journal(tmp_path) -> Journal:
    j = Journal(tmp_path / "journal.db")
    yield j
    j.close()


@pytest.fixture
def contrat(journal):
    pid = journal.save_plan(PLAN, collection_id="bank", rarity="mil-spec")
    return journal.contract(journal.follow(pid))


# --- Plans -------------------------------------------------------------------


def test_un_plan_est_conserve_avec_ses_chiffres(journal):
    journal.save_plan(PLAN, collection_id="bank", rarity="mil-spec")
    plans = journal.plans()
    assert len(plans) == 1
    assert plans[0]["collection"] == "The Bank Collection"
    assert plans[0]["profit"] == pytest.approx(4.61)
    assert plans[0]["followed"] == 0


def test_les_plans_reviennent_du_plus_recent_au_plus_ancien(journal):
    for i in range(3):
        journal.save_plan({**PLAN, "profit": float(i)}, collection_id="bank",
                          rarity="mil-spec")
        time.sleep(0.01)
    assert [p["profit"] for p in journal.plans()] == [2.0, 1.0, 0.0]


def test_suivre_un_plan_inconnu_echoue(journal):
    with pytest.raises(KeyError):
        journal.follow("nexistepas")


# --- Contrats ----------------------------------------------------------------


def test_suivre_un_plan_cree_ses_dix_objets(journal):
    pid = journal.save_plan(PLAN, collection_id="bank", rarity="mil-spec")
    c = journal.contract(journal.follow(pid))

    assert len(c.items) == 10
    assert all(i.from_plan for i in c.items)
    assert not any(i.purchased for i in c.items)
    assert c.spent == 0.0
    assert not c.complete
    assert journal.plans()[0]["followed"] == 1


def test_le_contrat_suit_ce_qui_est_reellement_depense(journal, contrat):
    prix_prevu = contrat.items[1].price
    journal.mark_purchased(contrat.items[0].id, price=1.15)  # paye plus cher
    journal.mark_purchased(contrat.items[1].id)  # prix du plan conserve

    c = journal.contract(contrat.id)
    assert len(c.purchased) == 2
    assert c.spent == pytest.approx(1.15 + prix_prevu)


def test_un_substitut_fait_deriver_le_float_moyen(journal, contrat):
    """Le coeur du sujet : une annonce partie, un substitut, et la sortie change.

    Le plan visait 0.0648 de moyenne, juste sous la frontiere Factory New. Un
    seul substitut a 0.14 au lieu de 0.0735 suffit a faire passer la moyenne
    reelle AU-DESSUS de 0.07 -- et la sortie perd un palier d'usure.
    """
    for it in contrat.items:
        journal.mark_purchased(it.id)
    prevu = journal.contract(contrat.id).actual_avg_float
    assert prevu == pytest.approx(0.0648)
    assert prevu < 0.07  # sortie Factory New

    journal.replace_item(
        contrat.items[-1].id,
        name="Desert Eagle | Meteorite (Minimal Wear)",
        float_value=0.14, price=0.95,
    )
    c = journal.contract(contrat.id)

    assert c.actual_avg_float == pytest.approx(0.07145)
    assert c.actual_avg_float > 0.07  # la sortie bascule en Minimal Wear
    assert c.float_drift == pytest.approx(0.00665)
    substitut = c.items[-1]
    assert not substitut.from_plan and substitut.purchased


def test_pas_de_moyenne_sans_achat(journal, contrat):
    assert journal.contract(contrat.id).actual_avg_float is None
    assert journal.contract(contrat.id).float_drift is None


# --- Verrou d'echange --------------------------------------------------------


def test_le_dernier_achat_commande_la_date_d_execution(journal, contrat):
    """Acheter etale coute des jours : c'est le dernier verrou qui compte."""
    base = time.time()
    for n, it in enumerate(contrat.items):
        journal.mark_purchased(it.id, at=base + n * 86400)  # un par jour

    c = journal.contract(contrat.id)
    assert c.complete
    # Le dernier a ete achete 9 jours apres le premier.
    assert c.craftable_at == pytest.approx(base + 9 * 86400 + TRADE_LOCK_SECONDS)
    assert not c.craftable


def test_contrat_executable_quand_les_verrous_sont_passes(journal, contrat):
    vieux = time.time() - TRADE_LOCK_SECONDS - 60
    for it in contrat.items:
        journal.mark_purchased(it.id, at=vieux)

    c = journal.contract(contrat.id)
    assert c.craftable
    assert not c.items[0].locked


def test_pas_de_date_tant_que_le_lot_est_incomplet(journal, contrat):
    for it in contrat.items[:9]:
        journal.mark_purchased(it.id)
    c = journal.contract(contrat.id)
    assert not c.complete
    assert c.craftable_at is None and not c.craftable


def test_un_objet_achete_est_verrouille_sept_jours(journal, contrat):
    journal.mark_purchased(contrat.items[0].id)
    it = journal.contract(contrat.id).items[0]
    assert it.locked
    assert it.tradable_at == pytest.approx(it.purchased_at + TRADE_LOCK_SECONDS)


# --- Cycle de vie ------------------------------------------------------------


def test_statuts_et_notes(journal, contrat):
    journal.set_status(contrat.id, "pret")
    journal.set_notes(contrat.id, "attendre la fin du verrou")
    c = journal.contract(contrat.id)
    assert c.status == "pret" and "verrou" in c.notes

    with pytest.raises(ValueError, match="statut inconnu"):
        journal.set_status(contrat.id, "n_importe_quoi")


def test_annuler_un_achat(journal, contrat):
    journal.mark_purchased(contrat.items[0].id)
    journal.unmark_purchased(contrat.items[0].id)
    assert not journal.contract(contrat.id).items[0].purchased


def test_supprimer_un_contrat_emporte_ses_objets(journal, contrat):
    journal.delete_contract(contrat.id)
    assert journal.contract(contrat.id) is None
    assert journal.stats()["contrats"] == 0


def test_plusieurs_contrats_ne_se_melangent_pas(journal):
    """Le cas d'usage : plusieurs trade-ups menes de front."""
    pid = journal.save_plan(PLAN, collection_id="bank", rarity="mil-spec")
    a = journal.contract(journal.follow(pid))
    b = journal.contract(journal.follow(pid))

    journal.mark_purchased(a.items[0].id, price=9.99)
    assert journal.contract(a.id).spent == pytest.approx(9.99)
    assert journal.contract(b.id).spent == 0.0
    assert {i.contract_id for i in journal.contract(b.id).items} == {b.id}


def test_filtrer_les_contrats_termines(journal, contrat):
    assert len(journal.contracts(include_done=False)) == 1
    journal.set_status(contrat.id, "realise")
    assert len(journal.contracts(include_done=False)) == 0
    assert len(journal.contracts(include_done=True)) == 1


def test_le_journal_survit_a_une_reouverture(tmp_path):
    chemin = tmp_path / "j.db"
    with Journal(chemin) as j:
        pid = j.save_plan(PLAN, collection_id="bank", rarity="mil-spec")
        cid = j.follow(pid)
        j.mark_purchased(j.contract(cid).items[0].id, price=2.22)

    with Journal(chemin) as j:
        c = j.contract(cid)
        assert c is not None and c.spent == pytest.approx(2.22)
        assert j.stats() == {"plans": 1, "contrats": 1, "objets_achetes": 1}
