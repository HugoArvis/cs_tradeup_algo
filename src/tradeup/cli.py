"""Interface en ligne de commande.

    tradeup db                       etat de la base statique
    tradeup price "AK-47 | Redline (Field-Tested)"
    tradeup scan --rarity mil-spec --collections "The Recoil Collection"
    tradeup inspect "The Recoil Collection" --rarity mil-spec
    tradeup cache
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import urllib.error
from datetime import datetime
from pathlib import Path

from .config import csfloat_api_key, mask
from .db import SkinDatabase
from .fees import FEE_MODELS
from .models import TRADEABLE_INPUT_RARITIES, Rarity, Wear
from .pricing.cache import QuoteCache
from .pricing.repository import MarketPricer
from .pricing.steam import CURRENCIES, SteamMarket
from .plan import BudgetEpuise, build_plan
from .pricing.csfloat import CSFloat
from .pricing.http import RateLimited
from .gold import GOLD_INPUT_COUNT, scan_crates
from .journal import Journal
from .inventory import (
    best_tradeups as best_inventory_tradeups,
    closest_gaps as inventory_gaps,
    from_csfloat_rows,
    load_file as load_inventory_file,
    summary as inventory_summary,
)
from .orders import (
    DEFAULT_MAX_DISCOUNT,
    DEFAULT_TARGET_ROI,
    fill_estimate,
    scan_orders,
)
from .daily import (
    DECISION_MAX_AGE,
    DEFAULT_BUDGET,
    DEFAULT_CONFIRM,
    DEFAULT_TOP,
    confirmer,
    Ligne,
    Passage,
    age_max,
    historique,
    journaliser,
    noms_prioritaires,
    recoter,
)
from .schedule import equivalent_cron, lanceur, supporte, taches
from .refresh import DEFAULT_DRIFT_THRESHOLD, collection_roles, refresh_quotes
from .report import write_and_open
from .scan import prefetch, required_market_names, scan
from .liquidity import execution_capacity
from .simulate import contracts_for_significance, sanity_check, simulate_series
from .scoring import Ranking, ScreenConfig, explain, shopping_list

RARITY_ALIASES = {
    "consumer": Rarity.CONSUMER,
    "industrial": Rarity.INDUSTRIAL,
    "mil-spec": Rarity.MIL_SPEC,
    "milspec": Rarity.MIL_SPEC,
    "restricted": Rarity.RESTRICTED,
    "classified": Rarity.CLASSIFIED,
}


def _progress(prefix: str):
    def cb(i: int, total: int) -> None:
        if i == total or i % 10 == 0:
            pct = 100 * i / total if total else 100
            print(f"\r  {prefix} {i}/{total} ({pct:.0f}%)", end="", file=sys.stderr)
            if i == total:
                print(file=sys.stderr)

    return cb


def _make_pricer(args) -> tuple[MarketPricer, QuoteCache]:
    cache = QuoteCache(ttl_seconds=args.ttl * 3600)
    steam = SteamMarket(
        currency=args.currency,
        cache=cache,
        calls_per_minute=args.rate,
        offline=args.offline,
        # Sans ce passage explicite, la source garde son defaut de 6 h et
        # ignore --ttl : le cache conservait bien les cotations, la source les
        # jugeait perimees et les redemandait. Un scan brulait alors tout son
        # budget Steam a recoter ce qu'il possedait deja, sans jamais
        # atteindre les noms manquants -- bloque a 813 sur 1678 pendant des
        # heures, en apparence "en cours".
        ttl_seconds=args.ttl * 3600,
    )

    partage: list[CSFloat] = []

    def csfloat_source(role: str) -> CSFloat:
        """Source CSFloat, creee une seule fois.

        Une SEULE instance meme si achat et revente passent tous deux par
        CSFloat : deux clients auraient chacun leur rate-limiter et emettraient
        donc le double du debit annonce -- exactement ce qui declenche les 429
        sur une fenetre longue.
        """
        if partage:
            return partage[0]
        key = csfloat_api_key(getattr(args, "api_key", None))
        if not key:
            print("Cle API CSFloat absente (voir .env).", file=sys.stderr)
            raise SystemExit(1)
        if args.currency != "USD":
            # Erreur et non avertissement : additionner un cout en EUR et un
            # produit de vente en USD donne un nombre qui ressemble a un profit
            # sans en etre un. Aucun avertissement ne rattrape ca -- mieux vaut
            # refuser de calculer.
            print(
                f"ERREUR : CSFloat cote en USD, Steam en {args.currency}.\n"
                f"Calculer un profit en additionnant deux monnaies n'a pas de "
                f"sens. Relance avec :\n"
                f"  --currency USD --{role}-market csfloat",
                file=sys.stderr,
            )
            raise SystemExit(2)
        source = CSFloat(
            key,
            cache=cache,
            ttl_seconds=args.ttl * 3600,
            calls_per_minute=getattr(args, "csfloat_rate", 10),
            offline=getattr(args, "offline", False),
            with_volume=getattr(args, "csfloat_volume", False),
        )
        partage.append(source)
        return source

    # Marche d'ACHAT : Steam par defaut, CSFloat si demande. Acheter sur CSFloat
    # rapproche `scan` de ce que `plan` executera vraiment -- au prix du quota.
    buy_source = steam
    buy_fees = args.buy_fees
    if getattr(args, "buy_market", "steam") == "csfloat":
        buy_source = csfloat_source("buy")
        buy_fees = "csfloat"

    # Marche de REVENTE distinct : acheter sur Steam et revendre sur CSFloat est
    # une strategie courante (le porte-monnaie Steam n'est pas retirable, celui
    # de CSFloat oui). Mais les deux marches ne cotent pas dans la meme monnaie
    # ni au meme niveau : il faut les prix REELS de chacun, pas les frais de
    # l'un appliques aux prix de l'autre.
    sell_source = steam
    sell_fees = args.sell_fees
    if getattr(args, "sell_market", "steam") == "csfloat":
        sell_source = csfloat_source("sell")
        sell_fees = "csfloat"
    elif args.sell_fees != steam.name:
        print(
            f"ATTENTION : --sell-fees {args.sell_fees} applique des frais "
            f"{args.sell_fees} a des prix STEAM. Aucun marche ne fonctionne "
            f"ainsi. Utilise --sell-market {args.sell_fees} pour prendre aussi "
            f"ses prix.",
            file=sys.stderr,
        )

    pricer = MarketPricer(
        buy_source,
        sell_source,
        buy_fees=buy_fees,
        sell_fees=sell_fees,
        safety_margin=args.margin,
        min_volume=args.min_volume,
        min_input_volume=getattr(args, "min_input_volume", None),
        price_basis=getattr(args, "price_basis", "listing"),
    )
    return pricer, cache


def _alerte_age_cache(pricer) -> None:
    """Dit sur quel age de cotations le calcul a travaille.

    Hors ligne le TTL est ignore, donc rien ne distingue un cache d'une heure
    d'un cache de six semaines. Or un prix de six semaines produit un resultat
    d'apparence normale et entierement faux.
    """
    ages = pricer.quote_age()
    if ages is None:
        return
    median, maxi = ages
    heures = median / 3600
    if heures < 24:
        return
    print(
        f"ATTENTION : prix vieux de {heures / 24:.0f} jours en mediane "
        f"({maxi / 3600 / 24:.0f} au pire).\n"
        f"  Les cotations ne sont pas rafraichies hors ligne. Un Tec-9 | "
        f"Brother (Factory New)\n"
        f"  affiche a 12.97 par un cache de 42 jours en valait 4.61 au "
        f"marche, soit -64 %.\n"
        f"  Recote avant de decider : verify --collection <nom>",
        file=sys.stderr,
    )


# --- Commandes ---------------------------------------------------------------


def cmd_db(args) -> int:
    db = SkinDatabase.load(args.db)
    print(f"Base : {len(db)} collections, {db.skin_count} skins (v{db.source_version})")
    print()
    for rarity in TRADEABLE_INPUT_RARITIES:
        usable = db.tradeable_collections(rarity)
        target = rarity.next_up
        print(
            f"  {rarity.label:<18} -> {target.label:<16} "
            f"{len(usable):>3} collections exploitables"
        )
    if args.list:
        print()
        for c in sorted(db, key=lambda c: c.name):
            counts = ", ".join(
                f"{len(c.by_rarity(r))} {r.label.split()[0].lower()}"
                for r in Rarity
                if c.by_rarity(r)
            )
            print(f"  {c.name:<45} {counts}")
    return 0


def cmd_inspect(args) -> int:
    """Detaille une collection : entrees, sorties, ranges de float."""
    db = SkinDatabase.load(args.db)
    col = db.find_collection(args.collection)
    if col is None:
        print(f"Collection introuvable : {args.collection!r}", file=sys.stderr)
        return 1

    rarity = RARITY_ALIASES[args.rarity]
    inputs = col.inputs_for_rarity(rarity, args.stattrak)
    outputs = col.outcomes_for_input_rarity(rarity, args.stattrak)
    if args.stattrak and not (inputs and outputs):
        print("Aucun contrat StatTrak possible dans cette collection a cette "
              "rarete.", file=sys.stderr)
        return 1

    print(f"{col.name}  [{col.id}]")
    print(f"\nEntrees ({rarity.label}) : {len(inputs)}")
    for s in inputs:
        print(f"  {s.name:<45} float {s.min_float:.3f}-{s.max_float:.3f}")
    print(f"\nSorties ({rarity.next_up.label}) : {len(outputs)}")
    for s in outputs:
        wears = "/".join(w.short for w in s.available_wears())
        print(f"  {s.name:<45} float {s.min_float:.3f}-{s.max_float:.3f}  [{wears}]")
    if outputs:
        print(f"\nChaque sortie a une probabilite de {1 / len(outputs):.1%} "
              f"en contrat mono-collection.")
    return 0


def cmd_collections(args) -> int:
    """Classe les collections par variance STRUCTURELLE, sans aucun appel reseau.

    Le nombre de sorties possibles est connu d'avance et determine entierement
    la variance : 1 sortie = resultat certain, 5 sorties = 20 % chacune. Comme
    telecharger les prix coute ~4 min par collection, autant savoir ou depenser
    ce temps avant de le depenser.
    """
    db = SkinDatabase.load(args.db)
    rarity = RARITY_ALIASES[args.rarity]
    cols = db.tradeable_collections(rarity, args.stattrak)

    rows = []
    for c in cols:
        outs = c.outcomes_for_input_rarity(rarity, args.stattrak)
        if args.max_outcomes is not None and len(outs) > args.max_outcomes:
            continue
        fn_possible = sum(1 for s in outs if s.min_float < Wear.FACTORY_NEW.hi)
        seuil = c.factory_new_threshold(rarity, args.stattrak)
        rows.append((len(outs), c,
                     len(c.inputs_for_rarity(rarity, args.stattrak)), fn_possible,
                     seuil))

    rows.sort(key=lambda r: (r[0], r[1].name))

    print(f"{len(rows)} collections en {rarity.label} -> {rarity.next_up.label}")
    print(f"(cout d'un telechargement complet : ~{len(rows) * 4 / 60:.1f} h)\n")
    print(f"{'collection':<44} {'sorties':>8} {'proba':>7} {'entrees':>8} "
          f"{'dont FN':>8} {'seuil FN':>9}")
    print("-" * 90)
    for n_out, c, n_in, fn, seuil in rows:
        marque = " <<" if n_out == 1 else ""
        print(
            f"{c.name:<44} {n_out:>8} {1 / n_out:>6.0%} {n_in:>8} {fn:>8} "
            f"{seuil:>9.3f}{marque}"
        )
    print("\n  seuil FN : moyenne d'entree maximale gardant TOUTES les sorties "
          "en Factory New.")
    print("  Plus il est haut, moins les entrees doivent etre bonnes -- 0.875 "
          "signifie que")
    print("  presque n'importe quelle entree suffit ; sous 0.10 il faut trier "
          "les annonces.")

    certains = [c for n, c, _, _, _ in rows if n == 1]
    if certains:
        print(
            f"\n{len(certains)} collections a UNE SEULE sortie : le resultat est "
            f"certain, la variance est nulle.\nCommence par celles-la :\n"
        )
        noms = " ".join(f'"{c.name}"' for c in certains[:4])
        print(
            f"  python -m tradeup.cli scan --rarity {args.rarity} "
            f"--collections {noms} --yes"
        )
    return 0


def cmd_plan(args) -> int:
    """Panier concret a partir des offres reellement en vente sur CSFloat."""
    key = csfloat_api_key(args.api_key)
    if not key:
        print(
            "Cle API CSFloat absente. Renseigne-la dans .env :\n"
            "  CSFLOAT_API_KEY=ta_cle\n"
            "(profil CSFloat -> Developer)",
            file=sys.stderr,
        )
        return 1

    db = SkinDatabase.load(args.db)
    rarity = RARITY_ALIASES[args.rarity]
    col = db.find_collection(args.collection)
    if col is None:
        print(f"Collection introuvable : {args.collection!r}", file=sys.stderr)
        return 1

    print(f"Cle CSFloat {mask(key)} | {col.name} | {rarity.label}", file=sys.stderr)
    source = CSFloat(key, calls_per_minute=args.rate)
    plan = build_plan(
        db, col, rarity, source,
        listings_per_skin=args.listings,
        safety_margin=args.margin,
        float_margin=args.float_margin,
    )

    if plan is None:
        print("Aucun panier realisable avec les offres actuellement en vente.")
        return 0

    r = plan.result
    # La rarete sur stdout, et pas seulement dans l'en-tete stderr : c'est ce
    # qu'on relit dans un fichier de sortie ou un copier-coller.
    print(f"\n{col.name} [{plan.rarity.label} -> {plan.rarity.next_up.label}]"
          f" -- {plan.listings_examined} offres examinees")
    print("Achat et revente sur CSFloat. Tous les montants sont en USD.\n")
    print("A ACHETER (offres reelles, floats exacts) :")
    for ligne in plan.shopping_lines():
        print(ligne)
    print(f"  {'-' * 70}")
    print(f"  {'TOTAL':<46} {r.cost:>7.2f}  moyenne {r.avg_input_float:.4f}")

    print("\nSORTIE :")
    for o in r.outcomes:
        print(f"  {o.name:<46} {o.probability:>6.1%}  float {o.float_value:.4f}"
              f"  net {o.net_value:>7.2f}")

    print(f"\nCout {r.cost:.2f} | EV nette {r.ev_net:.2f} | "
          f"profit {r.ev_profit:+.2f} ({r.roi:+.1%})")
    lent = plan.slowest_outcome
    if lent is not None:
        nom, st = lent
        v = st["ventes_jour"]
        delai = ("quelques heures" if v >= 15 else
                 "un a deux jours" if v >= 5 else "plusieurs jours")
        print(
            f"\nREVENTE : les prix ci-dessus supposent que tu vends AU MARCHE.\n"
            f"  Sortie la plus lente : {nom}\n"
            f"    {v} ventes par jour -- compte {delai} pour ecouler au bon prix."
        )
        if st.get("prix_median"):
            print(f"    prix median reellement paye : {st['prix_median']:.2f}")
        print(
            "  Accepter une offre rapide au lieu d'attendre coute environ un "
            "tiers de la valeur.\n"
            "  C'est ce qui a transforme un contrat annonce a +0.37 en -0.02."
        )

    if plan.all_outcomes_profitable:
        print(
            f"\nTOUTES LES SORTIES SONT RENTABLES : quel que soit le skin obtenu,\n"
            f"  vous gagnez entre {plan.worst_profit:+.2f} (pire cas) et "
            f"{plan.best_profit:+.2f} (meilleur cas).\n"
            f"  Le tirage ne peut pas vous faire perdre."
        )
    elif plan.worst_profit is not None:
        print(
            f"\nLE TIRAGE PEUT VOUS FAIRE PERDRE : selon la sortie, "
            f"de {plan.worst_profit:+.2f} a {plan.best_profit:+.2f}."
        )

    if plan.exit_loss is not None and r.ev_profit > 0:
        print(
            f"\nPORTE DE SORTIE : au bout des 7 jours les skins sont libres.\n"
            f"  Si le contrat n'est plus rentable, revends-les au lieu de les "
            f"fusionner.\n"
            f"  Cela coute {plan.exit_loss:+.2f} ({plan.exit_loss_ratio:+.1%}), "
            f"contre {r.ev_profit:+.2f} a gagner -- soit "
            f"{abs(r.ev_profit / plan.exit_loss):.1f}x plus a gagner qu'a perdre."
        )

    tolerance = plan.price_drop_tolerance
    if tolerance is not None:
        print(
            f"\nBLOCAGE 7 JOURS : les entrees achetees aujourd'hui ne seront "
            f"utilisables\n  dans un contrat que dans 7 jours. Vous revendrez "
            f"donc une semaine plus tard."
        )
        print(
            f"  Le prix de sortie peut baisser de {tolerance:.1%} d'ici la "
            f"avant que le contrat ne devienne perdant."
        )

    print(f"\nTOLERANCE D'EXECUTION : {plan.float_slack:.5f} de float sur la somme "
          f"des 10 entrees.")
    print("  Les floats sont connus (pas de tirage), mais ces 10 annonces sont "
          "publiques.")
    print("  Si l'une part et que la remplacante depasse cette tolerance, la "
          "sortie perd un palier :")
    if plan.downgrade_profit is not None:
        print(f"    reussite : {r.ev_profit:+.2f}   |   "
              f"palier rate : {plan.downgrade_profit:+.2f}")
        if plan.downgrade_profit < 0 < r.ev_profit:
            ratio = abs(plan.downgrade_profit) / r.ev_profit
            print(f"    l'echec coute {ratio:.1f}x ce que la reussite rapporte")
    else:
        print("    (valeur du palier inferieur inconnue)")
    if r.unpriced_probability:
        print(f"ATTENTION : {r.unpriced_probability:.0%} de la proba sans prix connu")

    if args.html is not None:
        chemin = args.html or (
            Path(__file__).resolve().parents[2] / "rapports"
            # La rarete fait partie du nom : sans elle, deux plans de la meme
            # collection calcules dans la meme minute s'ecrasaient, et rien ne
            # distinguait les fichiers deja ecrits.
            / f"plan-{col.id}-{args.rarity}-{datetime.now():%Y%m%d-%H%M}.html"
        )
        ecrit = write_and_open(plan, chemin, open_browser=not args.no_open)
        print(f"\nRapport : {ecrit}")
    return 0


def cmd_price(args) -> int:
    cache = QuoteCache(ttl_seconds=args.ttl * 3600)
    steam = SteamMarket(currency=args.currency, cache=cache,
                        calls_per_minute=args.rate,
                        ttl_seconds=getattr(args, "ttl", 6) * 3600)
    q = steam.refresh(args.name) if args.fresh else steam.fetch(args.name)
    if q is None:
        print(f"Aucun prix pour {args.name!r}", file=sys.stderr)
        return 1
    print(f"{q.market_hash_name}   [Steam]")
    print(f"  plus bas  : {q.lowest_price} {q.currency}")
    print(f"  median    : {q.median_price} {q.currency}")
    print(f"  volume 24h: {q.volume}")
    print(f"  age       : {q.age_seconds / 60:.0f} min")
    return 0


def cmd_scan(args) -> int:
    db = SkinDatabase.load(args.db)
    rarity = RARITY_ALIASES[args.rarity]
    pricer, cache = _make_pricer(args)

    collections = db.tradeable_collections(rarity, args.stattrak)
    if args.collections:
        wanted = [db.find_collection(c) for c in args.collections]
        missing = [c for c, f in zip(args.collections, wanted) if f is None]
        if missing:
            print(f"Collections introuvables : {missing}", file=sys.stderr)
            return 1
        collections = [c for c in wanted if c is not None]

    names = required_market_names(db, collections, rarity, args.stattrak)
    # `warm()` precharge sur CHAQUE marche distinct : compter le seul marche
    # d'achat sous-estimait l'attente de moitie des que la revente differait.
    sources = list(dict.fromkeys([pricer.buy_source, pricer.sell_source]))

    if not args.offline:
        eta = sum(s.estimated_duration(len(names)) for s in sources) / 60
        marches = " + ".join(s.name for s in sources)
        print(
            f"{len(collections)} collections -> {len(names)} cotations a recuperer "
            f"sur {marches} (~{eta:.0f} min).",
            file=sys.stderr,
        )
        if any(s.name == "csfloat" for s in sources):
            # Le quota CSFloat porte sur une fenetre longue : un scan large le
            # vide, et le 429 qui suit ne se rattrape pas en reessayant.
            print(
                f"ATTENTION : {len(names)} cotations CSFloat sur une fenetre de "
                f"quota longue. Au-dela de quelques collections le quota saute "
                f"et rien ne le rattrape : restreins avec --collections, ou "
                f"passe par `plan` qui ne cote que ce qu'il achete.",
                file=sys.stderr,
            )
        if eta > 5 and not args.yes:
            print(
                "Relance avec --yes pour confirmer, ou --offline pour "
                "n'utiliser que le cache.",
                file=sys.stderr,
            )
            return 2

    report = prefetch(pricer, names, progress=_progress("prix"))
    print(f"  prix : {report}", file=sys.stderr)
    _alerte_age_cache(pricer)

    # Sans ce garde-fou, un cache vide produit "aucun contrat ne passe les
    # filtres" -- indiscernable d'un vrai scan sans opportunite. L'utilisateur
    # doit savoir s'il regarde un resultat ou du vide.
    couverture = report["trouves"] / report["demandes"] if report["demandes"] else 0.0
    if couverture < 0.5:
        manque = report["manquants"]
        print(
            f"\nDONNEES INSUFFISANTES : {manque} prix manquants sur "
            f"{report['demandes']} ({1 - couverture:.0%}).",
            file=sys.stderr,
        )
        if args.offline:
            noms = " ".join(f'"{c.name}"' for c in collections[:2]) or '"<nom>"'
            print(
                "Le cache ne couvre pas cette selection. Telecharge d'abord les "
                "prix, collection par collection :\n"
                f"  python -m tradeup.cli scan --rarity {args.rarity} "
                f"--collections {noms} --yes",
                file=sys.stderr,
            )
        else:
            print(
                "Steam n'a pas de cotation pour ces objets (jamais vendus, ou "
                "noms introuvables). Le classement ci-dessous, s'il existe, ne "
                "porte que sur une fraction des contrats.",
                file=sys.stderr,
            )
        if report["trouves"] == 0:
            return 1

    if args.max_collections == 2 and not args.collections:
        n = len(collections)
        print(
            f"Attention : {n} collections en mode bi-collection = "
            f"~{n + n * (n - 1) // 2 * 9} recettes. Comptez plusieurs minutes de "
            f"calcul. Restreignez avec --collections pour aller plus vite.\n"
            f"  A savoir : melanger gagne rarement. L'EV brute est monotone en "
            f"la repartition, donc maximale en mono-collection ; un melange ne "
            f"peut gagner que par le cout des entrees. Mesure sur 10 "
            f"collections cotees, le meilleur melange faisait +1.17 contre "
            f"+1.38 en mono, et pour deux fois plus de sorties possibles.",
            file=sys.stderr,
        )

    # La profitabilite prime si elle est demandee : c'est la convention dans
    # laquelle l'utilisateur a pose son critere.
    min_roi = args.min_roi
    if getattr(args, "min_profitability", None) is not None:
        min_roi = args.min_profitability - 1.0
        print(f"Seuil : profitabilite >= {args.min_profitability:.0%} "
              f"(soit ROI >= {min_roi:+.0%}).", file=sys.stderr)

    screen = ScreenConfig(
        min_ev_profit=args.min_ev,
        min_roi=min_roi,
        min_profit_probability=args.min_pwin,
        max_cost=args.max_cost,
        max_unpriced_probability=args.max_unpriced,
        min_cliff_distance=args.min_cliff,
    )
    candidates, stats = scan(
        db,
        pricer,
        rarity,
        screen=screen,
        ranking=Ranking(args.rank),
        max_collections=args.max_collections,
        collection_filter=[c.id for c in collections] if args.collections else None,
        float_percentile=args.float_pct,
        float_safety=args.float_safety,
        float_model=args.float_model,
        stattrak=args.stattrak,
        max_unit_cost=args.max_unit_cost,
        limit=args.limit,
        progress=_progress("recettes"),
    )

    print(f"\nScan : {stats}\n", file=sys.stderr)

    # Sans ca, un filtre de liquidite d'entree trop severe ressemble a une
    # absence d'opportunite : l'utilisateur doit savoir ce qui a ete ecarte.
    illiquides = pricer.illiquid_inputs()
    if illiquides:
        print(
            f"{len(illiquides)} entrees ecartees faute de volume "
            f"(< {args.min_input_volume} ventes/jour) : "
            + ", ".join(illiquides[:3])
            + ("..." if len(illiquides) > 3 else "")
            + "\nBaisse --min-input-volume pour les reprendre.",
            file=sys.stderr,
        )

    if not candidates:
        if stats["evaluees"] == 0:
            print("Aucun contrat n'a pu etre EVALUE : il manque les prix. "
                  "Voir le message ci-dessus.")
            return 1
        print(f"Aucun contrat ne passe les filtres "
              f"({stats['evaluees']} evalues, {stats['rejetees']} rejetes). "
              "C'est le resultat normal la plupart du temps.")
        return 0

    # Sans cette ligne, rien dans l'affichage ne dit en quelle monnaie sont les
    # montants -- et `plan` sort des USD quand `scan` sort des EUR par defaut.
    # Les deux marches ne cotent pas dans la meme monnaie : sans cette ligne,
    # rien dans l'affichage ne dit laquelle on lit.
    achat = f"{pricer.buy_source.name} ({args.currency})"
    revente = f"{pricer.sell_source.name} ({args.currency})"
    print(f"Achat : {achat}   |   Revente : {revente}")
    print(f"Tous les montants ci-dessous sont en {args.currency}.")

    for i, cand in enumerate(candidates, 1):
        print(f"\n[{i}] {cand.summary}")
        problems = sanity_check(cand.result)
        if problems:
            print("  INCOHERENCES DETECTEES : " + " ; ".join(problems))
        n = contracts_for_significance(cand.result)
        if n is not None:
            print(f"  avantage detectable a 95 % apres ~{n} contrats "
                  f"(soit {n * cand.result.cost:.0f} de capital engage)")
        capacite = execution_capacity(cand.result, pricer)
        for ligne in capacite.report(n).splitlines():
            print(f"  {ligne}")
        if args.simulate:
            print()
            print(simulate_series(cand.result, n_contracts=args.simulate,
                                  seed=args.seed).report())
        if args.detail:
            print()
            print(shopping_list(cand.result))
            print()
            print(explain(cand.result))

    print(
        f"\nRappel : ces chiffres sont des ESPERANCES, calcules sur un jeu de "
        f"prix fige. Recote avant d'executer :\n"
        f"  python -m tradeup.cli verify --collection \"<collection>\" "
        f"--rarity {args.rarity} --market {pricer.buy_source.name}",
        file=sys.stderr,
    )
    cache.close()
    return 0


def cmd_inventory(args) -> int:
    """Meilleurs contrats realisables avec les skins deja possedes."""
    db = SkinDatabase.load(args.db)
    cache = QuoteCache(ttl_seconds=args.ttl * 3600)

    try:
        if args.file:
            items = load_inventory_file(args.file)
            source_nom = str(args.file)
        else:
            key = csfloat_api_key(args.api_key)
            if not key:
                print(
                    "Cle API CSFloat absente. Renseigne-la dans .env, ou passe "
                    "un inventaire exporte :\n"
                    "  python -m tradeup.cli inventory --file inventaire.json",
                    file=sys.stderr,
                )
                return 1
            source = CSFloat(key, cache=cache, calls_per_minute=args.rate)
            items = from_csfloat_rows(source.inventory())
            source_nom = "CSFloat"
    except (FileNotFoundError, ValueError) as exc:
        print(exc, file=sys.stderr)
        cache.close()
        return 1

    bilan = inventory_summary(db, items)
    print(f"Inventaire ({source_nom}) : {bilan['objets']} objets, "
          f"{bilan['utilisables']} utilisables en contrat", file=sys.stderr)
    for cle, libelle in (("sans_float", "sans float"),
                         ("verrouilles", "non echangeables (verrou 7 jours)"),
                         ("souvenirs", "Souvenir (admis depuis le 21/05/2026)"),
                         ("en_vente", "actuellement en vente")):
        if bilan[cle]:
            print(f"  {bilan[cle]} {libelle}", file=sys.stderr)
    if bilan["par_rarete"]:
        print("  par rarete : " + ", ".join(
            f"{n} {r}" for r, n in bilan["par_rarete"].items()), file=sys.stderr)

    # La valorisation passe par le marche choisi : un objet possede vaut ce
    # qu'on en tirerait en le revendant, pas ce qu'on l'a paye.
    pricer, cache2 = _make_pricer(args)
    rarity = RARITY_ALIASES[args.rarity]

    plans = best_inventory_tradeups(
        db, items, pricer, rarity,
        stattrak=args.stattrak,
        limit=args.limit,
        include_losing=args.show_losing,
        ranking=Ranking(args.rank),
    )

    if not plans:
        print(
            f"Aucun contrat realisable en {rarity.label} avec cet inventaire.\n"
            "Il faut 10 objets de la MEME collection a cette rarete, "
            "echangeables et cotes.",
        )
        # Chiffrer l'ecart : sinon ce message est indiscernable d'un bug.
        proches = inventory_gaps(db, items, rarity, stattrak=args.stattrak)
        if proches:
            print("\nLe plus proche du compte :")
            for nom, n in proches:
                print(f"  {n:>2}/10  {nom}   (il en manque {10 - n})")
        else:
            print("\nAucun objet de cette rarete dans l'inventaire.")
        if not args.show_losing:
            print("\nAjoute --show-losing pour voir aussi les contrats perdants.")
        cache.close()
        cache2.close()
        return 0

    print(f"\n{len(plans)} contrat(s) realisable(s), classes par {args.rank} -- montants en "
          f"{args.currency}.")
    print(
        "Les entrees sont valorisees a ce qu'elles rapporteraient REVENDUES : "
        "\nfondre un skin, c'est renoncer a le vendre.\n"
    )
    for i, plan in enumerate(plans, 1):
        print(f"[{i}] {plan.report()}")
        if args.detail:
            print()
            print(plan.inputs_table())
            print()
            print(explain(plan.result))
        print()

    cache.close()
    cache2.close()
    return 0


def cmd_knife(args) -> int:
    """Contrats vers un GOLD : cinq Covert d'une caisse, un couteau ou des gants."""
    db = SkinDatabase.load(args.db)
    pricer, cache = _make_pricer(args)

    caisses = db.crates()
    if args.crate:
        voulues = [c for c in caisses
                   if args.crate.lower() in c.name.lower()]
        if not voulues:
            print(f"Caisse introuvable : {args.crate!r}\n"
                  f"{len(caisses)} caisses connues, ex : "
                  + ", ".join(c.name for c in caisses[:4]), file=sys.stderr)
            cache.close()
            return 1
        caisses = voulues

    if not args.offline:
        noms = set()
        for c in caisses:
            for nom in c.inputs:
                skin = db.find(nom)
                if skin:
                    noms |= {skin.market_hash_name(w)
                             for w in skin.available_wears()}
            for g in c.golds:
                noms |= {g.market_hash_name(w) for w in g.available_wears()}
        eta = pricer.buy_source.estimated_duration(len(noms)) / 60
        print(f"{len(caisses)} caisses -> {len(noms)} cotations (~{eta:.0f} min).",
              file=sys.stderr)
        if eta > 5 and not args.yes:
            print("Relance avec --yes, ou --offline pour n'utiliser que le cache.",
                  file=sys.stderr)
            cache.close()
            return 2
        prefetch(pricer, sorted(noms), progress=_progress("prix"))

    plans = scan_crates(caisses, db, pricer,
                        float_percentile=args.float_pct, limit=args.limit)
    _alerte_age_cache(pricer)

    if not plans:
        print("Aucune caisse evaluable : il manque les prix.")
        cache.close()
        return 1

    rentables = [p for p in plans if p.ev_profit > 0]
    print(f"\n{len(plans)} caisse(s) evaluee(s), {len(rentables)} rentable(s). "
          f"Montants en {args.currency}.")
    print(f"Un contrat prend {GOLD_INPUT_COUNT} Covert de la MEME caisse.\n")
    for i, plan in enumerate(plans, 1):
        if plan.ev_profit <= 0 and not args.all:
            continue
        print(f"[{i}] {plan.report()}")
        print()
    if not rentables and not args.all:
        print("Aucune n'est rentable. --all pour voir le classement complet.")
    cache.close()
    return 0


