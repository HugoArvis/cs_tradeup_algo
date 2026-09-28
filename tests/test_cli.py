"""Tests de la ligne de commande.

Motivation : la suite est passee au vert alors que `cli.py` contenait une
erreur de syntaxe. Aucun test ne l'importait. Ces tests garantissent au minimum
que le module se charge et que les options sont cablees sur les bons defauts --
une option mal branchee ne fait pas planter, elle fausse silencieusement les
resultats.
"""

from __future__ import annotations

import types
from unittest import mock

import pytest

from tradeup import cli


def parse(*argv: str):
    return cli.build_parser().parse_args(list(argv))


def test_le_module_se_charge():
    # Attrape les erreurs de syntaxe et d'import, invisibles pour les autres tests.
    assert callable(cli.main)
    assert callable(cli.build_parser)


def test_toutes_les_sous_commandes_sont_declarees():
    for name in ("db", "inspect", "collections", "price", "scan", "cache"):
        args = parse(name, *(["x"] if name in ("inspect", "price") else []))
        assert hasattr(args, "func"), name


def test_collections_filtre_par_nombre_de_sorties():
    a = parse("collections", "--rarity", "mil-spec", "--max-outcomes", "2")
    assert a.max_outcomes == 2
    assert parse("collections").max_outcomes is None


def test_defauts_du_scan_conformes_a_la_documentation():
    a = parse("scan")
    assert a.rarity == "mil-spec"
    assert a.max_collections == 1  # mono-collection : faible variance
    assert a.float_pct == 0.15
    assert a.float_safety == 0.02  # ~3 ecarts-types, mesure sur lots reels
    assert a.min_volume == 5  # un objet sans vente n'a pas de prix reel
    assert a.margin == 0.05
    assert a.min_roi == 0.03
    assert a.max_unpriced == 0.02
    assert a.rank == "risk_adjusted"
    assert a.offline is False


def test_options_de_securite_transmises():
    a = parse("scan", "--float-pct", "0.5", "--min-cliff", "0.01",
              "--float-safety", "0.02", "--min-volume", "20")
    assert a.float_pct == 0.5
    assert a.min_cliff == 0.01
    assert a.float_safety == 0.02
    assert a.min_volume == 20


def test_plusieurs_collections_acceptees():
    a = parse("scan", "--collections", "The Fracture Collection", "The Recoil Collection")
    assert a.collections == ["The Fracture Collection", "The Recoil Collection"]


def test_simulation_et_graine():
    a = parse("scan", "--simulate", "30", "--seed", "7")
    assert a.simulate == 30 and a.seed == 7
    assert parse("scan").simulate is None


def test_raretes_valides_uniquement():
    assert parse("scan", "--rarity", "restricted").rarity == "restricted"
    with pytest.raises(SystemExit):
        parse("scan", "--rarity", "covert")  # Covert ne peut pas etre une entree


def test_max_collections_limite_a_deux():
    assert parse("scan", "--max-collections", "2").max_collections == 2
    with pytest.raises(SystemExit):
        parse("scan", "--max-collections", "3")


def test_alias_de_rarete_couvrent_les_entrees_possibles():
    from tradeup.models import TRADEABLE_INPUT_RARITIES

    cibles = set(cli.RARITY_ALIASES.values())
    assert cibles == set(TRADEABLE_INPUT_RARITIES)


def test_sous_commande_obligatoire():
    with pytest.raises(SystemExit):
        parse()


def test_db_introuvable_renvoie_un_code_derreur(tmp_path, capsys):
    code = cli.main(["--db", str(tmp_path / "absent.json"), "db"])
    assert code == 1
    assert "introuvable" in capsys.readouterr().err.lower()


# --- Marche d'achat CSFloat --------------------------------------------------


def test_marche_dachat_par_defaut_sur_steam():
    a = parse("scan")
    assert a.buy_market == "steam"
    assert a.sell_market == "steam"
    assert a.csfloat_rate == 10  # quota sur fenetre longue


def test_marches_dachat_et_de_revente_independants():
    a = parse("scan", "--buy-market", "csfloat", "--currency", "USD")
    assert a.buy_market == "csfloat" and a.sell_market == "steam"


