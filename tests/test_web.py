"""Tests de l'application web locale.

Deux points sensibles, testes ici :
  - la cle API ne doit JAMAIS sortir du serveur ;
  - une tache qui echoue (quota, bug) doit produire un etat lisible, pas une
    page figee : l'interface interroge l'avancement et doit toujours obtenir
    une reponse exploitable.
"""

from __future__ import annotations

import json
import time

import pytest

from tradeup.db import SkinDatabase
from tradeup.pricing.http import RateLimited
from tradeup.web import App, Job

RAW_DB = {
    "version": "test",
    "collections": [
        {
            "id": "col_a",
            "name": "Collection A",
            "skins": [
                {"key": "a_in", "name": "Arme A", "rarity": "Mil-Spec Grade",
                 "min_float": 0.0, "max_float": 1.0},
                {"key": "a_out", "name": "Sortie A", "rarity": "Restricted",
                 "min_float": 0.0, "max_float": 1.0},
            ],
        },
        {
            "id": "col_b",
            "name": "Collection B",
            "skins": [
                # max_float 0.40 : Battle-Scarred (0.45+) devient inatteignable,
                # donc 4 usures a coter au lieu de 5.
                {"key": "b_in", "name": "Arme B", "rarity": "Mil-Spec Grade",
                 "min_float": 0.0, "max_float": 0.40},
                {"key": "b_o1", "name": "Sortie B1", "rarity": "Restricted",
                 "min_float": 0.0, "max_float": 1.0},
                {"key": "b_o2", "name": "Sortie B2", "rarity": "Restricted",
                 "min_float": 0.0, "max_float": 1.0},
            ],
        },
    ],
}


@pytest.fixture
def app() -> App:
    return App(SkinDatabase.from_dict(RAW_DB), "cle-secrete-a-ne-pas-fuiter")


# --- Liste des collections ---------------------------------------------------


def test_collections_triees_par_variance(app):
    rows = app.collections("mil-spec")
    assert [r["name"] for r in rows] == ["Collection A", "Collection B"]
    assert rows[0]["outcomes"] == 1  # le resultat certain d'abord
    assert rows[1]["outcomes"] == 2


def test_le_cout_en_requetes_est_annonce(app):
    """L'interface doit pouvoir prevenir AVANT de bruler le quota."""
    rows = {r["id"]: r for r in app.collections("mil-spec")}
    # Arme A couvre les 5 usures, Arme B plafonne a 0.40 donc 4.
    assert rows["col_a"]["requests"] == 5
    assert rows["col_b"]["requests"] == 4


def test_rarete_sans_collection_exploitable(app):
    assert app.collections("classified") == []


# --- Taches ------------------------------------------------------------------


def test_le_quota_epuise_donne_un_etat_lisible(app, monkeypatch):
    def boom(*a, **kw):
        raise RateLimited("429")

    monkeypatch.setattr("tradeup.web.build_plan", boom)
    job = app.start_plan("col_a", "mil-spec")
    _attendre(job)

    assert job.state == "quota"
    assert "attendez" in job.message.lower()
    payload = app.job_payload(job)
    assert payload["state"] == "quota" and "plan" not in payload


def test_une_erreur_inattendue_ne_bloque_pas_l_interface(app, monkeypatch):
    monkeypatch.setattr(
        "tradeup.web.build_plan",
        lambda *a, **kw: (_ for _ in ()).throw(ValueError("casse")),
    )
    job = app.start_plan("col_a", "mil-spec")
    _attendre(job)

    assert job.state == "error"
    assert "ValueError" in job.message
    assert app.job_payload(job)["state"] == "error"


def test_absence_d_annonces_est_distinguee_d_une_erreur(app, monkeypatch):
    monkeypatch.setattr("tradeup.web.build_plan", lambda *a, **kw: None)
    job = app.start_plan("col_a", "mil-spec")
    _attendre(job)

    assert job.state == "empty"
    assert "annonces" in job.message.lower()


def test_deux_taches_coexistent(app, monkeypatch):
    monkeypatch.setattr("tradeup.web.build_plan", lambda *a, **kw: None)
    a = app.start_plan("col_a", "mil-spec")
    b = app.start_plan("col_b", "mil-spec")
    assert a.id != b.id
    _attendre(a)
    _attendre(b)
    assert set(app.jobs) == {a.id, b.id}


# --- Fuite de secret ---------------------------------------------------------


def test_la_cle_api_ne_sort_jamais(app, monkeypatch):
    monkeypatch.setattr("tradeup.web.build_plan", lambda *a, **kw: None)
    job = app.start_plan("col_a", "mil-spec")
    _attendre(job)

    tout = json.dumps(app.job_payload(job)) + json.dumps(app.collections("mil-spec"))
    assert "cle-secrete" not in tout
    # Meme partiellement : pas de fragment exploitable.
    assert "secrete" not in tout


def test_la_page_ne_contient_aucun_secret():
    from tradeup.web import PAGE

    assert "CSFLOAT_API_KEY" not in PAGE
    assert "Authorization" not in PAGE


