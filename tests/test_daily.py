"""Tests du passage quotidien.

Motivation mesuree : The Italy Collection ressortait a 94 % de profitabilite
sur un cache de quelques jours, et a 84 % re-cotee en direct -- le MP7 |
Anodized Navy (FN), un tiers des issues, avait perdu 31 % entre-temps. Le
calcul etait juste, les prix etaient morts. Un classement n'a donc pas un age
moyen : il a l'age de son plus vieux prix.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from tradeup.daily import (
    DECISION_MAX_AGE,
    Ligne,
    Passage,
    age_max,
    historique,
    journaliser,
    noms_prioritaires,
    recoter,
)
from tradeup.db import SkinDatabase
from tradeup.models import Rarity
from tradeup.pricing.base import Quote
from tradeup.pricing.cache import QuoteCache
from tradeup.scoring import Candidate

RAW_DB = {
    "version": "test",
    "collections": [
        {
            "id": "col_a", "name": "Collection A",
            "skins": [
                {"key": "a_in", "name": "Arme A", "rarity": "Industrial Grade",
                 "min_float": 0.0, "max_float": 1.0},
                {"key": "a_out", "name": "Sortie A", "rarity": "Mil-Spec Grade",
                 "min_float": 0.0, "max_float": 1.0},
            ],
        },
        {
            "id": "col_b", "name": "Collection B",
            "skins": [
                {"key": "b_in", "name": "Arme B", "rarity": "Industrial Grade",
                 "min_float": 0.0, "max_float": 1.0},
                {"key": "b_out", "name": "Sortie B", "rarity": "Mil-Spec Grade",
                 "min_float": 0.0, "max_float": 1.0},
            ],
        },
    ],
}


@pytest.fixture
def db():
    return SkinDatabase.from_dict(RAW_DB)


@pytest.fixture
def cache(tmp_path):
    c = QuoteCache(tmp_path / "p.db", ttl_seconds=10 ** 9)
    yield c
    c.close()


def quote(nom, prix=1.0, age=0.0):
    return Quote(market_hash_name=nom, source="steam", lowest_price=prix,
                 median_price=prix, volume=50, currency="EUR",
                 fetched_at=time.time() - age)


# --- Ce qui rend une ligne actionnable ---------------------------------------


def test_un_contrat_rentable_sur_prix_perimes_nest_pas_actionnable():
    """Le coeur du module : rentable ET frais, ou rien.

    94 % sur des prix de trois jours n'est pas une occasion, c'est une
    hypothese -- et celle d'Italy valait 84 % une fois verifiee.
    """
    vieux = Ligne("A", 1.0, 1.2, 1.20, 0.8, age_max=5 * 24 * 3600)
    assert vieux.profitability >= 1.0
    assert not vieux.frais
    assert not vieux.actionnable


def test_un_contrat_frais_mais_perdant_nest_pas_actionnable():
    frais = Ligne("A", 1.0, 0.84, 0.84, 0.5, age_max=600)
    assert frais.frais and not frais.actionnable


def test_les_trois_conditions_reunies():
    """Frais NE SUFFIT PLUS : il faut aussi la confirmation sur annonces.

    Ce test exigeait deux conditions jusqu'au 20 septembre. La troisieme est
    arrivee le jour ou `plan` a donne -0,3 % la ou `scan` annoncait 148 %.
    """
    sans = Ligne("A", 1.0, 1.1, 1.10, 0.9, age_max=DECISION_MAX_AGE - 1)
    assert sans.frais and not sans.actionnable

    bon = Ligne("A", 1.0, 1.1, 1.10, 0.9, age_max=DECISION_MAX_AGE - 1,
                confirmee=1.10)
    assert bon.actionnable


def test_un_contrat_sans_aucun_prix_nest_jamais_actionnable():
    """`age_max` a None veut dire "on ne sait pas", pas "c'est frais"."""
    inconnu = Ligne("A", 1.0, 2.0, 2.0, 1.0, age_max=None)
    assert not inconnu.frais and not inconnu.actionnable


# --- Ou depenser le budget ---------------------------------------------------


def candidat(label):
    class FauxResultat:
        cost = ev_net = profitability = profit_probability = 1.0

    return Candidate(result=FauxResultat(), label=label, score=1.0)


def test_le_budget_va_dabord_aux_meilleurs_candidats(db, cache):
    """Rafraichir le dernier du classement ne change aucune decision."""
    for nom in ("Arme A (Factory New)", "Sortie A (Factory New)"):
        cache.put(quote(nom, age=10))          # deja frais
    noms = noms_prioritaires(
        [candidat("10x Collection A"), candidat("10x Collection B")],
        {c.id: c for c in db.tradeable_collections(Rarity.INDUSTRIAL)},
        Rarity.INDUSTRIAL, cache, top=1, budget=100,
    )
    # Tout le premier candidat passe avant quoi que ce soit du second.
    premiers = [n for n in noms if "A" in n]
    assert noms[:len(premiers)] == premiers


def test_un_nom_jamais_cote_passe_avant_un_prix_recent(db, cache):
    """Une sortie sans prix n'abaisse pas un candidat, elle l'empeche d'exister."""
    cache.put(quote("Arme A (Factory New)", age=10))
    noms = noms_prioritaires(
        [candidat("10x Collection A")],
        {c.id: c for c in db.tradeable_collections(Rarity.INDUSTRIAL)},
        Rarity.INDUSTRIAL, cache, top=1, budget=100,
    )
    assert noms.index("Arme A (Factory New)") == len(noms) - 1