def test_achat_csfloat_hors_usd_refuse(monkeypatch, capsys):
    # Additionner un cout en USD et un produit de vente en EUR donne un nombre
    # qui ressemble a un profit sans en etre un : mieux vaut refuser.
    monkeypatch.setattr(cli, "csfloat_api_key", lambda *a, **k: "cle-de-test")
    args = parse("scan", "--buy-market", "csfloat", "--currency", "EUR")
    with pytest.raises(SystemExit) as exc:
        cli._make_pricer(args)
    assert exc.value.code == 2
    assert "--buy-market csfloat" in capsys.readouterr().err


def test_achat_et_revente_csfloat_partagent_une_seule_source(monkeypatch):
    # Deux instances auraient chacune leur rate-limiter et emettraient le double
    # du debit annonce -- exactement ce qui declenche les 429.
    monkeypatch.setattr(cli, "csfloat_api_key", lambda *a, **k: "cle-de-test")
    args = parse("scan", "--buy-market", "csfloat", "--sell-market", "csfloat",
                 "--currency", "USD")
    pricer, cache = cli._make_pricer(args)
    try:
        assert pricer.buy_source is pricer.sell_source
        assert pricer.buy_source.name == "csfloat"
        assert pricer.buy_fees.name == "csfloat"  # les frais suivent le marche
        assert pricer.sell_fees.name == "csfloat"
    finally:
        cache.close()


def test_duree_estimee_couvre_les_deux_marches(monkeypatch):
    # `warm()` precharge sur chaque marche distinct : ne compter que l'achat
    # sous-estimait l'attente de moitie.
    monkeypatch.setattr(cli, "csfloat_api_key", lambda *a, **k: "cle-de-test")
    seul = parse("scan", "--currency", "USD")
    mixte = parse("scan", "--sell-market", "csfloat", "--currency", "USD")
    p1, c1 = cli._make_pricer(seul)
    p2, c2 = cli._make_pricer(mixte)
    try:
        steam_seul = p1.buy_source.estimated_duration(600)
        deux = sum(s.estimated_duration(600)
                   for s in dict.fromkeys([p2.buy_source, p2.sell_source]))
        assert deux > steam_seul
    finally:
        c1.close()
        c2.close()


# --- Commande verify ---------------------------------------------------------


def test_verify_est_declaree():
    a = parse("verify", "AK-47 | Redline (Field-Tested)")
    assert a.func is cli.cmd_verify
    assert a.names == ["AK-47 | Redline (Field-Tested)"]
    assert a.market == "steam"
    assert a.max_drift == 0.05


def test_verify_sans_cible_refuse(capsys):
    # Ne rien verifier silencieusement donnerait un "tout va bien" mensonger.
    code = cli.main(["verify"])
    assert code == 2
    assert "Rien a verifier" in capsys.readouterr().err


def test_verify_collection_introuvable(capsys):
    assert cli.main(["verify", "--collection", "Collection Imaginaire"]) == 1
    assert "introuvable" in capsys.readouterr().err.lower()


def test_stattrak_est_declare_sur_les_commandes_utiles():
    assert parse("scan").stattrak is False
    assert parse("scan", "--stattrak").stattrak is True
    assert parse("collections", "--stattrak").stattrak is True
    assert parse("inspect", "X", "--stattrak").stattrak is True


def test_inspect_refuse_une_collection_sans_stattrak(capsys):
    code = cli.main(["inspect", "The Bank Collection", "--rarity", "industrial",
                     "--stattrak"])
    assert code == 1
    assert "StatTrak" in capsys.readouterr().err


def test_inventory_est_declaree():
    a = parse("inventory", "--file", "x.json", "--rarity", "industrial")
    assert a.func is cli.cmd_inventory
    assert a.file == "x.json"
    assert a.show_losing is False
    # On ne choisit pas ce qu'on possede deja : pas de filtre de volume impose.
    assert a.min_volume == 0


def test_inventory_fichier_absent(capsys):
    assert cli.main(["inventory", "--file", "inexistant.json"]) == 1
    assert "introuvable" in capsys.readouterr().err.lower()


