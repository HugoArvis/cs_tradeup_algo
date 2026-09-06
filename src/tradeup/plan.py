"""Plan d'achat concret a partir des offres REELLES en vente sur CSFloat.

Difference de nature avec `scan` :

  `scan`  travaille sur des prix par palier d'usure et un float SUPPOSE. Il
          repond a "quelle collection merite qu'on s'y interesse ?".
  `plan`  travaille sur les annonces reellement en vente, avec leur float
          exact. Il repond a "voici les 10 objets a acheter, maintenant".

Consequence majeure : le float n'etant plus un tirage mais une donnee, la
marge de securite anti-falaise devient inutile. C'est ce qui rend CSFloat
structurellement meilleur que Steam pour executer un trade-up.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .db import SkinDatabase
from .ev import InputItem, TradeUpResult, evaluate
from .generator import InputOption, cheapest_unique_selection, options_from_listings
from .models import Collection, Rarity
from .pricing.csfloat import CSFloat
from .wear import TRADEUP_INPUT_COUNT, wear_breakpoints, wear_of

log = logging.getLogger(__name__)


class CSFloatPricer:
    """`PriceLookup` alimente par les offres reelles CSFloat.

    Valorisation SENSIBLE AU FLOAT, et c'est essentiel. A l'interieur d'un meme
    palier d'usure, le prix varie enormement : mesure sur l'AK-47 Emerald
    Pinstripe, une Factory New se vend 24-25 USD a float 0.005 mais 17-20 a
    float 0.070. Un modele qui ne retient qu'un prix par palier prend la moins
    chere -- donc celle a haut float -- et conclut a tort qu'il ne sert a rien
    de viser bas.
    """

    def __init__(self, source: CSFloat, *, sell_fee: float = 0.02,
                 safety_margin: float = 0.05, listings_limit: int = 50):
        self.source = source
        self.sell_fee = sell_fee
        self.safety_margin = safety_margin
        self.listings_limit = listings_limit
        self._book: dict[str, list[tuple[float, float]]] = {}  # nom -> [(float, prix)]

    def _listings(self, name: str) -> list[tuple[float, float]]:
        """Carnet nettoye, utilisable pour valoriser une sortie de trade-up.

        Deux filtres, dans cet ordre :

        1. On ecarte les objets STICKES ou avec breloque. Une sortie de contrat
           sort toujours nue ; la comparer a un exemplaire portant des
           Katowice 2014 revient a valoriser les stickers, pas l'arme. C'est ce
           qui a produit une Five-SeveN Candy Apple estimee 582 USD alors que
           les exemplaires nus se vendaient 85 a 127.

        2. On ecarte ce qui depasse 3x la mediane. Il reste toujours des
           annonces gonflees pour des raisons qu'on ne modelise pas (motif rare,
           numero de serie, vendeur qui teste le marche). Une seule d'entre
           elles suffit a fausser toute la courbe.
        """
        if name in self._book:
            return self._book[name]

        offres = [
            o for o in self.source.listings(name, limit=self.listings_limit)
            if o.float_value is not None and getattr(o, "plain", True)
        ]
        if offres:
            prix = sorted(o.price for o in offres)
            mediane = prix[len(prix) // 2]
            plafond = 3.0 * mediane
            gardees = [o for o in offres if o.price <= plafond]
            if len(gardees) < len(offres):
                log.info(
                    "%s : %d annonce(s) ecartee(s) au-dessus de %.2f (3x mediane)",
                    name, len(offres) - len(gardees), plafond,
                )
            offres = gardees or offres

        self._book[name] = sorted((o.float_value, o.price) for o in offres)
        return self._book[name]

    def _lowest(self, name: str) -> float | None:
        book = self._listings(name)
        return min((p for _, p in book), default=None)

    def price_at_float(self, name: str, float_value: float) -> float | None:
        """Prix qu'on peut REELLEMENT esperer en revendant, frais exclus.

        On retient la moins chere des annonces nues du palier d'usure -- le
        prix qu'il faudra battre pour vendre dans un delai raisonnable.

        Ce choix ignore DELIBEREMENT la prime de bas float, alors qu'elle
        existe (une Factory New a 0.005 s'affiche 40 % au-dessus d'une a
        0.070). Trois raisons :

        1. Ce sont des prix DEMANDES. CSFloat ne publie aucun volume, donc rien
           ne dit qu'ils se concluent -- ni en combien de temps.
        2. Cette prime concerne une poignee d'annonces. En la prenant pour
           reference, le modele a value une Five-SeveN Candy Apple 582 USD,
           puis 181, puis 121, la ou les exemplaires nus se vendent 85.
        3. Un outil qui decide de depenses reelles doit se tromper du cote
           prudent. Sous-estimer fait rater une occasion ; surestimer fait
           perdre de l'argent.

        La prime reste captable a l'execution : si votre sortie a un tres bon
        float, vous la vendrez peut-etre plus cher que prevu. Ce sera un bonus,
        pas une hypothese de calcul.
        """
        book = self._listings(name)
        if not book:
            return None
        return min(prix for _, prix in book)

    def sell_net_at_float(
        self, skin, wear, stattrak: bool, float_value: float
    ) -> float | None:
        p = self.price_at_float(skin.market_hash_name(wear, stattrak), float_value)
        if p is None:
            return None
        return p * (1 - self.sell_fee) * (1 - self.safety_margin)

    def sell_net(self, skin, wear, stattrak=False) -> float | None:
        p = self._lowest(skin.market_hash_name(wear, stattrak))
        if p is None:
            return None
        return p * (1 - self.sell_fee) * (1 - self.safety_margin)

    def buy_cost(self, skin, wear, stattrak=False) -> float | None:
        return self._lowest(skin.market_hash_name(wear, stattrak))

    def volume(self, skin, wear, stattrak=False) -> int | None:
        return None  # l'endpoint listings ne donne pas de volume de ventes


@dataclass(frozen=True, slots=True)
class Plan:
    """Un panier concret, achetable en l'etat."""

    result: TradeUpResult
    options: tuple[InputOption, ...]
    collection: Collection
    listings_examined: int

    downgrade_net: float | None = None  # valeur nette si la sortie perd un palier

    @property
    def downgrade_profit(self) -> float | None:
        """Profit si la moyenne franchit la frontiere -- le vrai risque ici."""
        if self.downgrade_net is None:
            return None
        return self.downgrade_net - self.result.cost

    exit_value: float | None = None  # produit net d'une revente des entrees

    @property
    def exit_loss(self) -> float | None:
        """Cout d'un renoncement : acheter les entrees puis les revendre.

        Repond a la seule objection serieuse contre l'attente de 7 jours : "si
        le contrat n'est plus rentable dans une semaine, je suis coince avec
        des skins achetes". On n'est pas coince. Au bout des 7 jours les objets
        sont libres, et on peut les REVENDRE au lieu de les fusionner.

        Le risque reel n'est donc pas de perdre la mise, mais de perdre le
        frottement d'un aller-retour : les frais de vente et l'ecart entre le
        prix d'achat et celui qu'on obtiendra en revendant.
        """
        if self.exit_value is None:
            return None
        return self.exit_value - self.result.cost

    @property
    def exit_loss_ratio(self) -> float | None:
        perte = self.exit_loss
        if perte is None or not self.result.cost:
            return None
        return perte / self.result.cost

    @property
    def price_drop_tolerance(self) -> float | None:
        """Baisse du prix de sortie supportable avant de perdre de l'argent.

        Le blocage d'echange de 7 jours cree une exposition reelle : le cout
        d'achat est fige aujourd'hui, la revente se fera une semaine plus tard.
        Cette fraction dit combien le marche peut baisser d'ici la avant que le
        contrat ne devienne perdant.
        """
        if self.result.ev_net <= 0:
            return None
        marge = 1.0 - self.result.cost / self.result.ev_net
        # Sur un contrat deja perdant, cette "marge" est negative et n'a aucun
        # sens : il n'y a pas de baisse a encaisser avant de perdre, on perd
        # deja. Afficher "-120 %" laissait croire a un bug de calcul.
        return marge if marge > 0 else None

    @property
    def float_slack(self) -> float:
        """Tolerance en float, sur la SOMME des 10 entrees.

        C'est la grandeur qui parle a l'execution : de combien le float total
        peut deriver (annonce vendue, remplacee par une autre) avant que la
        sortie ne change de palier.
        """
        return self.result.cliff_distance * TRADEUP_INPUT_COUNT

    def shopping_lines(self) -> list[str]:
        lignes = []
        for opt in sorted(self.options, key=lambda o: (o.skin.name, o.float_value)):
            lignes.append(
                f"  {opt.name:<46} {opt.unit_cost:>7.2f}  float {opt.float_value:.4f}"
            )
        return lignes


