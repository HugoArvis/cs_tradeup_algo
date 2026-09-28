"""Tests de la selection 0/1 et de la construction de panier reel.

La difference avec `cheapest_selection` est subtile mais critique : une annonce
CSFloat est un objet UNIQUE. Un selecteur qui autorise la repetition produirait
un panier impossible a passer en caisse.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from tradeup.generator import (
    InputOption,
    cheapest_selection,
    cheapest_unique_selection,
    options_from_listings,
)
from tradeup.models import Rarity, Skin, Wear

SKIN = Skin(key="s", name="Arme", collection_id="c", rarity=Rarity.MIL_SPEC,
            min_float=0.0, max_float=1.0)
ETROIT = Skin(key="e", name="Etroit", collection_id="c", rarity=Rarity.MIL_SPEC,
              min_float=0.10, max_float=0.50)


def opt(cost, flt, wear=Wear.FACTORY_NEW, skin=SKIN):
    return InputOption(skin=skin, wear=wear, unit_cost=cost, float_value=flt)


@dataclass
class FakeListing:
    price: float
    float_value: float | None


# --- Selection sans repetition -----------------------------------------------

def test_une_annonce_ne_peut_servir_quune_fois():
    """Le coeur du sujet : 3 annonces ne suffisent pas pour 10 entrees."""
    options = [opt(1.0, 0.01), opt(1.0, 0.01), opt(1.0, 0.01)]

    # Le selecteur a repetition accepte : il croit a un stock infini.
    assert cheapest_selection(options, 10, float_budget=1.0) is not None
    # Le selecteur unique refuse : il n'y a que 3 objets reels.
    assert cheapest_unique_selection(options, 10, float_budget=1.0) is None


def test_selectionne_les_moins_cheres_sous_contrainte():
    options = [opt(5.0, 0.01), opt(1.0, 0.30), opt(2.0, 0.02), opt(3.0, 0.03)]
    sel = cheapest_unique_selection(options, 2, float_budget=0.10)

    assert sel is not None
    assert len(sel) == 2
    assert len({id(o) for o in sel}) == 2  # deux objets distincts
    assert sum(o.float_value for o in sel) <= 0.10 + 1e-9
    # 2.00 + 3.00 : la moins chere (1.00) a un float trop eleve.
    assert sum(o.unit_cost for o in sel) == pytest.approx(5.0)


def test_budget_serre_force_les_bas_floats_meme_chers():
    options = [opt(1.0, 0.20), opt(1.0, 0.20), opt(9.0, 0.01), opt(9.0, 0.01)]
    sel = cheapest_unique_selection(options, 2, float_budget=0.05)
    assert sel is not None
    assert sum(o.unit_cost for o in sel) == pytest.approx(18.0)


def test_budget_inatteignable():
    options = [opt(1.0, 0.20) for _ in range(10)]
    assert cheapest_unique_selection(options, 10, float_budget=0.5) is None


def test_cas_limites():
    assert cheapest_unique_selection([], 0, 1.0) == []
    assert cheapest_unique_selection([], 5, 1.0) is None
    # Exactement le nombre requis, budget suffisant.
    options = [opt(1.0, 0.01) for _ in range(10)]
    sel = cheapest_unique_selection(options, 10, float_budget=0.2)
    assert sel is not None and len(sel) == 10


def test_les_dix_objets_sont_bien_distincts():
    options = [opt(1.0 + i * 0.1, 0.01 * (i + 1)) for i in range(20)]
    sel = cheapest_unique_selection(options, 10, float_budget=10.0)
    assert sel is not None
    assert len({id(o) for o in sel}) == 10


# --- Conversion des offres reelles -------------------------------------------

def test_le_float_reel_remplace_lhypothese():
    listings = [FakeListing(price=1.43, float_value=0.0438),
                FakeListing(price=1.47, float_value=0.0296)]
    options = options_from_listings(SKIN, listings)

    assert [o.float_value for o in options] == [0.0438, 0.0296]
    assert [o.unit_cost for o in options] == [1.43, 1.47]  # triees par prix
    # L'usure est deduite du float, pas fournie par l'appelant.
    assert all(o.wear is Wear.FACTORY_NEW for o in options)


def test_offres_inexploitables_ecartees():
    listings = [
        FakeListing(price=1.0, float_value=None),  # float inconnu
        FakeListing(price=0.0, float_value=0.05),  # prix nul
        FakeListing(price=-1.0, float_value=0.05),  # prix negatif
        FakeListing(price=2.0, float_value=0.05),  # valide
    ]
    options = options_from_listings(SKIN, listings)
    assert len(options) == 1
    assert options[0].unit_cost == 2.0


def test_float_incoherent_avec_la_base_est_rejete():
    """Un float hors du range connu du skin signale une donnee douteuse.

    On prefere ignorer l'offre plutot que de calculer une sortie sur une base
    fausse.
    """
    listings = [FakeListing(price=1.0, float_value=0.05),  # < min_float 0.10
                FakeListing(price=1.0, float_value=0.90),  # > max_float 0.50
                FakeListing(price=1.0, float_value=0.30)]  # valide
    options = options_from_listings(ETROIT, listings)
    assert len(options) == 1
    assert options[0].float_value == 0.30


def test_limitation_du_nombre_doffres_par_skin():
    listings = [FakeListing(price=float(i), float_value=0.05) for i in range(1, 21)]
    options = options_from_listings(SKIN, listings, max_per_skin=5)
    assert len(options) == 5
    # Les 5 moins cheres.
    assert [o.unit_cost for o in options] == [1.0, 2.0, 3.0, 4.0, 5.0]


def test_le_plan_porte_la_rarete_dentree():
    """Sans elle, rien en aval ne pouvait l'afficher : ni le rapport, ni le
    nom du fichier, ni l'historique de l'application web."""
    from tradeup.models import Rarity
    from tradeup.plan import Plan

    assert "rarity" in Plan.__dataclass_fields__
    assert Plan.__dataclass_fields__["rarity"].default is Rarity.MIL_SPEC


# --- Frais du marche de REVENTE ----------------------------------------------
# On achete sur CSFloat et on revend sur Steam. Ce n'est pas le prix d'une
# sortie qui decide d'un contrat, c'est ce qu'on encaisse : les frais Steam
# ont un plancher de 0.01 PAR FRAIS, ce qui devore les petits montants.


class FauxMarcheDeVente:
    """Source de prix minimale, pour verifier le chemin de valorisation."""

    name = "faux"

    def __init__(self, prix: dict[str, float], volume: int | None = 50):
        self.prix = prix
        self.volume = volume
        self.appels: list[str] = []

    def fetch(self, market_hash_name: str, *, use_cache: bool = True):
        from tradeup.pricing.base import Quote

        self.appels.append(market_hash_name)
        p = self.prix.get(market_hash_name)
        if p is None:
            return None
        return Quote(market_hash_name=market_hash_name, source="faux",
                     lowest_price=p, median_price=p, volume=self.volume,
                     currency="USD")


def _pricer(prix_steam, **kw):
    from tradeup.fees import STEAM
    from tradeup.plan import CSFloatPricer

    marche = FauxMarcheDeVente(prix_steam, **kw)
    return CSFloatPricer(source=None, safety_margin=0.0,
                         sell_source=marche, sell_fees=STEAM), marche


def test_le_plancher_de_frais_steam_devore_les_petits_montants():
    """Regle du domaine : 0.01 minimum PAR frais, il y en a deux.

    A 0.03 le vendeur touche 0.01 -- 66 % de frais. Un modele a taux fixe
    annoncerait 0.026 et rendrait rentable un contrat qui perd.
    """
    nom = SKIN.market_hash_name(Wear.FACTORY_NEW)
    pricer, _ = _pricer({nom: 0.03})
    assert pricer.sell_net(SKIN, Wear.FACTORY_NEW) == pytest.approx(0.01)

    pricer, _ = _pricer({nom: 0.07})
    # 0.05 net : 28.6 % de frais, tres loin des 13 % du haut de l'echelle.
    assert pricer.sell_net(SKIN, Wear.FACTORY_NEW) == pytest.approx(0.05)

    pricer, _ = _pricer({nom: 10.0})
    net = pricer.sell_net(SKIN, Wear.FACTORY_NEW)
    assert 0.86 < net / 10.0 < 0.88  # regime normal : ~13 %


def test_sans_prix_de_vente_ni_marche_de_repli_la_sortie_ne_vaut_rien():
    """Mieux vaut une sortie sans prix qu'un prix invente.

    Le repli sur CSFloat suppose une source CSFloat. Sans elle, il n'y a rien
    a quoi se rabattre -- et c'est un fait, pas une panne a masquer.
    """
    pricer, _ = _pricer({})
    assert pricer.source is None
    assert pricer.sell_net(SKIN, Wear.FACTORY_NEW) is None


def test_une_sortie_sans_prix_steam_se_rabat_sur_csfloat():
    """Le repli est CONSERVATEUR : CSFloat rend 17 a 37 % de moins que Steam.

    Un contrat rentable ainsi valorise l'est donc forcement sur Steam. Sans ce
    repli, une panne de Steam rend tout le balayage inexploitable -- mesure du
    22 septembre, 9 collections sur 88 et zero enregistree.
    """
    from tradeup.fees import STEAM
    from tradeup.plan import CSFloatPricer

    nom = SKIN.market_hash_name(Wear.FACTORY_NEW)
    marche = FauxMarcheDeVente({})          # Steam ne connait rien
    p = CSFloatPricer(source=object(), sell_source=marche, sell_fees=STEAM,
                      safety_margin=0.0, sell_fee=0.02)
    p._book[nom] = [(0.01, 10.0)]           # mais CSFloat a des annonces

    assert p.sell_net(SKIN, Wear.FACTORY_NEW) == pytest.approx(9.8)
    assert p.replis == 1


def test_le_float_ne_change_pas_le_prix_quand_on_revend_sur_steam():
    """Steam n'affiche pas le float, donc ne le price pas.

    Viser 0.001 plutot que 0.06 dans un meme palier ne rapporte rien de plus :
    le modele ne doit pas payer une prime pour un avantage inexistant.
    """
    nom = SKIN.market_hash_name(Wear.FACTORY_NEW)
    pricer, _ = _pricer({nom: 10.0})
    bas = pricer.sell_net_at_float(SKIN, Wear.FACTORY_NEW, False, 0.001)
    haut = pricer.sell_net_at_float(SKIN, Wear.FACTORY_NEW, False, 0.060)
    assert bas == haut == pricer.sell_net(SKIN, Wear.FACTORY_NEW)


def test_la_liquidite_est_lue_sur_le_marche_de_vente_sans_requete_de_plus():
    """Le volume arrive avec la cotation ; l'interroger a part coute un appel."""
    nom = SKIN.market_hash_name(Wear.FACTORY_NEW)
    pricer, marche = _pricer({nom: 10.0}, volume=98)

    pricer.sell_net(SKIN, Wear.FACTORY_NEW)
    stats = pricer.sales_stats(nom)

    assert stats["ventes_jour"] == 98
    assert marche.appels == [nom]  # une seule cotation pour prix ET volume