def test_le_budget_est_respecte(db, cache):
    noms = noms_prioritaires(
        [candidat("10x Collection A"), candidat("10x Collection B")],
        {c.id: c for c in db.tradeable_collections(Rarity.INDUSTRIAL)},
        Rarity.INDUSTRIAL, cache, top=1, budget=3,
    )
    assert len(noms) == 3


# --- Un marche qui se ferme en cours -----------------------------------------


class MarcheQuiFerme:
    name = "steam"

    def __init__(self, avant_fermeture):
        self.restant = avant_fermeture
        self.appels = 0

    def refresh(self, nom):
        from tradeup.pricing.http import RateLimited

        self.appels += 1
        if self.restant <= 0:
            raise RateLimited("429")
        self.restant -= 1
        return quote(nom)


def test_un_refus_en_cours_de_route_garde_ce_qui_a_ete_obtenu(cache):
    """Un classement partiellement rafraichi vaut mieux qu'un echec sec.

    Les fenetres Steam se referment sans preavis ; abandonner le passage
    rendrait la commande inutilisable les jours ou elle sert le plus.
    """
    marche = MarcheQuiFerme(avant_fermeture=3)
    obtenus, epuise = recoter(marche, [f"Objet {i}" for i in range(10)], cache=cache)

    assert obtenus == 3 and epuise is True
    assert cache.get("Objet 0", "steam", ttl=10 ** 9) is not None
    assert cache.get("Objet 5", "steam", ttl=10 ** 9) is None


def test_sans_refus_tout_est_recote(cache):
    marche = MarcheQuiFerme(avant_fermeture=99)
    obtenus, epuise = recoter(marche, ["A", "B"], cache=cache)
    assert obtenus == 2 and epuise is False


# --- L'age d'un contrat ------------------------------------------------------


def test_lage_dun_contrat_est_celui_de_son_plus_vieux_prix(db, cache):
    """Le maximum, pas la moyenne : c'est la ligne perimee qui retourne un verdict."""
    col = db.collection("col_a")
    cache.put(quote("Arme A (Factory New)", age=60))
    cache.put(quote("Sortie A (Factory New)", age=9 * 3600))
    assert age_max(col, Rarity.INDUSTRIAL, cache) == pytest.approx(9 * 3600, rel=0.01)


def test_un_contrat_sans_prix_na_pas_dage(db, cache):
    assert age_max(db.collection("col_a"), Rarity.INDUSTRIAL, cache) is None


# --- Le journal --------------------------------------------------------------


def test_le_journal_rend_la_derive_lisible(tmp_path):
    """Un contrat a 99 % puis 84 % n'est pas un contrat a 90 %."""
    chemin = Path(tmp_path) / "passages.jsonl"
    for prof in (0.99, 0.84):
        p = Passage(rarity="industrial", recotes=10)
        p.lignes.append(Ligne("The Italy Collection", 0.90, 0.85, prof, 0.67, 600))
        journaliser(p, chemin)

    serie = historique(chemin, "The Italy Collection")
    assert [round(v, 2) for _, v in serie] == [0.99, 0.84]