def build_plan(
    db: SkinDatabase,
    collection: Collection,
    rarity: Rarity,
    source: CSFloat,
    *,
    listings_per_skin: int = 30,
    safety_margin: float = 0.05,
    sell_fee: float = 0.02,
    float_margin: float = 0.005,
) -> Plan | None:
    """Construit le meilleur panier realisable avec ce qui est en vente.

    Balaye les paliers d'usure de la sortie (l'EV est constante par morceaux) et,
    pour chacun, cherche les 10 annonces les moins cheres dont la moyenne de
    float tient sous la frontiere.

    `float_margin` protege d'un risque different de celui du scan Steam. Ici les
    floats sont connus : il n'y a pas de tirage. Mais les 10 annonces sont des
    objets publics que n'importe qui peut acheter avant vous. Si l'une part et
    que la remplacante a un float un peu superieur, la moyenne franchit la
    frontiere et la sortie perd un palier -- ce qui, sur The Bank, transforme
    +5.35 en -7.53. La marge achete de la tolerance a la substitution.
    """
    outcomes = collection.outcomes_for_input_rarity(rarity)
    if not outcomes:
        return None

    # --- Recuperer les offres reelles de chaque entree possible ---
    toutes: list[InputOption] = []
    examinees = 0
    for skin in collection.by_rarity(rarity):
        for wear in skin.available_wears():
            offres = source.listings(
                skin.market_hash_name(wear), limit=listings_per_skin
            )
            examinees += len(offres)
            toutes.extend(options_from_listings(skin, offres))

    if len(toutes) < TRADEUP_INPUT_COUNT:
        log.info("Seulement %d offres pour %s : insuffisant", len(toutes), collection.name)
        return None

    pricer = CSFloatPricer(source, sell_fee=sell_fee, safety_margin=safety_margin)
    outcomes_map = {collection.id: outcomes}

    meilleur: Plan | None = None
    for hi in _breakpoints_avec_annonces(outcomes, pricer):
        budget = (hi - max(float_margin, 1e-9)) * TRADEUP_INPUT_COUNT
        selection = cheapest_unique_selection(toutes, TRADEUP_INPUT_COUNT, budget)
        if selection is None:
            continue

        items = [
            InputItem(skin=o.skin, float_value=o.float_value, unit_cost=o.unit_cost)
            for o in selection
        ]
        atteint = sum(o.float_value for o in selection) / TRADEUP_INPUT_COUNT
        result = evaluate(
            items, outcomes_map, pricer, cliff_distance=hi - atteint
        )
        if meilleur is None or result.ev_profit > meilleur.result.ev_profit:
            meilleur = Plan(
                result=result,
                options=tuple(selection),
                collection=collection,
                listings_examined=examinees,
                downgrade_net=_net_si_palier_rate(result, pricer),
                exit_value=_valeur_de_revente(selection, pricer, sell_fee),
            )

    return meilleur


