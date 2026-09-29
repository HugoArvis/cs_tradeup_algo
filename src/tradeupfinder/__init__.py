"""Assistant d'aide a la decision pour les trade-up contracts CS2."""

from .db import SkinDatabase
from .ev import InputItem, Outcome, TradeUpResult, evaluate, outcome_probabilities
from .models import Collection, Rarity, Skin, Wear
from .scan import scan
from .scoring import Candidate, Ranking, ScreenConfig, explain, shopping_list
from .wear import output_float, output_wear, wear_of

__version__ = "0.1.0"

__all__ = [
    "Candidate",
    "Collection",
    "InputItem",
    "Outcome",
    "Ranking",
    "Rarity",
    "ScreenConfig",
    "Skin",
    "SkinDatabase",
    "TradeUpResult",
    "Wear",
    "evaluate",
    "explain",
    "outcome_probabilities",
    "output_float",
    "output_wear",
    "scan",
    "shopping_list",
    "wear_of",
]