def test_le_journal_sempile_sans_ecraser(tmp_path):
    chemin = Path(tmp_path) / "sous" / "dossier" / "p.jsonl"
    for _ in range(3):
        journaliser(Passage(rarity="industrial"), chemin)
    assert len(chemin.read_text(encoding="utf-8").strip().splitlines()) == 3


def test_une_ligne_de_journal_illisible_ne_casse_pas_la_lecture(tmp_path):
    """Un fichier tronque par un arret brutal doit rester exploitable."""
    chemin = Path(tmp_path) / "p.jsonl"
    p = Passage(rarity="industrial")
    p.lignes.append(Ligne("A", 1.0, 1.0, 1.0, 1.0, 60))
    journaliser(p, chemin)
    with chemin.open("a", encoding="utf-8") as f:
        f.write('{"horodatage": tronq\n')
    journaliser(p, chemin)

    assert len(historique(chemin, "A")) == 2


def test_le_resume_distingue_zero_candidat_dun_budget_epuise():
    """Sinon "aucun contrat" est indiscernable d'un marche ferme."""
    vide = Passage(rarity="industrial", recotes=200)
    ferme = Passage(rarity="industrial", recotes=12, epuise=True)
    assert "aucun contrat" in vide.resume() and "200 recotees" in vide.resume()
    assert "budget epuise" in ferme.resume()


def test_le_passage_serialise_lage_en_heures(tmp_path):
    chemin = Path(tmp_path) / "p.jsonl"
    p = Passage(rarity="industrial")
    p.lignes.append(Ligne("A", 1.0, 1.0, 1.0, 1.0, age_max=9000))
    journaliser(p, chemin)
    d = json.loads(chemin.read_text(encoding="utf-8").splitlines()[0])
    assert d["candidats"][0]["age_h"] == 2.5


class MarcheSansReseau:
    """Le DNS tombe en cours de passage -- machine qui change de reseau."""

    name = "steam"

    def __init__(self, avant_coupure):
        self.restant = avant_coupure

    def refresh(self, nom):
        import urllib.error

        if self.restant <= 0:
            raise urllib.error.URLError("[Errno 11001] getaddrinfo failed")
        self.restant -= 1
        return quote(nom)


def test_une_coupure_reseau_garde_ce_qui_a_ete_obtenu(cache):
    """Meme traitement qu'un refus du marche, et pour la meme raison.

    La distinction n'interesse personne a cet instant : dans les deux cas le
    passage s'arrete avec ses cotations, et la colonne d'age dit lesquelles
    sont fraiches. Laisser remonter l'erreur ferait perdre tout le travail
    deja paye -- c'est ce qui est arrive a un balayage de deux heures, tue
    par une minute sans DNS.
    """
    obtenus, epuise = recoter(MarcheSansReseau(avant_coupure=4),
                              [f"Objet {i}" for i in range(10)], cache=cache)

    assert obtenus == 4 and epuise is True
    assert cache.get("Objet 3", "steam", ttl=10 ** 9) is not None


# --- Confirmation sur annonces reelles ---------------------------------------
# La lecon du 20 septembre : `scan` annoncait le contrat Bank a 148 %, `plan`
# le donnait a -0.3 % sur 356 annonces reelles. L'ecart n'est pas une
# imprecision, c'est un renversement -- et il vient d'une hypothese devenue
# fausse : qu'un bas float s'obtienne au prix du palier.


def test_une_piste_non_confirmee_nest_jamais_achetable():
    piste = Ligne("A", 1.4, 2.2, 1.58, 1.0, age_max=600)
    assert piste.profitability >= 1.0 and piste.frais
    assert piste.confirmee is None
    assert not piste.actionnable


def test_une_piste_confirmee_perdante_nest_pas_achetable():
    """Le cas Bank exactement : 158 % en piste, 99.7 % en reel."""
    x = Ligne("Bank", 1.17, 1.17, 1.58, 1.0, age_max=600, confirmee=0.997)
    assert not x.actionnable
    assert x.ecart_confirmation < 0        # scan etait optimiste


def test_une_piste_confirmee_rentable_et_fraiche_est_achetable():
    x = Ligne("A", 1.0, 1.3, 1.20, 0.9, age_max=600, confirmee=1.30)
    assert x.actionnable and x.ecart_confirmation > 0


