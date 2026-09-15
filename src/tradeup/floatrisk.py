"""Le float d'entree est un TIRAGE, pas une valeur choisie.

Sur Steam on achete un palier d'usure, pas un float. Deux "Field-Tested" du
meme skin peuvent afficher 0.151 et 0.379 : l'acheteur subit la difference, et
c'est elle qui decide du palier de SORTIE.

Jusqu'ici ce risque etait couvert par une marge forfaitaire (`--float-safety`,
0.02, calibree a ~3 ecarts-types). Un forfait a le defaut de tout forfait : il
est trop lache pour un contrat sur des Factory New etroites, trop serre pour un
contrat sur des Field-Tested larges -- le palier le plus large du jeu fait 0.23,
le plus etroit 0.07.

Ce module remplace le forfait par la distribution reelle :

    float normalise d'une entree ~ uniforme sur la portion de palier accessible
    moyenne des 10                ~ normale (theoreme central limite)
    EV du contrat                 = somme des EV par palier, ponderees par la
                                    probabilite que la moyenne y tombe

L'EV etant constante par morceaux en fonction de la moyenne, cette somme est
EXACTE une fois la loi de la moyenne admise : il n'y a pas d'echantillonnage.

Sur la loi de la moyenne, justement : dix tirages ne font pas une gaussienne.
L'approximation normale seule se trompe de pres de deux points de pourcentage
sur les probabilites de palier (mesure contre Monte-Carlo, 400 000 tirages, huit
collections). Une correction d'Edgeworth au quatrieme ordre -- trois lignes,
l'asymetrie d'une uniforme etant nulle -- ramene cette erreur a 0.3 point, soit
le niveau du bruit d'echantillonnage. Voir `excess_kurtosis`.

Cas particulier important : une offre CSFloat porte son float exact. Il n'y a
alors plus de tirage, sigma vaut 0, et tout ce module se reduit au calcul
deterministe d'avant. C'est voulu -- c'est la difference entre explorer et
executer.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from .ev import InputItem, Outcome, PriceLookup, TradeUpResult, evaluate
from .models import Skin, Wear
from .wear import wear_breakpoints

#: Ecart-type d'une loi uniforme = largeur / sqrt(12).
_SQRT_12 = math.sqrt(12.0)

#: En deca, l'alea est negligeable et le calcul deterministe suffit.
NEGLIGIBLE_SIGMA = 1e-9


def uniform_sigma(lo: float, hi: float) -> float:
    """Ecart-type d'un float uniforme sur `[lo, hi]`."""
    return max(0.0, hi - lo) / _SQRT_12


def _normalized_span(option) -> tuple[float, float]:
    """Bornes NORMALISEES du float qu'un achat de ce palier peut donner."""
    skin: Skin = option.skin
    wear: Wear = option.wear
    if skin.float_span <= 0:
        n = skin.normalized(skin.min_float)
        return n, n
    lo = max(wear.lo, skin.min_float)
    hi = min(wear.hi, skin.max_float)
    return (
        (lo - skin.min_float) / skin.float_span,
        (hi - skin.min_float) / skin.float_span,
    )


def option_sigma(option) -> float:
    """Incertitude sur le float normalise d'une option d'achat.

    Une offre identifiee (`listing_id`) porte son float exact : plus de tirage,
    donc sigma nul. Sans identifiant, on achete un palier et le float est
    uniforme sur la portion de ce palier que le range du skin autorise.
    """
    if getattr(option, "listing_id", None):
        return 0.0
    return uniform_sigma(*_normalized_span(option))


def option_mu(option) -> float:
    """Float normalise ESPERE de cette option.

    Point qui a coute une version du modele : ce n'est pas `option.float_value`.
    Celui-la est une CIBLE de sourcing (`--float-pct 0.15` = "je vise 15 % du
    bas du palier"), alors que l'esperance d'un tirage uniforme tombe au milieu.
    Melanger les deux donnait un modele incoherent -- un sigma juste autour
    d'une moyenne fausse de 0.12, verifie contre Monte-Carlo.

    Une offre identifiee garde son float exact : elle n'est pas tiree.
    """
    if getattr(option, "listing_id", None):
        return option.skin.normalized(option.float_value)
    lo, hi = _normalized_span(option)
    return (lo + hi) / 2.0


