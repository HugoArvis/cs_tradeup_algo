"""Le contrat vers un GOLD : cinq Covert d'une caisse, un couteau ou des gants.

Possible depuis octobre 2025. C'est le contrat des videos "nothing to knife",
et le seul que le reste du projet ne savait pas calculer : il suppose dix
entrees, s'arrete a Covert, et travaille sur des collections alors que les
golds n'appartiennent qu'a des CAISSES.

CE QUI REND CE CONTRAT PARTICULIER

Les golds d'une meme caisse n'ont pas le meme range de float, et l'ecart est
enorme : le Kukri Fade va de 0 a 0.08, le Safari Mesh de 0.06 a 0.80. Pour une
meme moyenne d'entree, le premier sort Factory New et le second Field-Tested --
168 EUR contre 37. Un calcul qui suppose un range commun annonce des sorties
qui n'arriveront jamais ; c'est l'erreur qui a fait croire a un contrat
rentable a +8.5 % alors qu'il perd 54 %.

Le moteur d'EV gere deja ce cas : `output_float` lit le range de chaque sortie.
Il suffisait de lui donner les bons objets.

LA PROBABILITE

Uniforme sur le pool de golds de la caisse -- verifie contre un calculateur de
reference : treize Kukri, meme chance chacun. Ce n'est pas la formule des
contrats d'armes, ou le nombre de sorties par collection pondere le tirage.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .ev import Outcome, PriceLookup, TradeUpResult
from .models import Rarity, Skin, Wear
from .wear import output_float, wear_of

#: Un contrat vers un gold prend CINQ entrees, la ou un contrat d'armes en
#: prend dix.
GOLD_INPUT_COUNT = 5


@dataclass(frozen=True, slots=True)
class Crate:
    """Une caisse : ses Covert utilisables en entree, son pool de golds."""

    id: str
    name: str
    inputs: tuple[str, ...]  # noms marchands des Covert, sans usure
    golds: tuple[Skin, ...]

    @property
    def gold_probability(self) -> float:
        """Chance d'un gold donne. Uniforme sur le pool."""
        return 1.0 / len(self.golds) if self.golds else 0.0


def load_crates(raw: dict) -> list[Crate]:
    """Construit les caisses depuis le JSON de la base."""
    out: list[Crate] = []
    for c in raw.get("crates", []):
        golds = tuple(
            Skin(
                key=g["key"],
                name=g["name"],
                collection_id=c["id"],
                # Les golds sont hors de l'echelle Consumer..Covert du jeu.
                # On les range en Covert faute de niveau superieur : la rarete
                # ne sert ici qu'a identifier, jamais a calculer une sortie.
                rarity=Rarity.COVERT,
                min_float=float(g["min_float"]),
                max_float=float(g["max_float"]),
                stattrak=bool(g.get("stattrak", False)),
            )
            for g in c.get("golds", [])
        )
        if not golds or not c.get("inputs"):
            continue
        out.append(Crate(id=c["id"], name=c["name"],
                         inputs=tuple(c["inputs"]), golds=golds))
    return out


