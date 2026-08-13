"""Journal persistant : plans calcules, contrats suivis, objets achetes.

Repond a un probleme concret de l'execution reelle. Avec plusieurs contrats
menes de front et un verrou d'echange de 7 jours PAR OBJET, il devient vite
impossible de savoir de tete quel skin appartient a quel contrat, ni quand
chacun devient utilisable.

Trois notions distinctes :

  plan     un calcul, fige a un instant. Perissable : les annonces bougent.
  contrat  un plan qu'on a decide de suivre. Vivant : on y rattache des achats.
  objet    un skin reellement achete, avec son prix et son float REELS.

La distinction plan/contrat porte l'essentiel de la valeur. Un plan dit ce
qu'il FAUDRAIT acheter ; un contrat enregistre ce qu'on a VRAIMENT achete. Les
deux divergent des qu'une annonce part et qu'on prend un substitut -- et c'est
precisement cette divergence qui change le float moyen, donc l'usure de sortie,
donc le gain. Le journal recalcule donc toujours a partir du reel.

Stockage separe du cache de prix : celui-ci est jetable et purgeable, le
journal est une donnee utilisateur qu'on ne doit jamais perdre.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "data" / "journal.db"

TRADE_LOCK_SECONDS = 7 * 24 * 3600

STATUTS = ("en_cours", "pret", "realise", "abandonne")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id              TEXT PRIMARY KEY,
    created_at      REAL NOT NULL,
    collection_id   TEXT NOT NULL,
    collection_name TEXT NOT NULL,
    rarity          TEXT NOT NULL,
    cost            REAL NOT NULL,
    net             REAL NOT NULL,
    profit          REAL NOT NULL,
    roi             REAL NOT NULL,
    avg_float       REAL NOT NULL,
    payload         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plans_date ON plans (created_at DESC);

CREATE TABLE IF NOT EXISTS contracts (
    id         TEXT PRIMARY KEY,
    plan_id    TEXT NOT NULL REFERENCES plans(id),
    created_at REAL NOT NULL,
    status     TEXT NOT NULL DEFAULT 'en_cours',
    notes      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_contracts_date ON contracts (created_at DESC);

CREATE TABLE IF NOT EXISTS items (
    id               TEXT PRIMARY KEY,
    contract_id      TEXT NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
    market_hash_name TEXT NOT NULL,
    float_value      REAL NOT NULL,
    price            REAL NOT NULL,
    listing_id       TEXT,
    purchased_at     REAL,
    from_plan        INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_items_contract ON items (contract_id);
"""


@dataclass(frozen=True, slots=True)
class Item:
    id: str
    contract_id: str
    market_hash_name: str
    float_value: float
    price: float
    listing_id: str | None
    purchased_at: float | None
    from_plan: bool

    @property
    def purchased(self) -> bool:
        return self.purchased_at is not None

    @property
    def tradable_at(self) -> float | None:
        """Date a laquelle l'objet sort du verrou d'echange."""
        if self.purchased_at is None:
            return None
        return self.purchased_at + TRADE_LOCK_SECONDS

    @property
    def locked(self) -> bool:
        t = self.tradable_at
        return t is not None and t > time.time()