def average_sigma(options: Sequence[object]) -> float:
    """Ecart-type de la MOYENNE des floats normalises de ces entrees.

    Les tirages sont independants : les variances s'additionnent, et la moyenne
    divise par le nombre d'entrees. D'ou la division par n et non par sqrt(n) --
    c'est la somme des variances qui porte deja le sqrt.
    """
    if not options:
        return 0.0
    variance = sum(option_sigma(o) ** 2 for o in options)
    return math.sqrt(variance) / len(options)


def average_mu(options: Sequence[object]) -> float:
    """Moyenne normalisee ESPEREE du lot, coherente avec `average_sigma`."""
    if not options:
        return 0.0
    return sum(option_mu(o) for o in options) / len(options)


def _phi(x: float) -> float:
    """Fonction de repartition de la loi normale centree reduite."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _densite(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def excess_kurtosis(options: Sequence[object]) -> float:
    """Kurtosis excedentaire de la moyenne des floats normalises.

    Dix tirages, ce n'est pas l'infini : la somme de dix uniformes est encore
    sensiblement plus "plate" qu'une gaussienne, et l'approximation normale se
    trompe de pres de deux points sur les probabilites de palier (mesure contre
    Monte-Carlo). Ce defaut est entierement porte par le kurtosis -- la loi
    uniforme etant symetrique, l'asymetrie est nulle.

    Pour une uniforme de largeur w, le quatrieme cumulant vaut -w^4 / 120.
    """
    variance = sum(option_sigma(o) ** 2 for o in options)
    if variance <= 0:
        return 0.0
    kappa4 = sum(-((_largeur(o)) ** 4) / 120.0 for o in options)
    return kappa4 / (variance**2)


def _largeur(option) -> float:
    """Largeur normalisee du tirage d'une option (0 si le float est connu)."""
    if getattr(option, "listing_id", None):
        return 0.0
    lo, hi = _normalized_span(option)
    return max(0.0, hi - lo)


def _cdf(z: float, kurtosis: float) -> float:
    """Repartition corrigee au quatrieme ordre (Edgeworth).

    Terme unique car l'asymetrie est nulle :

        F(z) ~ Phi(z) - phi(z) * (gamma2 / 24) * He3(z),  He3(z) = z^3 - 3z

    Divise l'erreur systematique par ~6 sur les cas mesures, pour trois lignes.
    """
    base = _phi(z)
    if kurtosis == 0.0:
        return base
    he3 = z**3 - 3.0 * z
    corrige = base - _densite(z) * (kurtosis / 24.0) * he3
    # Une correction d'Edgeworth peut sortir de [0, 1] dans les queues, ou la
    # serie diverge. On rabat plutot que de propager une "probabilite" absurde.
    return min(1.0, max(0.0, corrige))


def segment_probabilities(
    mu: float,
    sigma: float,
    breakpoints: Sequence[float],
    *,
    kurtosis: float = 0.0,
) -> list[float]:
    """Probabilite que la moyenne tombe dans chaque segment de `breakpoints`.

    `breakpoints` est la liste triee des frontieres (0.0 et 1.0 inclus) ; le
    resultat a donc un element de moins. La loi est tronquee a [0, 1] : une
    moyenne de floats normalises ne peut pas en sortir, et sans troncature la
    masse perdue aux bords fausserait toutes les probabilites.

    `kurtosis` corrige le fait que dix tirages ne font pas une gaussienne (voir
    `excess_kurtosis`). A zero, on retombe sur l'approximation normale simple.
    """
    n = len(breakpoints) - 1
    if n <= 0:
        return []
    if sigma <= NEGLIGIBLE_SIGMA:
        # Aucun alea : toute la masse est dans le segment qui contient mu.
        probs = [0.0] * n
        for i in range(n):
            if breakpoints[i] <= mu < breakpoints[i + 1]:
                probs[i] = 1.0
                return probs
        probs[-1 if mu >= breakpoints[-1] else 0] = 1.0
        return probs

    masse = _cdf((1.0 - mu) / sigma, kurtosis) - _cdf((0.0 - mu) / sigma, kurtosis)
    if masse <= 0:
        # mu est si loin de [0, 1] que la troncature ne laisse rien : on evite
        # la division par zero en rabattant sur le segment le plus proche.
        probs = [0.0] * n
        probs[0 if mu < 0.5 else -1] = 1.0
        return probs

    cdf = [_cdf((b - mu) / sigma, kurtosis) for b in breakpoints]
    # max(0, ...) : la correction n'est pas garantie monotone dans les queues.
    return [max(0.0, cdf[i + 1] - cdf[i]) / masse for i in range(n)]


