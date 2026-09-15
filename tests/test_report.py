"""Tests du rapport HTML.

Le point sensible : les liens vers les annonces. C'est la raison d'etre de la
page -- sans eux il faut retrouver dix objets a la main par leur float, ce qui
prend assez de temps pour que certains partent et faussent le contrat.
"""

from __future__ import annotations

import pytest

from tradeup.ev import InputItem, Outcome, TradeUpResult
from tradeup.generator import InputOption
from tradeup.models import Collection, Rarity, Skin, Wear
from tradeup.plan import Plan
from tradeup.report import render, write_and_open

ENTREE = Skin(key="de", name="Desert Eagle | Meteorite", collection_id="bank",
              rarity=Rarity.MIL_SPEC, min_float=0.0, max_float=1.0)
SORTIE = Skin(key="ak", name="AK-47 | Emerald Pinstripe", collection_id="bank",
              rarity=Rarity.RESTRICTED, min_float=0.0, max_float=1.0)
COLLECTION = Collection(id="bank", name="The Bank Collection", skins=(ENTREE, SORTIE))


def make_plan(options=None, downgrade_net=3.65):
    options = options or tuple(
        InputOption(skin=ENTREE, wear=Wear.MINIMAL_WEAR, unit_cost=1.14,
                    float_value=0.07 + i * 0.001, listing_id=f"listing{i}")
        for i in range(10)
    )
    result = TradeUpResult(
        outcomes=(Outcome(skin=SORTIE, float_value=0.065, wear=Wear.FACTORY_NEW,
                          probability=1.0, net_value=16.02, priced=True),),
        inputs=tuple(InputItem(o.skin, o.float_value, o.unit_cost) for o in options),
        cost=sum(o.unit_cost for o in options),
        avg_input_float=0.0745,
        avg_normalized=0.0745,
        stattrak=False,
        unpriced_probability=0.0,
        cliff_distance=0.005,
    )
    return Plan(result=result, options=options, collection=COLLECTION,
                listings_examined=311, downgrade_net=downgrade_net)


def test_chaque_annonce_a_son_lien_direct():
    page = render(make_plan())
    for i in range(10):
        assert f"https://csfloat.com/item/listing{i}" in page
    assert page.count('class="buy"') == 10


def test_annonce_sans_identifiant_ne_fabrique_pas_de_lien_casse():
    """Mieux vaut afficher "non identifiee" qu'un lien qui menerait nulle part."""
    options = tuple(
        InputOption(skin=ENTREE, wear=Wear.MINIMAL_WEAR, unit_cost=1.0,
                    float_value=0.08, listing_id=None)
        for _ in range(10)
    )
    page = render(make_plan(options=options))
    assert "csfloat.com/item/None" not in page
    assert "annonce non identifiee" in page


def test_la_sortie_pointe_vers_une_recherche():
    page = render(make_plan())
    # Le nom contient | et espaces : il doit etre encode dans l'URL.
    assert "csfloat.com/search?market_hash_name=AK-47%20%7C%20Emerald" in page


def test_les_noms_sont_echappes():
    """Un nom de skin contient des caracteres qui casseraient le HTML."""
    piege = Skin(key="x", name='AK-47 | <script>"Test"', collection_id="bank",
                 rarity=Rarity.MIL_SPEC, min_float=0.0, max_float=1.0)
    options = tuple(
        InputOption(skin=piege, wear=Wear.MINIMAL_WEAR, unit_cost=1.0,
                    float_value=0.08, listing_id="abc")
        for _ in range(10)
    )
    page = render(make_plan(options=options))
    assert "<script>" not in page.split("<script>")[-1] or "&lt;script&gt;" in page
    assert "&lt;script&gt;" in page


def test_les_avertissements_sont_presents():
    page = render(make_plan())
    assert "Blocage de 7 jours" in page
    assert "Tolerance de float" in page
    assert "Panier perime" in page  # bloc masque, active par le script apres 15 min


def test_le_profit_est_signale_visuellement():
    gagnant = render(make_plan())
    assert 'class="val pos"' in gagnant

    perdant_plan = make_plan()
    object.__setattr__(perdant_plan.result, "cost", 99.0)
    assert 'class="val neg"' in render(perdant_plan)


def test_page_autonome_sans_ressource_externe():
    """Une ressource distante rendrait la page inutilisable hors ligne."""
    page = render(make_plan())
    for interdit in ("src=\"http", "<link", "@import", "cdn."):
        assert interdit not in page
    # Les seuls liens externes sont ceux vers les annonces, volontaires.
    assert page.count("href=\"https://csfloat.com") == 11  # 10 entrees + 1 sortie


