"""Tests de la ligne de commande.

Motivation : la suite est passee au vert alors que `cli.py` contenait une
erreur de syntaxe. Aucun test ne l'importait. Ces tests garantissent au minimum
que le module se charge et que les options sont cablees sur les bons defauts --
une option mal branchee ne fait pas planter, elle fausse silencieusement les
resultats.
"""

from __future__ import annotations

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
