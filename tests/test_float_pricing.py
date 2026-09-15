"""Valorisation d'une sortie selon son float, a l'interieur d'un meme palier.

Les points de prix utilises ici sont des relevés REELS sur CSFloat pour
AK-47 | Emerald Pinstripe (Factory New). Ils montrent l'ampleur du phenomene :
24-25 USD a float 0.005, 17-20 a float 0.070. Un modele qui ne retient qu'un
prix par palier d'usure rate un ecart de 40 %.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from tradeup.models import Rarity, Skin, Wear
from tradeup.plan import CSFloatPricer

CIBLE = Skin(key="ak", name="AK-47 | Emerald Pinstripe", collection_id="bank",
             rarity=Rarity.RESTRICTED, min_float=0.0, max_float=1.0)

# Releve reel du carnet, trie par float.
CARNET_REEL = [
    (0.0033, 24.00),
    (0.0037, 25.00),
    (0.0090, 24.43),
    (0.0093, 24.10),
    (0.0095, 24.75),
    (0.0698, 20.40),
    (0.0698, 17.21),
    (0.0699, 20.40),
]


@dataclass
class FakeListing:
    price: float
    float_value: float


class FakeSource:
    def __init__(self, carnet):
        self.carnet = carnet
        self.appels = 0

    def listings(self, name, limit=50, **kw):
        self.appels += 1
        return [FakeListing(price=p, float_value=f) for f, p in self.carnet]


def make_pricer(carnet=CARNET_REEL, **kw):
    return CSFloatPricer(FakeSource(carnet), **kw)


def test_la_valorisation_est_le_prix_a_battre_du_palier():
    """On retient la moins chere des annonces nues, quel que soit le float.

    La prime de bas float existe (24-25 a float 0.005 contre 17.21 a 0.070)
    mais ce sont des prix DEMANDES, sur une poignee d'annonces, sans volume
    publie. La compter a produit des sorties valorisees 582 puis 181 puis 121
    la ou le marche reel affichait 85.
    """
    p = make_pricer()
    assert p.price_at_float("x", 0.005) == pytest.approx(17.21)
    assert p.price_at_float("x", 0.0698) == pytest.approx(17.21)


def test_la_valorisation_ne_depend_pas_du_float_vise():
    """Choix assume : aucune prime de float n'entre dans le calcul.

    L'optimiseur ne peut donc plus se persuader qu'il gagne de l'argent en
    visant un float rare dont la revente n'est pas demontree.
    """
    p = make_pricer()
    valeurs = [p.price_at_float("x", f)
               for f in (0.001, 0.005, 0.02, 0.05, 0.07, 0.5)]
    assert valeurs == [pytest.approx(17.21)] * len(valeurs)


def test_aucune_prime_inventee_hors_du_carnet():
    """Un float meilleur que tout le carnet ne justifie aucune majoration."""
    p = make_pricer()
    assert p.price_at_float("x", 0.0001) == pytest.approx(17.21)
    assert p.price_at_float("x", 0.9) == pytest.approx(17.21)


def test_a_float_egal_on_retient_le_prix_le_plus_bas():
    """Deux annonces a 0.0698 : 17.21 et 20.40. C'est 17.21 qu'il faut battre."""
    p = make_pricer()
    assert p.price_at_float("x", 0.0698) == pytest.approx(17.21)


def test_le_modele_ne_surestime_jamais_la_revente():
    """Garde-fou general : jamais au-dessus de la plus chere annonce nue."""
    p = make_pricer()
    plafond = max(prix for _, prix in CARNET_REEL)
    for f in (0.001, 0.005, 0.02, 0.05, 0.07, 0.3):
        assert p.price_at_float("x", f) <= plafond


def test_carnet_vide():
    p = make_pricer(carnet=[])
    assert p.price_at_float("x", 0.05) is None
    assert p.sell_net_at_float(CIBLE, Wear.FACTORY_NEW, False, 0.05) is None


def test_le_net_applique_frais_et_decote():
    p = make_pricer(sell_fee=0.02, safety_margin=0.05)
    net = p.sell_net_at_float(CIBLE, Wear.FACTORY_NEW, False, 0.0698)
    assert net == pytest.approx(17.21 * 0.98 * 0.95)


def test_le_carnet_n_est_interroge_qu_une_fois():
    """Le balayage des paliers reinterroge le meme objet des dizaines de fois.

    Sans cache, le quota CSFloat saute immediatement -- ce qui est arrive en
    pratique pendant le developpement.
    """
    source = FakeSource(CARNET_REEL)
    p = CSFloatPricer(source)
    for f in (0.01, 0.02, 0.03, 0.04, 0.05):
        p.price_at_float("x", f)
    assert source.appels == 1


def test_sell_net_par_palier_reste_pessimiste():
    """La methode sans float garde l'ancien comportement : le prix plancher."""
    p = make_pricer()
    assert p.sell_net(CIBLE, Wear.FACTORY_NEW) == pytest.approx(
        17.21 * 0.98 * 0.95
    )