def test_sans_marche_de_vente_le_comportement_dorigine_est_conserve():
    """Le mode historique -- revendre sur CSFloat -- reste disponible."""
    from tradeup.plan import CSFloatPricer

    pricer = CSFloatPricer(source=None, sell_fee=0.02, safety_margin=0.0)
    pricer._book[SKIN.market_hash_name(Wear.FACTORY_NEW)] = [(0.01, 10.0)]
    assert pricer.sell_net(SKIN, Wear.FACTORY_NEW) == pytest.approx(9.8)


def test_les_frais_sappliquent_avant_la_conversion_de_devise():
    """Le plancher de Steam vaut 0,01 dans la devise ou l'on encaisse.

    Convertir d'abord puis appliquer les frais deplace le plancher, et le
    deplace precisement la ou il decide : en bas de l'echelle.
    """
    from tradeup.fees import STEAM, steam_net_proceeds

    nom = SKIN.market_hash_name(Wear.FACTORY_NEW)
    eur_vers_usd = 1 / 0.92
    pricer, _ = _pricer({nom: 0.08})
    pricer.sell_to_usd = eur_vers_usd

    # 0,08 EUR -> 0,06 EUR net -> converti ensuite.
    attendu = steam_net_proceeds(0.08) * eur_vers_usd
    assert pricer.sell_net(SKIN, Wear.FACTORY_NEW) == pytest.approx(attendu)

    # L'ordre inverse donnerait un autre chiffre : c'est bien un choix, pas
    # une equivalence.
    a_lenvers = STEAM.net_from_sale(0.08 * eur_vers_usd)
    assert a_lenvers != pytest.approx(attendu, rel=0.01)


