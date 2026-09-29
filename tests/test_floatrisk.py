"""Tests du float d'entree traite comme variable aleatoire.

Le risque propre a ce module est de produire un modele qui a l'air juste : des
formules plausibles, des probabilites qui somment a 1, et une moyenne fausse.
C'est exactement ce qui est arrive a la premiere version -- un sigma exact
autour d'un mu decale de 0.12, parce que la CIBLE de sourcing (`--float-pct`)
avait ete confondue avec l'ESPERANCE du tirage.

D'ou le test central : confronter le modele analytique a un Monte-Carlo brut.
Le tirage est la verite terrain ; si les deux divergent, c'est le modele qui a
tort.
"""

from __future__ import annotations

import math
import random
import statistics

import pytest

from tradeupfinder.db import SkinDatabase
from tradeupfinder.ev import InputItem, evaluate
from tradeupfinder.floatrisk import (
    average_mu,
    average_sigma,
    evaluate_stochastic,
    flatten,
    option_mu,
    option_sigma,
    excess_kurtosis,
    segment_probabilities,
    uniform_sigma,
)
from tradeupfinder.generator import InputOption, build_options, optimize_recipe, Recipe
from tradeupfinder.models import Rarity
from tradeupfinder.pricing.repository import StaticPricer
from tradeupfinder.wear import wear_breakpoints


@pytest.fixture(scope="module")
def db():
    return SkinDatabase.load()


@pytest.fixture(scope="module")
def bank(db):
    return db.find_collection("The Bank Collection")


def pricer_plat(col, rarity, prix=1.0):
    """Prix uniformes : isole la mecanique du float de celle des prix."""
    valeurs = {}
    for r in (rarity, rarity.next_up):
        for skin in col.by_rarity(r):
            for w in skin.available_wears():
                valeurs[skin.market_hash_name(w)] = prix
    return StaticPricer(valeurs)


# --- Moments d'une option ----------------------------------------------------


def test_sigma_dune_uniforme():
    assert uniform_sigma(0.0, 1.0) == pytest.approx(1 / math.sqrt(12))
    assert uniform_sigma(0.5, 0.5) == 0.0
    assert uniform_sigma(0.8, 0.2) == 0.0  # bornes inversees : pas de negatif


def test_une_offre_identifiee_na_aucun_alea(bank):
    skin = bank.by_rarity(Rarity.INDUSTRIAL)[0]
    from tradeupfinder.wear import wear_of

    f = (skin.min_float + skin.max_float) / 2
    offre = InputOption(skin, wear_of(f), 1.0, f, listing_id="abc123")
    assert option_sigma(offre) == 0.0
    assert option_mu(offre) == pytest.approx(skin.normalized(f))


def test_mu_dune_option_est_le_milieu_pas_la_cible(bank):
    """Le piege qui a fausse la premiere version du modele.

    `float_value` dit ou l'on VISE (--float-pct 0.15). L'esperance d'un tirage
    uniforme tombe au milieu du palier. Les confondre decale la moyenne.
    """
    pricer = pricer_plat(bank, Rarity.INDUSTRIAL)
    options = build_options(bank, Rarity.INDUSTRIAL, pricer, float_percentile=0.15)
    opt = options[0]
    assert option_mu(opt) != pytest.approx(opt.normalized)
    assert option_mu(opt) > opt.normalized  # viser bas ne suffit pas a l'obtenir


def test_mu_coincide_avec_la_cible_quand_on_vise_le_milieu(bank):
    pricer = pricer_plat(bank, Rarity.INDUSTRIAL)
    options = build_options(bank, Rarity.INDUSTRIAL, pricer, float_percentile=0.5)
    for opt in options:
        assert option_mu(opt) == pytest.approx(opt.normalized)


def test_sigma_de_la_moyenne_decroit_avec_le_nombre_dentrees(bank):
    pricer = pricer_plat(bank, Rarity.INDUSTRIAL)
    opt = build_options(bank, Rarity.INDUSTRIAL, pricer)[0]
    s1 = average_sigma([opt])
    s10 = average_sigma([opt] * 10)
    assert s10 == pytest.approx(s1 / math.sqrt(10))