def cmd_orders(args) -> int:
    """A quel prix d'ordre d'achat chaque contrat devient-il rentable ?

    Renverse la question habituelle. On ne demande plus si le contrat passe au
    prix affiche, mais combien il faut obtenir de rabais pour qu'il passe --
    puis on place les ordres et on attend.
    """
    db = SkinDatabase.load(args.db)
    rarity = RARITY_ALIASES[args.rarity]
    pricer, cache = _make_pricer(args)

    collections = None
    if args.collections:
        voulues = [db.find_collection(c) for c in args.collections]
        manquantes = [c for c, f in zip(args.collections, voulues) if f is None]
        if manquantes:
            print(f"Collections introuvables : {manquantes}", file=sys.stderr)
            cache.close()
            return 1
        collections = [c for c in voulues if c is not None]

    if not args.offline:
        noms = required_market_names(
            db, collections or db.tradeable_collections(rarity, args.stattrak),
            rarity, args.stattrak,
        )
        eta = pricer.buy_source.estimated_duration(len(noms)) / 60
        print(f"{len(noms)} cotations a recuperer (~{eta:.0f} min).",
              file=sys.stderr)
        if eta > 5 and not args.yes:
            print("Relance avec --yes, ou --offline pour n'utiliser que le cache.",
                  file=sys.stderr)
            cache.close()
            return 2
        prefetch(pricer, noms, progress=_progress("prix"))

    plans = scan_orders(
        db, rarity, pricer,
        collections=collections,
        target_roi=args.target_roi,
        max_discount=args.max_discount,
        stattrak=args.stattrak,
        keep_unfeasible=args.all,
    )

    # Apres le calcul : hors ligne, rien n'est precharge, les cotations ne sont
    # lues qu'en cours de route.
    _alerte_age_cache(pricer)

    if not plans:
        print(
            f"Aucune collection en {rarity.label} ne devient rentable avec un "
            f"rabais d'au plus {args.max_discount:.0%}.\n"
            f"Augmente --max-discount pour voir les cas plus exigeants, ou "
            f"baisse --target-roi."
        )
        cache.close()
        return 0

    print(f"\n{len(plans)} collection(s) ou des ordres d'achat peuvent rendre "
          f"le contrat rentable.")
    print(f"Montants en {args.currency}. Classees par rabais croissant : le "
          f"plus facile a obtenir d'abord.\n")

    for i, plan in enumerate(plans, 1):
        print(f"[{i}] {plan.report()}")
        # Le delai de remplissage decide du rythme, pas le profit affiche.
        goulot = min(plan.lines, key=lambda l: l.order_price)
        skin = db.find(goulot.name[: goulot.name.rindex("(")].strip())
        volume = None
        if skin is not None:
            from .wear import wear_of
            volume = pricer.buy_volume(skin, wear_of(0.0))
        print(f"  Delai d'un ordre a ce rabais : "
              f"{fill_estimate(volume, plan.discount)}")
        print()

    print(
        "Rappel : un ordre d'achat n'est pas un achat. Tant qu'il n'est pas "
        "servi,\nle prix de la SORTIE continue de bouger -- recote avec "
        "`verify` avant de fusionner.",
        file=sys.stderr,
    )
    cache.close()
    return 0