def test_sans_conversion_le_net_reste_dans_la_devise_dorigine():
    nom = SKIN.market_hash_name(Wear.FACTORY_NEW)
    pricer, _ = _pricer({nom: 0.08})
    assert pricer.sell_to_usd == 1.0
    assert pricer.sell_net(SKIN, Wear.FACTORY_NEW) == pytest.approx(0.06)


# --- Prix demande contre prix negocie ----------------------------------------
# La methode des guides : ce qui compte n'est pas ce qu'un vendeur DEMANDE,
# c'est ce a quoi les transactions se CONCLUENT. Les deux chiffres divergent,
# et pas toujours dans le meme sens.


class MarcheAvecHistorique(FauxMarcheDeVente):
    """Source distinguant la plus basse annonce du prix median des ventes."""

    def __init__(self, annonces: dict[str, float], ventes: dict[str, float]):
        super().__init__(annonces)
        self.ventes = ventes

    def fetch(self, market_hash_name: str, *, use_cache: bool = True):
        from tradeup.pricing.base import Quote

        self.appels.append(market_hash_name)
        if market_hash_name not in self.prix:
            return None
        return Quote(market_hash_name=market_hash_name, source="faux",
                     lowest_price=self.prix[market_hash_name],
                     median_price=self.ventes.get(market_hash_name),
                     volume=50, currency="EUR")


