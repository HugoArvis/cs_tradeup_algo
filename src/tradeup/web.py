"""Application web locale : choisir une collection et lancer un plan sans commande.

    python -m tradeup.web

Trois contraintes ont dicte la conception :

1. Un plan prend des MINUTES (30 requetes CSFloat a 10/min). Une requete HTTP
   bloquante donnerait une page figee puis un timeout. Les plans tournent donc
   en taches de fond, l'interface interroge leur avancement.

2. Le quota CSFloat est vite epuise -- c'est arrive plusieurs fois pendant le
   developpement. L'interface rend le cout de chaque plan visible AVANT de le
   lancer, et affiche les 429 comme une attente a respecter, pas une panne.

3. Le serveur detient la cle API. Il n'ecoute donc que sur 127.0.0.1 et ne
   renvoie jamais la cle, meme masquee.

Bibliotheque standard uniquement, comme le reste du projet.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .config import csfloat_api_key
from .db import SkinDatabase
from .inventory import (
    best_tradeups as best_inventory_tradeups,
    buildable_collections,
    closest_gaps as inventory_gaps,
    from_csfloat_rows,
    summary as inventory_summary,
)
from .journal import Journal
from .models import Rarity
from .fees import STEAM
from .plan import CSFloatPricer, Plan, build_plan
from .pricing.cache import QuoteCache
from .pricing.repository import MarketPricer
from .pricing.csfloat import CSFloat
from .pricing.http import RateLimited
from .pricing.steam import SteamMarket
from .report import render as render_report

log = logging.getLogger(__name__)

RARITES = {
    "consumer": Rarity.CONSUMER,
    "industrial": Rarity.INDUSTRIAL,
    "mil-spec": Rarity.MIL_SPEC,
    "restricted": Rarity.RESTRICTED,
    "classified": Rarity.CLASSIFIED,
}


def libelle_rarete(nom: str | None) -> str:
    """Libelle lisible d'une rarete stockee sous sa forme courte.

    Le journal enregistre "mil-spec", les plans fraichement calcules exposent
    "Mil-Spec Grade" : sans cette conversion, l'historique et les resultats du
    jour afficheraient deux ecritures differentes de la meme chose. Une valeur
    inconnue est rendue telle quelle plutot que masquee -- une vieille ligne
    reste ainsi identifiable.
    """
    rarete = RARITES.get(str(nom or ""))
    return rarete.label if rarete else str(nom or "")


@dataclass
class Job:
    """Un calcul de plan en cours ou termine."""

    id: str
    collection_id: str
    rarity: str
    state: str = "running"  # running | done | empty | error | quota
    started: float = field(default_factory=time.time)
    plan: Plan | None = None
    plan_id: str | None = None
    message: str = ""

    @property
    def elapsed(self) -> float:
        return time.time() - self.started


@dataclass
class Batch:
    """Balayage de toutes les collections d'une rarete, une par une.

    Pourquoi une file plutot qu'un gros calcul : couvrir les 88 collections
    Mil-Spec represente plusieurs milliers de requetes CSFloat, soit des heures
    a 10/min -- et le quota tombera bien avant la fin. Une file traite les
    collections dans l'ordre, enregistre chaque plan au journal au fil de l'eau,
    et sait s'interrompre puis reprendre quand le quota revient.

    Consequence : un balayage n'est pas une operation qu'on lance et qu'on
    attend, c'est un travail de fond qui progresse sur des heures.
    """

    id: str
    rarity: str
    pending: list[str]
    done: list[dict] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)
    total: int = 0
    state: str = "running"  # running | paused | finished | stopped
    message: str = ""
    started: float = field(default_factory=time.time)
    resume_at: float = 0.0

    @property
    def progress(self) -> float:
        traites = len(self.done) + len(self.failed)
        return traites / self.total if self.total else 0.0


@dataclass
class InventoryJob:
    """Calcul des contrats realisables avec l'inventaire, en tache de fond.

    Meme raison qu'un plan : valoriser les objets possedes demande une requete
    CSFloat par nom de marche, donc des minutes. Une requete HTTP bloquante
    donnerait une page figee.
    """

    id: str
    rarity: str
    state: str = "running"  # running | done | empty | error | quota
    message: str = ""
    results: list[dict] = field(default_factory=list)
    gaps: list[dict] = field(default_factory=list)
    started: float = field(default_factory=time.time)

    @property
    def elapsed(self) -> float:
        return time.time() - self.started


# Convention des guides et des calculateurs : 1.0 est le point mort, pas le
# profit. En dessous, le contrat detruit de la valeur.
SEUIL_PROFITABLE = 1.0


class App:
    """Etat partage du serveur : base, cle, taches."""

    def __init__(self, db: SkinDatabase, api_key: str, *, rate: int = 10,
                 journal: Journal | None = None):
        self.db = db
        self._api_key = api_key  # jamais expose
        self.rate = rate
        self.jobs: dict[str, Job] = {}
        self.batches: dict[str, Batch] = {}
        self.inventory_jobs: dict[str, InventoryJob] = {}
        # L'inventaire coute une requete a lire et ne bouge pas entre deux
        # clics : le relire a chaque affichage gaspillerait du quota.
        self._inventaire: list | None = None
        self._inventaire_lu: float = 0.0
        self.journal = journal or Journal()
        self._lock = threading.Lock()
        # On achete sur CSFloat (annonces avec float exact, prix bruts plus
        # bas) et on revend sur Steam (net superieur de 17 a 37 % malgre des
        # frais six fois plus eleves, et dix fois plus de volume).
        #
        # Steam est interroge dans la devise du COMPTE, pas en USD, pour deux
        # raisons : ses frais ont un plancher de 0,01 qui doit s'appliquer
        # dans la devise ou l'on encaisse, et le cache de prix est indexe par
        # devise -- coter en USD rendrait inutilisables toutes les cotations
        # deja obtenues. Le net est converti en USD APRES les frais, pour
        # rejoindre les couts CSFloat : additionner un cout en USD et un
        # produit de vente en EUR donne un nombre qui ressemble a un profit
        # sans en etre un.
        self._revente: SteamMarket | None = None
        # L'API CSFloat cote en USD, mais le site affiche -- et facture -- dans
        # la devise du profil. Sans conversion, les montants ne correspondent a
        # rien de ce que l'utilisateur voit ni de ce qu'il paie.
        self.currency = "USD"
        self.rate_usd = 1.0
        self._devise_chargee = False
        # "USD" est a la fois une reponse et une valeur par defaut. Sans ce
        # drapeau, une lecture ratee est indiscernable d'un compte en dollars,
        # et la page affirme une devise qu'elle n'a jamais verifiee.
        self._devise_lue = False
        # Le code evolue, le serveur non : un processus lance avant une
        # correction continue de servir l'ancienne logique. Afficher son heure
        # de demarrage rend ce piege visible au lieu de le laisser deviner.
        self.started_at = time.time()

    def load_currency(self) -> None:
        """Interroge une fois le profil et les taux. Echec = on reste en USD."""
        if self._devise_chargee:
            return
        self._devise_chargee = True
        try:
            source = CSFloat(self._api_key, calls_per_minute=self.rate)
            devise = source.account_currency()
            if devise:
                self._devise_lue = True
                if devise != "USD":
                    self.rate_usd = source.usd_rate(devise)
                    self.currency = devise
        except Exception:  # noqa: BLE001 - la conversion est un confort
            log.warning("Devise du compte indisponible, affichage en USD")

    def ordres(self, rarity_name: str, *, target_roi: float = 0.20) -> dict:
        """Les ORDRES D'ACHAT a placer, plutot que des annonces a cliquer.

        Un panier d'annonces ne se repete pas : chaque annonce est unique, et
        le lendemain il faut tout rechercher. Un ordre d'achat, lui, se pose
        une fois et se remplit tout seul -- c'est la seule forme exploitable
        quand on veut refaire le meme contrat.

        Lu sur le CACHE : aucune requete, donc reponse immediate. Les prix
        viennent du balayage de nuit, et leur age est renvoye pour que
        l'interface puisse le dire.
        """
        from .orders import scan_orders

        # Sans ca, `self.currency` vaut son defaut "USD" tant que le compte
        # n'a pas repondu -- et le cache, indexe par devise, ne rend alors
        # AUCUNE cotation. Zero ordre, sans la moindre erreur.
        self.load_currency()
        rarity = RARITES[rarity_name]
        cache = QuoteCache(ttl_seconds=720 * 3600)
        steam = SteamMarket(currency=self.currency, cache=cache, offline=True,
                            ttl_seconds=720 * 3600)
        pricer = MarketPricer(steam, steam, buy_fees="steam", sell_fees="steam",
                              safety_margin=0.05, min_input_volume=3,
                              price_basis="sales")
        try:
            plans = scan_orders(self.db, rarity, pricer, target_roi=target_roi)
        finally:
            ages = pricer.quote_age()
            cache.close()

        return {
            # Le marche est celui ou l'ordre se PLACE. Steam est le seul des
            # deux a accepter un ordre sur une usure sans choisir le float --
            # et c'est precisement ce que la strategie repetable demande.
            "market": "Steam Community Market",
            "currency": self.currency,
            "target_roi": target_roi,
            "age_hours": round(ages[0] / 3600, 1) if ages else None,
            "plans": [
                {
                    "collection": p.collection.name,
                    "rarity": p.rarity.label,
                    "rarity_target": p.rarity.next_up.label,
                    "ev_net": round(p.ev_net, 2),
                    "market_cost": round(p.market_cost, 2),
                    "budget": round(p.budget, 2),
                    "discount": round(p.discount, 4),
                    "feasible": p.feasible(),
                    "win_probability": round(p.result.profit_probability, 4),
                    "outcomes": p.result.distinct_outcomes,
                    "lines": [
                        {
                            "name": l.name, "quantity": l.quantity,
                            "market_price": round(l.market_price, 2),
                            "order_price": round(l.order_price, 2),
                            "discount": round(l.discount, 4),
                            "below_floor": l.below_floor,
                        }
                        for l in p.lines
                    ],
                }
                for p in plans
            ],
        }

    def revente(self) -> SteamMarket:
        """Marche de revente des sorties, partage par tous les calculs.

        Une seule instance : deux auraient chacune leur limiteur de debit et
        emettraient donc le double du rythme annonce, ce qui est exactement ce
        qui fait fermer la porte cote Steam.
        """
        self.load_currency()
        with self._lock:
            if self._revente is None:
                self._revente = SteamMarket(
                    currency=self.currency,
                    cache=QuoteCache(ttl_seconds=6 * 3600),
                    calls_per_minute=15,
                )
            return self._revente

    @property
    def currency_known(self) -> bool:
        """La devise affichee vient-elle du compte, ou du defaut ?"""
        return self._devise_lue

    def conv(self, montant_usd: float) -> float:
        return round(montant_usd * self.rate_usd, 2)

    def collections(self, rarity_name: str) -> list[dict]:
        rarity = RARITES[rarity_name]
        rows = []
        for c in self.db.tradeable_collections(rarity):
            sorties = c.outcomes_for_input_rarity(rarity)
            entrees = c.by_rarity(rarity)
            rows.append(
                {
                    "id": c.id,
                    "name": c.name,
                    "outcomes": len(sorties),
                    "inputs": len(entrees),
                    # Moyenne d'entree maximale gardant la sortie en Factory
                    # New. Haut = presque n'importe quelle entree suffit ; bas
                    # = il faut trier les annonces une par une.
                    "fn_threshold": round(c.factory_new_threshold(rarity), 4),
                    # Cout en requetes CSFloat : une par couple (skin, usure).
                    "requests": sum(len(s.available_wears()) for s in entrees),
                }
            )
        rows.sort(key=lambda r: (r["outcomes"], r["name"]))
        return rows

    # --- Inventaire ---

    def inventaire(self, *, force: bool = False) -> list:
        """Objets possedes, relus au plus une fois par minute.

        Une lecture coute une requete et l'inventaire ne change pas entre deux
        clics de l'interface.
        """
        if not force and self._inventaire is not None:
            if time.time() - self._inventaire_lu < 60:
                return self._inventaire
        source = CSFloat(self._api_key, calls_per_minute=self.rate)
        self._inventaire = from_csfloat_rows(source.inventory())
        self._inventaire_lu = time.time()
        return self._inventaire

    def inventory_overview(self, rarity_name: str, *, force: bool = False) -> dict:
        """Ce que contient l'inventaire et ce qu'un calcul couterait.

        Le cout est annonce AVANT de lancer, comme pour un plan : le quota
        CSFloat se vide vite, et l'utilisateur doit pouvoir renoncer.
        """
        self.load_currency()
        items = self.inventaire(force=force)
        rarity = RARITES[rarity_name]
        constructibles = buildable_collections(self.db, items, rarity)
        return {
            "rarity": rarity.label,
            "currency": self.currency,
            "summary": inventory_summary(self.db, items),
            "buildable": constructibles,
            "requests": sum(c["requests"] for c in constructibles),
            "gaps": [
                {"collection": nom, "owned": n}
                for nom, n in inventory_gaps(self.db, items, rarity)
            ],
            "read_at": self._inventaire_lu,
        }

    def start_inventory(self, rarity_name: str) -> InventoryJob:
        job = InventoryJob(id=uuid.uuid4().hex[:12], rarity=rarity_name)
        with self._lock:
            self.inventory_jobs[job.id] = job
        threading.Thread(
            target=self._run_inventory, args=(job,), daemon=True
        ).start()
        return job

    def _run_inventory(self, job: InventoryJob) -> None:
        try:
            self.load_currency()
            items = self.inventaire()
            rarity = RARITES[job.rarity]
            source = CSFloat(self._api_key, calls_per_minute=self.rate)
            pricer = CSFloatPricer(source)

            plans = best_inventory_tradeups(
                self.db, items, pricer, rarity,
                # On montre aussi les perdants : c'est une information, pas un
                # echec. Savoir que dix skins valent plus vendus que fondus
                # vaut mieux que de ne rien afficher.
                include_losing=True,
            )
            job.results = [self._inventory_dict(p) for p in plans]
            job.gaps = [
                {"collection": nom, "owned": n}
                for nom, n in inventory_gaps(self.db, items, rarity)
            ]
            if not plans:
                job.state = "empty"
                job.message = (
                    f"Aucun contrat realisable en {rarity.label} : il faut dix "
                    f"objets de la meme collection a cette rarete."
                )
            else:
                job.state = "done"
                rentables = sum(1 for p in plans if p.gain > 0)
                job.message = (
                    f"{len(plans)} contrat(s) possible(s), {rentables} rentable(s)."
                )
        except RateLimited:
            job.state = "quota"
            job.message = (
                "Quota CSFloat epuise. Attendez quelques minutes : reessayer "
                "tout de suite ne fait qu'aggraver."
            )
        except Exception as exc:  # noqa: BLE001 - remonte tel quel a l'interface
            job.state = "error"
            job.message = str(exc)
            log.exception("calcul d'inventaire echoue")

    def _inventory_dict(self, plan) -> dict:
        """Serialise un contrat d'inventaire, montants dans la devise du compte."""
        r = plan.result
        return {
            "collection": plan.collection.name,
            "rarity": plan.rarity.label,
            "currency": self.currency,
            # Ce que les dix entrees rapporteraient VENDUES : fondre, c'est y
            # renoncer. Ce n'est pas ce qu'on les a payees.
            "opportunity_cost": self.conv(plan.opportunity_cost),
            "net": self.conv(r.ev_net),
            "gain": self.conv(plan.gain),
            "roi": round(r.roi, 4),
            "profitability": round(r.profitability, 4),
            # Le gain seul est trompeur : un +2 une fois sur trois vaut moins
            # qu'un +0.50 a tous les coups. Le classement a besoin des deux.
            "win_probability": round(r.profit_probability, 4),
            "always_profitable": plan.always_profitable,
            "outcomes_count": r.distinct_outcomes,
            "stdev": self.conv(r.stdev),
            "avg_float": round(r.avg_input_float, 5),
            "decorated": [i.market_hash_name for i in plan.decorated_inputs],
            "listed": [i.market_hash_name for i in plan.listed_inputs],
            "inputs": [
                {
                    "name": o.name,
                    "float": round(o.float_value, 5),
                    "value": self.conv(o.unit_cost),
                }
                for o in plan.options
            ],
            "outcomes": [
                {
                    "name": o.name,
                    "probability": round(o.probability, 4),
                    "float": round(o.float_value, 5),
                    "net": self.conv(o.net_value),
                }
                for o in r.outcomes
            ],
        }

    def inventory_job_payload(self, job: InventoryJob) -> dict:
        return {
            "id": job.id,
            "state": job.state,
            "message": job.message,
            "elapsed": round(job.elapsed),
            "rarity": libelle_rarete(job.rarity),
            "currency": self.currency,
            "results": job.results,
            "gaps": job.gaps,
        }

    def start_plan(self, collection_id: str, rarity_name: str) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], collection_id=collection_id,
                  rarity=rarity_name)
        with self._lock:
            self.jobs[job.id] = job
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def _run(self, job: Job) -> None:
        try:
            self.load_currency()
            col = self.db.collection(job.collection_id)
            source = CSFloat(self._api_key, calls_per_minute=self.rate)
            plan = build_plan(self.db, col, RARITES[job.rarity], source,
                              sell_source=self.revente(), sell_fees=STEAM,
                              sell_to_usd=1.0 / self.rate_usd)
            if plan is None:
                job.state = "empty"
                job.message = "Pas assez d'annonces en vente pour composer un panier."
            else:
                job.plan = plan
                job.state = "done"
                # Tout plan calcule entre au journal : il a coute des requetes,
                # et on veut pouvoir le retrouver meme sans l'avoir suivi.
                job.plan_id = self.journal.save_plan(
                    self.job_payload(job)["plan"],
                    collection_id=job.collection_id,
                    rarity=job.rarity,
                )
        except RateLimited:
            job.state = "quota"
            job.message = (
                "Quota CSFloat epuise. Il porte sur une fenetre longue : "
                "relancer tout de suite ne sert a rien, attendez 5 a 10 minutes."
            )
        except Exception as exc:  # noqa: BLE001 - l'UI doit survivre a tout
            log.exception("Plan %s en echec", job.id)
            job.state = "error"
            job.message = f"{type(exc).__name__} : {exc}"

    # --- Balayage complet ----------------------------------------------------

    def start_batch(self, rarity_name: str, *, max_outcomes: int | None = None) -> Batch:
        cols = self.collections(rarity_name)
        if max_outcomes is not None:
            cols = [c for c in cols if c["outcomes"] <= max_outcomes]
        batch = Batch(
            id=uuid.uuid4().hex[:12],
            rarity=rarity_name,
            pending=[c["id"] for c in cols],
            total=len(cols),
        )
        with self._lock:
            self.batches[batch.id] = batch
        threading.Thread(target=self._run_batch, args=(batch,), daemon=True).start()
        return batch

    def _run_batch(self, batch: Batch) -> None:
        self.load_currency()
        source = CSFloat(self._api_key, calls_per_minute=self.rate)

        while batch.pending and batch.state in ("running", "paused"):
            if batch.state == "paused":
                if time.time() < batch.resume_at:
                    time.sleep(5)
                    continue
                batch.state = "running"
                batch.message = ""

            cid = batch.pending[0]
            col = self.db.collection(cid)
            try:
                plan = build_plan(self.db, col, RARITES[batch.rarity], source,
                                  sell_source=self.revente(), sell_fees=STEAM,
                                  sell_to_usd=1.0 / self.rate_usd)
            except RateLimited:
                # On NE retire PAS la collection de la file : le quota reviendra
                # et elle sera retentee. Perdre une collection parce que l'API a
                # dit non serait un trou silencieux dans le balayage.
                batch.state = "paused"
                batch.resume_at = time.time() + 600
                batch.message = (
                    "Quota CSFloat epuise. Reprise automatique dans 10 minutes."
                )
                continue
            except Exception as exc:  # noqa: BLE001
                log.exception("Balayage : %s en echec", col.name)
                batch.pending.pop(0)
                batch.failed.append({"collection": col.name,
                                     "rarity": RARITES[batch.rarity].label,
                                     "error": str(exc)})
                continue

            batch.pending.pop(0)
            if plan is None:
                batch.failed.append(
                    {"collection": col.name,
                     "rarity": RARITES[batch.rarity].label,
                     "error": "pas assez d'annonces"}
                )
                continue

            # Meme garde que `sweep` : une sortie sans prix vaut zero dans
            # l'EV et donne un plan d'apparence normale, entierement faux.
            # `pending` a deja ete depile juste au-dessus : ne pas le refaire,
            # sinon la collection SUIVANTE disparait du balayage en silence.
            if plan.result.unpriced_probability > 0.02:
                batch.failed.append(
                    {"collection": col.name,
                     "rarity": RARITES[batch.rarity].label,
                     "error": "sorties non cotees"}
                )
                continue

            payload = self._plan_dict(plan)
            plan_id = self.journal.save_plan(
                payload, collection_id=cid, rarity=batch.rarity
            )
            batch.done.append({
                "plan_id": plan_id,
                "collection": col.name,
                "rarity": RARITES[batch.rarity].label,
                "cost": payload["cost"],
                "net": payload["net"],
                "profit": payload["profit"],
                "roi": payload["roi"],
                "profitability": payload.get("profitability", 0.0),
                "outcomes": len(payload["outcomes"]),
                "worst_profit": payload.get("worst_profit"),
                "all_profitable": payload.get("all_profitable", False),
                "win_probability": payload.get("win_probability", 0.0),
                "stdev": payload.get("stdev", 0.0),
            })

        if batch.state != "stopped":
            batch.state = "finished"
            batch.message = (
                f"{len(batch.done)} plans calcules, {len(batch.failed)} echecs."
            )

    def batch_payload(self, batch: Batch) -> dict:
        """Ce que la page affiche : les contrats rentables, et eux seuls.

        Le tri se fait ici, pas dans le navigateur. Un contrat sous le point
        mort n'est pas un candidat moins bon, c'est une perte : le presenter
        dans une liste de recommandations, meme dernier, invite a le lire
        comme une option. Rien n'est perdu pour autant -- tous les plans sont
        enregistres au journal, l'onglet Historique les retrouve.
        """
        retenus = [d for d in batch.done
                   if d.get("profitability", 0.0) >= SEUIL_PROFITABLE]
        retenus.sort(key=lambda d: -d["profitability"])
        return {
            "id": batch.id,
            "rarity": batch.rarity,
            "state": batch.state,
            "message": batch.message,
            "total": batch.total,
            "remaining": len(batch.pending),
            "progress": round(batch.progress, 3),
            "elapsed": round(time.time() - batch.started),
            "resume_in": max(0, round(batch.resume_at - time.time())),
            "currency": self.currency,
            "computed": len(batch.done),
            "rejected": len(batch.done) - len(retenus),
            "results": retenus,
            "failed": batch.failed[-10:],
        }

    def job_payload(self, job: Job) -> dict:
        base = {
            "id": job.id,
            "state": job.state,
            "elapsed": round(job.elapsed),
            "message": job.message,
            "plan_id": job.plan_id,
        }
        if job.state != "done" or job.plan is None:
            return base

        base["plan"] = self._plan_dict(job.plan)
        return base

    def _plan_dict(self, plan: Plan) -> dict:
        """Serialise un plan, montants convertis dans la devise du compte."""
        p, r = plan, plan.result
        return {
            "collection": p.collection.name,
            # Une meme collection donne un plan different par rarete : sans
            # elle, deux lignes de l'historique sont indiscernables.
            "rarity": p.rarity.label,
            "rarity_target": p.rarity.next_up.label,
            "currency": self.currency,
            "cost": self.conv(r.cost),
            "net": self.conv(r.ev_net),
            "profit": self.conv(r.ev_profit),
            "roi": round(r.roi, 4),
            # Convention des guides : 1.0 = point mort, pas le profit.
            "profitability": round(r.profitability, 4),
            "win_probability": round(r.profit_probability, 4),
            "outcomes_count": r.distinct_outcomes,
            "stdev": self.conv(r.stdev),
            "avg_float": round(r.avg_input_float, 5),
            "listings_examined": p.listings_examined,
            "float_slack": round(p.float_slack, 5),
            "downgrade_profit": (
                self.conv(p.downgrade_profit) if p.downgrade_profit is not None else None
            ),
            "worst_profit": (
                self.conv(p.worst_profit) if p.worst_profit is not None else None
            ),
            "best_profit": (
                self.conv(p.best_profit) if p.best_profit is not None else None
            ),
            "all_profitable": p.all_outcomes_profitable,
            "replis": p.replis,
            "exit_loss": (
                self.conv(p.exit_loss) if p.exit_loss is not None else None
            ),
            "exit_loss_ratio": (
                round(p.exit_loss_ratio, 4) if p.exit_loss_ratio is not None else None
            ),
            "price_drop_tolerance": (
                round(p.price_drop_tolerance, 4)
                if p.price_drop_tolerance is not None
                else None
            ),
            "inputs": [
                {
                    "name": o.name,
                    "float": round(o.float_value, 4),
                    "price": self.conv(o.unit_cost),
                    "url": o.url,
                }
                for o in sorted(p.options, key=lambda o: (o.skin.name, o.float_value))
            ],
            "outcomes": [
                {
                    "name": o.name,
                    "probability": round(o.probability, 4),
                    "float": round(o.float_value, 4),
                    "net": self.conv(o.net_value),
                }
                for o in r.outcomes
            ],
        }

    def contract_payload(self, c) -> dict:
        """Etat d'un contrat : le REEL confronte a ce qui etait prevu.

        La comparaison est le coeur du suivi. Un plan dit ce qu'il fallait
        acheter ; des qu'une annonce part on prend un substitut, et le float
        moyen derive -- ce qui peut changer l'usure de sortie et donc le gain.
        """
        return {
            "id": c.id,
            "plan_id": c.plan_id,
            "currency": self.currency,
            "collection": c.collection_name,
            "rarity": libelle_rarete(c.rarity),
            "created_at": c.created_at,
            "status": c.status,
            "notes": c.notes,
            "planned_cost": round(c.planned_cost, 2),
            "planned_profit": round(c.planned_profit, 2),
            "planned_avg_float": round(c.planned_avg_float, 5),
            "spent": round(c.spent, 2),
            "purchased": len(c.purchased),
            "complete": c.complete,
            "actual_avg_float": (
                round(c.actual_avg_float, 5) if c.actual_avg_float is not None else None
            ),
            "float_drift": (
                round(c.float_drift, 5) if c.float_drift is not None else None
            ),
            "craftable_at": c.craftable_at,
            "craftable": c.craftable,
            "items": [
                {
                    "id": i.id,
                    "name": i.market_hash_name,
                    "float": round(i.float_value, 4),
                    "price": round(i.price, 2),
                    "url": (
                        f"https://csfloat.com/item/{i.listing_id}"
                        if i.listing_id else None
                    ),
                    "purchased": i.purchased,
                    "purchased_at": i.purchased_at,
                    "tradable_at": i.tradable_at,
                    "locked": i.locked,
                    "from_plan": i.from_plan,
                }
                for i in c.items
            ],
        }