def _valeur_de_revente(
    selection: list[InputOption], pricer: CSFloatPricer, sell_fee: float
) -> float | None:
    """Ce qu'on recupererait en revendant les entrees au lieu de les fusionner.

    On se valorise au prix qu'il faudra battre pour vendre -- la moins chere
    annonce nue du palier -- diminue des frais. C'est volontairement pessimiste
    et sans decote supplementaire : il s'agit d'une porte de sortie, pas d'une
    operation qu'on cherche a rendre attirante.
    """
    total = 0.0
    for opt in selection:
        prix = pricer.price_at_float(opt.name, opt.float_value)
        if prix is None:
            return None
        total += prix * (1 - sell_fee)
    return total


def _breakpoints_avec_annonces(
    outcomes: list, pricer: CSFloatPricer, max_points: int = 60
) -> list[float]:
    """Moyennes d'entree a tester, frontieres d'usure ET paliers de prix.

    Une fois la sortie cotee au float pres, l'EV ne saute plus seulement aux
    frontieres d'usure : elle saute a chaque annonce concurrente. Viser un float
    juste sous une annonce chere permet de se vendre a son prix. Ces points-la
    doivent donc etre explores, sinon l'optimiseur ne verra jamais l'interet de
    descendre en float.
    """
    points = set(wear_breakpoints(outcomes)[1:])

    for skin in outcomes:
        for wear in skin.available_wears():
            book = pricer._listings(skin.market_hash_name(wear))
            for float_annonce, _ in book:
                # Moyenne d'entree qui produirait exactement ce float de sortie.
                if skin.float_span <= 0:
                    continue
                avg = (float_annonce - skin.min_float) / skin.float_span
                if 0.0 < avg <= 1.0:
                    points.add(round(avg, 9))

    ordonnes = sorted(points)
    if len(ordonnes) <= max_points:
        return ordonnes
    # Echantillonnage regulier pour borner le cout de calcul, en gardant les
    # extremes qui portent l'essentiel de l'information.
    pas = len(ordonnes) / max_points
    garde = {ordonnes[min(int(i * pas), len(ordonnes) - 1)] for i in range(max_points)}
    garde.update(ordonnes[:5])
    garde.update(ordonnes[-5:])
    return sorted(garde)