@dataclass(frozen=True, slots=True)
class FloatOutcome:
    """Une issue du tirage de float : un palier de moyenne et son EV."""

    lo: float  # borne basse de moyenne normalisee
    hi: float  # borne haute (exclusive)
    probability: float
    result: TradeUpResult

    @property
    def ev_profit(self) -> float:
        return self.result.ev_profit


@dataclass(frozen=True, slots=True)
class StochasticResult:
    """Evaluation d'un contrat en integrant l'alea du float d'entree."""

    target: TradeUpResult  # le contrat tel qu'il serait si la moyenne visee tombait
    mu: float
    sigma: float
    branches: tuple[FloatOutcome, ...]

    @property
    def deterministic(self) -> bool:
        """Les floats sont-ils connus (offres reelles) plutot que tires ?"""
        return self.sigma <= NEGLIGIBLE_SIGMA

    @property
    def ev_net(self) -> float:
        return sum(b.probability * b.result.ev_net for b in self.branches)

    @property
    def cost(self) -> float:
        """Le cout ne depend pas du tirage : on achete avant de savoir."""
        return self.target.cost

    @property
    def ev_profit(self) -> float:
        return self.ev_net - self.cost

    @property
    def roi(self) -> float:
        return self.ev_profit / self.cost if self.cost > 0 else 0.0

    @property
    def hit_probability(self) -> float:
        """Probabilite d'obtenir le palier que l'optimiseur a VISE.

        Le palier vise est celui du panier tel qu'il a ete choisi
        (`target.avg_normalized`), pas celui de `mu` : mu est l'esperance du
        tirage, et tout l'interet est de mesurer l'ecart entre les deux.

        C'est le chiffre que la marge forfaitaire cherchait a garantir sans
        jamais le mesurer.
        """
        vise = self.target.avg_normalized
        for b in self.branches:
            if b.lo <= vise < b.hi:
                return b.probability
        return 0.0

    @property
    def optimism(self) -> float:
        """Ecart entre le profit affiche par le calcul fixe et le profit reel.

        Positif = le calcul deterministe surestime. C'est exactement ce que la
        marge forfaitaire masquait : elle deplacait la cible sans jamais dire
        de combien le chiffre affiche etait faux.
        """
        return self.target.ev_profit - self.ev_profit

    @property
    def downside_probability(self) -> float:
        """Probabilite que le contrat soit perdant a cause du tirage seul."""
        return sum(b.probability for b in self.branches if b.ev_profit < 0)

    def report(self) -> str:
        if self.deterministic:
            return (
                "Floats d'entree EXACTS (offres identifiees) : aucun tirage, "
                f"profit {self.ev_profit:+.2f}."
            )
        lignes = [
            f"Float d'entree aleatoire : moyenne visee {self.mu:.4f}, "
            f"ecart-type {self.sigma:.4f}",
            f"  probabilite de tenir le palier vise : {self.hit_probability:.1%}",
            f"  profit si le palier tombe : {self.target.ev_profit:+.2f}",
            f"  profit en esperance sur le tirage : {self.ev_profit:+.2f}",
        ]
        if self.optimism > 0.005:
            lignes.append(
                f"  le calcul a float fixe surestime de {self.optimism:+.2f}"
            )
        if self.downside_probability > 0:
            lignes.append(
                f"  {self.downside_probability:.1%} de chances que le tirage "
                f"rende le contrat perdant"
            )
        return "\n".join(lignes)