# --- HTTP --------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    app: App  # injecte par serve()

    def log_message(self, fmt, *args):  # silence les logs par requete
        log.debug(fmt, *args)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # L'application evolue a chaque redemarrage : une page servie depuis le
        # cache donne l'impression que les corrections n'ont pas pris effet.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path)
        params = parse_qs(route.query)

        if route.path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif route.path == "/api/collections":
            rarity = (params.get("rarity") or ["mil-spec"])[0]
            if rarity not in RARITES:
                self._json({"error": "rarete inconnue"}, 400)
                return
            self._json({"collections": self.app.collections(rarity)})
        elif route.path.startswith("/api/job/"):
            job = self.app.jobs.get(route.path.rsplit("/", 1)[-1])
            if job is None:
                self._json({"error": "tache inconnue"}, 404)
                return
            self._json(self.app.job_payload(job))
        elif route.path == "/api/status":
            # Deux requetes CSFloat au plus, une fois par processus, et elles
            # ne cotent aucun objet. C'est le prix d'une en-tete qui dit vrai :
            # annoncer USD a un compte en euros fausse toute lecture des
            # montants, et le decalage ne se voit nulle part ailleurs.
            self.app.load_currency()
            self._json({
                "started_at": self.app.started_at,
                "currency": self.app.currency,
                "currency_known": self.app.currency_known,
            })
        elif route.path.startswith("/api/batch/"):
            batch = self.app.batches.get(route.path.rsplit("/", 1)[-1])
            if batch is None:
                self._json({"error": "balayage inconnu"}, 404)
                return
            self._json(self.app.batch_payload(batch))
        elif route.path == "/api/inventory":
            rarity = (params.get("rarity") or ["mil-spec"])[0]
            if rarity not in RARITES:
                self._json({"error": "rarete inconnue"}, 400)
                return
            force = (params.get("force") or ["0"])[0] == "1"
            try:
                self._json(self.app.inventory_overview(rarity, force=force))
            except RateLimited:
                self._json({"error": "Quota CSFloat epuise. Attendez quelques "
                                     "minutes avant de relire l'inventaire."}, 429)
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, 502)
        elif route.path.startswith("/api/inventory-job/"):
            job = self.app.inventory_jobs.get(route.path.rsplit("/", 1)[-1])
            if job is None:
                self._json({"error": "calcul inconnu"}, 404)
                return
            self._json(self.app.inventory_job_payload(job))
        elif route.path == "/api/orders":
            rarity = (params.get("rarity") or ["industrial"])[0]
            if rarity not in RARITES:
                self._json({"error": "rarete inconnue"}, 400)
                return
            try:
                self._json(self.app.ordres(rarity))
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, 500)
        elif route.path == "/api/latest":
            # Ce que la nuit a trouve. Aucune requete, aucune attente : un
            # balayage complet dure plus d'une heure, il n'a pas a etre refait
            # parce qu'on ouvre la page.
            rarity = (params.get("rarity") or ["industrial"])[0]
            if rarity not in RARITES:
                self._json({"error": "rarete inconnue"}, 400)
                return
            self._json({
                "plans": self.app.journal.latest_profitable(rarity),
                "currency": self.app.currency,
            })
        elif route.path == "/api/history":
            plans = self.app.journal.plans(limit=40)
            for ligne in plans:
                ligne["rarity"] = libelle_rarete(ligne.get("rarity"))
            self._json({"plans": plans})
        elif route.path.startswith("/api/plan/"):
            payload = self.app.journal.plan_payload(route.path.rsplit("/", 1)[-1])
            if payload is None:
                self._json({"error": "plan inconnu"}, 404)
                return
            # Les plans enregistres avant la conversion de devise sont en USD.
            # Mieux vaut le dire que laisser croire qu'ils sont dans la devise
            # du compte.
            payload.setdefault("currency", "USD")
            self._json({"plan": payload})
        elif route.path == "/api/contracts":
            actifs = (params.get("all") or ["0"])[0] != "1"
            self._json({
                "contracts": [
                    self.app.contract_payload(c)
                    for c in self.app.journal.contracts(include_done=not actifs)
                ]
            })
        elif route.path.startswith("/report/"):
            job = self.app.jobs.get(route.path.rsplit("/", 1)[-1])
            if job is None or job.plan is None:
                self._send(404, b"rapport indisponible", "text/plain; charset=utf-8")
                return
            html = render_report(job.plan)
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        else:
            self._send(404, b"introuvable", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        chemin = urlparse(self.path).path
        taille = int(self.headers.get("Content-Length") or 0)
        try:
            corps = json.loads(self.rfile.read(taille) or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "corps illisible"}, 400)
            return

        j = self.app.journal
        if chemin == "/api/inventory":
            rarity = corps.get("rarity", "mil-spec")
            if rarity not in RARITES:
                self._json({"error": "rarete inconnue"}, 400)
                return
            job = self.app.start_inventory(rarity)
            self._json({"id": job.id})
            return

        if chemin == "/api/batch":
            rarity = corps.get("rarity", "mil-spec")
            if rarity not in RARITES:
                self._json({"error": "rarete inconnue"}, 400)
                return
            maxi = corps.get("max_outcomes")
            batch = self.app.start_batch(
                rarity, max_outcomes=int(maxi) if maxi else None
            )
            self._json({"batch": batch.id, "total": batch.total})
            return
        if chemin == "/api/batch/stop":
            batch = self.app.batches.get(corps.get("batch", ""))
            if batch is None:
                self._json({"error": "balayage inconnu"}, 404)
                return
            batch.state = "stopped"
            batch.message = "Interrompu."
            self._json({"ok": True})
            return
        if chemin == "/api/follow":
            try:
                self._json({"contract": j.follow(corps.get("plan", ""))})
            except KeyError:
                self._json({"error": "plan inconnu"}, 404)
            return
        if chemin == "/api/item":
            item = corps.get("item")
            if not item:
                self._json({"error": "objet manquant"}, 400)
                return
            if corps.get("action") == "annuler":
                j.unmark_purchased(item)
            elif corps.get("action") == "remplacer":
                j.replace_item(
                    item, name=corps.get("name", ""),
                    float_value=float(corps.get("float", 0)),
                    price=float(corps.get("price", 0)),
                    listing_id=corps.get("listing_id") or None,
                )
            else:
                j.mark_purchased(
                    item,
                    price=float(corps["price"]) if corps.get("price") else None,
                    float_value=float(corps["float"]) if corps.get("float") else None,
                )
            self._json({"ok": True})
            return
        if chemin == "/api/contract":
            cid = corps.get("contract", "")
            if corps.get("delete"):
                j.delete_contract(cid)
            else:
                if corps.get("status"):
                    j.set_status(cid, corps["status"])
                if corps.get("notes") is not None:
                    j.set_notes(cid, corps["notes"])
            self._json({"ok": True})
            return
        if chemin != "/api/plan":
            self._send(404, b"introuvable", "text/plain; charset=utf-8")
            return

        cid, rarity = corps.get("collection"), corps.get("rarity", "mil-spec")
        if not cid or cid not in self.app.db.collections:
            self._json({"error": "collection inconnue"}, 400)
            return
        if rarity not in RARITES:
            self._json({"error": "rarete inconnue"}, 400)
            return
        self._json({"job": self.app.start_plan(cid, rarity).id})


