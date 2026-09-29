"""Tests de la planification du passage quotidien.

Le module ne fait que CONSTRUIRE les commandes ; le CLI les execute. C'est ce
qui rend l'enregistrement verifiable sans jamais toucher au planificateur de
la machine qui fait tourner les tests.
"""

from __future__ import annotations

from pathlib import Path

from tradeupfinder.schedule import (
    PASSAGES,
    PREFIXE,
    equivalent_cron,
    lanceur,
    taches,
)


def test_deux_passages_a_des_heures_eloignees():
    """Un creneau unique rate la fenetre Steam la moitie du temps.

    Observe le 19 septembre 2026 : neuf refus consecutifs de 04:38 a 11:20,
    puis une fenetre ouverte a 11:35. Deux creneaux eloignes divisent ce
    risque sans doubler la consommation -- le second constate que les
    candidats de tete sont deja frais.
    """
    assert len(PASSAGES) == 2
    heures = [int(h.split(":")[0]) for _, h in PASSAGES]
    assert abs(heures[0] - heures[1]) >= 8


def test_chaque_tache_porte_le_prefixe_du_projet():
    """Sans prefixe commun, on ne peut ni les retrouver ni les retirer."""
    assert all(t.nom.startswith(PREFIXE) for t in taches())
    # Deux passages courts + le balayage de nuit, tous distincts.
    assert len({t.nom for t in taches()}) == len(taches())


def test_la_tache_doit_pouvoir_demarrer_sur_batterie():
    """Le defaut de Windows est un echec quotidien SILENCIEUX.

    Mesure sur cette machine : une tache creee par `schtasks /create` garde
    `DisallowStartIfOnBatteries` a vrai. Sur un portable sur batterie elle
    reste en file -- etat "Queued", dernier resultat 0, aucune sortie, aucune
    erreur. Rien ne distingue ce cas d'une tache qui tourne bien.
    """
    cmd = " ".join(taches()[0].creer())
    assert "-AllowStartIfOnBatteries" in cmd
    assert "-DontStopIfGoingOnBatteries" in cmd


def test_un_passage_manque_est_rattrape():
    """Si la machine dormait a l'heure dite, la journee entiere sauterait --
    et la serie du journal, qui n'a de valeur que par sa continuite, se
    trouerait."""
    assert "-StartWhenAvailable" in " ".join(taches()[0].creer())


def test_la_reinstallation_remplace_au_lieu_d_echouer():
    """Sinon l'ANCIENNE version du lanceur continue de tourner : le pire des
    deux mondes."""
    assert "-Force" in " ".join(taches()[0].creer())


def test_la_commande_porte_le_lanceur_et_l_heure():
    t = taches()[0]
    cmd = " ".join(t.creer())
    assert t.heure in cmd
    assert "passage_quotidien" in cmd
    assert "-Daily" in cmd


def test_la_suppression_ne_demande_pas_de_confirmation():
    """Une suppression interactive bloquerait un script sans terminal."""
    assert "-Confirm:$false" in " ".join(taches()[0].supprimer())


def test_l_etat_signale_une_tache_presente_mais_bloquee():
    """Une tache qui existe sans pouvoir partir n'est pas une tache qui
    tourne, et c'est exactement ce qu'on a observe."""
    assert "DisallowStartIfOnBatteries" in " ".join(taches()[0].etat())


def test_le_lanceur_existe_vraiment():
    """Enregistrer une tache vers un script absent cree un echec quotidien
    silencieux : le planificateur signale un code d'erreur que personne ne
    lit."""
    assert lanceur().exists(), f"lanceur manquant : {lanceur()}"


def test_l_equivalent_cron_est_propose_pour_les_autres_systemes(tmp_path):
    """Mieux vaut donner de quoi faire a la main que refuser sans rien
    proposer."""
    lignes = equivalent_cron(Path(tmp_path)).splitlines()
    assert len(lignes) == 2
    for ligne, (_, heure) in zip(lignes, PASSAGES):
        h, m = heure.split(":")
        assert ligne.startswith(f"{int(m)} {int(h)} * * *")
        assert PREFIXE in ligne


def test_le_balayage_long_est_une_tache_a_part():
    """Un travail d'une heure et demie ne doit pas partager le creneau d'un
    passage court : l'un bloquerait l'autre.

    Il tourne de nuit, une seule fois -- c'est la raison d'etre de la
    separation, pas un detail d'organisation.
    """
    from tradeupfinder.schedule import BALAYAGE

    noms = [t.nom for t in taches()]
    assert f"{PREFIXE}-balayage" in noms
    assert len(taches()) == len(PASSAGES) + 1

    bal = next(t for t in taches() if t.nom.endswith("balayage"))
    heure = int(bal.heure.split(":")[0])
    assert 0 <= heure <= 5, "le balayage doit tourner la nuit"
    assert bal.lanceur.name == BALAYAGE[2]
    assert bal.lanceur != lanceur(), "lanceur distinct du passage court"


def test_le_lanceur_du_balayage_existe():
    bal = next(t for t in taches() if t.nom.endswith("balayage"))
    assert bal.lanceur.exists(), f"lanceur manquant : {bal.lanceur}"


# --- Renommage du projet -------------------------------------------------------
# Les taches sont enregistrees dans Windows sous un nom qui contient le nom du
# projet. Renommer le projet ne renomme pas les taches deja posees : elles
# continuent de tourner, mais le code ne les reconnait plus. Il en enregistre
# alors de nouvelles, et deux passages partent a la meme heure en se volant le
# quota Steam -- les deux REUSSISSENT, donc aucun journal ne signale rien.


def test_les_anciens_prefixes_sont_connus():
    """Sans cette liste, rien ne peut retrouver les taches d'avant."""
    from tradeupfinder.schedule import ANCIENS_PREFIXES, PREFIXE

    assert PREFIXE == "tradeupfinder-quotidien"
    assert "tradeup-quotidien" in ANCIENS_PREFIXES
    # Un ancien prefixe egal au nouveau ferait passer les taches courantes pour
    # des survivantes, et `--remove` les retirerait deux fois.
    assert PREFIXE not in ANCIENS_PREFIXES


def test_la_recherche_des_survivantes_couvre_tous_les_anciens_prefixes():
    from tradeupfinder.schedule import ANCIENS_PREFIXES, survivantes

    commande = " ".join(survivantes())
    for prefixe in ANCIENS_PREFIXES:
        assert f"'{prefixe}-*'" in commande, prefixe
    # Silencieux : l'absence de tache n'est pas une erreur a remonter.
    assert "SilentlyContinue" in commande


def test_retirer_une_ancienne_tache_ne_demande_pas_confirmation():
    """Le planificateur tourne sans terminal : une invite bloquerait tout."""
    from tradeupfinder.schedule import retire_ancienne

    commande = " ".join(retire_ancienne("tradeup-quotidien-matin"))
    assert "Unregister-ScheduledTask" in commande
    assert "-Confirm:$false" in commande
    assert "tradeup-quotidien-matin" in commande