def test_le_seuil_de_profitabilite_se_traduit_en_roi():
    """L'utilisateur pose son critere en profitabilite (1.0 = point mort),
    le moteur raisonne en ROI. La conversion doit etre exacte, sinon on
    compare 40 a 100."""
    a = parse("scan", "--min-profitability", "1.2")
    assert a.min_profitability == pytest.approx(1.2)
    # 120 % de profitabilite = +20 % de ROI.
    assert a.min_profitability - 1.0 == pytest.approx(0.20)
    assert parse("scan").min_profitability is None  # sans effet par defaut


def test_le_rejet_donne_les_deux_conventions():
    """Un motif qui n'affiche que le ROI invite a le confondre avec la
    profitabilite lue dans une video."""
    from tradeup.ev import Outcome, TradeUpResult
    from tradeup.models import Rarity, Skin, Wear
    from tradeup.scoring import ScreenConfig

    skin = Skin("s", "S", "c", Rarity.RESTRICTED, 0.0, 1.0)
    perdant = TradeUpResult(
        outcomes=(Outcome(skin=skin, float_value=0.1, wear=Wear.FACTORY_NEW,
                          probability=1.0, net_value=40.0, priced=True),),
        inputs=(), cost=100.0, avg_input_float=0.1, avg_normalized=0.1,
        stattrak=False, unpriced_probability=0.0)

    screen = ScreenConfig(min_roi=0.0)
    assert not screen.passes(perdant)
    motif = " ".join(screen.last_reasons)
    assert "ROI" in motif and "profitabilite" in motif
    assert "40%" in motif  # le contrat rend 40 centimes par euro


def test_un_429_steam_nest_pas_annonce_comme_un_quota_csfloat(capsys):
    """Un scan interroge les deux marches : le message doit nommer le bon.

    Annoncer "quota CSFloat" sur un 429 de Steam envoie chercher une cle API
    la ou il fallait attendre et baisser le debit.
    """
    from tradeup.pricing.http import RateLimited

    def echoue(_args):
        raise RateLimited(
            "429 persistant sur https://steamcommunity.com/market/priceoverview/"
        )

    faux = types.SimpleNamespace(func=echoue, verbose=False)
    with mock.patch("tradeup.cli.build_parser") as bp:
        bp.return_value.parse_args.return_value = faux
        code = cli.main([])

    err = capsys.readouterr().err
    assert code == 3
    assert "Steam" in err and "CSFloat" not in err


def test_un_429_csfloat_garde_son_message(capsys):
    from tradeup.pricing.http import RateLimited

    def echoue(_args):
        raise RateLimited("429 persistant sur https://csfloat.com/api/v1/listings")

    faux = types.SimpleNamespace(func=echoue, verbose=False)
    with mock.patch("tradeup.cli.build_parser") as bp:
        bp.return_value.parse_args.return_value = faux
        code = cli.main([])

    err = capsys.readouterr().err
    assert code == 3
    assert "CSFloat" in err


def test_le_ttl_demande_atteint_la_source_pas_seulement_le_cache():
    """Regle : c'est la SOURCE qui juge une cotation perimee, pas le cache.

    `QuoteCache` conserve tout ; a chaque lecture la source lui impose son
    propre TTL. Tant que `--ttl` n'arrivait qu'au cache, la source gardait son
    defaut de 6 h et redemandait des prix deja acquis -- un scan brulait son
    budget Steam a recoter les memes 813 noms sans jamais atteindre les 865
    manquants, en affichant une progression normale.
    """
    faux = types.SimpleNamespace(
        ttl=168, currency="EUR", rate=4, offline=False, margin=0.05,
        min_volume=0, min_input_volume=3, buy_fees="steam", sell_fees="steam",
        buy_market="steam", sell_market="steam", price_basis="listing",
    )
    pricer, cache = cli._make_pricer(faux)

    assert pricer.buy_source.ttl == 168 * 3600
    assert pricer.sell_source.ttl == 168 * 3600
    # Le cache porte la meme duree : les deux doivent s'accorder, sinon l'un
    # purge ce que l'autre croit encore valable.
    assert cache.ttl == 168 * 3600


