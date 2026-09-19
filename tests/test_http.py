"""Tests du client HTTP partage.

Motivation : un en-tete a coute deux heures de diagnostic. Steam repond 429 a
tout `User-Agent` qu'il ne reconnait pas -- des la premiere requete, sans
rapport avec le debit -- et le projet en envoyait un a lui. Le scan Industrial
mourait donc systematiquement, en affichant un message de quota epuise.
"""

from __future__ import annotations

from tradeup.pricing.http import HttpClient, RateLimiter


def test_aucun_user_agent_maison_par_defaut():
    """Le defaut doit laisser urllib annoncer son propre nom.

    Ce n'est pas cosmetique : une chaine maison est refusee en bloc par Steam,
    quelle qu'elle soit. Verifie a l'epoque sur "cs-tradeup-algo/0.1", sur une
    chaine quelconque, sur un mot isole et sur la chaine vide -- tous 429,
    quand "Python-urllib/3.x" passait au meme instant.
    """
    assert "User-Agent" not in HttpClient().headers


def test_un_appelant_peut_toujours_imposer_le_sien():
    """Une autre API peut en exiger un ; le point d'extension reste ouvert."""
    c = HttpClient(user_agent="autre-outil/2.0")
    assert c.headers["User-Agent"] == "autre-outil/2.0"


def test_les_en_tetes_explicites_priment():
    c = HttpClient(user_agent="a/1", headers={"User-Agent": "b/2"})
    assert c.headers["User-Agent"] == "b/2"


def test_accept_json_est_toujours_annonce():
    assert HttpClient().headers["Accept"] == "application/json"


def test_le_limiteur_de_debit_reste_en_place():
    """Le correctif porte sur l'identite du client, pas sur son rythme."""
    limiteur = RateLimiter(max_calls=8)
    c = HttpClient(rate_limiter=limiteur)
    assert c.limiter is limiteur
    assert limiteur.max_calls == 8
