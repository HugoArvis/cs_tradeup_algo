"""Tests des trade-ups calcules depuis l'inventaire possede.

Le risque principal n'est pas technique, il est economique. Valoriser a zero
des objets "qu'on a deja" rend TOUS les contrats rentables et pousse a fondre
des skins qui valaient mieux vendus. Le cout retenu doit etre le cout
d'OPPORTUNITE -- ce qu'on encaisserait en revendant -- et plusieurs tests ici
ne verifient rien d'autre que ca.
"""

from __future__ import annotations

import json

import pytest

from tradeup.db import SkinDatabase
from tradeup.inventory import (
    OwnedItem,
    best_tradeups,
    closest_gaps,
    from_csfloat_rows,
    load_file,
    resolve,
    summary,
)
from tradeup.models import Rarity, Skin
from tradeup.pricing.repository import StaticPricer
from tradeup.wear import wear_of


@pytest.fixture(scope="module")
def db():
    return SkinDatabase.load()


@pytest.fixture(scope="module")
def bank(db):
    return db.find_collection("The Bank Collection")


def inventaire(bank, n=10, rarity=Rarity.INDUSTRIAL, **kw):
    """`n` objets possedes de cette collection, floats bas."""
    skins = bank.by_rarity(rarity)
    items = []
    for i in range(n):
        s = skins[i % len(skins)]
        f = s.min_float + 0.05 * (s.max_float - s.min_float)
        items.append(OwnedItem(
            market_hash_name=s.market_hash_name(wear_of(f)),
            float_value=f,
            asset_id=f"a{i}",
            **kw,
        ))
    return items


def prix(bank, rarity=Rarity.INDUSTRIAL, entree=1.0, sortie=100.0):
    valeurs = {}
    for s in bank.by_rarity(rarity):
        for w in s.available_wears():
            valeurs[s.market_hash_name(w)] = entree
    for s in bank.by_rarity(rarity.next_up):
        for w in s.available_wears():
            valeurs[s.market_hash_name(w)] = sortie
    return StaticPricer(valeurs)


# --- Le point economique -----------------------------------------------------


def test_les_entrees_sont_valorisees_a_leur_prix_de_revente(db, bank):
    """Et non a zero, ni a ce qu'on a paye."""
    items = inventaire(bank)
    plans = best_tradeups(db, items, prix(bank, entree=2.0), Rarity.INDUSTRIAL,
                          include_losing=True)
    assert plans
    # 10 entrees a 2.0, moins les frais de revente Steam : le cout doit s'en
    # approcher, et surtout ne pas etre nul.
    assert 0 < plans[0].opportunity_cost < 20.0
    assert plans[0].opportunity_cost == pytest.approx(plans[0].result.cost)


def test_un_contrat_qui_detruit_de_la_valeur_est_perdant(db, bank):
    """Des entrees qui valent plus que la sortie ne doivent pas paraitre gratuites.

    C'est le piege que le cout d'opportunite existe pour eviter : a cout nul,
    ce contrat afficherait un gain positif et on fondrait des skins qui
    valaient mieux vendus.
    """
    items = inventaire(bank)
    pricer = prix(bank, entree=50.0, sortie=1.0)
    assert best_tradeups(db, items, pricer, Rarity.INDUSTRIAL) == []

    perdants = best_tradeups(db, items, pricer, Rarity.INDUSTRIAL,
                             include_losing=True)
    assert perdants and perdants[0].gain < 0
    assert "PERDANT" in perdants[0].report()


def test_le_prix_paye_ninfluence_pas_la_decision(db, bank):
    """Un cout irrecuperable ne doit peser sur aucune decision."""
    pricer = prix(bank)
    sans = best_tradeups(db, inventaire(bank), pricer, Rarity.INDUSTRIAL)
    avec = best_tradeups(db, inventaire(bank, paid=999.0), pricer,
                         Rarity.INDUSTRIAL)
    assert sans and avec
    assert sans[0].gain == pytest.approx(avec[0].gain)


# --- Selection ---------------------------------------------------------------


def test_il_faut_dix_objets_de_la_meme_collection(db, bank):
    assert best_tradeups(db, inventaire(bank, n=9), prix(bank),
                         Rarity.INDUSTRIAL, include_losing=True) == []
    assert best_tradeups(db, inventaire(bank, n=10), prix(bank),
                         Rarity.INDUSTRIAL, include_losing=True) != []