def cmd_verify(args) -> int:
    """Recote des objets et dit ce qui a bouge depuis le dernier releve.

    C'est le dernier geste avant d'executer : un scan travaille sur un jeu de
    prix fige de plusieurs heures, et la marge d'un trade-up ne survit pas a
    une derive de quelques pour cent.
    """
    cache = QuoteCache(ttl_seconds=args.ttl * 3600)

    if args.market == "csfloat":
        key = csfloat_api_key(args.api_key)
        if not key:
            print("Cle API CSFloat absente (voir .env).", file=sys.stderr)
            cache.close()
            return 1
        source = CSFloat(key, cache=cache, calls_per_minute=args.rate)
    else:
        source = SteamMarket(currency=args.currency, cache=cache,
                             calls_per_minute=args.rate,
                             ttl_seconds=getattr(args, "ttl", 6) * 3600)

    roles: dict[str, str] = {}
    names = list(args.names)

    if args.collection:
        db = SkinDatabase.load(args.db)
        col = db.find_collection(args.collection)
        if col is None:
            print(f"Collection introuvable : {args.collection!r}", file=sys.stderr)
            cache.close()
            return 1
        roles = collection_roles(col, RARITY_ALIASES[args.rarity])
        names += [n for n in roles if n not in names]

    if not names:
        print(
            "Rien a verifier. Donne des noms d'objets, ou --collection pour "
            "reprendre tout un contrat :\n"
            '  python -m tradeup.cli verify --collection "The Bank Collection" '
            "--rarity industrial",
            file=sys.stderr,
        )
        cache.close()
        return 2

    if args.stale is not None:
        # Recoter ce qui est encore frais gaspille du quota pour confirmer un
        # chiffre qu'on vient de lire.
        ages = {n: _age_cache(cache, n, source) for n in names}
        frais = [n for n, a in ages.items() if a is not None and a < args.stale * 3600]
        inconnus = [n for n, a in ages.items() if a is None]
        names = [n for n in names if n not in frais]
        # Les objets jamais cotes sont gardes : un panier ne se verifie pas avec
        # des trous. Mais il faut le dire, sinon --stale semble n'avoir rien
        # filtre alors qu'il a fait son travail.
        print(f"{len(frais)} cotations de moins de {args.stale:g} h, ignorees.",
              file=sys.stderr)
        if inconnus:
            print(f"{len(inconnus)} objets jamais cotes : gardes malgre --stale.",
                  file=sys.stderr)
        if not names:
            print("Tout est frais : rien a recoter.")
            cache.close()
            return 0

    eta = source.estimated_duration(len(names)) / 60
    print(f"{len(names)} objets a recoter sur {source.name} (~{eta:.0f} min).",
          file=sys.stderr)
    if eta > 5 and not args.yes:
        print("Relance avec --yes pour confirmer.", file=sys.stderr)
        cache.close()
        return 2

    report = refresh_quotes(
        source, names, cache=cache, roles=roles, seuil=args.max_drift,
        progress=_progress("recotation"),
    )
    print()
    print(report.report())
    cache.close()
    # Code de sortie exploitable dans un script : 1 signale qu'il ne faut pas
    # executer le contrat sans avoir recalcule.
    return 1 if report.a_recalculer else 0