def test_aucune_option_aucun_alea():
    assert average_sigma([]) == 0.0
    assert average_mu([]) == 0.0


# --- Probabilites par segment ------------------------------------------------


def test_les_probabilites_somment_a_un():
    bornes = [0.0, 0.2, 0.5, 0.8, 1.0]
    probs = segment_probabilities(0.4, 0.1, bornes)
    assert len(probs) == len(bornes) - 1
    assert sum(probs) == pytest.approx(1.0)


def test_sans_alea_toute_la_masse_est_dans_un_segment():
    probs = segment_probabilities(0.45, 0.0, [0.0, 0.2, 0.5, 1.0])
    assert probs == [0.0, 1.0, 0.0]


def test_la_loi_est_tronquee_a_zero_un():
    # Une moyenne de floats normalises ne peut pas sortir de [0, 1] : sans
    # troncature, la masse perdue aux bords fausserait toutes les probabilites.
    probs = segment_probabilities(0.02, 0.2, [0.0, 0.5, 1.0])
    assert sum(probs) == pytest.approx(1.0)


def test_le_modele_colle_au_monte_carlo(bank):
    """Verite terrain : on tire, et on compare.

    Tolerance a 1 point, dont ~0.2 de bruit d echantillonnage a 60 000 tirages.
    Elle n est tenable que grace a la correction
    d'Edgeworth : l'approximation normale nue se trompe de pres de 2 points
    (voir le test suivant).
    """
    rarity = Rarity.INDUSTRIAL
    pricer = pricer_plat(bank, rarity)
    options = build_options(bank, rarity, pricer, float_percentile=0.5)
    panier = (options * 10)[:10]

    sorties = [s for s in bank.by_rarity(rarity.next_up)]
    bornes = wear_breakpoints(sorties)
    mu, sigma = average_mu(panier), average_sigma(panier)
    analytique = segment_probabilities(
        mu, sigma, bornes, kurtosis=excess_kurtosis(panier)
    )

    rng = random.Random(12345)
    tirages = []
    for _ in range(60_000):
        total = 0.0
        for o in panier:
            a = max(o.wear.lo, o.skin.min_float)
            b = min(o.wear.hi, o.skin.max_float)
            total += o.skin.normalized(rng.uniform(a, b))
        tirages.append(total / len(panier))

    # La moyenne empirique doit tomber sur mu : c'est ce que la v1 ratait.
    assert statistics.fmean(tirages) == pytest.approx(mu, abs=0.005)
    assert statistics.pstdev(tirages) == pytest.approx(sigma, abs=0.002)

    compte = [0] * (len(bornes) - 1)
    for m in tirages:
        for i in range(len(bornes) - 1):
            if bornes[i] <= m < bornes[i + 1]:
                compte[i] += 1
                break
    simule = [c / len(tirages) for c in compte]
    for a, s in zip(analytique, simule):
        assert abs(a - s) < 0.01

    # La correction d'Edgeworth gagne bien quelque chose : sans elle, l'ecart
    # depasse la tolerance ci-dessus. Si ce test casse, c'est que la correction
    # a cesse d'etre appliquee quelque part.
    nue = segment_probabilities(mu, sigma, bornes)
    assert max(abs(a - s) for a, s in zip(nue, simule)) > max(
        abs(a - s) for a, s in zip(analytique, simule)
    )


def test_le_kurtosis_est_nul_sans_alea(bank):
    from tradeupfinder.wear import wear_of

    skin = bank.by_rarity(Rarity.INDUSTRIAL)[0]
    f = (skin.min_float + skin.max_float) / 2
    offre = InputOption(skin, wear_of(f), 1.0, f, listing_id="abc")
    assert excess_kurtosis([offre]) == 0.0
    assert excess_kurtosis([]) == 0.0