def _pricer_historique(annonces, ventes, basis):
    from tradeup.fees import STEAM
    from tradeup.plan import CSFloatPricer

    marche = MarcheAvecHistorique(annonces, ventes)
    return CSFloatPricer(source=None, safety_margin=0.0, sell_source=marche,
                         sell_fees=STEAM, sell_basis=basis)


def test_la_base_ventes_retient_le_prix_negocie_pas_le_prix_demande():
    """Mesure sur une sortie Bank : affichee 0,28, vendue 0,24."""
    from tradeup.fees import steam_net_proceeds

    nom = SKIN.market_hash_name(Wear.FACTORY_NEW)
    ventes = _pricer_historique({nom: 0.28}, {nom: 0.24}, "sales")
    annonces = _pricer_historique({nom: 0.28}, {nom: 0.24}, "listing")

    assert ventes.sell_net(SKIN, Wear.FACTORY_NEW) == pytest.approx(
        steam_net_proceeds(0.24))
    # En base "listing" on retient la plus prudente des deux, donc 0,24 aussi
    # ici : `sell_reference` prend le minimum. La difference se voit quand le
    # prix negocie est SUPERIEUR au prix demande.
    assert annonces.sell_net(SKIN, Wear.FACTORY_NEW) == pytest.approx(
        steam_net_proceeds(0.24))


def test_un_prix_negocie_superieur_a_l_annonce_est_plafonne():
    """Le Galil Tuxedo se vendait 0,91 alors qu'il s'affichait 0,88.

    Ce test affirmait le contraire jusqu'au 20 septembre : que la base
    "ventes" devait retenir 0,91. C'etait faux, et la meme erreur a valorise
    un M4A4 | Radiation Hazard (FT) a 165 EUR sur UNE vente alors qu'un
    exemplaire etait affiche a 35,65.

    On ne vend pas au-dessus de la plus basse annonce : personne n'achete le
    second exemplaire quand le premier est moins cher. Les deux bases
    coincident donc ici, et c'est normal.
    """
    from tradeup.fees import steam_net_proceeds

    nom = SKIN.market_hash_name(Wear.FACTORY_NEW)
    ventes = _pricer_historique({nom: 0.88}, {nom: 0.91}, "sales")
    annonces = _pricer_historique({nom: 0.88}, {nom: 0.91}, "listing")

    attendu = pytest.approx(steam_net_proceeds(0.88))
    assert ventes.sell_net(SKIN, Wear.FACTORY_NEW) == attendu
    assert annonces.sell_net(SKIN, Wear.FACTORY_NEW) == attendu


def test_sans_historique_de_vente_on_retombe_sur_l_annonce():
    """Un objet jamais vendu n'est pas un objet gratuit."""
    from tradeup.fees import steam_net_proceeds

    nom = SKIN.market_hash_name(Wear.FACTORY_NEW)
    p = _pricer_historique({nom: 0.50}, {}, "sales")
    assert p.sell_net(SKIN, Wear.FACTORY_NEW) == pytest.approx(
        steam_net_proceeds(0.50))


def test_un_marche_de_revente_en_panne_est_reconnu():
    """Trois refus d'affilee ne sont plus un hasard, c'est une panne.

    Mesure du 22 septembre : un balayage de nuit a fait 9 collections sur 88
    en 155 minutes, presque entierement passees a attendre des 429 -- 75 s de
    backoff par nom, pour un echec certain d'avance.
    """
    import urllib.error

    from tradeup.fees import STEAM
    from tradeup.plan import CSFloatPricer

    class MarcheMort:
        name = "steam"

        def fetch(self, nom, *, use_cache=True):
            raise urllib.error.URLError("429")

    p = CSFloatPricer(source=None, sell_source=MarcheMort(), sell_fees=STEAM)
    assert not p.revente_en_panne
    for i in range(3):
        p.sell_net(SKIN, Wear.FACTORY_NEW) if i else None
        p._quote_de_vente(f"objet {i}")
    assert p.revente_en_panne