def _age_cache(cache: QuoteCache, name: str, source) -> float | None:
    """Age du dernier releve en secondes, None si l'objet n'a jamais ete cote."""
    q = cache.get(name, source.name, ttl=float("inf"),
                  currency=getattr(source, "currency", None))
    return q.age_seconds if q else None


def cmd_cache(args) -> int:
    cache = QuoteCache()
    if args.prune is not None:
        n = cache.prune(args.prune)
        print(f"{n} releves supprimes (> {args.prune} jours)")
    print(f"Cache {cache.path} : {cache.stats()}")
    cache.close()
    return 0


# --- Point d'entree ----------------------------------------------------------


def cmd_daily(args) -> int:
    """Passage quotidien : recoter ce qui decide, classer, garder la trace.

    L'ordre est deliberement celui-ci : on classe D'ABORD sur le cache (gratuit)
    pour savoir OU depenser les requetes, on recote ensuite les candidats de
    tete, puis on reclasse. Recoter avant de savoir quoi recoter reviendrait a
    depenser le budget au hasard.
    """
    from pathlib import Path

    db = SkinDatabase.load(args.db)
    rarity = RARITY_ALIASES[args.rarity]
    journal = Path(args.journal)

    # --- 1. Classement sur le cache, pour savoir ou depenser ---
    args.offline = True
    pricer, cache = _make_pricer(args)
    screen = ScreenConfig(min_ev_profit=-1e9, min_roi=-1.0,
                          min_profit_probability=0.0,
                          min_outcome_volume=args.min_volume or None,
                          max_unpriced_probability=args.max_unpriced)
    avant, _ = scan(db, pricer, rarity, screen=screen, ranking=Ranking.ROI,
                    float_model=args.float_model,
                    float_percentile=args.float_pct, limit=None)

    collections = {c.id: c for c in db.tradeable_collections(rarity)}
    noms = noms_prioritaires(avant, collections, rarity, cache,
                             top=args.top, budget=args.budget)
    print(f"{len(avant)} candidats en cache, {len(noms)} cotations a rafraichir.",
          file=sys.stderr)

    # --- 2. Re-cotation, dans la limite du budget et de ce que Steam accepte ---
    args.offline = False
    frais_pricer, _ = _make_pricer(args)
    source = frais_pricer.sell_source
    recotes, epuise = recoter(source, noms, cache=cache,
                              progress=_progress("recote"))
    if epuise:
        print(f"Marche ferme apres {recotes} cotations : le classement melange "
              f"des prix frais et des prix plus anciens. La colonne age le dit.",
              file=sys.stderr)

    # --- 3. Reclassement, sur le cache mis a jour ---
    args.offline = True
    pricer2, cache2 = _make_pricer(args)
    apres, stats = scan(db, pricer2, rarity, screen=screen, ranking=Ranking.ROI,
                        float_model=args.float_model,
                        float_percentile=args.float_pct, limit=args.limit)

    passage = Passage(rarity=args.rarity, recotes=recotes,
                      budget=args.budget, epuise=epuise)
    for cand in apres:
        cid = next((i for i, c in collections.items() if c.name in cand.label), None)
        col = collections.get(cid) if cid else None
        passage.lignes.append(Ligne(
            collection=cand.label,
            cost=cand.result.cost,
            ev_net=cand.result.ev_net,
            profitability=cand.result.profitability,
            profit_probability=cand.result.profit_probability,
            age_max=age_max(col, rarity, cache2) if col else None,
        ))

    # --- 4. Confirmation sur annonces REELLES ---
    # Sans cette etape, le classement n'est qu'une piste : `scan` suppose
    # qu'un bas float s'obtient au prix du palier, ce qui est faux depuis que
    # le float est lisible sur Steam. Mesure : Bank a 148 % selon `scan`,
    # -0.3 % selon `plan` sur 356 annonces reelles.
    if args.confirm > 0:
        cle = csfloat_api_key(getattr(args, "api_key", None))
        if not cle:
            print("Cle CSFloat absente : aucune confirmation possible, les "
                  "chiffres restent des pistes.", file=sys.stderr)
        else:
            csf = CSFloat(cle, calls_per_minute=getattr(args, "csfloat_rate", 8))
            taux = 1.0
            print(f"Confirmation des {args.confirm} meilleurs sur annonces "
                  f"reelles (CSFloat)...", file=sys.stderr)
            passage.lignes = confirmer(
                db, passage.lignes, collections, rarity, csf,
                combien=args.confirm, sell_source=source, sell_to_usd=taux,
                progress=_progress("confirme"))

    journaliser(passage, journal)

    # --- 4. Restitution ---
    print()
    print(f"Passage du {datetime.now():%Y-%m-%d %H:%M} -- {args.rarity}")
    print(f"{'collection':<34}{'piste':>8}{'REEL':>8}{'cout':>7}"
          f"{'age':>7}{'':>4}{'note':<26}")
    print("-" * 94)
    for x in passage.lignes:
        age = "jamais" if x.age_max is None else f"{x.age_max / 3600:.0f}h"
        reel = f"{x.confirmee:.0%}" if x.confirmee is not None else "-"
        if x.actionnable:
            marque, note = "  <<", "ACHETABLE"
        elif x.confirmee is not None:
            marque, note = "  xx", "confirme perdant"
        else:
            marque, note = "   ?", x.motif_echec or "non confirme"
        if not x.frais:
            note = f"prix vieux -- {note}"
        print(f"{x.collection[:32]:<34}{x.profitability:>7.0%}{reel:>8}"
              f"{x.cost:>7.2f}{age:>7}{marque:>4}  {note:<26}")
    print("-" * 94)
    print("  piste = estimation `scan`, sur un prix par palier d'usure.")
    print("  REEL  = `plan`, sur les annonces reellement en vente et leur float.")
    print("  Seul REEL engage : un bas float ne s'obtient plus au prix du palier.")
    print(f"  <<  achetable : REEL au-dessus du point mort et prix de moins de "
          f"{DECISION_MAX_AGE // 3600} h")
    print()
    print(passage.resume())
    print(f"Journal : {journal}")

    cache.close()
    cache2.close()
    # Code 10 : de quoi declencher une alerte dans un planificateur sans avoir
    # a relire la sortie.
    return 10 if passage.actionnables else 0


