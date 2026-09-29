"""Mecanique du float : moyenne d'entree -> float de sortie -> palier d'usure.

Formule officielle du contrat d'echange :

    normalise_i = (float_i - min_i) / (max_i - min_i)     pour chaque entree
    avg          = (1/10) * somme des normalise_i
    float_sortie = avg * (max_cible - min_cible) + min_cible

Deux pieges, dont un a coute un contrat reel :
  1. La moyenne porte sur les floats NORMALISES, pas sur les floats affiches.
     Moyenner les floats bruts fait predire du Factory New la ou le jeu produit
     du Minimal Wear.
  2. Le remap final utilise le range du skin de SORTIE, pas celui des entrees.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from .models import WEARS_ORDERED, Skin, Wear

TRADEUP_INPUT_COUNT = 10


def average_normalized(entrees: Sequence[tuple[Skin, float]]) -> float:
    """Moyenne des floats NORMALISES, chacun rapporte au range de son skin.

    C'est la grandeur qui pilote le float de sortie. Voir `output_float` pour
    pourquoi la moyenne des floats absolus donne un resultat faux.
    """
    if not entrees:
        raise ValueError("Aucune entree fournie")
    return sum(skin.normalized(f) for skin, f in entrees) / len(entrees)


def output_float(avg_normalized_float: float, target: Skin) -> float:
    """Float du skin obtenu, a partir de la moyenne NORMALISEE des entrees.

        float_sortie = moyenne_normalisee x (max_cible - min_cible) + min_cible

    Le premier argument n'est PAS la moyenne des floats affiches. Chaque entree
    doit d'abord etre ramenee a sa position dans son propre range :
    `(float - min_skin) / (max_skin - min_skin)`.

    La distinction n'est pas theorique. Sur un contrat reellement execute, dix
    entrees affichant 0.0789 de moyenne mais 0.4011 en normalise ont produit un
    Desert Eagle Meteorite a float 0.0722 -- Minimal Wear. La moyenne des floats
    absolus predisait 0.0142, soit Factory New, et une sortie valorisee 1.16 EUR
    au lieu de 0.73 reels.
    """
    if not 0.0 <= avg_normalized_float <= 1.0:
        raise ValueError(f"Moyenne normalisee hors [0, 1] : {avg_normalized_float}")
    return avg_normalized_float * target.float_span + target.min_float


def wear_of(float_value: float) -> Wear:
    """Palier d'usure correspondant a un float."""
    if not 0.0 <= float_value <= 1.0:
        raise ValueError(f"Float hors [0, 1] : {float_value}")
    for w in WEARS_ORDERED:
        if float_value < w.hi:
            return w
    return Wear.BATTLE_SCARRED  # float == 1.0


def output_wear(avg_normalized_float: float, target: Skin) -> Wear:
    return wear_of(output_float(avg_normalized_float, target))


def required_average_for_wear(target: Skin, wear: Wear) -> tuple[float, float] | None:
    """Intervalle de moyennes d'entree donnant `wear` sur `target`.

    Renvoie ``(lo, hi)`` en moyenne d'entree, avec `hi` exclusif, ou ``None``
    si ce palier est inatteignable pour ce skin.
    """
    lo_f = max(wear.lo, target.min_float)
    hi_f = min(wear.hi, target.max_float)
    if lo_f >= hi_f:
        return None
    lo_avg = (lo_f - target.min_float) / target.float_span
    hi_avg = (hi_f - target.min_float) / target.float_span
    return (max(0.0, lo_avg), min(1.0, hi_avg))


def wear_breakpoints(targets: Iterable[Skin]) -> list[float]:
    """Moyennes d'entree ou l'usure d'au moins un skin de sortie change.

    L'EV d'un trade-up est constante par morceaux en fonction de la moyenne des
    floats d'entree : elle ne saute qu'aux frontieres d'usure. Renvoyer ces
    frontieres permet d'explorer l'espace des floats de facon EXACTE plutot que
    par echantillonnage.

    Le resultat inclut toujours 0.0 et 1.0 et est trie / dedoublonne.
    """
    points: set[float] = {0.0, 1.0}
    for skin in targets:
        for w in WEARS_ORDERED:
            for boundary in (w.lo, w.hi):
                if skin.min_float < boundary < skin.max_float:
                    avg = (boundary - skin.min_float) / skin.float_span
                    if 0.0 < avg < 1.0:
                        points.add(round(avg, 12))
    return sorted(points)