@dataclass(frozen=True, slots=True)
class Repetition:
    """Un contrat dans une serie, avec le prix realise a la revente."""

    rang: int
    cout: float
    net_revente: float

    @property
    def profit(self) -> float:
        return self.net_revente - self.cout


def plan_series(
    db: SkinDatabase,
    collection: Collection,
    rarity: Rarity,
    source: CSFloat,
    *,
    max_contracts: int = 20,
    listings_per_skin: int = 50,
    sell_fee: float = 0.02,
    safety_margin: float = 0.05,
    float_margin: float = 0.005,
) -> list[Repetition]:
    """Combien de fois le contrat reste rentable, carnet d'ordres a l'appui.

    Repeter un contrat n'est pas repeter le meme prix. Deux effets s'usent :

      - a l'ACHAT, chaque panier consomme les annonces les moins cheres ; le
        suivant paie plus cher.
      - a la REVENTE, ecouler N exemplaires oblige a descendre dans le carnet :
        le k-ieme se vend autour du k-ieme meilleur prix existant.

    On s'arrete des que le profit marginal devient negatif. Le resultat n'est
    pas une prevision mais un ordre de grandeur : il suppose que personne
    d'autre ne bouge pendant ce temps.
    """
    outcomes = collection.outcomes_for_input_rarity(rarity)
    if len(outcomes) != 1:
        raise ValueError(
            "plan_series ne traite que les collections a sortie unique "
            f"({collection.name} en a {len(outcomes)})"
        )
    cible = outcomes[0]

    # --- Pool d'achat, recupere une seule fois ---
    pool: list[InputOption] = []
    for skin in collection.by_rarity(rarity):
        for wear in skin.available_wears():
            offres = source.listings(skin.market_hash_name(wear), limit=listings_per_skin)
            pool.extend(options_from_listings(skin, offres))

    # --- Carnet de revente : les prix qu'il faudra battre, dans l'ordre ---
    serie: list[Repetition] = []
    restant = list(pool)

    for rang in range(1, max_contracts + 1):
        meilleur_cout = None
        meilleure_selection = None
        meilleur_net = None

        for hi in wear_breakpoints([cible])[1:]:
            budget = (hi - max(float_margin, 1e-9)) * TRADEUP_INPUT_COUNT
            sel = cheapest_unique_selection(restant, TRADEUP_INPUT_COUNT, budget)
            if sel is None:
                continue
            moyenne = sum(o.float_value for o in sel) / TRADEUP_INPUT_COUNT
            usure = wear_of(moyenne * cible.float_span + cible.min_float)
            asks = sorted(
                o.price for o in source.listings(
                    cible.market_hash_name(usure), limit=listings_per_skin
                )
            )
            if not asks:
                continue
            # Le k-ieme exemplaire vendu s'ecoule autour du k-ieme meilleur prix.
            ask = asks[min(rang - 1, len(asks) - 1)]
            net = ask * (1 - sell_fee) * (1 - safety_margin)
            cout = sum(o.unit_cost for o in sel)
            if meilleur_cout is None or net - cout > meilleur_net - meilleur_cout:
                meilleur_cout, meilleure_selection, meilleur_net = cout, sel, net

        if meilleure_selection is None or meilleur_net - meilleur_cout <= 0:
            break

        serie.append(Repetition(rang=rang, cout=meilleur_cout, net_revente=meilleur_net))
        utilisees = {id(o) for o in meilleure_selection}
        restant = [o for o in restant if id(o) not in utilisees]

    return serie


def _net_si_palier_rate(result: TradeUpResult, pricer: CSFloatPricer) -> float | None:
    """Valeur nette de la sortie si la moyenne franchit la frontiere.

    Sans ce chiffre, un plan affiche "+50 %" sans dire que l'echec coute -70 %.
    L'asymetrie est l'information decisive.
    """
    from .models import WEARS_ORDERED

    total = 0.0
    connu = False
    for outcome in result.outcomes:
        index = WEARS_ORDERED.index(outcome.wear)
        pire = WEARS_ORDERED[min(index + 1, len(WEARS_ORDERED) - 1)]
        if pire not in outcome.skin.available_wears():
            continue
        net = pricer.sell_net(outcome.skin, pire, result.stattrak)
        if net is not None:
            total += outcome.probability * net
            connu = True
    return total if connu else None