def cmd_schedule(args) -> int:
    """Enregistre (ou retire) le passage quotidien aupres du systeme.

    La planification appartient au projet : une tache portee par un terminal
    ouvert meurt avec lui, et c'est entre deux consultations que les prix
    bougent.
    """
    import subprocess

    liste = taches()
    script = lanceur()

    if not supporte():
        print("Planificateur non pilote sur ce systeme. Lignes crontab "
              "equivalentes :")
        print()
        print(equivalent_cron())
        return 1

    if not script.exists():
        print(f"Lanceur introuvable : {script}", file=sys.stderr)
        return 1

    if args.remove:
        for t in liste:
            r = subprocess.run(t.supprimer(), capture_output=True, text=True)
            etat = "retiree" if r.returncode == 0 else "absente"
            print(f"  {t.nom:<32} {etat}")
        return 0

    if not args.install:
        # Etat par defaut : dire ce qui tourne, sans rien modifier.
        actif = 0
        for t in liste:
            r = subprocess.run(t.etat(), capture_output=True, text=True)
            sortie = (r.stdout or "").strip()
            if r.returncode != 0:
                print(f"  {t.nom:<32} absente")
            elif sortie.startswith("BATTERIE"):
                # Etat piegeux : la tache existe, le planificateur la met en
                # file, et rien ne tourne jamais. Le dire explicitement.
                print(f"  {t.nom:<32} BLOQUEE (ne demarre pas sur batterie) "
                      f"-- relance schedule --install")
            else:
                actif += 1
                prochaine = sortie[3:].strip() or t.heure
                print(f"  {t.nom:<32} active, prochaine : {prochaine}")
        if not actif:
            print()
            print("Aucun passage planifie. Pour les installer :")
            print("  python -m tradeup.cli schedule --install")
        return 0

    echecs = 0
    for t in liste:
        r = subprocess.run(t.creer(), capture_output=True, text=True)
        if r.returncode == 0:
            print(f"  {t.nom:<32} installee, tous les jours a {t.heure}")
        else:
            echecs += 1
            print(f"  {t.nom:<32} ECHEC : {(r.stderr or r.stdout).strip()}",
                  file=sys.stderr)
    if echecs:
        print("Un echec vient le plus souvent d'un manque de droits : "
              "relance le terminal en administrateur.", file=sys.stderr)
        return 1
    print()
    print(f"Lanceur : {script}")
    print("Le passage tourne desormais sans terminal ouvert. Journal dans "
          "data/passages.jsonl, sortie brute dans data/passages.log.")
    return 0