def _attendre(job: Job, timeout: float = 5.0) -> None:
    debut = time.time()
    while job.state == "running" and time.time() - debut < timeout:
        time.sleep(0.01)
    assert job.state != "running", "la tache n'a pas abouti"


# --- Contrats exposes par l'API ----------------------------------------------


def test_le_contrat_expose_le_reel_et_le_prevu(tmp_path):
    """L'interface doit pouvoir montrer l'ecart, pas seulement le plan."""
    from tradeup.journal import Journal

    j = Journal(tmp_path / "j.db")
    app = App(SkinDatabase.from_dict(RAW_DB), "cle", journal=j)
    plan = {
        "collection": "Collection A", "cost": 10.0, "net": 15.0, "profit": 5.0,
        "roi": 0.5, "avg_float": 0.05,
        "inputs": [{"name": f"Arme {i}", "float": 0.05, "price": 1.0,
                    "url": f"https://csfloat.com/item/z{i}"} for i in range(10)],
        "outcomes": [],
    }
    cid = j.follow(j.save_plan(plan, collection_id="col_a", rarity="mil-spec"))
    j.mark_purchased(j.contract(cid).items[0].id, price=2.5, float_value=0.30)

    p = app.contract_payload(j.contract(cid))
    assert p["planned_cost"] == 10.0 and p["planned_avg_float"] == 0.05
    assert p["spent"] == 2.5 and p["purchased"] == 1
    assert p["actual_avg_float"] == 0.3  # un seul achat, a 0.30
    assert p["float_drift"] == pytest.approx(0.25)
    assert not p["complete"] and p["craftable_at"] is None
    assert p["items"][0]["locked"] is True
    assert p["items"][0]["url"] == "https://csfloat.com/item/z0"
    j.close()


def test_le_payload_de_contrat_ne_fuit_pas_la_cle(tmp_path):
    from tradeup.journal import Journal

    j = Journal(tmp_path / "j.db")
    app = App(SkinDatabase.from_dict(RAW_DB), "cle-ultra-secrete", journal=j)
    plan = {"collection": "A", "cost": 1.0, "net": 2.0, "profit": 1.0, "roi": 1.0,
            "avg_float": 0.05, "inputs": [], "outcomes": []}
    cid = j.follow(j.save_plan(plan, collection_id="col_a", rarity="mil-spec"))
    assert "secrete" not in json.dumps(app.contract_payload(j.contract(cid)))
    j.close()


# --- Rarete affichee ---------------------------------------------------------


def test_le_libelle_de_rarete_est_uniforme():
    """Le journal stocke "mil-spec", un plan frais expose "Mil-Spec Grade".

    Sans conversion, l'historique et les resultats du jour afficheraient deux
    ecritures de la meme chose.
    """
    from tradeup.web import libelle_rarete

    assert libelle_rarete("mil-spec") == "Mil-Spec Grade"
    assert libelle_rarete("industrial") == "Industrial Grade"
    # Une valeur inconnue reste lisible plutot que d'etre masquee : une vieille
    # ligne d'historique doit rester identifiable.
    assert libelle_rarete("rarete-disparue") == "rarete-disparue"
    assert libelle_rarete(None) == ""


# --- Onglet inventaire -------------------------------------------------------


def inventaire_de_test(n=10, nom="Arme A (Factory New)"):
    from tradeup.inventory import OwnedItem

    return [
        OwnedItem(market_hash_name=nom, float_value=0.01 + i * 0.001,
                  asset_id=f"a{i}", tradable=True)
        for i in range(n)
    ]


def test_lapercu_annonce_le_cout_avant_de_lancer(app, monkeypatch):
    """Regle de l'application : le quota se voit AVANT d'etre depense."""
    monkeypatch.setattr(app, "inventaire", lambda **k: inventaire_de_test())
    monkeypatch.setattr(app, "load_currency", lambda: None)

    d = app.inventory_overview("mil-spec")
    assert d["summary"]["objets"] == 10
    assert d["buildable"] and d["buildable"][0]["owned"] == 10
    # 1 nom d'entree distinct + 1 sortie x 5 usures.
    assert d["requests"] == 6


def test_lapercu_chiffre_lecart_quand_il_manque_des_objets(app, monkeypatch):
    """Sans ca, "aucun contrat" est indiscernable d'une panne."""
    monkeypatch.setattr(app, "inventaire", lambda **k: inventaire_de_test(n=4))
    monkeypatch.setattr(app, "load_currency", lambda: None)

    d = app.inventory_overview("mil-spec")
    assert d["buildable"] == []
    assert d["requests"] == 0
    assert d["gaps"][0] == {"collection": "Collection A", "owned": 4}