def test_la_correction_reste_une_probabilite_valide():
    """Edgeworth diverge dans les queues : les valeurs doivent rester bornees."""
    bornes = [0.0, 0.1, 0.2, 0.5, 1.0]
    for mu in (0.001, 0.05, 0.5, 0.95, 0.999):
        for sigma in (0.002, 0.05, 0.3):
            probs = segment_probabilities(mu, sigma, bornes, kurtosis=-0.5)
            assert all(p >= 0.0 for p in probs)
            assert sum(probs) == pytest.approx(1.0)


# --- Evaluation integree -----------------------------------------------------


def panier_de_test(bank, rarity, pricer, percentile=0.5):
    options = build_options(bank, rarity, pricer, float_percentile=percentile)
    choisies = (options * 10)[:10]
    items = [
        InputItem(skin=o.skin, float_value=o.float_value, unit_cost=o.unit_cost)
        for o in choisies
    ]
    return choisies, items


def test_sans_alea_lintegration_ne_change_rien(bank, db):
    rarity = Rarity.INDUSTRIAL
    pricer = pricer_plat(bank, rarity)
    _, items = panier_de_test(bank, rarity, pricer)
    carte = db.outcomes_map([bank], rarity)

    fixe = evaluate(items, carte, pricer)
    integre = flatten(evaluate_stochastic(items, carte, pricer, sigma=0.0))
    assert integre.ev_net == pytest.approx(fixe.ev_net)
    assert integre.cost == pytest.approx(fixe.cost)


def test_lintegration_conserve_la_masse_de_probabilite(bank, db):
    rarity = Rarity.INDUSTRIAL
    pricer = pricer_plat(bank, rarity)
    choisies, items = panier_de_test(bank, rarity, pricer)
    carte = db.outcomes_map([bank], rarity)

    resultat = flatten(
        evaluate_stochastic(
            items, carte, pricer,
            sigma=average_sigma(choisies), mu=average_mu(choisies),
        )
    )
    assert sum(o.probability for o in resultat.outcomes) == pytest.approx(1.0)


def test_lalea_fait_apparaitre_des_paliers_que_le_calcul_fixe_ignorait(bank, db):
    """Le contrat "certain" ne l'est pas : le tirage ouvre d'autres sorties."""
    rarity = Rarity.INDUSTRIAL
    pricer = pricer_plat(bank, rarity)
    choisies, items = panier_de_test(bank, rarity, pricer)
    carte = db.outcomes_map([bank], rarity)

    fixe = evaluate(items, carte, pricer)
    integre = flatten(
        evaluate_stochastic(
            items, carte, pricer,
            sigma=average_sigma(choisies), mu=average_mu(choisies),
        )
    )
    assert integre.distinct_outcomes > fixe.distinct_outcomes


def test_le_rapport_chiffre_loptimisme_du_calcul_fixe(bank, db):
    rarity = Rarity.INDUSTRIAL
    # Prix en escalier : une sortie Factory New vaut bien plus que la meme en
    # Minimal Wear, donc rater le palier coute cher et se mesure.
    valeurs = {}
    for skin in bank.by_rarity(rarity):
        for w in skin.available_wears():
            valeurs[skin.market_hash_name(w)] = 0.10
    for skin in bank.by_rarity(rarity.next_up):
        for i, w in enumerate(skin.available_wears()):
            valeurs[skin.market_hash_name(w)] = 100.0 / (10 ** i)
    pricer = StaticPricer(valeurs)

    choisies, items = panier_de_test(bank, rarity, pricer)
    carte = db.outcomes_map([bank], rarity)
    stoch = evaluate_stochastic(
        items, carte, pricer,
        sigma=average_sigma(choisies), mu=average_mu(choisies),
    )
    assert not stoch.deterministic
    assert 0.0 <= stoch.hit_probability <= 1.0
    assert "aleatoire" in stoch.report()