def test_chaque_objet_ne_sert_quune_fois(db, bank):
    """On possede des objets uniques, pas un stock au meilleur prix."""
    items = inventaire(bank, n=12)
    plans = best_tradeups(db, items, prix(bank), Rarity.INDUSTRIAL,
                          include_losing=True)
    assert plans
    ids = [i.asset_id for i in plans[0].items]
    assert len(ids) == 10
    assert len(set(ids)) == 10


def test_les_floats_sont_exacts_donc_sans_alea(db, bank):
    from tradeup.floatrisk import average_sigma

    plans = best_tradeups(db, inventaire(bank), prix(bank), Rarity.INDUSTRIAL,
                          include_losing=True)
    assert average_sigma(plans[0].options) == 0.0
    assert all(o.owned and o.exact_float for o in plans[0].options)
    # Un objet possede n'a pas d'annonce a ouvrir.
    assert all(o.url is None for o in plans[0].options)


# --- Objets inutilisables ----------------------------------------------------


def test_un_objet_verrouille_est_ecarte(db, bank):
    """Verrou de 7 jours : l'objet existe mais ne peut pas entrer en contrat."""
    items = inventaire(bank, n=10, tradable=False)
    assert resolve(db, items) == []
    assert best_tradeups(db, items, prix(bank), Rarity.INDUSTRIAL,
                         include_losing=True) == []


def test_un_souvenir_peut_entrer_en_contrat_depuis_mai_2026():
    """La regle a change : "Souvenir quality items can now be selected in
    Trade Up Contract" (mise a jour du 21 mai 2026).

    Le prefixe reste detecte -- il sert a reconnaitre le skin sous-jacent et a
    coter le bon objet de marche -- mais il ne bloque plus.
    """
    souvenir = OwnedItem("Souvenir M4A4 | Tornado (Field-Tested)", 0.2)
    normal = OwnedItem("M4A4 | Tornado (Field-Tested)", 0.2)
    assert souvenir.souvenir and souvenir.usable
    assert not normal.souvenir and normal.usable


def test_un_souvenir_est_rattache_a_son_skin(db):
    """Sans retrait du prefixe, la base ne reconnait pas l'objet et l'ignore."""
    from tradeup.wear import wear_of

    skin = db.find("AK-47 | Redline")
    assert skin is not None
    f = (skin.min_float + skin.max_float) / 2
    nom = f"Souvenir {skin.market_hash_name(wear_of(f))}"
    apparies = resolve(db, [OwnedItem(nom, f)])
    assert len(apparies) == 1
    assert apparies[0][1].key == skin.key


def test_un_objet_sans_float_est_ecarte():
    assert not OwnedItem("AK-47 | Redline (Field-Tested)", None).usable


def test_le_stattrak_ne_se_melange_pas(db, bank):
    items = inventaire(bank, stattrak=True)
    # Des entrees StatTrak ne doivent pas alimenter un contrat normal.
    assert best_tradeups(db, items, prix(bank), Rarity.INDUSTRIAL,
                         include_losing=True) == []


# --- Lecture de l'inventaire -------------------------------------------------


def test_lecture_des_lignes_de_lapi():
    items = from_csfloat_rows([
        {"market_hash_name": "AK-47 | Redline (Field-Tested)",
         "float_value": 0.23, "asset_id": 123, "is_stattrak": False,
         "tradable": 1, "stickers": [{"name": "x"}], "listing_id": "999"},
        {"pas_de_nom": True},
    ])
    assert len(items) == 1
    it = items[0]
    assert it.asset_id == "123" and it.stickers == 1 and it.listed
    assert it.decorated


def test_lecture_dun_fichier_json(tmp_path):
    p = tmp_path / "inv.json"
    p.write_text(json.dumps([
        {"market_hash_name": "AK-47 | Redline (Field-Tested)",
         "float_value": 0.23, "paid": 12.5},
    ]), encoding="utf-8")
    items = load_file(p)
    assert items[0].paid == 12.5
    assert items[0].float_value == pytest.approx(0.23)


def test_lecture_dun_fichier_csv(tmp_path):
    """Un CSV ne porte que du texte : tout doit etre reconverti."""
    p = tmp_path / "inv.csv"
    p.write_text(
        "market_hash_name,float_value,tradable\n"
        "AK-47 | Redline (Field-Tested),0.23,false\n",
        encoding="utf-8",
    )
    it = load_file(p)[0]
    assert it.float_value == pytest.approx(0.23)  # pas la chaine "0.23"
    assert it.tradable is False  # pas la chaine "false", qui serait vraie