def test_le_calcul_dinventaire_produit_un_resultat_exploitable(app, monkeypatch):
    from tradeup.pricing.repository import StaticPricer

    prix = {}
    for nom in ("Arme A", "Sortie A"):
        for usure in ("Factory New", "Minimal Wear", "Field-Tested",
                      "Well-Worn", "Battle-Scarred"):
            prix[f"{nom} ({usure})"] = 1.0 if nom == "Arme A" else 40.0

    monkeypatch.setattr(app, "inventaire", lambda **k: inventaire_de_test())
    monkeypatch.setattr(app, "load_currency", lambda: None)
    monkeypatch.setattr("tradeup.web.CSFloat", lambda *a, **k: None)
    monkeypatch.setattr("tradeup.web.CSFloatPricer",
                        lambda *a, **k: StaticPricer(prix))

    job = app.start_inventory("mil-spec")
    for _ in range(100):
        if job.state != "running":
            break
        time.sleep(0.05)

    assert job.state == "done"
    payload = app.inventory_job_payload(job)
    ligne = payload["results"][0]
    assert ligne["collection"] == "Collection A"
    assert ligne["rarity"] == "Mil-Spec Grade"
    assert len(ligne["inputs"]) == 10
    assert ligne["outcomes"]
    # Les entrees sont valorisees a leur prix de REVENTE, jamais a zero.
    assert ligne["opportunity_cost"] > 0
    assert ligne["gain"] == pytest.approx(ligne["net"] - ligne["opportunity_cost"],
                                          abs=0.02)


def test_un_quota_epuise_donne_un_etat_lisible(app, monkeypatch):
    def boum(*a, **k):
        raise RateLimited("429")

    monkeypatch.setattr(app, "inventaire", boum)
    job = app.start_inventory("mil-spec")
    for _ in range(100):
        if job.state != "running":
            break
        time.sleep(0.05)
    assert job.state == "quota"
    assert "quota" in app.inventory_job_payload(job)["message"].lower()


def test_linventaire_nest_pas_relu_a_chaque_clic(app, monkeypatch):
    """Une lecture coute une requete et l'inventaire ne bouge pas entre deux clics."""
    appels = []

    class FakeSource:
        def inventory(self):
            appels.append(1)
            return []

    monkeypatch.setattr("tradeup.web.CSFloat", lambda *a, **k: FakeSource())
    app.inventaire()
    app.inventaire()
    assert len(appels) == 1
    app.inventaire(force=True)  # le bouton "Relire" doit, lui, retaper l'API
    assert len(appels) == 2


def test_le_payload_dinventaire_porte_de_quoi_classer(app, monkeypatch):
    """L'interface reclasse cote client : elle a besoin des grandeurs, pas
    seulement du gain."""
    from tradeup.pricing.repository import StaticPricer

    prix = {}
    for nom in ("Arme A", "Sortie A"):
        for usure in ("Factory New", "Minimal Wear", "Field-Tested",
                      "Well-Worn", "Battle-Scarred"):
            prix[f"{nom} ({usure})"] = 1.0 if nom == "Arme A" else 40.0

    monkeypatch.setattr(app, "inventaire", lambda **k: inventaire_de_test())
    monkeypatch.setattr(app, "load_currency", lambda: None)
    monkeypatch.setattr("tradeup.web.CSFloat", lambda *a, **k: None)
    monkeypatch.setattr("tradeup.web.CSFloatPricer",
                        lambda *a, **k: StaticPricer(prix))

    job = app.start_inventory("mil-spec")
    for _ in range(100):
        if job.state != "running":
            break
        time.sleep(0.05)

    ligne = app.inventory_job_payload(job)["results"][0]
    for champ in ("gain", "roi", "win_probability", "stdev",
                  "outcomes_count", "always_profitable"):
        assert champ in ligne, champ
    assert 0.0 <= ligne["win_probability"] <= 1.0


# --- Ce que le balayage renvoie a la page ------------------------------------


def test_le_balayage_ne_renvoie_que_les_contrats_rentables(app):
    """Regle d'affichage : sous le point mort, ce n'est pas un candidat.

    Le tri se fait cote serveur pour qu'aucune page ne puisse le desactiver.
    Un contrat a 99 % est ecarte comme un contrat a 40 % : il perd de
    l'argent, l'ecart n'est que de degre.
    """
    from tradeup.web import Batch

    b = Batch(id="x", rarity="mil-spec", pending=[], total=4)
    b.done = [
        {"plan_id": "p1", "profitability": 0.40, "profit": -3.0},
        {"plan_id": "p2", "profitability": 1.20, "profit": 2.0},
        {"plan_id": "p3", "profitability": 0.99, "profit": -0.1},
        {"plan_id": "p4", "profitability": 1.05, "profit": 0.5},
    ]
    d = app.batch_payload(b)

    assert [r["plan_id"] for r in d["results"]] == ["p2", "p4"]
    assert d["computed"] == 4 and d["rejected"] == 2


def test_le_balayage_dit_qu_il_a_travaille_meme_sans_resultat(app):
    """Sinon "aucun resultat" est indiscernable d'une panne."""
    from tradeup.web import Batch

    b = Batch(id="y", rarity="mil-spec", pending=[], total=2)
    b.done = [{"plan_id": "p1", "profitability": 0.80, "profit": -1.0}]
    d = app.batch_payload(b)

    assert d["results"] == []
    assert d["computed"] == 1 and d["rejected"] == 1