def test_une_cotation_reussie_remet_le_compteur_a_zero():
    """Deux echecs isoles separes par une reussite ne sont pas une panne."""
    from tradeup.fees import STEAM
    from tradeup.plan import CSFloatPricer

    class MarcheCapricieux:
        name = "steam"

        def __init__(self):
            self.n = 0

        def fetch(self, nom, *, use_cache=True):
            from tradeup.pricing.base import Quote

            self.n += 1
            if self.n % 2:
                raise RuntimeError("refus")
            return Quote(market_hash_name=nom, source="steam", lowest_price=1.0,
                         median_price=1.0, volume=50, currency="EUR")

    p = CSFloatPricer(source=None, sell_source=MarcheCapricieux(), sell_fees=STEAM)
    for i in range(6):
        p._quote_de_vente(f"objet {i}")
    assert not p.revente_en_panne


def test_un_marche_en_panne_nest_plus_interroge():
    """Le repli ne suffit pas : il faut aussi cesser d'attendre.

    Chaque tentative sur un marche qui refuse coute 75 s de backoff. Sans ce
    court-circuit, le balayage se rabat correctement mais reste aussi lent
    qu'avant -- 37 minutes d'attente par collection pour un echec connu des le
    troisieme nom.
    """
    from tradeup.fees import STEAM
    from tradeup.plan import CSFloatPricer

    class MarcheMortCompteur:
        name = "steam"

        def __init__(self):
            self.appels = 0

        def fetch(self, nom, *, use_cache=True):
            self.appels += 1
            raise RuntimeError("429")

    marche = MarcheMortCompteur()
    p = CSFloatPricer(source=None, sell_source=marche, sell_fees=STEAM)
    for i in range(20):
        p._quote_de_vente(f"objet {i}")

    assert p.revente_en_panne
    assert marche.appels == 3, f"{marche.appels} appels au lieu de 3"


# --- Profondeur du carnet ----------------------------------------------------
# `plan` retient par construction les annonces les MOINS cheres : tout autre
# acheteur faisant le meme calcul les prend avant nous. La question n'est donc
# pas seulement "ce contrat est-il rentable" mais "l'est-il encore si je
# n'arrive pas premier".


def _plan_factice(cout_base, cout_profond, ev):
    """Un Plan minimal, pour exercer les seules proprietes de profondeur."""
    from tradeup.plan import Plan

    class FauxResultat:
        ev_net = ev
        ev_profit = ev - cout_base
        cost = cout_base
        outcomes = ()

        @property
        def profitability(self):
            return ev / cout_base

    return Plan(result=FauxResultat(), options=(), collection=None,
                listings_examined=0, deep_cost=cout_profond)


def test_un_contrat_profond_reste_rentable_sans_arriver_premier():
    """Mesure sur The Dead Hand Collection : 942 annonces exploitables, 5
    points perdus seulement a profondeur 4."""
    p = _plan_factice(cout_base=1.19, cout_profond=1.25, ev=1.36)
    assert p.result.profitability == pytest.approx(1.143, abs=0.01)
    assert p.deep_profitability == pytest.approx(1.088, abs=0.01)
    assert not p.fragile


def test_un_contrat_qui_ne_tient_qu_en_arrivant_premier_est_fragile():
    """Le cas dangereux : rentable sur le papier, perdant des que quelques
    annonces sont prises. C'est une course, pas une occasion."""
    p = _plan_factice(cout_base=1.00, cout_profond=1.40, ev=1.10)
    assert p.result.profitability >= 1.0
    assert p.deep_profitability < 1.0
    assert p.fragile


def test_un_panier_impossible_en_profondeur_est_fragile():
    """Si le panier ne peut meme plus etre compose, c'est le cas le plus
    fragile -- et `None` ne doit surtout pas se lire comme "pas de probleme"."""
    p = _plan_factice(cout_base=1.00, cout_profond=None, ev=1.50)
    assert p.deep_profitability is None
    assert p.fragile


def test_un_contrat_deja_perdant_n_est_pas_dit_fragile():
    """La fragilite qualifie ce qui BASCULE : un contrat perdant des le depart
    est simplement perdant, et le marquer fragile noierait le signal."""
    p = _plan_factice(cout_base=1.00, cout_profond=1.40, ev=0.80)
    assert p.result.profitability < 1.0
    assert not p.fragile