def serve(*, host: str = "127.0.0.1", port: int = 8765, rate: int = 10,
          open_browser: bool = True, db_path: str | None = None) -> None:
    """Demarre l'application locale."""
    key = csfloat_api_key()
    if not key:
        raise SystemExit(
            "Cle API CSFloat absente. Renseigne CSFLOAT_API_KEY dans .env."
        )

    handler = type("BoundHandler", (Handler,),
                   {"app": App(SkinDatabase.load(db_path), key, rate=rate)})
    # 127.0.0.1 et pas 0.0.0.0 : le serveur detient une cle API, il n'a rien a
    # faire sur le reseau local.
    serveur = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}/"
    print(f"Application sur {url}   (Ctrl+C pour arreter)")
    if open_browser:
        webbrowser.open(url)
    try:
        serveur.serve_forever()
    except KeyboardInterrupt:
        print("\nArret.")
    finally:
        serveur.server_close()


PAGE = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trade-up CS2</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1b1f24;--muted:#5b6673;--line:#e2e6eb;
--pos:#0f7a3d;--neg:#b3261e;--warn:#8a5a00;--warn-bg:#fff6e0;--accent:#1a56b0;}
@media(prefers-color-scheme:dark){:root{--bg:#14171b;--card:#1c2126;--ink:#e8ecf1;
--muted:#9aa5b1;--line:#2b3239;--pos:#4ec27e;--neg:#ff6b5e;--warn:#f0b400;
--warn-bg:#2e2609;--accent:#6ba5ff;}}
*{box-sizing:border-box}
body{margin:0;padding:20px 16px 60px;background:var(--bg);color:var(--ink);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1000px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--muted);font-size:13px;margin-bottom:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:16px;margin-bottom:14px}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
select,input{padding:7px 10px;border:1px solid var(--line);border-radius:7px;
background:var(--card);color:var(--ink);font:inherit}
button{padding:7px 14px;border:0;border-radius:7px;background:var(--accent);
color:#fff;font:inherit;font-weight:600;cursor:pointer}
button:disabled{opacity:.5;cursor:not-allowed}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);
white-space:nowrap}
th{font-size:12px;color:var(--muted)}
td.num,th.num{text-align:right}
tr.pick{cursor:pointer}
tr.pick:hover{background:var(--warn-bg)}
tr.sel{outline:2px solid var(--accent);outline-offset:-2px}
.tabs{display:flex;gap:6px;margin-bottom:14px;border-bottom:1px solid var(--line)}
.tab{padding:8px 16px;cursor:pointer;border-bottom:2px solid transparent;
color:var(--muted);font-weight:600}
.tab.on{color:var(--ink);border-bottom-color:var(--accent)}
.pane{display:none}.pane.on{display:block}
button.ghost{background:transparent;color:var(--accent);border:1px solid var(--line)}
button.sm{padding:3px 10px;font-size:13px}
tr.done td{opacity:.55}
.bar{height:5px;background:var(--line);border-radius:3px;overflow:hidden;margin-top:6px}
.bar>i{display:block;height:100%;background:var(--accent)}
.tag{display:inline-block;padding:1px 7px;border-radius:20px;font-size:12px;
background:var(--warn-bg);color:var(--warn);font-weight:600}
.kpis{display:flex;gap:20px;flex-wrap:wrap}
.kpi{flex:1 1 120px}
.kpi .lab{color:var(--muted);font-size:12px;text-transform:uppercase}
.kpi .val{font-size:22px;font-weight:650}
.pos{color:var(--pos)}.neg{color:var(--neg)}
.warn{background:var(--warn-bg);border-left:3px solid var(--warn);padding:10px 12px;
border-radius:0 7px 7px 0;margin-bottom:10px;font-size:14px}
a.buy{display:inline-block;padding:3px 11px;border-radius:6px;background:var(--accent);
color:#fff;text-decoration:none;font-size:13px;font-weight:600}
.muted{color:var(--muted);font-size:13px}
#banniere{position:fixed;top:0;left:0;right:0;z-index:99;padding:12px 16px;
background:var(--accent);color:#fff;font-weight:600;display:none;
box-shadow:0 2px 10px rgba(0,0,0,.25)}
#banniere.on{display:block}
#banniere.err{background:var(--neg)}
#banniere .spin{border-color:rgba(255,255,255,.4);border-top-color:#fff}
/* --- Profitabilite : la seule metrique affichee --- */
/* 100 % est le point mort, pas le profit. La page ne montre que ce qui est
   au-dessus, donc la couleur n'a plus a departager : elle confirme. */