@dataclass(frozen=True, slots=True)
class GoldPlan:
    """Un contrat 5 Covert -> 1 gold, evalue."""

    crate: Crate
    input_name: str
    input_wear: Wear
    unit_cost: float
    avg_normalized: float
    outcomes: tuple[Outcome, ...]

    @property
    def cost(self) -> float:
        return GOLD_INPUT_COUNT * self.unit_cost

    @property
    def ev_net(self) -> float:
        return sum(o.probability * o.net_value for o in self.outcomes)

    @property
    def ev_profit(self) -> float:
        return self.ev_net - self.cost

    @property
    def roi(self) -> float:
        return self.ev_profit / self.cost if self.cost else 0.0

    @property
    def win_probability(self) -> float:
        return sum(o.probability for o in self.outcomes
                   if o.net_value >= self.cost)

    @property
    def unpriced_probability(self) -> float:
        return sum(o.probability for o in self.outcomes if not o.priced)

    def report(self, limit: int = 8) -> str:
        lignes = [
            f"{self.crate.name}  [5x {self.input_name} ({self.input_wear.label})]",
            f"  cout {self.cost:.2f} ({self.unit_cost:.2f} l'unite)  |  "
            f"EV nette {self.ev_net:.2f}  |  profit {self.ev_profit:+.2f} "
            f"({self.roi:+.1%})",
            f"  {self.win_probability:.0%} de chances d'y gagner  |  "
            f"{len(self.outcomes)} golds possibles, "
            f"{self.crate.gold_probability:.1%} chacun",
            "",
            f"  {'sortie':<44} {'float':>7} {'palier':>14} {'net':>9}",
            "  " + "-" * 78,
        ]
        for o in sorted(self.outcomes, key=lambda o: -o.net_value)[:limit]:
            lignes.append(
                f"  {o.skin.name[:44]:<44} {o.float_value:>7.3f} "
                f"{o.wear.label:>14} {o.net_value:>9.2f}"
            )
        if len(self.outcomes) > limit:
            lignes.append(f"  ... et {len(self.outcomes) - limit} autres")
        if self.unpriced_probability:
            lignes.append(
                f"  ATTENTION : {self.unpriced_probability:.0%} de la "
                f"probabilite sans prix connu"
            )
        return "\n".join(lignes)


def evaluate_crate(
    crate: Crate,
    input_skin: Skin,
    input_wear: Wear,
    unit_cost: float,
    prices: PriceLookup,
    *,
    float_percentile: float = 0.5,
) -> GoldPlan | None:
    """Evalue le contrat : cinq exemplaires d'une meme entree.

    `float_percentile` situe le float suppose des entrees dans leur palier. Par
    defaut le milieu -- sur un achat au palier, c'est l'esperance du tirage.

    Chaque gold recoit SON float, calcule depuis SON range : c'est tout l'enjeu.
    """
    lo = max(input_wear.lo, input_skin.min_float)
    hi = min(input_wear.hi, input_skin.max_float)
    flt = lo + float_percentile * (hi - lo)
    avg = input_skin.normalized(flt)

    p = crate.gold_probability
    outcomes: list[Outcome] = []
    for gold in crate.golds:
        f_out = output_float(avg, gold)
        w = wear_of(f_out)
        net = prices.sell_net(gold, w, False)
        outcomes.append(Outcome(
            skin=gold, float_value=f_out, wear=w, probability=p,
            net_value=net if net is not None else 0.0,
            priced=net is not None,
        ))

    if not outcomes:
        return None
    return GoldPlan(
        crate=crate, input_name=input_skin.name, input_wear=input_wear,
        unit_cost=unit_cost, avg_normalized=avg, outcomes=tuple(outcomes),
    )


def best_for_crate(
    crate: Crate,
    db,
    prices: PriceLookup,
    *,
    float_percentile: float = 0.5,
) -> GoldPlan | None:
    """Meilleur contrat de cette caisse, toutes entrees et usures confondues.

    On essaie chaque Covert de la caisse dans chaque usure : la moins chere
    n'est pas toujours la meilleure, puisque l'usure decide aussi du palier de
    sortie.
    """
    meilleur: GoldPlan | None = None
    for nom in crate.inputs:
        skin = db.find(nom)
        if skin is None:
            continue
        for wear in skin.available_wears():
            cout = prices.buy_cost(skin, wear, False)
            if cout is None or cout <= 0:
                continue
            plan = evaluate_crate(crate, skin, wear, cout, prices,
                                  float_percentile=float_percentile)
            if plan is None:
                continue
            if meilleur is None or plan.ev_profit > meilleur.ev_profit:
                meilleur = plan
    return meilleur


def scan_crates(
    crates: Sequence[Crate],
    db,
    prices: PriceLookup,
    *,
    float_percentile: float = 0.5,
    limit: int | None = 10,
) -> list[GoldPlan]:
    """Classe les caisses par profit espere."""
    plans = [
        p for p in (
            best_for_crate(c, db, prices, float_percentile=float_percentile)
            for c in crates
        )
        if p is not None
    ]
    plans.sort(key=lambda p: -p.ev_profit)
    return plans[:limit] if limit else plans