def test_fichier_absent_signale(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_file(tmp_path / "rien.json")


# --- Diagnostic --------------------------------------------------------------


def test_le_bilan_compte_ce_qui_bloque(db, bank):
    items = (
        inventaire(bank, n=3)
        + [OwnedItem("Souvenir AK-47 | Redline (Field-Tested)", 0.2)]
        + [OwnedItem("AK-47 | Redline (Field-Tested)", None)]
        + [OwnedItem("AK-47 | Redline (Field-Tested)", 0.2, tradable=False)]
    )
    bilan = summary(db, items)
    assert bilan["objets"] == 6
    # Le Souvenir compte desormais parmi les utilisables : 3 + lui.
    assert bilan["utilisables"] == 4
    assert bilan["sans_float"] == 1
    assert bilan["verrouilles"] == 1
    assert bilan["souvenirs"] == 1  # toujours compte, pour information


def test_lecart_au_compte_est_chiffre(db, bank):
    """Sans ca, "aucun contrat" est indiscernable d'un bug."""
    ecarts = closest_gaps(db, inventaire(bank, n=4), Rarity.INDUSTRIAL)
    assert ecarts == [("The Bank Collection", 4)]
    assert closest_gaps(db, [], Rarity.INDUSTRIAL) == []


# --- Classement --------------------------------------------------------------


def test_le_classement_change_reellement_lordre(db, bank):
    """Le gain brut et la probabilite ne designent pas le meme gagnant."""
    from tradeup.scoring import Ranking

    rarity = Rarity.INDUSTRIAL
    autre = next(
        c for c in db.tradeable_collections(rarity)
        if c.id != bank.id and len(c.by_rarity(rarity)) >= 2
        and c.outcomes_for_input_rarity(rarity)
    )

    valeurs = {}
    # Bank : toutes les sorties rapportent un peu plus que les entrees.
    for s in bank.by_rarity(rarity):
        for w in s.available_wears():
            valeurs[s.market_hash_name(w)] = 1.0
    for s in bank.by_rarity(rarity.next_up):
        for w in s.available_wears():
            valeurs[s.market_hash_name(w)] = 12.0
    # L'autre : une seule sortie paie, mais tres gros.
    for s in autre.by_rarity(rarity):
        for w in s.available_wears():
            valeurs[s.market_hash_name(w)] = 1.0
    sorties = autre.outcomes_for_input_rarity(rarity)
    for i, s in enumerate(sorties):
        for w in s.available_wears():
            valeurs[s.market_hash_name(w)] = 400.0 if i == 0 else 0.5

    pricer = StaticPricer(valeurs)
    items = inventaire(bank) + inventaire(autre)

    par_gain = best_tradeups(db, items, pricer, rarity, include_losing=True,
                             ranking=Ranking.EV)
    par_proba = best_tradeups(db, items, pricer, rarity, include_losing=True,
                              ranking=Ranking.SAFETY)
    assert len(par_gain) == 2 and len(par_proba) == 2

    # La loterie gagne au gain espere, la reguliere gagne a la probabilite.
    assert par_gain[0].collection.id == autre.id
    assert par_proba[0].collection.id == bank.id
    assert par_proba[0].result.profit_probability == pytest.approx(1.0)
    assert par_proba[0].always_profitable


def test_toutes_rentables_se_reconnait():
    from tradeup.ev import Outcome, TradeUpResult
    from tradeup.inventory import InventoryTradeUp
    from tradeup.models import Wear

    def contrat(nets, cout=10.0):
        skin = Skin(key="s", name="S", collection_id="c", rarity=Rarity.MIL_SPEC,
                    min_float=0.0, max_float=1.0)
        outcomes = tuple(
            Outcome(skin=skin, float_value=0.1, wear=Wear.FACTORY_NEW,
                    probability=1 / len(nets), net_value=n, priced=True)
            for n in nets
        )
        res = TradeUpResult(outcomes=outcomes, inputs=(), cost=cout,
                            avg_input_float=0.1, avg_normalized=0.1,
                            stattrak=False, unpriced_probability=0.0)
        return InventoryTradeUp(result=res, options=(), collection=None,
                                rarity=Rarity.MIL_SPEC, items=())

    assert contrat([11.0, 12.0, 13.0]).always_profitable
    assert not contrat([11.0, 12.0, 9.0]).always_profitable