@dataclass(frozen=True, slots=True)
class Contract:
    id: str
    plan_id: str
    created_at: float
    status: str
    notes: str
    items: tuple[Item, ...]
    collection_name: str
    planned_cost: float
    planned_avg_float: float
    planned_profit: float

    # --- Etat reel, recalcule depuis les achats ---

    @property
    def purchased(self) -> tuple[Item, ...]:
        return tuple(i for i in self.items if i.purchased)

    @property
    def spent(self) -> float:
        return sum(i.price for i in self.purchased)

    @property
    def complete(self) -> bool:
        return len(self.purchased) >= 10

    @property
    def actual_avg_float(self) -> float | None:
        """Moyenne des floats REELLEMENT achetes.

        C'est cette valeur qui determine l'usure de sortie, pas celle du plan.
        Elle derive des qu'un substitut remplace une annonce partie.
        """
        achetes = self.purchased
        if not achetes:
            return None
        return sum(i.float_value for i in achetes) / len(achetes)

    @property
    def float_drift(self) -> float | None:
        """Ecart entre la moyenne reelle et celle prevue."""
        reel = self.actual_avg_float
        return None if reel is None else reel - self.planned_avg_float

    @property
    def craftable_at(self) -> float | None:
        """Date a partir de laquelle le contrat peut etre execute.

        C'est le DERNIER objet achete qui commande : son verrou expire en
        dernier. Acheter etale coute donc des jours d'immobilisation.
        """
        if not self.complete:
            return None
        return max(i.tradable_at for i in self.purchased if i.tradable_at)

    @property
    def craftable(self) -> bool:
        d = self.craftable_at
        return d is not None and d <= time.time()


