"""Mecanique du float : moyenne d'entree -> float de sortie -> palier d'usure.

Formule officielle du contrat d'echange :

    avg = (1/10) * somme des floats absolus des 10 entrees
    float_sortie = avg * (max_float_cible - min_float_cible) + min_float_cible

Deux pieges classiques, evites ici :
  1. La moyenne porte sur les floats ABSOLUS des entrees, pas sur leur position
     normalisee dans leur propre range.
  2. Le remap utilise le range du skin de SORTIE, pas celui des entrees.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from .models import WEARS_ORDERED, Skin, Wear

TRADEUP_INPUT_COUNT = 10


def average_float(floats: Sequence[float]) -> float:
    """Moyenne arithmetique des floats d'entree."""
    if not floats:
        raise ValueError("Aucun float fourni")
    return sum(floats) / len(floats)


def output_float(avg_input_float: float, target: Skin) -> float:
    """Float du skin obtenu, pour une moyenne d'entree donnee."""
    if not 0.0 <= avg_input_float <= 1.0:
        raise ValueError(f"Moyenne de float hors [0, 1] : {avg_input_float}")
    return avg_input_float * target.float_span + target.min_float


def wear_of(float_value: float) -> Wear:
    """Palier d'usure correspondant a un float."""
    if not 0.0 <= float_value <= 1.0:
        raise ValueError(f"Float hors [0, 1] : {float_value}")
    for w in WEARS_ORDERED:
        if float_value < w.hi:
            return w
    return Wear.BATTLE_SCARRED  # float == 1.0


def output_wear(avg_input_float: float, target: Skin) -> Wear:
    return wear_of(output_float(avg_input_float, target))


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


def feasible_average_range(inputs: Sequence[Skin]) -> tuple[float, float]:
    """Bornes de la moyenne atteignable pour un lot d'entrees donne."""
    if len(inputs) != TRADEUP_INPUT_COUNT:
        raise ValueError(f"Il faut exactement {TRADEUP_INPUT_COUNT} entrees")
    lo = sum(s.min_float for s in inputs) / len(inputs)
    hi = sum(s.max_float for s in inputs) / len(inputs)
    return (lo, hi)