.prof{font-weight:700;color:var(--pos)}
.gros{font-size:30px;line-height:1.1}
.jauge{height:4px;background:var(--line);border-radius:2px;overflow:hidden;
margin-top:6px}
.jauge i{display:block;height:100%;background:var(--pos)}
.tete{display:flex;justify-content:space-between;align-items:flex-start;
gap:16px;flex-wrap:wrap;margin-bottom:10px}
.tete h2{font-size:17px;margin:0 0 2px}
.chiffres{display:flex;gap:18px;flex-wrap:wrap;margin:12px 0;
color:var(--muted);font-size:14px}
.chiffres b{color:var(--ink);font-variant-numeric:tabular-nums}
.warn.ok{border-left-color:var(--pos)}
.etape{font-weight:650;margin:18px 0 4px;padding-top:14px;
border-top:1px solid var(--line)}
details{margin-top:12px}
summary{cursor:pointer;color:var(--accent);font-size:13px}
.spin{display:inline-block;width:13px;height:13px;border:2px solid var(--line);
border-top-color:var(--accent);border-radius:50%;animation:s .8s linear infinite;
vertical-align:-2px;margin-right:7px}
@keyframes s{to{transform:rotate(360deg)}}
</style></head><body><div id="banniere"></div><div class="wrap">