def test_ecriture_et_chemin(tmp_path):
    cible = tmp_path / "sous" / "rapport.html"
    ecrit = write_and_open(make_plan(), cible, open_browser=False)
    assert ecrit == cible and cible.exists()
    assert "The Bank Collection" in cible.read_text(encoding="utf-8")


def test_devise_affichee():
    assert "montants en USD" in render(make_plan(), currency="USD")
    assert "montants en EUR" in render(make_plan(), currency="EUR")


# --- Porte de sortie ---------------------------------------------------------


def test_renoncer_coute_bien_moins_que_le_gain_espere():
    """La reponse a "et si le contrat n'est plus rentable dans 7 jours ?".

    On n'est pas engage : au bout du verrou les objets sont libres et on peut
    les revendre. Le risque n'est pas la mise entiere, mais le frottement d'un
    aller-retour.
    """
    plan = make_plan()  # sortie nette 16.02
    revente = plan.result.cost * 0.98  # revendu au meme prix, 2 % de frais
    avec_sortie = Plan(
        result=plan.result, options=plan.options, collection=plan.collection,
        listings_examined=plan.listings_examined, downgrade_net=plan.downgrade_net,
        exit_value=revente,
    )

    assert avec_sortie.exit_loss == pytest.approx(-plan.result.cost * 0.02)
    assert avec_sortie.exit_loss_ratio == pytest.approx(-0.02)
    # Le gain espere pese bien plus lourd que le cout du renoncement.
    assert avec_sortie.result.ev_profit > abs(avec_sortie.exit_loss) * 5


def test_pas_de_porte_de_sortie_sans_prix_de_revente():
    """Si une entree n'est plus cotee, on ne fabrique pas un chiffre."""
    plan = make_plan()
    assert plan.exit_value is None
    assert plan.exit_loss is None and plan.exit_loss_ratio is None


def test_pas_de_marge_affichee_sur_un_contrat_perdant():
    """Une "tolerance de baisse" negative n'a pas de sens.

    Sur un contrat deja perdant il n'y a rien a encaisser avant de perdre.
    L'affichage montrait "-120.7 %", ce qui ressemblait a un bug de calcul.
    """
    plan = make_plan()
    assert plan.price_drop_tolerance is not None  # gagnant : marge reelle
    assert plan.price_drop_tolerance > 0

    perdant = make_plan()
    object.__setattr__(perdant.result, "cost", 99.0)  # coute plus que la sortie
    assert perdant.result.ev_profit < 0
    assert perdant.price_drop_tolerance is None


# --- Le pire cas, vrai critere de securite -----------------------------------


def test_toutes_les_sorties_rentables_est_le_bon_critere():
    """Le nombre de sorties ne dit rien une fois les prix connus.

    Un contrat a 3 issues toutes rentables vaut mieux qu'un contrat a issue
    unique dont le gain est marginal : dans le premier le tirage ne peut pas
    faire perdre.
    """
    trois_sorties = tuple(
        Outcome(skin=SORTIE, float_value=0.05, wear=Wear.FACTORY_NEW,
                probability=1 / 3, net_value=v, priced=True)
        for v in (14.0, 18.0, 25.0)  # la pire couvre le cout (11.40)
    )
    plan = make_plan()
    object.__setattr__(plan.result, "outcomes", trois_sorties)

    assert plan.all_outcomes_profitable
    assert plan.worst_profit == pytest.approx(14.0 - plan.result.cost)
    assert plan.best_profit == pytest.approx(25.0 - plan.result.cost)


def test_une_seule_sortie_perdante_suffit_a_lever_le_drapeau():
    perdantes = tuple(
        Outcome(skin=SORTIE, float_value=0.05, wear=Wear.FACTORY_NEW,
                probability=0.5, net_value=v, priced=True)
        for v in (5.0, 30.0)  # 5.0 ne couvre pas les 11.40 de mise
    )
    plan = make_plan()
    object.__setattr__(plan.result, "outcomes", perdantes)

    assert not plan.all_outcomes_profitable
    assert plan.worst_profit < 0 < plan.best_profit


def test_sortie_unique_rentable_reste_signalee():
    """Le cas deterministe entre naturellement dans le meme critere."""
    plan = make_plan()  # une sortie a 16.02 pour 11.40 de cout
    assert plan.all_outcomes_profitable
    assert plan.worst_profit == pytest.approx(plan.best_profit)