def cmd_sweep(args) -> int:
    """Balaye TOUTE une rarete avec `plan` et enregistre chaque resultat.

    C'est le travail long -- plus d'une heure pour 46 collections -- et c'est
    precisement pour cela qu'il ne doit pas se faire pendant qu'on regarde
    l'ecran. Lance la nuit par le planificateur, il remplit le journal ; au
    matin l'interface lit ce journal et affiche les contrats rentables
    immediatement, sans rien recalculer.

    Un quota epuise n'interrompt pas le balayage : la collection est remise en
    file et retentee apres une pause. Perdre une collection parce que l'API a
    dit non laisserait un trou silencieux dans le classement.
    """
    import time as _t

    db = SkinDatabase.load(args.db)
    rarity = RARITY_ALIASES[args.rarity]
    cle = csfloat_api_key(args.api_key)
    if not cle:
        print("Cle CSFloat absente (voir .env).", file=sys.stderr)
        return 1

    journal = Journal()
    source = CSFloat(cle, calls_per_minute=args.rate)
    steam = SteamMarket(currency=args.currency, cache=QuoteCache(ttl_seconds=6 * 3600),
                        calls_per_minute=15, ttl_seconds=6 * 3600)

    # CSFloat cote en USD, Steam dans la devise demandee. Sans conversion, le
    # cout est en dollars et la revente en euros : le rapport des deux n'est
    # plus une profitabilite, c'est un taux de change deguise. Mesure sur The
    # Dead Hand Collection -- 110 % annonce contre 126 % reel, l'erreur allant
    # ici dans le sens PESSIMISTE (les montants USD sont numeriquement plus
    # gros que leur equivalent en euros), donc elle faisait ecarter des
    # contrats rentables.
    #
    # On calcule en USD de bout en bout, puis on convertit le resultat une
    # seule fois, a l'enregistrement.
    try:
        usd_vers_devise = source.usd_rate(args.currency)
    except Exception as exc:  # noqa: BLE001
        print(f"Taux de change indisponible ({type(exc).__name__}) : les "
              f"montants resteraient en USD alors que Steam cote en "
              f"{args.currency}. Rien ne serait comparable -- arret.",
              file=sys.stderr)
        journal.close()
        return 1
    print(f"1 USD = {usd_vers_devise:.4f} {args.currency}", file=sys.stderr)

    cols = list(db.tradeable_collections(rarity))
    if args.collections:
        voulus = {c.lower() for c in args.collections}
        cols = [c for c in cols if c.name.lower() in voulus]

    # REPRENDRE, pas recommencer : les collections jamais calculees d'abord,
    # puis les plus anciennes. Un balayage coupe par le quota ou par la limite
    # de duree repartirait sinon de la premiere et referait eternellement les
    # memes -- le meme piege que le TTL qui ne remontait pas jusqu'a la source.
    vues = journal.last_swept(args.rarity)
    cols.sort(key=lambda c: vues.get(c.id, 0.0))
    jamais = sum(1 for c in cols if c.id not in vues)
    if jamais:
        print(f"  {jamais} collection(s) jamais calculee(s), traitees en "
              f"premier.", file=sys.stderr)

    print(f"{len(cols)} collections a calculer en {rarity.label}.", file=sys.stderr)
    print(f"  ACHAT sur CSFloat, REVENTE estimee sur Steam. Les prix d'entree "
          f"affiches sont ceux de CSFloat", file=sys.stderr)
    print(f"  -- verifier une entree sur Steam donnera un chiffre plus eleve, "
          f"sans que l'un des deux soit faux.", file=sys.stderr)
    file = list(cols)
    faits, echecs, rentables = 0, 0, 0
    debut = _t.time()

    limite = args.max_minutes * 60
    while file:
        if _t.time() - debut > limite:
            print(f"  budget de {args.max_minutes} min atteint, arret propre "
                  f"({faits}/{len(cols)}). La suite ira au prochain passage.",
                  file=sys.stderr)
            break
        col = file[0]
        try:
            plan = build_plan(db, col, rarity, source, sell_source=steam,
                              sell_fees=FEE_MODELS["steam"],
                              sell_to_usd=1.0 / usd_vers_devise,
                              deadline=debut + limite)
        except RateLimited:
            # On NE retire PAS la collection de la file : le quota reviendra.
            print(f"  quota epuise, pause de {args.pause // 60} min "
                  f"({faits}/{len(cols)} faits)", file=sys.stderr)
            _t.sleep(args.pause)
            continue
        except BudgetEpuise:
            # La collection n'a pas echoue, elle n'a pas fini : on la LAISSE
            # dans la file pour que le prochain passage la reprenne.
            print(f"  budget de {args.max_minutes} min atteint pendant "
                  f"{col.name} -- arret propre ({faits}/{len(cols)}).",
                  file=sys.stderr)
            print("  Elle sera reprise au prochain passage, comme jamais "
                  "calculee.", file=sys.stderr)
            break
        except Exception as exc:  # noqa: BLE001
            file.pop(0)
            echecs += 1
            print(f"  [KO] {col.name} : {type(exc).__name__}", file=sys.stderr)
            continue

        file.pop(0)
        faits += 1
        if plan is None:
            echecs += 1
            print(f"  [--] {col.name} : pas assez d'annonces", file=sys.stderr)
            continue

        # Une sortie sans prix vaut ZERO dans l'EV, ce qui produit un plan
        # d'apparence normale et entierement faux. C'est arrive : un balayage
        # lance pendant un refus de Steam a enregistre The Bank Collection
        # avec ses trois sorties a 0.00, cout 0.64, profit -0.64. Le plan
        # n'etait pas perdant, il n'etait pas CALCULE -- et rien ne l'aurait
        # dit. On refuse de l'enregistrer.
        manquant = plan.result.unpriced_probability
        if manquant >= 0.99:
            # Toutes les sorties sans prix : ce n'est pas cette collection qui
            # pose probleme, c'est le marche de revente. Insister coute 75 s de
            # backoff par nom pour un echec certain.
            echecs += 1
            print(f"  [!!] {col.name} : marche de revente indisponible, arret.",
                  file=sys.stderr)
            print(file=sys.stderr)
            print("  Le balayage s'arrete : sans prix de revente, aucun "
                  "contrat n'est calculable.", file=sys.stderr)
            print("  Les collections deja faites sont au journal ; la reprise "
                  "partira des suivantes.", file=sys.stderr)
            break
        if manquant > 0.02:
            echecs += 1
            print(f"  [!!] {col.name} : {manquant:.0%} de la sortie sans prix, "
                  f"non enregistre", file=sys.stderr)
            continue

        payload = _plan_payload(plan, args.currency, usd_vers_devise)
        journal.save_plan(payload, collection_id=col.id, rarity=args.rarity)
        prof = payload["profitability"]
        if prof >= 1.0:
            rentables += 1
        marque = "<<" if prof >= 1.0 else "  "
        # Un plan valorise par repli est PRUDENT, pas exact : la revente
        # reelle sur Steam rapporterait 17 a 37 % de plus. Le taire ferait
        # passer une sous-estimation pour une mesure.
        repli = f"  [repli CSFloat x{plan.replis}]" if plan.valorisation_de_repli else ""
        alt = (f"  [sur Steam : {plan.alt_profitability:.0%}]"
               if plan.alt_profitability is not None else "")
        if plan.fragile:
            alt += "  [FRAGILE : ne tient qu'en arrivant premier]"
        elif plan.deep_profitability is not None:
            alt += f"  [a -3 annonces : {plan.deep_profitability:.0%}]"
        print(f"  [{prof:>5.0%}] {col.name:<40} {marque} "
              f"({faits}/{len(cols)}){alt}{repli}", file=sys.stderr)

    duree = (_t.time() - debut) / 60
    print()
    print(f"Balayage termine en {duree:.0f} min : {faits} calculees, "
          f"{rentables} rentables, {echecs} sans resultat.")
    print("L'interface les affiche sans recalculer : python -m tradeup.web")
    journal.close()
    return 10 if rentables else 0