<h1>Assistant trade-up CS2</h1>
<div class="sub">Application locale &middot; montants en
  <b id="devise">…</b> <span id="devise-note"></span>
  &middot; serveur démarré <b id="demarrage">…</b></div>

<div class="tabs">
  <div class="tab on" data-pane="calcul">Calculer</div>
  <div class="tab" data-pane="ordres">Ordres d’achat</div>
  <div class="tab" data-pane="inventaire">Mon inventaire</div>
  <div class="tab" data-pane="histo">Historique des plans</div>
  <div class="tab" data-pane="contrats">Mes contrats</div>
</div>

<div class="pane on" id="pane-calcul">
  <div class="card">
    <p class="muted">Le modèle cote chaque collection de la rareté choisie et
    ne garde que les contrats <b>rentables</b> : ceux dont la revente attendue
    dépasse ce que les dix entrées coûtent. Les autres ne sont pas affichés —
    il n’y a rien à en faire.</p>
    <div class="row">
      <label>Rareté d’entrée
        <select id="rarity">
          <option value="consumer">Consumer</option>
          <option value="industrial" selected>Industrial</option>
          <option value="mil-spec">Mil-Spec</option>
          <option value="restricted">Restricted</option>
          <option value="classified">Classified</option>
        </select>
      </label>
      <button id="chercher">Relancer maintenant (plus d’1 h)</button>
    </div>
    <p class="muted" id="cout-balayage">…</p>
  </div>
  <div id="avancement"></div>
  <div id="resultats"></div>
</div>

<div class="pane" id="pane-ordres">
  <div class="card">
    <p class="muted">Un panier d’annonces ne se répète pas : chaque annonce est
    unique, et le lendemain il faut tout rechercher. Un <b>ordre d’achat</b> se
    pose une fois et se remplit tout seul — c’est la forme exploitable quand on
    veut <b>refaire</b> le même contrat. Le calcul est renversé : au lieu de
    partir du prix du marché, il déduit le <b>prix maximal</b> que chaque entrée
    peut coûter pour que le contrat tienne.</p>
    <div class="row">
      <label>Rareté d’entrée
        <select id="ord-rarity">
          <option value="consumer">Consumer</option>
          <option value="industrial" selected>Industrial</option>
          <option value="mil-spec">Mil-Spec</option>
          <option value="restricted">Restricted</option>
          <option value="classified">Classified</option>
        </select>
      </label>
      <button class="ghost" id="ord-relire">Relire</button>
      <span class="muted">lecture du cache : instantané, aucune requête</span>
    </div>
  </div>
  <div id="ord-sortie"></div>
</div>

<div class="pane" id="pane-inventaire">
  <div class="card">
    <p class="muted">Contrats realisables avec les skins que vous possedez deja.
    Les entrees sont valorisees a ce qu'elles rapporteraient <b>revendues</b> :
    fondre un skin, c'est renoncer a le vendre. Le prix que vous l'avez paye
    n'entre pas dans le calcul &mdash; il est deja depense quoi que vous
    decidiez.</p>
    <div class="row">
      <label>Rarete d'entree
        <select id="inv-rarity">
          <option value="consumer">Consumer</option>
          <option value="industrial">Industrial</option>
          <option value="mil-spec" selected>Mil-Spec</option>
          <option value="restricted">Restricted</option>
          <option value="classified">Classified</option>
        </select>
      </label>
      <label>Classer par
        <select id="inv-rank">
          <option value="risk_adjusted" selected>gain régulier (défaut)</option>
          <option value="safety">probabilité de gagner</option>
          <option value="ev">gain le plus élevé</option>
          <option value="roi">rendement</option>
        </select>
      </label>
      <button class="ghost sm" id="inv-relire">Relire l'inventaire</button>
    </div>
    <div id="inv-bilan" class="muted">chargement&hellip;</div>
    <div class="row" style="border-top:1px solid var(--line);padding-top:12px">
      <button id="inv-calculer" disabled>Calculer</button>
      <span class="muted" id="inv-cout"></span>
    </div>
  </div>
  <div id="inv-sortie"></div>
</div>

<div class="pane" id="pane-histo">
  <div class="card">
    <p class="muted">Tout plan calcule est conserve ici : il a coute des requetes,
    autant pouvoir le retrouver. &laquo;&nbsp;Suivre&nbsp;&raquo; en fait un contrat
    auquel rattacher vos achats reels.</p>
    <div class="scroll"><table>
      <thead><tr><th>Date</th><th>Collection</th><th class="num">Cout</th>
        <th class="num">Profit</th><th class="num">Rendement</th>
        <th class="num">Suivis</th><th></th></tr></thead>
      <tbody id="histo"></tbody>
    </table></div>
  </div>
</div>

<div class="pane" id="pane-contrats">
  <div class="row">
    <label><input type="checkbox" id="tous"> afficher aussi les contrats termines</label>
    <button class="ghost sm" id="rafraichir">Rafraichir</button>
  </div>
  <div id="contrats"></div>
</div>
</div>

<script>
const $ = s => document.querySelector(s);
// --- Profitabilite ----------------------------------------------------------
// Convention des guides et des calculateurs : 1.0 est le POINT MORT, pas le
// profit. Un contrat annonce "a 40 %" rend 40 centimes par euro engage, il en
// detruit 60. La page ne montre rien en dessous de 100 % : ce qui detruit de
// la valeur n'est pas un candidat, et le ranger parmi des candidats invite a
// le lire comme tel.

function profTexte(p) { return Math.round((p || 0) * 100) + '%'; }

// Jauge bornee a 200 % : au-dela l'echelle ecraserait tout le reste.
function jauge(p) {
  const pct = Math.min(100, ((p || 0) / 2) * 100);
  return '<div class="jauge"><i style="width:' + pct + '%"></i></div>';
}

function banniere(texte, erreur) {
  const b = $('#banniere');
  b.className = 'on' + (erreur ? ' err' : '');
  b.innerHTML = (erreur ? '' : '<span class="spin"></span>') + texte;
}
function cacherBanniere() { $('#banniere').className = ''; }

// Sans ca, une erreur JavaScript laisse la page muette : l'utilisateur clique
// et rien ne se passe, sans le moindre indice.
window.addEventListener('error', e =>
  banniere('Erreur dans la page : ' + e.message, true));
window.addEventListener('unhandledrejection', e =>
  banniere('Erreur : ' + (e.reason && e.reason.message || e.reason), true));

let collections = [];

async function charger() {
  const rep = await fetch('/api/collections?rarity=' + $('#rarity').value)
    .then(x => x.json());
  collections = rep.collections || [];
  const req = collections.reduce((n, c) => n + c.requests, 0);
  const h = req / 10 / 60;
  $('#cout-balayage').textContent = collections.length
    ? `${collections.length} collections à coter, ~${req} requêtes CSFloat, ` +
      `soit ~${h < 1 ? Math.round(h * 60) + ' min' : h.toFixed(1) + ' h'}. ` +
      `Le quota interrompra peut-être la recherche : elle reprend toute seule.`
    : 'aucune collection dans cette rareté.';
}

// --- La carte d'un contrat --------------------------------------------------
// Un seul rendu pour tout : resultat d'une recherche ou plan repris de
// l'historique disent la meme chose, il n'y a aucune raison de les mettre en
// forme deux fois.