def test_aucun_nom_non_defini_dans_le_paquet():
    """Un import manquant ne se voit qu'a l'execution de SA commande.

    `cmd_sweep` referencait `Journal` sans l'importer. Le module se chargeait,
    le parseur acceptait la commande, tous les tests passaient -- et le
    balayage planifie de 3h11 aurait plante a sa premiere ligne. Une nuit
    perdue, sans personne pour le voir.

    pyflakes lit le paquet entier sans l'executer : c'est le seul moyen
    d'attraper ca avant que le planificateur ne le fasse.
    """
    import subprocess
    import sys
    from pathlib import Path

    paquet = Path(__file__).resolve().parents[1] / "src" / "tradeup"
    r = subprocess.run([sys.executable, "-m", "pyflakes", str(paquet)],
                       capture_output=True, text=True)
    graves = [l for l in r.stdout.splitlines() if "undefined name" in l]
    assert not graves, "noms non definis : " + " | ".join(graves)


def test_le_payload_du_balayage_convertit_tous_les_montants():
    """CSFloat cote en USD, Steam dans la devise du compte.

    Sans conversion, le cout est en dollars et la revente en euros : le
    rapport des deux n'est plus une profitabilite mais un taux de change
    deguise. Mesure sur The Dead Hand Collection -- 110 % annonce contre
    126 % reel. L'erreur allait dans le sens PESSIMISTE, les montants USD
    etant numeriquement plus gros que leur equivalent en euros : elle
    faisait donc ecarter des contrats rentables.
    """
    class FauxResultat:
        cost = 10.0
        ev_net = 12.0
        ev_profit = 2.0
        roi = 0.2
        profitability = 1.2
        profit_probability = 0.8
        distinct_outcomes = 1
        stdev = 1.0
        avg_input_float = 0.1
        outcomes = ()

    class FauxSkin:
        name = "X"

    class FauxOption:
        name = "X (Factory New)"
        float_value = 0.01
        unit_cost = 1.0
        url = None
        skin = FauxSkin()

    class FauxRarete:
        label = "Mil-Spec Grade"

        @property
        def next_up(self):
            return self

    class FauxPlan:
        result = FauxResultat()
        collection = types.SimpleNamespace(name="C")
        rarity = FauxRarete()
        options = (FauxOption(),)
        listings_examined = 10
        float_slack = 0.01
        worst_profit = 1.0
        best_profit = 3.0
        all_outcomes_profitable = True
        downgrade_profit = -1.0
        replis = 0
        alt_cost = 14.0            # le meme panier coute plus cher sur Steam
        alt_profitability = 12.0 / 14.0
        deep_cost = 11.0           # et plus cher encore sans arriver premier
        deep_profitability = 12.0 / 11.0
        fragile = False
        float_subi_compatible = True

        def steam_order_budget(self, target_roi=0.20):
            return 12.0 / 1.2

        def steam_order_discount(self, target_roi=0.20):
            return 1.0 - (12.0 / 1.2) / 14.0

    d = cli._plan_payload(FauxPlan(), "EUR", 0.8779)

    assert d["currency"] == "EUR"
    assert d["cost"] == pytest.approx(8.779)
    assert d["net"] == pytest.approx(10.5348)
    assert d["profit"] == pytest.approx(1.7558)
    assert d["inputs"][0]["price"] == pytest.approx(0.8779)
    # Le ROI est un RAPPORT : il ne se convertit pas.
    assert d["roi"] == pytest.approx(0.2)
    # Le cout alternatif se convertit, sa profitabilite non.
    assert d["cost_alt"] == pytest.approx(12.2906)
    assert d["profitability_alt"] == pytest.approx(0.8571, abs=1e-4)
    assert d["buy_market"] == "CSFloat" and d["sell_market"] == "Steam"
    assert d["cost_deep"] == pytest.approx(9.6569)
    assert d["profitability_deep"] == pytest.approx(1.0909, abs=1e-4)
    assert d["fragile"] is False
    # La voie ordre Steam : le budget se convertit, le rabais est un RAPPORT.
    assert d["float_subi_ok"] is True
    assert d["order_budget"] == pytest.approx(8.779)
    assert d["order_discount"] == pytest.approx(0.2857, abs=1e-4)