def evaluate_stochastic(
    inputs: Sequence[InputItem],
    outcomes_per_collection: dict[str, Sequence[Skin]],
    prices: PriceLookup,
    *,
    sigma: float,
    mu: float | None = None,
    kurtosis: float = 0.0,
    stattrak: bool = False,
) -> StochasticResult:
    """Evalue un contrat en integrant l'alea sur la moyenne des floats d'entree.

    `sigma` est l'ecart-type de la MOYENNE normalisee (voir `average_sigma`).
    A sigma nul, le resultat est identique au calcul deterministe.

    `mu` doit provenir du MEME modele que `sigma` -- soit `average_mu` sur les
    options. Par defaut on retombe sur la moyenne des floats vises, ce qui n'est
    correct que si ces floats sont ceux qu'on obtiendra vraiment (offres
    identifiees). Sur un achat au palier, la cible visee n'est pas l'esperance.

    L'EV etant constante entre deux frontieres d'usure, evaluer un point par
    segment suffit : aucune valeur intermediaire n'apporte d'information.
    """
    base = evaluate(
        inputs, outcomes_per_collection, prices, stattrak=stattrak
    )
    if mu is None:
        mu = base.avg_normalized

    all_outcomes = [s for skins in outcomes_per_collection.values() for s in skins]
    bornes = wear_breakpoints(all_outcomes)
    probs = segment_probabilities(mu, sigma, bornes, kurtosis=kurtosis)

    branches: list[FloatOutcome] = []
    for i, p in enumerate(probs):
        if p <= 1e-9:
            continue  # segment inatteignable : l'evaluer coute sans rien dire
        lo, hi = bornes[i], bornes[i + 1]
        milieu = (lo + hi) / 2.0
        branches.append(
            FloatOutcome(
                lo=lo,
                hi=hi,
                probability=p,
                result=evaluate(
                    inputs,
                    outcomes_per_collection,
                    prices,
                    stattrak=stattrak,
                    avg_override=milieu,
                ),
            )
        )

    return StochasticResult(
        target=base, mu=mu, sigma=sigma, branches=tuple(branches)
    )


def flatten(stochastic: StochasticResult) -> TradeUpResult:
    """Ecrase les branches de float en UNE distribution de sorties.

    L'alea du float n'est pas une couche a part : c'est une source d'incertitude
    de plus sur ce qu'on obtiendra. Une fois ecrase, tout l'aval -- variance,
    probabilite de gain, value at risk, classement, rapport HTML -- travaille
    sur la vraie distribution sans savoir que le float etait aleatoire.

    Un meme skin apparait une fois par palier d'usure atteignable : une sortie
    Factory New et la meme en Minimal Wear sont deux objets distincts sur le
    marche, avec deux prix.
    """
    if not stochastic.branches:
        return stochastic.target

    cumul: dict[tuple[str, Wear], list] = {}
    for branch in stochastic.branches:
        for o in branch.result.outcomes:
            p = branch.probability * o.probability
            if p <= 1e-9:
                continue  # une issue a 1e-11 pollue l'affichage sans rien dire
            cle = (o.skin.key, o.wear)
            if cle not in cumul:
                cumul[cle] = [o, 0.0, 0.0]
            entry = cumul[cle]
            entry[1] += p
            entry[2] += p * o.float_value

    fusionnes = [
        Outcome(
            skin=o.skin,
            # Float moyen de cette sortie, pondere : dans un palier donne le
            # float varie encore, et c'est lui qu'on affichera.
            float_value=somme_float / proba,
            wear=o.wear,
            probability=proba,
            net_value=o.net_value,
            priced=o.priced,
        )
        for (o, proba, somme_float) in cumul.values()
    ]
    fusionnes.sort(key=lambda o: o.net_value, reverse=True)

    base = stochastic.target
    return TradeUpResult(
        outcomes=tuple(fusionnes),
        inputs=base.inputs,
        cost=base.cost,
        # Ce que l'utilisateur VISE reste ce qu'il lit : c'est la consigne
        # d'achat. L'ecart a l'esperance est porte par la distribution.
        avg_input_float=base.avg_input_float,
        avg_normalized=base.avg_normalized,
        stattrak=base.stattrak,
        unpriced_probability=sum(
            b.probability * b.result.unpriced_probability
            for b in stochastic.branches
        ),
        cliff_distance=base.cliff_distance,
    )