function carte(p, planId, archive) {
  const bloc = [];

  if (archive) bloc.push(`<div class="warn"><b>Plan archivé.</b> Ces valeurs
    sont figées au moment du calcul : les annonces ont pu partir et les prix
    bouger. Relancez une recherche avant d’acheter.</div>`);

  if (p.replis) {
    bloc.push(`<div class="warn"><b>Valorisation prudente.</b>
      ${p.replis} sortie(s) n’avaient pas de prix Steam et ont été valorisées
      sur CSFloat, qui rend <b>17 à 37 % de moins</b>. Le gain réel sera donc
      supérieur à celui affiché — jamais inférieur de ce fait.</div>`);
  }
  if (p.all_profitable) {
    bloc.push(`<div class="warn ok"><b>Toutes les sorties sont rentables.</b>
      Quel que soit le skin obtenu vous gagnez, entre
      <b>+${(p.worst_profit || 0).toFixed(2)}</b> et
      <b>+${(p.best_profit || 0).toFixed(2)}</b>. Seule une chute des prix peut
      vous faire perdre, pas le tirage.</div>`);
  } else if (p.worst_profit !== null && p.worst_profit !== undefined) {
    bloc.push(`<div class="warn"><b>Le tirage peut vous faire perdre.</b>
      Selon la sortie obtenue, le résultat va de
      <span class="neg">${p.worst_profit.toFixed(2)}</span> à
      <span class="pos">+${(p.best_profit || 0).toFixed(2)}</span>.</div>`);
  }

  const lignes = p.inputs.map(i => `<tr>
    <td>${i.name}</td>
    <td class="num">${i.float.toFixed(4)}</td>
    <td class="num">${i.price.toFixed(2)}</td>
    <td>${i.url ? `<a class="buy" href="${i.url}" target="_blank">Acheter</a>`
      : '<span class="muted">annonce non identifiée</span>'}</td></tr>`).join('');

  return `<div class="card">
    <div class="tete">
      <div>
        <h2>${p.collection} <span class="tag">${p.rarity}</span></h2>
        <div class="muted">10 entrées ${p.rarity} → 1 sortie ${p.rarity_target}</div>
      </div>
      <div style="text-align:right">
        <div class="gros prof">${profTexte(p.profitability)}</div>
        ${jauge(p.profitability)}
        <div class="muted">profitabilité — 100 % = point mort</div>
      </div>
    </div>
    <div class="chiffres">
      <span>coût <b>${p.cost.toFixed(2)}</b></span>
      <span>revente nette attendue <b>${p.net.toFixed(2)}</b></span>
      <span>gain <b class="pos">+${p.profit.toFixed(2)}</b></span>
      <span>chances de gagner <b>${((p.win_probability || 0) * 100).toFixed(0)}%</b></span>
    </div>
    ${bloc.join('')}
    <div class="etape">Méthode d’achat</div>
    <p class="muted">Ces dix annonces précises, sur CSFloat. Le float de chacune
    est déjà celui qu’il faut : c’est lui qui décide de l’usure en sortie, donc
    n’en remplacez aucune par un exemplaire moins cher.</p>
    <div class="scroll"><table>
      <thead><tr><th>Objet</th><th class="num">Float</th>
        <th class="num">Prix</th><th></th></tr></thead>
      <tbody>${lignes}</tbody>
    </table></div>
    <p class="muted">Moyenne de float ${p.avg_float.toFixed(4)} sur
    ${p.listings_examined} annonces examinées. Achetez les dix d’un coup : le
    verrou de 7 jours part à la réception de chaque objet, et le contrat ne
    peut se faire qu’une fois le dernier libéré.</p>
    <details>
      <summary>Ce que le contrat peut sortir</summary>
      <div class="scroll"><table>
        <thead><tr><th>Skin</th><th class="num">Probabilité</th>
          <th class="num">Float</th><th class="num">Revente nette</th></tr></thead>
        <tbody>${p.outcomes.map(o => `<tr><td>${o.name}</td>
          <td class="num">${(o.probability * 100).toFixed(1)}%</td>
          <td class="num">${o.float.toFixed(4)}</td>
          <td class="num">${o.net.toFixed(2)}</td></tr>`).join('')}</tbody>
      </table></div>
    </details>
    <div class="row" style="margin:12px 0 0">
      <button class="sm" data-follow="${planId}">Suivre ce contrat</button>
    </div>
  </div>`;
}

// --- Ce que la nuit a trouve ------------------------------------------------
// Un balayage complet dure plus d'une heure. Le refaire parce qu'on ouvre la
// page ferait attendre devant un travail deja fait : on lit donc d'abord le
// journal, et le bouton ne sert qu'a rafraichir volontairement.

function ageTexte(ts) {
  const h = (Date.now() / 1000 - ts) / 3600;
  if (h < 1) return Math.round(h * 60) + ' min';
  if (h < 48) return Math.round(h) + ' h';
  return Math.round(h / 24) + ' j';
}

async function dernierBalayage() {
  const r = $('#rarity').value;
  const d = await fetch('/api/latest?rarity=' + r).then(x => x.json());
  if (d.error) return;
  const plans = d.plans || [];

  if (!plans.length) {
    $('#avancement').innerHTML = `<div class="card">
      <b>Rien de rentable au dernier balayage.</b>
      <p class="muted">Le balayage de nuit n’a retenu aucun contrat au-dessus
      du point mort dans cette rareté, ou n’a pas encore tourné. Lancer une
      recherche maintenant prend plus d’une heure — mieux vaut la laisser se
      faire cette nuit.</p></div>`;
    return;
  }

  // Le detail d'achat vient du journal, pas d'un nouveau calcul.
  const plansComplets = await Promise.all(plans.map(p => chargerPlan(p.plan_id)));
  const age = Math.min(...plans.map(p => Date.now() / 1000 - p.created_at));

  $('#avancement').innerHTML = `<div class="card">
    <b>${plans.length} contrat(s) rentable(s) au dernier balayage</b>
    <p class="muted">Calculé il y a ${ageTexte(Date.now() / 1000 - age)} ·
    montants en ${d.currency} · sur annonces réelles.
    ${age > 24 * 3600 ? '<b>Prix de plus de 24 h : recotez avant d’acheter.</b>'
      : 'Prix récents.'}</p></div>`;
  $('#resultats').innerHTML = plans
    .map((p, i) => carte(plansComplets[i], p.plan_id, age > 24 * 3600)).join('');
}

// --- Les ordres d'achat -----------------------------------------------------
// Un panier d'annonces ne se repete pas : chaque annonce est unique, et le
// lendemain il faut tout rechercher. Un ordre se pose une fois et se remplit
// tout seul -- c'est la seule forme exploitable quand on veut REFAIRE le
// meme contrat.

async function ordres() {
  const r = $('#ord-rarity').value;
  $('#ord-sortie').innerHTML = '<div class="card"><span class="spin"></span>lecture…</div>';
  const d = await fetch('/api/orders?rarity=' + r).then(x => x.json());
  if (d.error) {
    $('#ord-sortie').innerHTML = '<div class="card"><div class="warn">' +
      d.error + '</div></div>';
    return;
  }
  const plans = d.plans || [];
  const vieux = d.age_hours !== null && d.age_hours > 24;

  if (!plans.length) {
    $('#ord-sortie').innerHTML = `<div class="card">
      <b>Aucun ordre ne peut rendre un contrat rentable dans cette rareté.</b>
      <p class="muted">Soit le rabais nécessaire dépasse ce qu’un ordre peut
      espérer obtenir, soit il tomberait sous le plancher de 0,03 € de Steam.
      Les prix viennent du dernier balayage.</p></div>`;
    return;
  }

  $('#ord-sortie').innerHTML = `
    <div class="card">
      <b>${plans.length} contrat(s) atteignables par ordre d’achat</b>
      <p class="muted">Ordres à placer sur <b>${d.market}</b> ·
      montants en ${d.currency} · rendement visé
      +${Math.round(d.target_roi * 100)} %
      ${d.age_hours === null ? '' : `· prix vieux de ${d.age_hours} h`}
      ${vieux ? '<br><b>Plus de 24 h : recotez avant de placer.</b>' : ''}</p>
    </div>` + plans.map(p => `
    <div class="card">
      <div class="tete">
        <div>
          <h2>${p.collection} <span class="tag">${p.rarity}</span></h2>
          <div class="muted">10 entrées → 1 sortie ${p.rarity_target}</div>
        </div>
        <div style="text-align:right">
          <div class="gros prof">−${Math.round(p.discount * 100)} %</div>
          <div class="muted">rabais à obtenir</div>
        </div>
      </div>
      <div class="chiffres">
        <span>au prix demandé <b>${p.market_cost.toFixed(2)}</b></span>
        <span>budget maximal <b>${p.budget.toFixed(2)}</b></span>
        <span>revente nette attendue <b>${p.ev_net.toFixed(2)}</b></span>
        <span>chances de gagner <b>${Math.round(p.win_probability * 100)} %</b></span>
      </div>
      <div class="etape">Ordres à placer sur ${d.market}</div>
      <div class="scroll"><table>
        <thead><tr><th>Objet</th><th class="num">Qté</th>
          <th class="num">Prix marché</th><th class="num">Prix d’ordre</th>
          <th class="num">Rabais</th></tr></thead>
        <tbody>${p.lines.map(l => `<tr>
          <td>${l.name}${l.below_floor
            ? ' <span class="tag">sous le plancher Steam</span>' : ''}</td>
          <td class="num">${l.quantity}</td>
          <td class="num">${l.market_price.toFixed(2)}</td>
          <td class="num prof">${l.order_price.toFixed(2)}</td>
          <td class="num">${Math.round(l.discount * 100)} %</td></tr>`).join('')}
        </tbody>
      </table></div>
      <div class="warn"><b>Le float sera tiré au hasard dans le palier.</b>
      Un ordre d’achat porte sur une usure, pas sur un float : c’est le prix
      qui est choisi, pas la qualité. Les chances de gagner ci-dessus en
      tiennent compte.</div>
    </div>`).join('');
}

// --- La recherche -----------------------------------------------------------

let sondageBatch = null, dernierBatch = null;
// Un plan enregistre ne change plus : le relire a chaque sondage serait du
// trafic pur.
const plansCharges = {};

async function chargerPlan(id) {
  if (!plansCharges[id]) {
    const r = await fetch('/api/plan/' + id).then(x => x.json());
    if (r.error) throw new Error(r.error);
    plansCharges[id] = r.plan;
  }
  return plansCharges[id];
}

$('#chercher').addEventListener('click', async () => {
  const req = collections.reduce((n, c) => n + c.requests, 0);
  // Les sauts de ligne sont ecrits en clair : un antislash dans ce fichier
  // serait interprete par Python avant d'atteindre le navigateur.
  const avert = `Coter ${collections.length} collections ?

Environ ${req} requêtes CSFloat, soit un long moment. Laissez le terminal et
cette page ouverts : la recherche reprend toute seule si le quota s’épuise.`;
  if (!confirm(avert)) return;

  $('#chercher').disabled = true;
  $('#resultats').innerHTML = '';
  const r = await fetch('/api/batch', {method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({rarity: $('#rarity').value})}).then(x => x.json());
  if (r.error) { banniere(r.error, true); $('#chercher').disabled = false; return; }
  clearInterval(sondageBatch);
  sondageBatch = setInterval(() => suivreBatch(r.batch), 5000);
  suivreBatch(r.batch);
});