def _plan_payload(plan, devise: str, taux: float = 1.0) -> dict:
    """Serialise un plan pour le journal, montants convertis dans `devise`.

    Le calcul se fait en USD de bout en bout -- CSFloat y cote, et la revente
    Steam y est ramenee par `sell_to_usd`. La conversion vers la devise du
    compte n'a lieu QU'ICI, une seule fois : convertir en cours de route
    multiplierait les occasions de melanger les deux.
    """
    r = plan.result

    def c(v):
        return round(v * taux, 4)

    return {
        "collection": plan.collection.name,
        "rarity": plan.rarity.label,
        "rarity_target": plan.rarity.next_up.label,
        "currency": devise,
        # Les entrees sont achetees sur CSFloat, la revente estimee sur Steam.
        # Sans le dire, un utilisateur verifie les prix d'entree sur Steam et
        # conclut a une erreur : mesure sur le M4A4 | Zubastick (WW), 0,07 EUR
        # sur CSFloat contre 0,11 sur Steam. Les deux chiffres sont justes, ce
        # sont deux marches.
        "buy_market": "CSFloat",
        "sell_market": "Steam",
        # Le meme panier achete sur Steam : dit si le contrat ne tient QUE
        # grace a l'ecart entre les deux marches.
        "cost_alt": (c(plan.alt_cost) if plan.alt_cost is not None else None),
        "profitability_alt": (round(plan.alt_profitability, 4)
                              if plan.alt_profitability is not None else None),
        # Le meme panier si trois annonces par objet sont prises avant nous :
        # separe une occasion d'une course.
        "cost_deep": (c(plan.deep_cost) if plan.deep_cost is not None else None),
        "profitability_deep": (round(plan.deep_profitability, 4)
                               if plan.deep_profitability is not None else None),
        "fragile": plan.fragile,
        # La voie ORDRE STEAM : repetable, float subi. Le rabais est ce qu'il
        # faut obtenir sur le prix affiche pour tenir +20 % de rendement.
        "float_subi_ok": plan.float_subi_compatible,
        "order_budget": (c(plan.steam_order_budget())
                         if plan.steam_order_budget() is not None else None),
        "order_discount": (round(plan.steam_order_discount(), 4)
                           if plan.steam_order_discount() is not None else None),
        "cost": c(r.cost), "net": c(r.ev_net),
        "profit": c(r.ev_profit), "roi": round(r.roi, 4),
        "profitability": round(r.profitability, 4),
        "win_probability": round(r.profit_probability, 4),
        "outcomes_count": r.distinct_outcomes,
        "stdev": c(r.stdev),
        "avg_float": round(r.avg_input_float, 5),
        "listings_examined": plan.listings_examined,
        "float_slack": round(plan.float_slack, 5),
        "worst_profit": (c(plan.worst_profit)
                         if plan.worst_profit is not None else None),
        "best_profit": (c(plan.best_profit)
                        if plan.best_profit is not None else None),
        "all_profitable": plan.all_outcomes_profitable,
        "replis": plan.replis,
        "downgrade_profit": (c(plan.downgrade_profit)
                             if plan.downgrade_profit is not None else None),
        "exit_loss": None, "exit_loss_ratio": None,
        "price_drop_tolerance": None,
        "inputs": [{"name": o.name, "float": round(o.float_value, 4),
                    "price": c(o.unit_cost), "url": o.url}
                   for o in sorted(plan.options,
                                   key=lambda o: (o.skin.name, o.float_value))],
        "outcomes": [{"name": o.name, "probability": round(o.probability, 4),
                      "float": round(o.float_value, 4),
                      "net": c(o.net_value)} for o in r.outcomes],
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tradeup", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", help="chemin de collections.json")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_pricing_args(sp):
        sp.add_argument("--currency", default=os.getenv("TRADEUP_CURRENCY", "EUR"),
                        choices=sorted(CURRENCIES))
        sp.add_argument("--rate", type=int, default=15,
                        help="requetes Steam par minute (defaut 15)")
        sp.add_argument("--ttl", type=float, default=6.0,
                        help="duree de validite du cache, en heures")

    s = sub.add_parser("db", help="etat de la base statique")
    s.add_argument("--list", action="store_true", help="detailler les collections")
    s.set_defaults(func=cmd_db)

    s = sub.add_parser("inspect", help="detail d'une collection")
    s.add_argument("collection")
    s.add_argument("--rarity", default="mil-spec", choices=sorted(RARITY_ALIASES))
    s.add_argument("--stattrak", action="store_true")
    s.set_defaults(func=cmd_inspect)

    s = sub.add_parser("collections",
                       help="classer les collections par variance (hors ligne)")
    s.add_argument("--rarity", default="mil-spec", choices=sorted(RARITY_ALIASES))
    s.add_argument("--max-outcomes", type=int, default=None,
                   help="ne garder que les collections a au plus N sorties")
    s.add_argument("--stattrak", action="store_true",
                   help="ne lister que les collections utilisables en StatTrak")
    s.set_defaults(func=cmd_collections)

    s = sub.add_parser("price", help="cotation Steam d'un objet")
    s.add_argument("name")
    s.add_argument("--fresh", action="store_true", help="ignorer le cache")
    add_pricing_args(s)
    s.set_defaults(func=cmd_price)

    s = sub.add_parser("scan", help="chercher les meilleurs contrats")
    s.add_argument("--rarity", default="mil-spec", choices=sorted(RARITY_ALIASES))
    s.add_argument("--collections", nargs="*", help="restreindre a ces collections")
    s.add_argument("--max-collections", type=int, default=1, choices=(1, 2),
                   help="1 = mono-collection (defaut, faible variance)")
    s.add_argument("--limit", type=int, default=15)
    s.add_argument("--rank", default=Ranking.RISK_ADJUSTED.value,
                   choices=[r.value for r in Ranking])
    s.add_argument("--detail", action="store_true", help="detailler la distribution")
    s.add_argument("--simulate", type=int, metavar="N", default=None,
                   help="simuler une serie de N contrats (Monte-Carlo)")
    s.add_argument("--seed", type=int, default=None,
                   help="graine de simulation, pour des resultats reproductibles")
    s.add_argument("--offline", action="store_true", help="n'utiliser que le cache")
    s.add_argument("--yes", action="store_true", help="ne pas demander confirmation")
    # Filtres
    s.add_argument("--min-ev", type=float, default=0.0)
    s.add_argument("--min-roi", type=float, default=0.03)
    s.add_argument("--min-profitability", type=float, default=None,
                   metavar="X",
                   help="seuil dans la convention des guides : 1.0 = point "
                        "mort, 1.2 = +20 %%. Equivaut a --min-roi (X - 1) et "
                        "le remplace s'il est donne")
    s.add_argument("--min-pwin", type=float, default=0.0)
    s.add_argument("--max-cost", type=float, default=None)
    s.add_argument("--max-unit-cost", type=float, default=None)
    s.add_argument("--max-unpriced", type=float, default=0.02)
    s.add_argument("--min-volume", type=int, default=5,
                   help="volume 24 h minimal pour compter une sortie "
                        "(0 pour desactiver ; un objet sans vente n'a pas de prix reel)")
    s.add_argument("--min-input-volume", type=int, default=3,
                   help="volume 24 h minimal pour retenir une ENTREE : il en "
                        "faut dix, le prix d'une annonce unique ne dit rien du "
                        "cout des neuf suivantes (0 pour desactiver)")
    # Modelisation
    s.add_argument("--margin", type=float, default=0.05,
                   help="decote de securite sur la revente (defaut 5 %%)")
    s.add_argument("--float-pct", type=float, default=0.15,
                   help="position du float sourcable dans son palier")
    s.add_argument("--stattrak", action="store_true",
                   help="contrat StatTrak : entrees ET sorties StatTrak "
                        "uniquement (aucune collection sous le Mil-Spec)")
    s.add_argument("--float-model", default="fixed", choices=("fixed", "random"),
                   help="random : le float d'entree est un TIRAGE, son risque "
                        "est chiffre dans l'EV au lieu d'etre evite par une "
                        "marge (impose --float-pct 0.5 et --float-safety 0)")
    s.add_argument("--float-safety", type=float, default=0.02,
                   help="marge de moyenne interdite sous une frontiere d'usure "
                        "(0 seulement si les floats sont verifies via CSFloat)")
    s.add_argument("--min-cliff", type=float, default=0.0,
                   help="marge minimale exigee avant la falaise d'usure")
    s.add_argument("--buy-fees", default="steam", choices=sorted(FEE_MODELS))
    s.add_argument("--sell-fees", default="steam", choices=sorted(FEE_MODELS))
    s.add_argument("--price-basis", default="listing",
                   choices=("listing", "sales"),
                   help="listing : plus basse annonce (ce qu'on paie en "
                        "cliquant). sales : mediane des ventes recentes, "
                        "c'est-a-dire ce que le marche negocie -- l'achat "
                        "y suppose alors un ORDRE d'achat qui attend")
    s.add_argument("--buy-market", default="steam", choices=("steam", "csfloat"),
                   help="marche d'ACHAT des entrees : csfloat prend ses vrais "
                        "prix (utiliser avec --currency USD ; gros consommateur "
                        "de quota)")
    s.add_argument("--sell-market", default="steam", choices=("steam", "csfloat"),
                   help="marche de REVENTE : csfloat prend ses vrais prix "
                        "(utiliser avec --currency USD)")
    s.add_argument("--csfloat-volume", action="store_true",
                   help="recuperer aussi le volume de ventes CSFloat, sans quoi "
                        "--min-volume ne filtre rien et la capacite d'execution "
                        "reste inconnue (DOUBLE la consommation de quota)")
    s.add_argument("--csfloat-rate", type=int, default=10,
                   help="requetes CSFloat par minute (quota sur fenetre longue : "
                        "ne pas monter sans raison)")
    s.add_argument("--api-key", default=None, help="cle CSFloat, sinon lue depuis .env")
    add_pricing_args(s)
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("plan",
                       help="panier concret depuis les offres reelles CSFloat")
    s.add_argument("collection")
    s.add_argument("--rarity", default="mil-spec", choices=sorted(RARITY_ALIASES))
    s.add_argument("--listings", type=int, default=30,
                   help="offres a examiner par objet (defaut 30)")
    s.add_argument("--margin", type=float, default=0.05,
                   help="decote de securite sur la revente")
    s.add_argument("--float-margin", type=float, default=0.005,
                   help="tolerance de float gardee sous la frontiere d'usure, "
                        "contre le risque qu'une annonce soit vendue avant toi")
    s.add_argument("--rate", type=int, default=10,
                   help="requetes CSFloat par minute (quota sur fenetre longue : ne pas monter sans raison)")
    s.add_argument("--api-key", default=None, help="sinon lue depuis .env")
    s.add_argument("--html", nargs="?", const="", metavar="FICHIER",
                   help="generer une page HTML cliquable et l'ouvrir")
    s.add_argument("--no-open", action="store_true",
                   help="ecrire la page sans ouvrir le navigateur")
    s.set_defaults(func=cmd_plan)

    s = sub.add_parser(
        "inventory",
        help="meilleurs contrats realisables avec les skins deja possedes")
    s.add_argument("--file", default=None, metavar="FICHIER",
                   help="inventaire exporte (JSON ou CSV) ; sinon lu sur CSFloat")
    s.add_argument("--rarity", default="mil-spec", choices=sorted(RARITY_ALIASES))
    s.add_argument("--stattrak", action="store_true")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--detail", action="store_true")
    s.add_argument("--rank", default=Ranking.RISK_ADJUSTED.value,
                   choices=[r.value for r in Ranking],
                   help="critere de classement : risk_adjusted (defaut) combine "
                        "gain et regularite, safety classe par probabilite de "
                        "gagner, ev par gain brut, roi par rendement")
    s.add_argument("--show-losing", action="store_true",
                   help="montrer aussi les contrats qui detruisent de la valeur")
    s.add_argument("--offline", action="store_true", help="n'utiliser que le cache")
    s.add_argument("--margin", type=float, default=0.05)
    s.add_argument("--min-volume", type=int, default=0,
                   help="volume minimal pour compter une sortie (0 par defaut : "
                        "on ne choisit pas ce qu'on possede deja)")
    s.add_argument("--buy-fees", default="steam", choices=sorted(FEE_MODELS))
    s.add_argument("--sell-fees", default="steam", choices=sorted(FEE_MODELS))
    s.add_argument("--price-basis", default="listing",
                   choices=("listing", "sales"),
                   help="listing : plus basse annonce (ce qu'on paie en "
                        "cliquant). sales : mediane des ventes recentes, "
                        "c'est-a-dire ce que le marche negocie -- l'achat "
                        "y suppose alors un ORDRE d'achat qui attend")
    s.add_argument("--sell-market", default="steam", choices=("steam", "csfloat"),
                   help="marche ou l'on valorise entrees et sorties")
    s.add_argument("--api-key", default=None, help="sinon lue depuis .env")
    add_pricing_args(s)
    s.set_defaults(func=cmd_inventory)

    s = sub.add_parser(
        "knife", help="contrats vers un couteau ou des gants (5 Covert)")
    s.add_argument("--crate", default=None, help="restreindre a une caisse")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--all", action="store_true",
                   help="montrer aussi les caisses perdantes")
    s.add_argument("--float-pct", type=float, default=0.5,
                   help="position du float d'entree dans son palier "
                        "(0.5 = milieu, l'esperance d'un achat au palier)")
    s.add_argument("--offline", action="store_true")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--min-volume", type=int, default=0)
    s.add_argument("--margin", type=float, default=0.05)
    s.add_argument("--buy-fees", default="steam", choices=sorted(FEE_MODELS))
    s.add_argument("--sell-fees", default="steam", choices=sorted(FEE_MODELS))
    s.add_argument("--price-basis", default="listing",
                   choices=("listing", "sales"),
                   help="listing : plus basse annonce (ce qu'on paie en "
                        "cliquant). sales : mediane des ventes recentes, "
                        "c'est-a-dire ce que le marche negocie -- l'achat "
                        "y suppose alors un ORDRE d'achat qui attend")
    s.add_argument("--api-key", default=None)
    add_pricing_args(s)
    s.set_defaults(func=cmd_knife)

    s = sub.add_parser(
        "orders",
        help="prix d'ordre d'achat qui rend chaque contrat rentable")
    s.add_argument("--rarity", default="mil-spec", choices=sorted(RARITY_ALIASES))
    s.add_argument("--collections", nargs="*", help="restreindre a ces collections")
    s.add_argument("--target-roi", type=float, default=DEFAULT_TARGET_ROI,
                   help="rendement vise (defaut 20 %%) : a l'equilibre exact, "
                        "la moindre variation rend le contrat perdant")
    s.add_argument("--max-discount", type=float, default=DEFAULT_MAX_DISCOUNT,
                   help="rabais maximal juge realiste (defaut 35 %%) ; au-dela "
                        "l'ordre n'est jamais servi")
    s.add_argument("--all", action="store_true",
                   help="montrer aussi les contrats hors de portee")
    s.add_argument("--stattrak", action="store_true")
    s.add_argument("--offline", action="store_true", help="n'utiliser que le cache")
    s.add_argument("--yes", action="store_true", help="ne pas demander confirmation")
    s.add_argument("--min-volume", type=int, default=0)
    s.add_argument("--min-input-volume", type=int, default=3)
    s.add_argument("--margin", type=float, default=0.05)
    s.add_argument("--buy-fees", default="steam", choices=sorted(FEE_MODELS))
    s.add_argument("--sell-fees", default="steam", choices=sorted(FEE_MODELS))
    s.add_argument("--price-basis", default="listing",
                   choices=("listing", "sales"),
                   help="listing : plus basse annonce (ce qu'on paie en "
                        "cliquant). sales : mediane des ventes recentes, "
                        "c'est-a-dire ce que le marche negocie -- l'achat "
                        "y suppose alors un ORDRE d'achat qui attend")
    s.add_argument("--api-key", default=None)
    add_pricing_args(s)
    s.set_defaults(func=cmd_orders)

    s = sub.add_parser("verify",
                       help="recoter et mesurer la derive depuis le dernier releve")
    s.add_argument("names", nargs="*", metavar="NOM",
                   help="market_hash_name a verifier")
    s.add_argument("--collection", default=None,
                   help="reprendre entrees ET sorties de toute une collection")
    s.add_argument("--rarity", default="mil-spec", choices=sorted(RARITY_ALIASES),
                   help="rarete d'ENTREE, avec --collection")
    s.add_argument("--market", default="steam", choices=("steam", "csfloat"))
    s.add_argument("--max-drift", type=float, default=DEFAULT_DRIFT_THRESHOLD,
                   help="derive defavorable qui declenche l'alerte "
                        "(defaut 5 %%, code de sortie 1)")
    s.add_argument("--stale", type=float, nargs="?", const=6.0, default=None,
                   metavar="HEURES",
                   help="ne recoter que ce qui date de plus de N heures")
    s.add_argument("--yes", action="store_true", help="ne pas demander confirmation")
    s.add_argument("--api-key", default=None, help="cle CSFloat, sinon lue depuis .env")
    add_pricing_args(s)
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("daily",
                       help="passage quotidien : recote les candidats de tete, "
                            "classe, et journalise la derive")
    s.set_defaults(func=cmd_daily)
    s.add_argument("--rarity", default="industrial", choices=sorted(RARITY_ALIASES))
    s.add_argument("--top", type=int, default=DEFAULT_TOP,
                   help="nombre de candidats dont les prix doivent etre frais "
                        "a tout prix (defaut 5)")
    s.add_argument("--budget", type=int, default=DEFAULT_BUDGET,
                   help="cotations maximales par passage (defaut 200, cale sur "
                        "ce qu'une fenetre Steam ouverte laisse passer)")
    s.add_argument("--confirm", type=int, default=DEFAULT_CONFIRM,
                   help="nombre de candidats de tete recalcules sur des "
                        "ANNONCES REELLES via CSFloat (defaut 3). 0 pour "
                        "s'en passer -- mais alors rien n'est achetable")
    s.add_argument("--csfloat-rate", type=int, default=8)
    s.add_argument("--api-key", default=None)
    s.add_argument("--journal", default="data/passages.jsonl",
                   help="fichier ou s'empile un enregistrement par passage")
    s.add_argument("--limit", type=int, default=15)
    s.add_argument("--min-volume", type=int, default=0)
    s.add_argument("--max-unpriced", type=float, default=0.02)
    s.add_argument("--min-input-volume", type=int, default=3)
    s.add_argument("--margin", type=float, default=0.05)
    s.add_argument("--float-model", default="fixed", choices=("fixed", "random"),
                   help="defaut 'fixed' : la strategie visee TRIE les annonces "
                        "Steam, ou le float n'est pas price. Passer a 'random' "
                        "si les entrees viennent d'ordres d'achat, ou le float "
                        "est subi -- l'ecart atteint 62 points sur Bank")
    s.add_argument("--float-pct", type=float, default=0.15,
                   help="position visee dans le palier d'usure, 0 = le plus bas "
                        "(defaut 0.15). Plus il est bas, plus il faut inspecter "
                        "d'annonces pour reunir les dix entrees")
    s.add_argument("--price-basis", default="sales", choices=("listing", "sales"),
                   help="defaut 'sales' ici : un passage quotidien sert a "
                        "decider, donc a raisonner sur ce que le marche negocie")
    s.add_argument("--buy-fees", default="steam", choices=sorted(FEE_MODELS))
    s.add_argument("--sell-fees", default="steam", choices=sorted(FEE_MODELS))
    s.add_argument("--currency", default="EUR", choices=sorted(CURRENCIES))
    s.add_argument("--rate", type=int, default=4,
                   help="requetes Steam par minute (defaut 4 : au-dela, Steam "
                        "ferme pour des heures)")
    s.add_argument("--ttl", type=float, default=24,
                   help="duree de validite du cache, en heures (defaut 24)")
    s.add_argument("--db", default=None)

    s = sub.add_parser("sweep",
                       help="balayer toute une rarete avec `plan` et journaliser "
                            "(long : a lancer la nuit)")
    s.set_defaults(func=cmd_sweep)
    s.add_argument("--rarity", default="industrial", choices=sorted(RARITY_ALIASES))
    s.add_argument("--collections", nargs="*", default=None)
    s.add_argument("--rate", type=int, default=8,
                   help="requetes CSFloat par minute (defaut 8)")
    s.add_argument("--max-minutes", type=int, default=150,
                   help="budget de temps par passage (defaut 150). Le "
                        "planificateur coupe a 3 h : mieux vaut s'arreter "
                        "proprement avant, le reste ira au prochain passage")
    s.add_argument("--pause", type=int, default=600,
                   help="pause en secondes quand le quota est epuise (defaut 600)")
    s.add_argument("--currency", default="EUR", choices=sorted(CURRENCIES))
    s.add_argument("--api-key", default=None)
    s.add_argument("--db", default=None)

    s = sub.add_parser("schedule",
                       help="planifier le passage quotidien sur cette machine")
    s.set_defaults(func=cmd_schedule)
    s.add_argument("--install", action="store_true",
                   help="enregistrer les deux passages quotidiens")
    s.add_argument("--remove", action="store_true",
                   help="retirer les taches planifiees")

    s = sub.add_parser("cache", help="etat du cache de prix")
    s.add_argument("--prune", type=float, nargs="?", const=30.0, default=None,
                   metavar="JOURS")
    s.set_defaults(func=cmd_cache)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return args.func(args)
    except RateLimited as exc:
        # Erreur la plus frequente a l'usage : un traceback Python n'aide
        # personne, et la seule bonne reaction est d'attendre.
        #
        # Encore faut-il attendre le bon guichet. Un scan interroge Steam ET
        # CSFloat ; annoncer "quota CSFloat" sur un 429 de Steam envoie
        # chercher une cle API la ou il fallait juste baisser le debit.
        if "steamcommunity" in str(exc):
            print(
                "\nSteam a ferme la porte (429).\n"
                "  Steam tolere environ 15 requetes par minute, et compte aussi\n"
                "  sur une fenetre plus longue. Attends 10 a 15 min.\n"
                "  Les prix deja obtenus sont en cache : la reprise repart de la.\n"
                "  Pour une longue serie, descends a --rate 8.",
                file=sys.stderr,
            )
        else:
            print(
                "\nQuota CSFloat epuise.\n"
                "  CSFloat limite sur une fenetre longue, pas seulement par minute.\n"
                "  Relancer tout de suite ne fera qu'aggraver : attends 5 a 10 min.\n"
                "  Si ca se reproduit, baisse le debit : --rate 5",
                file=sys.stderr,
            )
        return 3
    except urllib.error.URLError as exc:
        # Une coupure reseau n'est pas une erreur de programme, et surtout pas
        # une raison d'abandonner : le travail est REPRENABLE depuis le cache.
        # Un long balayage mourait avec une trace Python parce que la machine
        # avait change de reseau -- deux heures de fenetre Steam perdues pour
        # une minute sans DNS.
        print(file=sys.stderr)
        print(f"Connexion indisponible ({exc.reason}).", file=sys.stderr)
        print("  Les cotations deja obtenues sont en cache : la reprise "
              "repart de la,", file=sys.stderr)
        print("  rien n'est reperdu. Relance quand le reseau est revenu.",
              file=sys.stderr)
        return 4
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrompu.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
