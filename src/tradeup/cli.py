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
from datetime import datetime
from pathlib import Path

from .config import csfloat_api_key, mask
from .db import SkinDatabase
from .fees import FEE_MODELS
from .models import TRADEABLE_INPUT_RARITIES, Rarity, Wear
from .pricing.cache import QuoteCache
from .pricing.repository import MarketPricer
from .pricing.steam import CURRENCIES, SteamMarket
from .plan import build_plan
from .pricing.csfloat import CSFloat
from .pricing.http import RateLimited
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
            calls_per_minute=getattr(args, "csfloat_rate", 10),
            offline=getattr(args, "offline", False),
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
    )
    return pricer, cache


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
        rows.append((len(outs), c,
                     len(c.inputs_for_rarity(rarity, args.stattrak)), fn_possible))

    rows.sort(key=lambda r: (r[0], r[1].name))

    print(f"{len(rows)} collections en {rarity.label} -> {rarity.next_up.label}")
    print(f"(cout d'un telechargement complet : ~{len(rows) * 4 / 60:.1f} h)\n")
    print(f"{'collection':<44} {'sorties':>8} {'proba':>7} {'entrees':>8} {'dont FN':>8}")
    print("-" * 80)
    for n_out, c, n_in, fn in rows:
        marque = " <<" if n_out == 1 else ""
        print(
            f"{c.name:<44} {n_out:>8} {1 / n_out:>6.0%} {n_in:>8} {fn:>8}{marque}"
        )

    certains = [c for n, c, _, _ in rows if n == 1]
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
    print(f"\n{col.name} -- {plan.listings_examined} offres examinees")
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
            / f"plan-{col.id}-{datetime.now():%Y%m%d-%H%M}.html"
        )
        ecrit = write_and_open(plan, chemin, open_browser=not args.no_open)
        print(f"\nRapport : {ecrit}")
    return 0


def cmd_price(args) -> int:
    cache = QuoteCache(ttl_seconds=args.ttl * 3600)
    steam = SteamMarket(currency=args.currency, cache=cache, calls_per_minute=args.rate)
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
            f"calcul. Restreignez avec --collections pour aller plus vite.",
            file=sys.stderr,
        )

    screen = ScreenConfig(
        min_ev_profit=args.min_ev,
        min_roi=args.min_roi,
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
                             calls_per_minute=args.rate)

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
    s.add_argument("--buy-market", default="steam", choices=("steam", "csfloat"),
                   help="marche d'ACHAT des entrees : csfloat prend ses vrais "
                        "prix (utiliser avec --currency USD ; gros consommateur "
                        "de quota)")
    s.add_argument("--sell-market", default="steam", choices=("steam", "csfloat"),
                   help="marche de REVENTE : csfloat prend ses vrais prix "
                        "(utiliser avec --currency USD)")
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
    except RateLimited:
        # Erreur la plus frequente a l'usage : un traceback Python n'aide
        # personne, et la seule bonne reaction est d'attendre.
        print(
            "\nQuota CSFloat epuise.\n"
            "  CSFloat limite sur une fenetre longue, pas seulement par minute.\n"
            "  Relancer tout de suite ne fera qu'aggraver : attends 5 a 10 min.\n"
            "  Si ca se reproduit, baisse le debit : --rate 5",
            file=sys.stderr,
        )
        return 3
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrompu.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