# --- Exposition au blocage de 7 jours ----------------------------------------


def test_tolerance_a_la_baisse_de_prix():
    """Combien le marche peut baisser pendant les 7 jours de blocage.

    Le cout est fige a l'achat, la revente a lieu une semaine plus tard : c'est
    une exposition directionnelle reelle, qu'il faut chiffrer plutot que de la
    passer sous silence.
    """
    from tradeup.ev import InputItem, Outcome, TradeUpResult
    from tradeup.models import Rarity, Skin
    from tradeup.plan import Plan

    entree = Skin(key="in", name="in", collection_id="c", rarity=Rarity.MIL_SPEC,
                  min_float=0.0, max_float=1.0)
    result = TradeUpResult(
        outcomes=(Outcome(skin=CIBLE, float_value=0.05, wear=Wear.FACTORY_NEW,
                          probability=1.0, net_value=16.02, priced=True),),
        inputs=tuple(InputItem(entree, 0.05, 1.141) for _ in range(10)),
        cost=11.41,
        avg_input_float=0.05,
        stattrak=False,
        unpriced_probability=0.0,
    )
    plan = Plan(result=result, options=(), collection=None, listings_examined=0)

    # 11.41 de cout pour 16.02 de revente : le prix peut chuter de ~28.8 %.
    assert plan.price_drop_tolerance == pytest.approx(1 - 11.41 / 16.02)
    assert 0.28 < plan.price_drop_tolerance < 0.29


def test_pas_de_tolerance_si_la_sortie_ne_vaut_rien():
    from tradeup.ev import InputItem, TradeUpResult
    from tradeup.models import Rarity, Skin
    from tradeup.plan import Plan

    entree = Skin(key="in", name="in", collection_id="c", rarity=Rarity.MIL_SPEC,
                  min_float=0.0, max_float=1.0)
    result = TradeUpResult(
        outcomes=(), inputs=tuple(InputItem(entree, 0.05, 1.0) for _ in range(10)),
        cost=10.0, avg_input_float=0.05, stattrak=False, unpriced_probability=0.0,
    )
    plan = Plan(result=result, options=(), collection=None, listings_examined=0)
    assert plan.price_drop_tolerance is None


# --- Nettoyage du carnet -----------------------------------------------------


@dataclass
class RichListing:
    price: float
    float_value: float | None
    stickers: int = 0
    keychains: int = 0

    @property
    def plain(self) -> bool:
        return not self.stickers and not self.keychains


class RichSource:
    def __init__(self, offres):
        self.offres = offres

    def listings(self, name, limit=50, **kw):
        return self.offres


def test_les_objets_stickes_sont_ecartes():
    """Une sortie de trade-up sort NUE : la comparer a un exemplaire sticke
    revient a valoriser les stickers plutot que l'arme.

    Cas reel : une Five-SeveN Candy Apple estimee 582 USD alors que les
    exemplaires nus se vendaient 85 a 127.
    """
    p = CSFloatPricer(RichSource([
        RichListing(90.0, 0.040),
        RichListing(600.0, 0.041, stickers=4),   # Katowice 2014 : hors sujet
        RichListing(95.0, 0.042),
    ]), sell_fee=0.0, safety_margin=0.0)

    book = p._listings("x")
    assert [prix for _, prix in book] == [90.0, 95.0]
    assert p.price_at_float("x", 0.041) == pytest.approx(90.0)  # et non 600