async function suivreBatch(id) {
  const b = await fetch('/api/batch/' + id).then(x => x.json());
  if (b.error) { clearInterval(sondageBatch); $('#chercher').disabled = false; return; }

  const fini = b.state === 'finished' || b.state === 'stopped';
  if (fini) {
    clearInterval(sondageBatch);
    cacherBanniere();
    $('#chercher').disabled = false;
  } else {
    banniere(`Recherche ${Math.round(b.progress * 100)}% — ` +
      `${b.results.length} rentable(s) trouvé(s), ${b.remaining} collections ` +
      `restantes` + (b.state === 'paused'
        ? ` — quota épuisé, reprise dans ${b.resume_in}s` : ''));
  }
  dernierBatch = b;
  await dessinerBatch(b);
}

async function dessinerBatch(b) {
  const pct = Math.round(b.progress * 100);
  const fini = b.state === 'finished' || b.state === 'stopped';

  $('#avancement').innerHTML = `<div class="card">
    <div class="row" style="justify-content:space-between;margin:0">
      <b>${fini ? 'Recherche terminée' : 'Recherche en cours'} —
        ${b.computed} collections cotées sur ${b.total}</b>
      ${fini ? '' : `<button class="ghost sm" data-stop="${b.id}">Arrêter</button>`}
    </div>
    <div class="bar"><i style="width:${pct}%"></i></div>
    <p class="muted">${b.results.length} rentable(s), ${b.rejected} écarté(s)
      sous le point mort, ${b.failed.length} sans assez d’annonces &middot;
      ${Math.round(b.elapsed / 60)} min écoulées &middot; montants en
      ${b.currency}</p>
  </div>`;

  if (!b.results.length) {
    $('#resultats').innerHTML = fini
      ? `<div class="card"><b>Aucun tradeup rentable dans cette rareté.</b>
         <p class="muted">${b.computed} collections cotées, aucune ne rend plus
         qu’elle ne coûte aux prix du moment. Ce n’est pas une panne, c’est le
         résultat. Essayez une autre rareté, ou relancez plus tard — les prix
         bougent, la réponse aussi.</p></div>`
      : '';
    return;
  }

  // Les plans arrivent au fil de l'eau : on charge le detail de chacun avant
  // d'afficher, pour qu'une carte ne s'ouvre jamais sans sa liste d'achat.
  const plans = await Promise.all(b.results.map(x => chargerPlan(x.plan_id)));
  $('#resultats').innerHTML = b.results
    .map((x, i) => carte(plans[i], x.plan_id, false)).join('');
}

document.addEventListener('click', async e => {
  const st = e.target.closest('[data-stop]');
  if (st) {
    await fetch('/api/batch/stop', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({batch: st.dataset.stop})});
    suivreBatch(st.dataset.stop);
  }
});

document.querySelectorAll('.tab').forEach(t => t.onclick = () => {
  document.querySelectorAll('.tab').forEach(x => x.classList.toggle('on', x === t));
  document.querySelectorAll('.pane').forEach(p =>
    p.classList.toggle('on', p.id === 'pane-' + t.dataset.pane));
  if (t.dataset.pane === 'ordres') ordres();
  if (t.dataset.pane === 'histo') histo();
  if (t.dataset.pane === 'contrats') contrats();
  if (t.dataset.pane === 'inventaire') invApercu(false);
});

// --- Classement partage -----------------------------------------------------
// Les memes criteres que `scan --rank`, appliques cote client : reclasser une
// liste deja calculee ne doit rien recouter en requetes CSFloat.
//
// Chaque critere renvoie un tableau compare terme a terme, ce qui exprime
// "probabilite d'abord, gain ensuite" sans nombre magique. Tous se departagent
// par le gain : a egalite, mieux vaut gagner plus.

const CRITERES = {
  risk_adjusted: p => [p.gain / Math.max(p.stdev || 0.01, 0.01), p.gain],
  ev: p => [p.gain, p.win],
  roi: p => [p.roi, p.gain],
  safety: p => [p.win, p.gain],
};

function trier(liste, critere, lire) {
  const cle = CRITERES[critere] || CRITERES.risk_adjusted;
  return liste.slice().sort((a, b) => {
    const x = cle(lire(a)), y = cle(lire(b));
    for (let i = 0; i < x.length; i++) {
      if (y[i] !== x[i]) return y[i] - x[i];
    }
    return 0;
  });
}

const dt = ts => new Date(ts * 1000).toLocaleString('fr-FR',
  {day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'});

async function histo() {
  const d = await fetch('/api/history').then(x => x.json());
  const l = d.plans || [];
  $('#histo').innerHTML = l.length ? l.map(p => `<tr>
    <td>${dt(p.created_at)}</td>
    <td>${p.collection}${p.rarity ? ` <span class="tag">${p.rarity}</span>` : ''}</td>
    <td class="num">${p.cost.toFixed(2)}</td>
    <td class="num ${p.profit >= 0 ? 'pos' : 'neg'}">${p.profit >= 0 ? '+' : ''}${p.profit.toFixed(2)}</td>
    <td class="num">${(p.roi * 100).toFixed(1)}%</td>
    <td class="num">${p.followed || '-'}</td>
    <td><button class="ghost sm" data-voir="${p.id}">Voir</button>
        <button class="sm" data-follow="${p.id}">Suivre</button></td></tr>`).join('')
    : '<tr><td colspan="7" class="muted">aucun plan calcule pour le moment</td></tr>';
}

async function contrats() {
  const tous = $('#tous').checked ? '?all=1' : '';
  const d = await fetch('/api/contracts' + tous).then(x => x.json());
  const l = d.contracts || [];
  if (!l.length) {
    $('#contrats').innerHTML = '<div class="card muted">Aucun contrat suivi. ' +
      'Ouvrez l&rsquo;historique et cliquez Suivre.</div>';
    return;
  }
  $('#contrats').innerHTML = l.map(c => {
    const pct = Math.round(c.purchased / 10 * 100);
    let etat;
    if (c.craftable) {
      etat = '<span class="tag" style="background:#0f7a3d;color:#fff">executable maintenant</span>';
    } else if (c.craftable_at) {
      etat = '<span class="tag">executable le ' + dt(c.craftable_at) + '</span>';
    } else {
      etat = '<span class="tag">' + c.purchased + '/10 achetes</span>';
    }

    let derive = '';
    if (c.float_drift !== null) {
      const gros = Math.abs(c.float_drift) > 0.003;
      derive = '<div class="' + (gros ? 'warn' : 'muted') + '">Float moyen reel : <b>' +
        c.actual_avg_float.toFixed(4) + '</b> (prevu ' + c.planned_avg_float.toFixed(4) +
        ', ecart ' + (c.float_drift >= 0 ? '+' : '') + c.float_drift.toFixed(4) + ')' +
        (gros ? ' &mdash; verifiez que la sortie n&rsquo;a pas change de palier.' : '') +
        '</div>';
    }

    const lignes = c.items.map(i => {
      const verrou = i.purchased
        ? (i.locked ? 'jusqu&rsquo;au ' + dt(i.tradable_at) : 'libre')
        : '<span class="muted">non achete</span>';
      const actions = i.purchased
        ? '<button class="ghost sm" data-item="' + i.id + '" data-act="annuler">Annuler</button>'
        : (i.url ? '<a class="buy" href="' + i.url + '" target="_blank">Acheter</a> ' : '') +
          '<button class="sm" data-item="' + i.id + '" data-act="acheter">Achete</button>';
      return '<tr class="' + (i.purchased ? 'done' : '') + '"><td>' + i.name +
        (i.from_plan ? '' : ' <span class="tag">substitut</span>') +
        '</td><td class="num">' + i.float.toFixed(4) +
        '</td><td class="num">' + i.price.toFixed(2) +
        '</td><td>' + verrou + '</td><td>' + actions + '</td></tr>';
    }).join('');

    return '<div class="card"><div class="row" style="justify-content:space-between">' +
      '<b>' + c.collection + '</b> ' +
      (c.rarity ? '<span class="tag">' + c.rarity + '</span> ' : '') +
      etat + '</div>' +
      '<div class="bar"><i style="width:' + pct + '%"></i></div>' +
      '<p class="muted">Depense ' + c.spent.toFixed(2) + ' / prevu ' +
      c.planned_cost.toFixed(2) + ' &middot; cree le ' + dt(c.created_at) +
      ' &middot; statut : ' + c.status + '</p>' + derive +
      '<div class="scroll"><table><thead><tr><th>Objet</th><th class="num">Float</th>' +
      '<th class="num">Prix</th><th>Verrou</th><th></th></tr></thead><tbody>' +
      lignes + '</tbody></table></div>' +
      '<div class="row" style="margin-top:10px">' +
      '<button class="ghost sm" data-ct="' + c.id + '" data-status="realise">Marquer realise</button>' +
      '<button class="ghost sm" data-ct="' + c.id + '" data-status="abandonne">Abandonner</button>' +
      '<button class="ghost sm" data-ct="' + c.id + '" data-del="1">Supprimer</button>' +
      '</div></div>';
  }).join('');
}

document.addEventListener('click', async e => {
  const v = e.target.closest('[data-voir]');
  if (v) {
    const r = await fetch('/api/plan/' + v.dataset.voir).then(x => x.json());
    if (r.error) { banniere(r.error, true); return; }
    document.querySelector('.tab[data-pane="calcul"]').click();
    $('#avancement').innerHTML = '';
    $('#resultats').innerHTML = carte(r.plan, v.dataset.voir, true);
    $('#resultats').scrollIntoView({behavior: 'smooth', block: 'start'});
    return;
  }
  const f = e.target.closest('[data-follow]');
  if (f) {
    await fetch('/api/follow', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({plan: f.dataset.follow})});
    document.querySelector('.tab[data-pane="contrats"]').click();
    return;
  }
  const it = e.target.closest('[data-item]');
  if (it) {
    const corps = {item: it.dataset.item, action: it.dataset.act};
    if (it.dataset.act === 'acheter') {
      const p = prompt('Prix reellement paye (vide = prix du plan)');
      if (p) corps.price = p;
      const fl = prompt('Float reel (vide = float du plan)');
      if (fl) corps.float = fl;
    }
    await fetch('/api/item', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(corps)});
    contrats();
    return;
  }
  const ct = e.target.closest('[data-ct]');
  if (ct) {
    const corps = {contract: ct.dataset.ct};
    if (ct.dataset.del) {
      if (!confirm('Supprimer ce contrat et ses objets ?')) return;
      corps.delete = true;
    } else { corps.status = ct.dataset.status; }
    await fetch('/api/contract', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(corps)});
    contrats();
  }
});

