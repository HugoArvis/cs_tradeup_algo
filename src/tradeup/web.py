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
from .plan import CSFloatPricer, Plan, build_plan
from .pricing.csfloat import CSFloat
from .pricing.http import RateLimited
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
        # L'API CSFloat cote en USD, mais le site affiche -- et facture -- dans
        # la devise du profil. Sans conversion, les montants ne correspondent a
        # rien de ce que l'utilisateur voit ni de ce qu'il paie.
        self.currency = "USD"
        self.rate_usd = 1.0
        self._devise_chargee = False
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
            if devise and devise != "USD":
                self.rate_usd = source.usd_rate(devise)
                self.currency = devise
        except Exception:  # noqa: BLE001 - la conversion est un confort
            log.warning("Devise du compte indisponible, affichage en USD")

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
            plan = build_plan(self.db, col, RARITES[job.rarity], source)
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
                plan = build_plan(self.db, col, RARITES[batch.rarity], source)
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
            # Tri par defaut au profit ; l'interface reclasse sans recalculer.
            "results": sorted(batch.done, key=lambda d: -d["profit"])[:40],
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
            self._json({
                "started_at": self.app.started_at,
                "currency": self.app.currency,
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
/* --- Profitabilite : la metrique qui decide --- */
/* 100 % est le point mort, pas le profit. Un contrat a 40 % rend 40 centimes
   par euro engage : il en detruit 60. La couleur porte ce seuil pour qu'aucune
   ligne ne soit lue a l'envers. */
.prof{font-weight:700}
.prof.go{color:var(--pos)}
.prof.no{color:var(--neg)}
.kpi .val.big{font-size:26px}
.seuil{font-variant-numeric:tabular-nums}
.seuil.facile{color:var(--pos);font-weight:600}
.seuil.dur{color:var(--muted)}
.jauge{height:4px;background:var(--line);border-radius:2px;overflow:hidden;
margin-top:6px}
.jauge i{display:block;height:100%;background:var(--pos)}
.jauge i.no{background:var(--neg)}
.spin{display:inline-block;width:13px;height:13px;border:2px solid var(--line);
border-top-color:var(--accent);border-radius:50%;animation:s .8s linear infinite;
vertical-align:-2px;margin-right:7px}
@keyframes s{to{transform:rotate(360deg)}}
</style></head><body><div id="banniere"></div><div class="wrap">

<h1>Assistant trade-up CS2</h1>
<div class="sub">Application locale &middot; montants en
  <b id="devise">USD</b> (devise de votre compte CSFloat)
  &middot; serveur démarré <b id="demarrage">…</b></div>

<div class="tabs">
  <div class="tab on" data-pane="calcul">Calculer</div>
  <div class="tab" data-pane="inventaire">Mon inventaire</div>
  <div class="tab" data-pane="histo">Historique des plans</div>
  <div class="tab" data-pane="contrats">Mes contrats</div>
</div>

<div class="pane on" id="pane-calcul">
<div id="sortie"></div>
<div class="card">
  <div class="row">
    <label>Rareté d'entrée
      <select id="rarity">
        <option value="consumer">Consumer</option>
        <option value="industrial">Industrial</option>
        <option value="mil-spec" selected>Mil-Spec</option>
        <option value="restricted">Restricted</option>
        <option value="classified">Classified</option>
      </select>
    </label>
    <label>Sorties max
      <select id="maxout">
        <option value="1">1 (résultat certain)</option>
        <option value="2">≤ 2</option>
        <option value="3">≤ 3</option>
        <option value="5">≤ 5</option>
        <option value="99" selected>toutes</option>
      </select>
    </label>
    <input id="filtre" placeholder="filtrer par nom…" style="flex:1;min-width:160px">
  </div>
  <p class="muted" id="compte">…</p>
  <div class="row" style="border-top:1px solid var(--line);padding-top:12px">
    <button class="ghost" id="balayer">Tout calculer pour cette rareté</button>
    <label><input type="checkbox" id="bat-profitables"> seulement les
      profitables (&gt; 100 %)</label>
    <label>Classer par
      <select id="bat-rank">
        <option value="risk_adjusted" selected>gain régulier (défaut)</option>
        <option value="safety">probabilité de gagner</option>
        <option value="ev">gain le plus élevé</option>
        <option value="roi">rendement</option>
      </select>
    </label>
    <span class="muted" id="cout-balayage"></span>
  </div>
  <div id="balayage"></div>
  <div class="scroll"><table>
    <thead><tr><th>Collection</th><th class="num">Sorties</th>
      <th class="num">Probabilité</th><th class="num">Entrées</th>
      <th class="num">Seuil FN</th>
      <th class="num">Requêtes</th><th></th></tr></thead>
    <tbody id="liste"><tr><td colspan="6" class="muted">chargement…</td></tr></tbody>
  </table></div>
  <p class="muted"><b>Seuil FN</b> : moyenne de float d'entrée maximale qui
  garde la sortie en Factory New. Il se lit à l'envers de l'intuition —
  <b>plus il est haut, moins les entrées doivent être bonnes</b>. Au-dessus de
  0,300 le Factory New s'obtient sans trier ; en dessous il faut inspecter les
  annonces une par une. Ce chiffre ne coûte aucune requête.</p>
  <p class="muted">La colonne <b>Requêtes</b> est le coût CSFloat du plan.
  À 10 requêtes/minute, comptez environ ce nombre divisé par 10 en minutes.
  Le quota est limité sur une fenêtre longue : enchaînez sans excès.</p>
</div>

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
// profit. Un contrat annonce "a 40 %" rend 40 centimes par euro engage.
// Afficher un rendement de +18 % a cote d'une profitabilite de 118 % sans les
// distinguer conduit a lire l'un pour l'autre.

const SEUIL_PROFITABLE = 1.0;

function profClasse(p) {
  return (p || 0) >= SEUIL_PROFITABLE ? 'go' : 'no';
}

function profTexte(p) {
  return Math.round((p || 0) * 100) + '%';
}

// Jauge bornee a 200 % : au-dela l'echelle ecraserait tout le reste.
function jauge(p) {
  const pct = Math.min(100, ((p || 0) / 2) * 100);
  const cls = profClasse(p) === 'go' ? '' : ' class="no"';
  return '<div class="jauge"><i' + cls + ' style="width:' + pct + '%"></i></div>';
}

// Le seuil Factory New se lit a l'envers de l'intuition : PLUS il est haut,
// MOINS les entrees doivent etre bonnes.
function seuilFN(v) {
  if (!v) return '<span class="seuil dur">—</span>';
  const facile = v >= 0.30;
  const titre = facile
    ? 'Factory New atteint sans trier les annonces'
    : 'il faut trier les floats un par un';
  return '<span class="seuil ' + (facile ? 'facile' : 'dur') + '" title="' +
    titre + '">' + v.toFixed(3) + '</span>';
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
let collections = [], choisie = null, sondage = null;

async function charger() {
  const r = $('#rarity').value;
  $('#liste').innerHTML = '<tr><td colspan="6" class="muted">chargement…</td></tr>';
  const rep = await fetch('/api/collections?rarity=' + r).then(x => x.json());
  collections = rep.collections || [];
  dessiner();
}

function dessiner() {
  const max = +$('#maxout').value;
  const q = $('#filtre').value.trim().toLowerCase();
  const vues = collections.filter(c => c.outcomes <= max &&
    (!q || c.name.toLowerCase().includes(q)));
  // Sans ce compteur, un filtre actif donne l'impression que des collections
  // manquent alors qu'elles sont simplement masquees.
  const caches = collections.length - vues.length;
  setTimeout(estimerBalayage, 0);
  $('#compte').textContent = caches
    ? `${vues.length} affichées sur ${collections.length} — ${caches} masquées par les filtres`
    : `${collections.length} collections`;
  if (!vues.length) {
    $('#liste').innerHTML = '<tr><td colspan="6" class="muted">aucune collection</td></tr>';
    return;
  }
  $('#liste').innerHTML = vues.map(c => `
    <tr class="pick${choisie === c.id ? ' sel' : ''}" data-id="${c.id}">
      <td>${c.name}</td>
      <td class="num">${c.outcomes === 1
        ? '<span class="tag">1 — certain</span>' : c.outcomes}</td>
      <td class="num">${(100 / c.outcomes).toFixed(0)}%</td>
      <td class="num">${c.inputs}</td>
      <td class="num">${seuilFN(c.fn_threshold)}</td>
      <td class="num">${c.requests}</td>
      <td><button data-run="${c.id}">Calculer</button></td>
    </tr>`).join('');
}

document.addEventListener('click', e => {
  const b = e.target.closest('[data-run]');
  if (b) {
    // Retour immediat SUR le bouton : sans lui, un clic sur une longue liste
    // semble ne rien faire, le resultat s'affichant hors du champ de vision.
    b.disabled = true;
    b.textContent = 'Calcul…';
    choisie = b.dataset.run;
    lancer(b.dataset.run);
    return;
  }
  const tr = e.target.closest('tr.pick');
  if (tr) { choisie = tr.dataset.id; dessiner(); }
});

async function lancer(id) {
  clearInterval(sondage);
  const col = collections.find(c => c.id === id);
  if (!col) { echec('Collection introuvable : ' + id); return; }
  const mins = Math.max(1, Math.round(col.requests / 10));
  $('#sortie').innerHTML = `<div class="card"><span class="spin"></span>
    Calcul en cours sur <b>${col.name}</b> —
    <span id="chrono">0</span> s écoulées.
    <div class="muted">Environ ${col.requests} requêtes CSFloat, soit ~${mins} min.
    Ne fermez pas la page.</div></div>`;
  $('#sortie').scrollIntoView({behavior: 'smooth', block: 'start'});
  banniere('Calcul en cours sur ' + col.name + ' — environ ' + mins + ' min…');

  try {
    const rep = await fetch('/api/plan', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({collection: id, rarity: $('#rarity').value})
    }).then(x => x.json());
    if (rep.error) { echec(rep.error); return; }
    sondage = setInterval(() => suivre(rep.job), 2000);
    suivre(rep.job);
  } catch (err) {
    // Sans ce filet, une coupure du serveur laissait la page tourner
    // indefiniment sans rien dire.
    echec('Le serveur ne répond pas (' + err.message +
          '). Vérifiez que le terminal tourne toujours.');
  }
}

function echec(message) {
  clearInterval(sondage);
  banniere(message, true);
  setTimeout(cacherBanniere, 8000);
  $('#sortie').innerHTML = '<div class="card"><div class="warn">' + message + '</div></div>';
  $('#sortie').scrollIntoView({behavior: 'smooth', block: 'start'});
  dessiner();
}

async function suivre(job) {
  const d = await fetch('/api/job/' + job).then(x => x.json());
  const chrono = $('#chrono');
  if (chrono) chrono.textContent = d.elapsed;
  if (d.state === 'running') {
    banniere('Calcul en cours — ' + d.elapsed + ' s écoulées…');
  }
  if (d.state === 'running') return;
  clearInterval(sondage);
  if (d.state === 'done') { cacherBanniere(); afficher(d); dessiner(); }
  else echec(d.message || 'Aucun résultat.');
}

function afficher(d) {
  const p = d.plan, cls = p.profit >= 0 ? 'pos' : 'neg';
  if (p.currency && !d.archive) $('#devise').textContent = p.currency;
  const avert = [];
  if (d.archive) avert.push('<div class="warn"><b>Plan archivé.</b> Ces valeurs ' +
    'sont figées au moment du calcul : les annonces ont pu être vendues et les ' +
    'prix bouger. Montants en ' + (p.currency || 'USD') + '. ' +
    '<b>Relancez le calcul avant d\u2019acheter.</b></div>');
  if (p.price_drop_tolerance !== null) avert.push(`<div class="warn">
    <b>Blocage 7 jours.</b> Les achats CSFloat arrivent par échange : utilisables
    dans un contrat seulement dans 7 jours. Le prix de sortie peut baisser de
    <b>${(p.price_drop_tolerance * 100).toFixed(1)}%</b> d'ici là avant de perdre.</div>`);
  if (p.downgrade_profit !== null) avert.push(`<div class="warn">
    <b>Tolérance de float : ${p.float_slack.toFixed(4)}</b> sur la somme des dix
    entrées. Si une annonce part et que la remplaçante dépasse cette marge, la
    sortie perd un palier : <span class="neg">${p.downgrade_profit.toFixed(2)}</span>
    au lieu de <span class="pos">+${p.profit.toFixed(2)}</span>.</div>`);
  if (p.all_profitable) {
    avert.push(`<div class="warn" style="border-left-color:var(--pos)">
      <b>Toutes les sorties sont rentables.</b> Quel que soit le skin obtenu,
      vous gagnez : entre <b>+${p.worst_profit.toFixed(2)}</b> (pire cas) et
      <b>+${p.best_profit.toFixed(2)}</b> (meilleur cas). Le tirage ne peut pas
      vous faire perdre — seule une chute des prix le pourrait.</div>`);
  } else if (p.worst_profit !== null && p.worst_profit !== undefined) {
    avert.push(`<div class="warn">
      <b>Le tirage peut vous faire perdre.</b> Selon la sortie obtenue, le
      résultat va de <span class="neg">${p.worst_profit.toFixed(2)}</span> à
      <span class="pos">+${p.best_profit.toFixed(2)}</span>.</div>`);
  }
  if (p.exit_loss !== null && p.exit_loss !== undefined && p.profit > 0) {
    avert.push(`<div class="warn" style="border-left-color:var(--pos)">
      <b>Vous n'êtes pas engagé.</b> Au bout des 7 jours vos skins sont libres :
      si le contrat n'est plus rentable, revendez-les au lieu de les fusionner.
      Cela coûte <b>${p.exit_loss.toFixed(2)}</b>
      (${(p.exit_loss_ratio * 100).toFixed(1)}%), contre
      <b>+${p.profit.toFixed(2)}</b> à gagner — soit
      ${Math.abs(p.profit / p.exit_loss).toFixed(1)}x plus à gagner qu'à perdre.</div>`);
  }

  $('#sortie').innerHTML = `
  <div class="card kpis">
    <div class="kpi"><div class="lab">Coût</div><div class="val">${p.cost.toFixed(2)}</div></div>
    <div class="kpi"><div class="lab">Revente nette</div><div class="val">${p.net.toFixed(2)}</div></div>
    <div class="kpi"><div class="lab">Profit</div>
      <div class="val ${cls}">${p.profit >= 0 ? '+' : ''}${p.profit.toFixed(2)}</div></div>
    <div class="kpi"><div class="lab">Profitabilité</div>
      <div class="val big prof ${profClasse(p.profitability)}">${
        profTexte(p.profitability)}</div>
      ${jauge(p.profitability)}
      <div class="lab">100 % = point mort</div></div>
  </div>
  ${avert.join('')}
  <div class="card">
    <div class="row" style="justify-content:space-between">
      <b>${p.collection}${p.rarity ? ` <span class="tag">${p.rarity}</span>` : ''} — 10 annonces à acheter</b>
      <span><button class="sm" data-follow="${d.plan_id}">Suivre ce plan</button>
      ${d.id ? `<a class="buy" href="/report/${d.id}" target="_blank">Rapport imprimable</a>` : ''}</span>
    </div>
    <div class="scroll"><table>
      <thead><tr><th>Objet</th><th class="num">Float</th><th class="num">Prix</th><th></th></tr></thead>
      <tbody>${p.inputs.map(i => `<tr><td>${i.name}</td>
        <td class="num">${i.float.toFixed(4)}</td>
        <td class="num">${i.price.toFixed(2)}</td>
        <td>${i.url ? `<a class="buy" href="${i.url}" target="_blank">Acheter</a>`
          : '<span class="muted">non identifiée</span>'}</td></tr>`).join('')}</tbody>
    </table></div>
    <p class="muted">Moyenne de float : ${p.avg_float.toFixed(4)} &middot;
    ${p.listings_examined} annonces examinées. Achetez les dix groupés :
    le compteur de 7 jours démarre à la réception de chaque objet.</p>
  </div>
  <div class="card">
    <b>Sortie</b>
    <div class="scroll"><table>
      <thead><tr><th>Skin</th><th class="num">Probabilité</th>
        <th class="num">Float</th><th class="num">Net</th></tr></thead>
      <tbody>${p.outcomes.map(o => `<tr><td>${o.name}</td>
        <td class="num">${(o.probability * 100).toFixed(1)}%</td>
        <td class="num">${o.float.toFixed(4)}</td>
        <td class="num">${o.net.toFixed(2)}</td></tr>`).join('')}</tbody>
    </table></div>
  </div>`;
}

let sondageBatch = null;

function estimerBalayage() {
  const max = +$('#maxout').value;
  const vues = collections.filter(c => c.outcomes <= max);
  const req = vues.reduce((n, c) => n + c.requests, 0);
  const h = req / 10 / 60;
  $('#cout-balayage').textContent = vues.length
    ? `${vues.length} collections, ~${req} requêtes CSFloat, soit ~${
        h < 1 ? Math.round(h * 60) + ' min' : h.toFixed(1) + ' h'}`
    : '';
}

$('#balayer').addEventListener('click', async () => {
  const max = +$('#maxout').value;
  const vues = collections.filter(c => c.outcomes <= max);
  const req = vues.reduce((n, c) => n + c.requests, 0);
  if (!confirm(`Calculer ${vues.length} collections ?\n\nEnviron ${req} requêtes ` +
      `CSFloat, soit plusieurs heures. Le quota interrompra probablement le ` +
      `balayage : il reprendra tout seul.\n\nLaissez le terminal ouvert.`)) return;

  const r = await fetch('/api/batch', {method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({rarity: $('#rarity').value, max_outcomes: max})
  }).then(x => x.json());
  if (r.error) { banniere(r.error, true); return; }
  clearInterval(sondageBatch);
  sondageBatch = setInterval(() => suivreBatch(r.batch), 5000);
  suivreBatch(r.batch);
});

// Le dernier etat recu, pour reclasser sans refaire d'appel : un balayage
// termine n'est plus sonde, et changer de critere ne doit rien recouter.
let dernierBatch = null;

async function suivreBatch(id) {
  const b = await fetch('/api/batch/' + id).then(x => x.json());
  if (b.error) { clearInterval(sondageBatch); return; }

  const pct = Math.round(b.progress * 100);
  const fini = b.state === 'finished' || b.state === 'stopped';
  if (fini) { clearInterval(sondageBatch); cacherBanniere(); }
  else banniere(`Balayage ${pct}% — ${b.remaining} collections restantes` +
    (b.state === 'paused' ? ` — quota épuisé, reprise dans ${b.resume_in}s` : ''));

  dernierBatch = b;
  dessinerBatch(b);
}

function dessinerBatch(b) {
  const pct = Math.round(b.progress * 100);
  const fini = b.state === 'finished' || b.state === 'stopped';

  // Filtrer AVANT de classer : un contrat sous le point mort n'a pas a
  // occuper une place dans un classement de candidats.
  const retenus = $('#bat-profitables').checked
    ? b.results.filter(x => (x.profitability || 0) >= SEUIL_PROFITABLE)
    : b.results;
  const classes = trier(retenus, $('#bat-rank').value,
    x => ({gain: x.profit, roi: x.roi, win: x.win_probability || 0,
           stdev: x.stdev || 0.01}));
  const lignes = classes.map(x => `<tr>
    <td>${x.collection}${x.rarity ? ` <span class="tag">${x.rarity}</span>` : ''}</td>
    <td class="num">${x.outcomes}${x.all_profitable
      ? ' <span class="tag" style="background:#0f7a3d;color:#fff">toutes OK</span>' : ''}</td>
    <td class="num">${x.cost.toFixed(2)}</td>
    <td class="num ${x.profit >= 0 ? 'pos' : 'neg'}">${
      x.profit >= 0 ? '+' : ''}${x.profit.toFixed(2)}</td>
    <td class="num prof ${profClasse(x.profitability)}">${
      profTexte(x.profitability)}</td>
    <td class="num">${((x.win_probability || 0) * 100).toFixed(0)}%</td>
    <td><button class="ghost sm" data-voir="${x.plan_id}">Voir</button>
        <button class="sm" data-follow="${x.plan_id}">Suivre</button></td></tr>`).join('');

  $('#balayage').innerHTML = `<div class="card">
    <div class="row" style="justify-content:space-between">
      <b>Balayage ${b.rarity} — ${pct}%</b>
      ${fini ? '' : `<button class="ghost sm" data-stop="${b.id}">Arrêter</button>`}
    </div>
    <div class="bar"><i style="width:${pct}%"></i></div>
    <p class="muted">${b.results.length} plans calculés, ${b.failed.length} échecs,
      ${b.remaining} restantes &middot; ${Math.round(b.elapsed / 60)} min écoulées
      ${b.message ? '&middot; ' + b.message : ''}</p>
    ${lignes ? `<div class="scroll"><table>
      <thead><tr><th>Collection</th><th class="num">Sorties</th>
        <th class="num">Coût</th><th class="num">Profit</th>
        <th class="num">Profitabilité</th><th class="num">P(gain)</th>
        <th></th></tr></thead>
      <tbody>${lignes}</tbody></table></div>
      <p class="muted">${classes.length} sur ${b.results.length} affichés.
      Classé par ${$('#bat-rank').selectedOptions[0].textContent}.
      Montants en ${b.currency}. <b>P(gain)</b> est la probabilité que le tirage
      rapporte plus que les entrées n’ont coûté.</p>`
      : '<p class="muted">Aucun résultat pour l\u2019instant.</p>'}
  </div>`;
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
    afficher({id: '', plan_id: v.dataset.voir, plan: r.plan, archive: true});
    $('#sortie').scrollIntoView({behavior: 'smooth', block: 'start'});
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
$('#bat-rank').addEventListener('change', () => {
  if (dernierBatch) dessinerBatch(dernierBatch);
});
$('#bat-profitables').addEventListener('change', () => {
  if (dernierBatch) dessinerBatch(dernierBatch);
});
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
$('#rarity').addEventListener('change', charger);
$('#maxout').addEventListener('change', dessiner);
$('#filtre').addEventListener('input', dessiner);
charger();
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