def test_les_breloques_aussi():
    p = CSFloatPricer(RichSource([
        RichListing(50.0, 0.05),
        RichListing(400.0, 0.05, keychains=1),
    ]))
    assert [prix for _, prix in p._listings("x")] == [50.0]


def test_une_annonce_aberrante_est_ecartee():
    """Il reste des prix gonfles sans sticker : motif rare, vendeur qui teste.

    Une seule suffit a deformer toute la courbe, donc on plafonne a 3x la
    mediane.
    """
    offres = [RichListing(10.0, 0.01 * i) for i in range(1, 6)]
    offres.append(RichListing(900.0, 0.06))  # aberrante, sans sticker
    p = CSFloatPricer(RichSource(offres))

    prix = [x for _, x in p._listings("x")]
    assert 900.0 not in prix
    assert len(prix) == 5


def test_un_carnet_entierement_aberrant_n_est_pas_vide():
    """Mieux vaut des prix douteux que plus de prix du tout."""
    p = CSFloatPricer(RichSource([RichListing(100.0, 0.01), RichListing(900.0, 0.02)]))
    assert len(p._listings("x")) == 2


def test_carnet_uniquement_sticke_devient_vide():
    """Sans exemplaire nu, on ne sait pas valoriser : on le dit."""
    p = CSFloatPricer(RichSource([RichListing(600.0, 0.04, stickers=4)]))
    assert p._listings("x") == []
    assert p.price_at_float("x", 0.04) is None


# --- Liquidite : combien de temps pour revendre ------------------------------


class SourceAvecHistorique(RichSource):
    def __init__(self, offres, ventes, jours):
        super().__init__(offres)
        self.ventes = ventes
        self.jours = jours

    def sales_history(self, name):
        return self.ventes

    def daily_sales(self, name, jours=7):
        return self.jours[:jours]


def test_les_stats_de_vente_donnent_le_rythme_reel():
    """Le prix affiche suppose qu'on vend au marche ; la liquidite dit combien
    de temps ca prend. Sans elle, un contrat annonce a +0.37 s'est solde a
    -0.02 faute d'avoir attendu."""
    p = CSFloatPricer(SourceAvecHistorique(
        offres=[RichListing(1.20, 0.05)],
        ventes=[RichListing(1.30, 0.05), RichListing(1.40, 0.04),
                RichListing(9.00, 0.03, stickers=4)],  # stickee : ignoree
        jours=[{"jour": "2026-09-15", "ventes": 20, "prix_moyen": 1.35},
               {"jour": "2026-09-14", "ventes": 10, "prix_moyen": 1.30}],
    ))
    st = p.sales_stats("x")
    assert st["ventes_jour"] == 15  # moyenne de 20 et 10
    assert st["prix_median"] == pytest.approx(1.40)  # mediane des ventes NUES
    assert st["ventes_observees"] == 2


def test_la_mediane_des_ventes_n_entre_pas_dans_la_valorisation():
    """Elle est souvent au-dessus du prix demande le plus bas : l'utiliser
    rendrait le modele plus optimiste, exactement le mauvais sens."""
    p = CSFloatPricer(SourceAvecHistorique(
        offres=[RichListing(1.18, 0.05)],
        ventes=[RichListing(1.31, 0.05)],
        jours=[{"jour": "2026-09-15", "ventes": 18, "prix_moyen": 1.31}],
    ))
    assert p.price_at_float("x", 0.05) == pytest.approx(1.18)  # l'ask, pas 1.31


def test_absence_d_historique_ne_casse_rien():
    p = CSFloatPricer(SourceAvecHistorique(
        offres=[RichListing(1.0, 0.05)], ventes=[], jours=[]))
    assert p.sales_stats("x") is None