fetch('/api/status').then(x => x.json()).then(d => {
  $('#demarrage').textContent =
    new Date(d.started_at * 1000).toLocaleTimeString('fr-FR',
      {hour: '2-digit', minute: '2-digit'});
  if (d.currency) $('#devise').textContent = d.currency;
  // Une lecture ratee laisse "USD" par defaut : le dire, plutot que de le
  // presenter comme la devise du compte.
  $('#devise-note').textContent = d.currency_known
    ? '(devise de votre compte CSFloat)'
    : '(devise du compte illisible — aucune conversion appliquée)';
}).catch(() => {});

// --- Mon inventaire ---------------------------------------------------------

let invSondage = null;

async function invApercu(force) {
  const r = $('#inv-rarity').value;
  $('#inv-bilan').innerHTML = '<span class="spin"></span>lecture de l’inventaire…';
  $('#inv-calculer').disabled = true;
  $('#inv-cout').textContent = '';
  try {
    const d = await fetch('/api/inventory?rarity=' + r + (force ? '&force=1' : ''))
      .then(x => x.json());
    if (d.error) {
      $('#inv-bilan').innerHTML = '<span class="neg">' + d.error + '</span>';
      return;
    }
    const s = d.summary;
    const bloque = [];
    if (s.sans_float) bloque.push(s.sans_float + ' sans float');
    if (s.verrouilles) bloque.push(s.verrouilles + ' verrouillés (7 jours)');
    if (s.souvenirs) bloque.push(s.souvenirs + ' Souvenir (interdits)');
    if (s.en_vente) bloque.push(s.en_vente + ' en vente');

    let html = '<b>' + s.objets + ' objets</b>, ' + s.utilisables +
      ' utilisables en contrat' +
      (bloque.length ? ' <span class="muted">— ' + bloque.join(', ') + '</span>' : '');

    if (d.buildable.length) {
      html += '<div style="margin-top:8px">' + d.buildable.map(c =>
        '<span class="tag">' + c.name + ' — ' + c.owned + ' objets</span>'
      ).join(' ') + '</div>';
      $('#inv-calculer').disabled = false;
      $('#inv-cout').textContent = d.requests + ' requêtes CSFloat, soit ~' +
        Math.ceil(d.requests / 10) + ' min';
    } else {
      html += '<div style="margin-top:8px" class="muted">Aucun contrat possible en ' +
        d.rarity + ' : il faut <b>dix</b> objets de la même collection.' +
        (d.gaps.length ? ' Le plus proche : ' + d.gaps.map(g =>
          g.collection + ' (' + g.owned + '/10)').join(', ') : '') + '</div>';
    }
    $('#inv-bilan').innerHTML = html;
  } catch (e) {
    $('#inv-bilan').innerHTML = '<span class="neg">' + e + '</span>';
  }
}

function invLigne(p) {
  const signe = p.gain >= 0 ? 'pos' : 'neg';
  const alertes = [];
  if (p.gain <= 0) alertes.push(
    '<div class="warn">Ces dix skins valent <b>plus vendus que fondus</b>. ' +
    'Le contrat détruirait ' + Math.abs(p.gain).toFixed(2) + ' ' + p.currency + '.</div>');
  if (p.decorated.length) alertes.push(
    '<div class="warn">' + p.decorated.length + ' objet(s) portent des stickers ' +
    "ou une breloque : le contrat les détruit, et leur valeur n’est pas comptée " +
    'ici. — ' + p.decorated.join(', ') + '</div>');
  if (p.listed.length) alertes.push(
    '<div class="warn">' + p.listed.length + ' objet(s) sont actuellement ' +
    '<b>en vente</b> sur CSFloat : il faudra retirer les annonces.</div>');

  return '<div class="card">' +
    '<div class="row" style="justify-content:space-between">' +
      '<b>' + p.collection + ' <span class="tag">' + p.rarity + '</span></b>' +
      '<span class="' + signe + '" style="font-size:18px">' +
        (p.gain >= 0 ? '+' : '') + p.gain.toFixed(2) + ' ' + p.currency +
        ' (' + (p.roi * 100).toFixed(1) + '%)</span>' +
    '</div>' +
    '<p class="muted">Valeur des dix entrées si vendues : ' +
      p.opportunity_cost.toFixed(2) + ' &middot; revente nette espérée : ' +
      p.net.toFixed(2) + ' &middot; float moyen ' + p.avg_float.toFixed(4) +
      ' <span class="muted">(exact, aucun tirage)</span></p>' +
    // Le gain seul ne dit pas a quelle frequence on l'obtient.
    '<p class="muted"><b>' + Math.round(p.win_probability * 100) +
      '%</b> de chances d’y gagner &middot; ' + p.outcomes_count +
      ' sorties possibles' + (p.always_profitable
        ? ' &middot; <span class="pos"><b>toutes rentables</b> : le tirage ne ' +
          'peut pas vous faire perdre</span>' : '') + '</p>' +
    alertes.join('') +
    '<div class="scroll"><table>' +
      '<thead><tr><th>À fondre</th><th class="num">Float</th>' +
      '<th class="num">Valeur si vendu</th></tr></thead><tbody>' +
      p.inputs.map(i => '<tr><td>' + i.name + '</td><td class="num">' +
        i.float.toFixed(4) + '</td><td class="num">' + i.value.toFixed(2) +
        '</td></tr>').join('') +
    '</tbody></table></div>' +
    '<div class="scroll"><table>' +
      '<thead><tr><th>Sortie possible</th><th class="num">Probabilité</th>' +
      '<th class="num">Float</th><th class="num">Revente nette</th></tr></thead><tbody>' +
      p.outcomes.map(o => '<tr><td>' + o.name + '</td><td class="num">' +
        (o.probability * 100).toFixed(1) + '%</td><td class="num">' +
        o.float.toFixed(4) + '</td><td class="num">' + o.net.toFixed(2) +
        '</td></tr>').join('') +
    '</tbody></table></div></div>';
}

async function invSuivre(id) {
  const d = await fetch('/api/inventory-job/' + id).then(x => x.json());
  if (d.state === 'running') {
    banniere("Valorisation de l’inventaire… " + d.elapsed + "s");
    return;
  }
  clearInterval(invSondage);
  invSondage = null;
  cacherBanniere();
  $('#inv-calculer').disabled = false;

  if (d.state === 'quota' || d.state === 'error') {
    banniere(d.message, true);
    $('#inv-sortie').innerHTML = '<div class="card neg">' + d.message + '</div>';
    return;
  }
  if (!d.results.length) {
    $('#inv-sortie').innerHTML = '<div class="card muted">' + d.message +
      (d.gaps.length ? '<br>Le plus proche du compte : ' + d.gaps.map(g =>
        g.collection + ' (' + g.owned + '/10)').join(', ') : '') + '</div>';
    return;
  }
  invDernier = d;
  invDessiner();
}

// Reclasser ne relance aucun calcul : les contrats sont deja connus, seul
// leur ordre change.
let invDernier = null;

function invDessiner() {
  const d = invDernier;
  if (!d || !d.results.length) return;
  const tries = trier(d.results, $('#inv-rank').value,
    p => ({gain: p.gain, roi: p.roi, win: p.win_probability, stdev: p.stdev}));
  $('#inv-sortie').innerHTML = '<p class="muted">' + d.message + '</p>' +
    tries.map(invLigne).join('');
}

$('#inv-rank').addEventListener('change', invDessiner);
$('#inv-rarity').addEventListener('change', () => invApercu(false));
$('#inv-relire').addEventListener('click', () => invApercu(true));
$('#inv-calculer').addEventListener('click', async () => {
  $('#inv-calculer').disabled = true;
  $('#inv-sortie').innerHTML = '';
  banniere("Valorisation de l’inventaire…");
  const d = await fetch('/api/inventory', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({rarity: $('#inv-rarity').value}),
  }).then(x => x.json());
  if (d.error) { banniere(d.error, true); $('#inv-calculer').disabled = false; return; }
  invSondage = setInterval(() => invSuivre(d.id), 2000);
  invSuivre(d.id);
});

$('#tous').addEventListener('change', contrats);
$('#rafraichir').addEventListener('click', contrats);
$('#ord-rarity').addEventListener('change', ordres);
$('#ord-relire').addEventListener('click', ordres);
$('#rarity').addEventListener('change', () => {
  charger();
  dernierBalayage();
});
charger();
dernierBalayage();
</script></body></html>
"""


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="tradeup.web", description=__doc__)
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--rate", type=int, default=10,
                   help="requetes CSFloat par minute")
    p.add_argument("--no-open", action="store_true")
    p.add_argument("--db", default=None)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    serve(port=args.port, rate=args.rate, open_browser=not args.no_open,
          db_path=args.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
