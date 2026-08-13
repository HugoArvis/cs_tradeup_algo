"""Tests de la source CSFloat, avec un faux client HTTP.

PORTEE DE CES TESTS -- a lire avant de leur faire confiance.

Ils valident la logique de parsing et de selection A SCHEMA DONNE. Ils ne
valident PAS que le schema suppose est le bon : les reponses simulees ici sont
ecrites a partir de la meme hypothese que le code (`data[].item.float_value`,
prix en centimes, parametre `max_float`). Si cette hypothese est fausse, ces
tests passent au vert en confirmant l'erreur.

Seul un appel reel avec une cle API peut lever ce doute. Voir README, section
"Etat du projet".
"""

from __future__ import annotations

import pytest

from tradeup.pricing.csfloat import CSFloat


class FakeHttpClient:
    """Renvoie une reponse figee et enregistre les parametres recus."""

    def __init__(self, response):
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    def get_json(self, url, params=None):
        self.calls.append((url, dict(params or {})))
        return self.response


def make_row(listing_id, price_cents, float_value=0.05, name="AK-47 | Redline (FT)"):
    return {
        "id": listing_id,
        "price": price_cents,
        "item": {
            "market_hash_name": name,
            "float_value": float_value,
            "paint_seed": 42,
        },
    }


def make_source(response) -> tuple[CSFloat, FakeHttpClient]:
    client = FakeHttpClient(response)
    return CSFloat("fausse-cle", client=client), client


# --- Parsing -----------------------------------------------------------------


def test_les_prix_sont_convertis_depuis_les_centimes():
    src, _ = make_source({"data": [make_row("a", 1297), make_row("b", 305)]})
    listings = src.listings("peu importe")
    assert [l.price for l in listings] == [12.97, 3.05]


def test_reponse_en_liste_nue_acceptee():
    # L'API a deja repondu tantot {"data": [...]} tantot [...].
    src, _ = make_source([make_row("a", 500)])
    assert len(src.listings("x")) == 1


def test_reponse_vide_ou_absente():
    for response in ({"data": []}, [], None):
        src, _ = make_source(response)
        assert src.listings("x") == []


def test_lignes_inexploitables_ignorees_sans_planter():
    src, _ = make_source(
        {
            "data": [
                make_row("ok", 1000),
                "chaine parasite",  # pas un dict
                {"id": "sans_prix", "item": {}},  # price manquant
                {"id": "sans_item", "price": 250},  # item manquant
            ]
        }
    )
    listings = src.listings("x")
    # La ligne sans item reste exploitable : c'est le PRIX qui est indispensable.
    assert {l.listing_id for l in listings} == {"ok", "sans_item"}
    assert next(l for l in listings if l.listing_id == "sans_item").float_value is None


def test_float_absent_donne_has_float_false():
    src, _ = make_source({"data": [make_row("a", 100, float_value=None)]})
    listing = src.listings("x")[0]
    assert listing.float_value is None
    assert listing.has_float is False


# --- Parametres envoyes ------------------------------------------------------


def test_les_filtres_de_float_sont_transmis_au_serveur():
    src, client = make_source({"data": []})
    src.listings("AK-47 | Redline (Field-Tested)", max_float=0.08, min_float=0.01)

    _, params = client.calls[0]
    assert params["max_float"] == 0.08
    assert params["min_float"] == 0.01
    assert params["market_hash_name"] == "AK-47 | Redline (Field-Tested)"
    assert params["type"] == "buy_now"
    assert params["sort_by"] == "lowest_price"


def test_les_filtres_absents_ne_sont_pas_envoyes():
    src, client = make_source({"data": []})
    src.listings("x")
    _, params = client.calls[0]
    assert "max_float" not in params and "min_float" not in params


def test_limite_plafonnee_a_50():
    src, client = make_source({"data": []})
    src.listings("x", limit=500)
    assert client.calls[0][1]["limit"] == 50


# --- Selection ---------------------------------------------------------------


def test_cheapest_at_float_refiltre_cote_client():
    """Le filtre serveur n'est pas cru sur parole.

    Si l'API ignore `max_float` (parametre renomme, quota, bug), accepter sa
    reponse telle quelle ferait acheter un objet hors contrainte -- et ferait
    derailler la moyenne de float du contrat.
    """
    src, _ = make_source(
        {
            "data": [
                make_row("cher_conforme", 1000, float_value=0.04),
                make_row("pas_cher_hors_contrainte", 100, float_value=0.30),
                make_row("moyen_conforme", 500, float_value=0.06),
            ]
        }
    )
    best = src.cheapest_at_float("x", max_float=0.07)
    assert best is not None
    assert best.listing_id == "moyen_conforme"  # pas le moins cher dans l'absolu
    assert best.float_value <= 0.07


def test_cheapest_at_float_sans_candidat():
    src, _ = make_source({"data": [make_row("a", 100, float_value=0.5)]})
    assert src.cheapest_at_float("x", max_float=0.07) is None


def test_cheapest_at_float_ignore_les_offres_sans_float():
    src, _ = make_source({"data": [make_row("a", 100, float_value=None)]})
    assert src.cheapest_at_float("x", max_float=0.07) is None


# --- Cotation agregee --------------------------------------------------------


def test_fetch_agrege_lowest_et_median():
    src, _ = make_source(
        {"data": [make_row("a", 300), make_row("b", 500), make_row("c", 1100)]}
    )
    q = src.fetch("x", use_cache=False)
    assert q is not None
    assert q.lowest_price == 3.00
    assert q.median_price == 5.00
    assert q.source == "csfloat"
    # L'endpoint listings ne renseigne pas le volume de ventes.
    assert q.volume is None


def test_fetch_sans_offre_renvoie_none():
    src, _ = make_source({"data": []})
    assert src.fetch("x", use_cache=False) is None


def test_cle_api_obligatoire():
    with pytest.raises(ValueError, match="[Cc]le API"):
        CSFloat("")