class Journal:
    """Acces au journal. Sans dependance : SQLite de la bibliotheque standard."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else DEFAULT_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # --- Plans ---------------------------------------------------------------

    def save_plan(self, payload: dict, *, collection_id: str, rarity: str) -> str:
        """Enregistre un plan calcule. Renvoie son identifiant."""
        pid = uuid.uuid4().hex[:12]
        self._conn.execute(
            """INSERT INTO plans (id, created_at, collection_id, collection_name,
                                  rarity, cost, net, profit, roi, avg_float, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pid, time.time(), collection_id, payload.get("collection", ""),
                rarity, payload.get("cost", 0.0), payload.get("net", 0.0),
                payload.get("profit", 0.0), payload.get("roi", 0.0),
                payload.get("avg_float", 0.0), json.dumps(payload),
            ),
        )
        self._conn.commit()
        return pid

    def plans(self, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            """SELECT p.*, (SELECT COUNT(*) FROM contracts c WHERE c.plan_id = p.id)
                      AS suivis
               FROM plans p ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "created_at": r["created_at"],
                "collection": r["collection_name"],
                "collection_id": r["collection_id"],
                "rarity": r["rarity"],
                "cost": r["cost"],
                "net": r["net"],
                "profit": r["profit"],
                "roi": r["roi"],
                "followed": r["suivis"],
            }
            for r in rows
        ]

    def plan_payload(self, plan_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT payload FROM plans WHERE id = ?", (plan_id,)
        ).fetchone()
        return json.loads(row["payload"]) if row else None

    # --- Contrats ------------------------------------------------------------

    def follow(self, plan_id: str) -> str:
        """Cree un contrat a partir d'un plan, avec ses 10 objets a acheter."""
        payload = self.plan_payload(plan_id)
        if payload is None:
            raise KeyError(f"plan inconnu : {plan_id}")

        cid = uuid.uuid4().hex[:12]
        self._conn.execute(
            "INSERT INTO contracts (id, plan_id, created_at) VALUES (?, ?, ?)",
            (cid, plan_id, time.time()),
        )
        for entree in payload.get("inputs", []):
            self._conn.execute(
                """INSERT INTO items (id, contract_id, market_hash_name,
                                      float_value, price, listing_id, from_plan)
                   VALUES (?, ?, ?, ?, ?, ?, 1)""",
                (
                    uuid.uuid4().hex[:12], cid, entree["name"],
                    entree["float"], entree["price"], entree.get("url", "").rsplit("/", 1)[-1]
                    if entree.get("url") else None,
                ),
            )
        self._conn.commit()
        return cid

    def mark_purchased(
        self, item_id: str, *, price: float | None = None,
        float_value: float | None = None, at: float | None = None,
    ) -> None:
        """Marque un objet comme achete, avec son prix et float REELS.

        Prix et float peuvent differer du plan : c'est le cas des qu'on prend
        un substitut. On enregistre ce qui a ete paye, pas ce qui etait prevu.
        """
        champs, valeurs = ["purchased_at = ?"], [at if at is not None else time.time()]
        if price is not None:
            champs.append("price = ?")
            valeurs.append(price)
        if float_value is not None:
            champs.append("float_value = ?")
            valeurs.append(float_value)
        valeurs.append(item_id)
        self._conn.execute(
            f"UPDATE items SET {', '.join(champs)} WHERE id = ?", valeurs
        )
        self._conn.commit()

    def unmark_purchased(self, item_id: str) -> None:
        self._conn.execute(
            "UPDATE items SET purchased_at = NULL WHERE id = ?", (item_id,)
        )
        self._conn.commit()

    def replace_item(
        self, item_id: str, *, name: str, float_value: float, price: float,
        listing_id: str | None = None,
    ) -> None:
        """Remplace un objet du plan par un substitut reellement achete."""
        self._conn.execute(
            """UPDATE items SET market_hash_name = ?, float_value = ?, price = ?,
                                listing_id = ?, from_plan = 0, purchased_at = ?
               WHERE id = ?""",
            (name, float_value, price, listing_id, time.time(), item_id),
        )
        self._conn.commit()

    def set_status(self, contract_id: str, status: str) -> None:
        if status not in STATUTS:
            raise ValueError(f"statut inconnu : {status} (connus : {STATUTS})")
        self._conn.execute(
            "UPDATE contracts SET status = ? WHERE id = ?", (status, contract_id)
        )
        self._conn.commit()

    def set_notes(self, contract_id: str, notes: str) -> None:
        self._conn.execute(
            "UPDATE contracts SET notes = ? WHERE id = ?", (notes, contract_id)
        )
        self._conn.commit()

    def delete_contract(self, contract_id: str) -> None:
        self._conn.execute("DELETE FROM items WHERE contract_id = ?", (contract_id,))
        self._conn.execute("DELETE FROM contracts WHERE id = ?", (contract_id,))
        self._conn.commit()

    def contract(self, contract_id: str) -> Contract | None:
        row = self._conn.execute(
            """SELECT c.*, p.collection_name, p.cost, p.avg_float, p.profit
               FROM contracts c JOIN plans p ON p.id = c.plan_id
               WHERE c.id = ?""",
            (contract_id,),
        ).fetchone()
        if row is None:
            return None
        return self._build_contract(row)

    def contracts(self, *, include_done: bool = True) -> list[Contract]:
        sql = """SELECT c.*, p.collection_name, p.cost, p.avg_float, p.profit
                 FROM contracts c JOIN plans p ON p.id = c.plan_id"""
        if not include_done:
            sql += " WHERE c.status NOT IN ('realise', 'abandonne')"
        sql += " ORDER BY c.created_at DESC"
        return [self._build_contract(r) for r in self._conn.execute(sql).fetchall()]

    def _build_contract(self, row: sqlite3.Row) -> Contract:
        items = tuple(
            Item(
                id=r["id"], contract_id=r["contract_id"],
                market_hash_name=r["market_hash_name"],
                float_value=r["float_value"], price=r["price"],
                listing_id=r["listing_id"], purchased_at=r["purchased_at"],
                from_plan=bool(r["from_plan"]),
            )
            for r in self._conn.execute(
                "SELECT * FROM items WHERE contract_id = ? ORDER BY rowid",
                (row["id"],),
            ).fetchall()
        )
        return Contract(
            id=row["id"], plan_id=row["plan_id"], created_at=row["created_at"],
            status=row["status"], notes=row["notes"], items=items,
            collection_name=row["collection_name"], planned_cost=row["cost"],
            planned_avg_float=row["avg_float"], planned_profit=row["profit"],
        )

    # --- Divers --------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        c = self._conn.execute
        return {
            "plans": c("SELECT COUNT(*) FROM plans").fetchone()[0],
            "contrats": c("SELECT COUNT(*) FROM contracts").fetchone()[0],
            "objets_achetes": c(
                "SELECT COUNT(*) FROM items WHERE purchased_at IS NOT NULL"
            ).fetchone()[0],
        }

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Journal":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