def test_un_panier_sans_alea_le_dit(bank, db):
    rarity = Rarity.INDUSTRIAL
    pricer = pricer_plat(bank, rarity)
    _, items = panier_de_test(bank, rarity, pricer)
    carte = db.outcomes_map([bank], rarity)
    stoch = evaluate_stochastic(items, carte, pricer, sigma=0.0)
    assert stoch.deterministic
    assert "EXACTS" in stoch.report()


# --- Branchement dans l'optimiseur -------------------------------------------


def test_le_mode_aleatoire_impose_le_percentile_median(bank, db):
    """Viser 0.15 sans pouvoir filtrer les floats n'est pas prudent, c'est faux.

    Le mode random doit donc ignorer --float-pct : le float moyen retenu est
    celui du milieu du palier, pas celui qu'on esperait sourcer.
    """
    rarity = Rarity.INDUSTRIAL
    pricer = pricer_plat(bank, rarity)
    recipe = Recipe(counts=((bank.id, 10),), rarity=rarity)

    fixe = optimize_recipe(recipe, db, pricer, float_percentile=0.15)
    alea = optimize_recipe(recipe, db, pricer, float_percentile=0.15,
                           float_model="random")
    assert fixe is not None and alea is not None
    assert alea.avg_input_float > fixe.avg_input_float


def test_le_mode_aleatoire_reste_calculable_sur_toute_la_base(db):
    """Garde-fou : aucune recette ne doit lever sur le chemin stochastique."""
    rarity = Rarity.INDUSTRIAL
    for col in db.tradeable_collections(rarity)[:5]:
        pricer = pricer_plat(col, rarity)
        recipe = Recipe(counts=((col.id, 10),), rarity=rarity)
        resultat = optimize_recipe(recipe, db, pricer, float_model="random")
        if resultat is not None:
            assert sum(o.probability for o in resultat.outcomes) == pytest.approx(1.0)


def test_evaluate_refuse_une_moyenne_imposee_absurde(bank, db):
    rarity = Rarity.INDUSTRIAL
    pricer = pricer_plat(bank, rarity)
    _, items = panier_de_test(bank, rarity, pricer)
    carte = db.outcomes_map([bank], rarity)
    with pytest.raises(ValueError):
        evaluate(items, carte, pricer, avg_override=1.5)


def test_loptimiseur_recule_devant_une_falaise_quand_ca_paie(bank, db):
    """La marge de securite devient une DECISION, plus un reglage subi.

    On fabrique une falaise : la sortie Factory New vaut 100, la Minimal Wear 1,
    et les entrees a bas float coutent plus cher. Se coller sous la frontiere
    est le moins cher mais laisse une chance sur deux de basculer.

    Le mode fixe ne voit pas le risque et se gare au plus pres. Le mode
    aleatoire doit accepter de payer pour s'en eloigner.
    """
    rarity = Rarity.INDUSTRIAL
    valeurs = {}
    for skin in bank.by_rarity(rarity):
        for i, w in enumerate(skin.available_wears()):
            valeurs[skin.market_hash_name(w)] = 1.0 / (i + 1)
    for skin in bank.by_rarity(rarity.next_up):
        for i, w in enumerate(skin.available_wears()):
            valeurs[skin.market_hash_name(w)] = 100.0 if i == 0 else 1.0
    pricer = StaticPricer(valeurs)
    recipe = Recipe(counts=((bank.id, 10),), rarity=rarity)

    fixe = optimize_recipe(recipe, db, pricer, float_model="fixed",
                           float_percentile=0.5)
    alea = optimize_recipe(recipe, db, pricer, float_model="random",
                           float_percentile=0.5)
    assert fixe is not None and alea is not None

    # Il recule : moyenne visee plus basse, donc entrees plus cheres.
    assert alea.avg_normalized < fixe.avg_normalized
    assert alea.cost > fixe.cost

    def proba_fn(r):
        return sum(o.probability for o in r.outcomes
                   if o.wear.label == "Factory New")

    assert proba_fn(alea) > proba_fn(fixe)