def test_une_confirmation_sur_prix_vieux_ne_suffit_pas():
    x = Ligne("A", 1.0, 1.3, 1.20, 0.9, age_max=5 * 24 * 3600, confirmee=1.30)
    assert not x.actionnable


def test_un_melange_reste_une_piste(db, cache, monkeypatch):
    """`plan` est mono-collection : un melange ne peut pas etre confirme.

    L'afficher sans le dire laisserait croire qu'il a ete verifie.
    """
    from tradeup.daily import confirmer

    lignes = [Ligne("7x Collection A + 3x Collection B", 1.0, 1.2, 1.2, 0.9, 600)]
    sortie = confirmer(db, lignes,
                       {c.id: c for c in db.tradeable_collections(Rarity.INDUSTRIAL)},
                       Rarity.INDUSTRIAL, None, combien=3)
    assert sortie[0].confirmee is None
    assert "mono-collection" in sortie[0].motif_echec
    assert not sortie[0].actionnable


def test_un_echec_de_confirmation_ne_fait_pas_tomber_le_passage(db, monkeypatch):
    """Une collection qui casse ne doit pas emporter les autres."""
    from tradeup import daily as mod

    def boum(*a, **k):
        raise RuntimeError("quota")

    monkeypatch.setattr("tradeup.plan.build_plan", boum)
    lignes = [Ligne("Collection A", 1.0, 1.2, 1.2, 0.9, 600)]
    sortie = mod.confirmer(
        db, lignes,
        {c.id: c for c in db.tradeable_collections(Rarity.INDUSTRIAL)},
        Rarity.INDUSTRIAL, None, combien=1)
    assert sortie[0].confirmee is None and not sortie[0].actionnable
    assert "RuntimeError" in sortie[0].motif_echec


def test_pas_assez_dannonces_se_distingue_dun_contrat_perdant(db, monkeypatch):
    """Sans la distinction, "aucune occasion" masque "aucune donnee"."""
    from tradeup import daily as mod

    monkeypatch.setattr("tradeup.plan.build_plan", lambda *a, **k: None)
    lignes = [Ligne("Collection A", 1.0, 1.2, 1.2, 0.9, 600)]
    sortie = mod.confirmer(
        db, lignes,
        {c.id: c for c in db.tradeable_collections(Rarity.INDUSTRIAL)},
        Rarity.INDUSTRIAL, None, combien=1)
    assert sortie[0].motif_echec == "pas assez d'annonces"


# --- Reprise d'un balayage interrompu ----------------------------------------


def test_le_balayage_reprend_par_les_collections_jamais_faites(tmp_path):
    """Sans cet ordre, un balayage coupe refait eternellement les memes.

    88 collections Mil-Spec a 8 requetes/minute depassent largement le budget
    d'une nuit. Si le passage repart chaque fois de la premiere, les
    collections de la fin ne sont JAMAIS calculees -- le meme piege que le TTL
    qui ne remontait pas jusqu'a la source.
    """
    from tradeup.journal import Journal

    j = Journal(Path(tmp_path) / "j.db")
    plan = {"collection": "A", "cost": 1.0, "net": 1.0, "profit": 0.0,
            "roi": 0.0, "avg_float": 0.1, "inputs": [], "outcomes": []}
    j.save_plan(plan, collection_id="col_a", rarity="mil-spec")
    time.sleep(0.02)
    j.save_plan(plan, collection_id="col_b", rarity="mil-spec")

    vues = j.last_swept("mil-spec")
    assert set(vues) == {"col_a", "col_b"}
    assert vues["col_b"] > vues["col_a"], "col_b est la plus recente"

    # L'ordre de traitement : jamais vues d'abord, puis la plus ancienne.
    ids = ["col_c", "col_b", "col_a"]
    ids.sort(key=lambda i: vues.get(i, 0.0))
    assert ids == ["col_c", "col_a", "col_b"]
    j.close()


def test_une_rarete_jamais_balayee_ne_casse_pas_l_ordre(tmp_path):
    from tradeup.journal import Journal

    j = Journal(Path(tmp_path) / "j.db")
    assert j.last_swept("mil-spec") == {}
    j.close()
