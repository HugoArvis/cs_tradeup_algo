"""Cache disque des releves de prix (SQLite).

Deux roles :
  - eviter de retaper l'API Steam, qui est severement rate-limitee ;
  - garder un historique, utile pour reperer les prix instables avant de
    valider un trade-up.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path

from .base import Quote

DEFAULT_CACHE_PATH = Path(__file__).resolve().parents[3] / "data" / "prices.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes (
    market_hash_name TEXT NOT NULL,
    source           TEXT NOT NULL,
    lowest_price     REAL,
    median_price     REAL,
    volume           INTEGER,
    currency         TEXT NOT NULL,
    fetched_at       REAL NOT NULL,
    PRIMARY KEY (market_hash_name, source, fetched_at)
);
CREATE INDEX IF NOT EXISTS idx_quotes_latest
    ON quotes (market_hash_name, source, fetched_at DESC);
"""


class QuoteCache:
    """Stockage des cotations avec expiration par TTL."""

    def __init__(self, path: Path | str | None = None, ttl_seconds: float = 6 * 3600):
        self.path = Path(path) if path else DEFAULT_CACHE_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl_seconds
        self._conn = sqlite3.connect(str(self.path))
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def get(
        self,
        name: str,
        source: str,
        ttl: float | None = None,
        currency: str | None = None,
    ) -> Quote | None:
        """Derniere cotation non expiree, ou None.

        `currency` fait partie de l'identite d'une cotation : 16.42 EUR et
        16.42 USD ne sont pas le meme prix. Sans ce filtre, changer de devise
        renvoyait silencieusement les montants de l'ancienne -- et un calcul
        achat-Steam/revente-CSFloat comparait des euros a des dollars.
        """
        cutoff = time.time() - (self.ttl if ttl is None else ttl)
        sql = """SELECT market_hash_name, source, lowest_price, median_price,
                        volume, currency, fetched_at
                 FROM quotes
                 WHERE market_hash_name = ? AND source = ? AND fetched_at >= ?"""
        params: list[object] = [name, source, cutoff]
        if currency is not None:
            sql += " AND currency = ?"
            params.append(currency)
        sql += " ORDER BY fetched_at DESC LIMIT 1"
        row = self._conn.execute(sql, params).fetchone()
        return _row_to_quote(row) if row else None

    def put(self, quote: Quote) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO quotes
               (market_hash_name, source, lowest_price, median_price,
                volume, currency, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                quote.market_hash_name,
                quote.source,
                quote.lowest_price,
                quote.median_price,
                quote.volume,
                quote.currency,
                quote.fetched_at,
            ),
        )
        self._conn.commit()

    def put_many(self, quotes: Iterable[Quote]) -> None:
        for q in quotes:
            self.put(q)

    def history(self, name: str, source: str, limit: int = 50) -> list[Quote]:
        rows = self._conn.execute(
            """SELECT market_hash_name, source, lowest_price, median_price,
                      volume, currency, fetched_at
               FROM quotes WHERE market_hash_name = ? AND source = ?
               ORDER BY fetched_at DESC LIMIT ?""",
            (name, source, limit),
        ).fetchall()
        return [_row_to_quote(r) for r in rows]

    def prune(self, older_than_days: float = 30) -> int:
        cutoff = time.time() - older_than_days * 86400
        cur = self._conn.execute("DELETE FROM quotes WHERE fetched_at < ?", (cutoff,))
        self._conn.commit()
        return cur.rowcount

    def stats(self) -> dict[str, int]:
        total = self._conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
        distinct = self._conn.execute(
            "SELECT COUNT(DISTINCT market_hash_name) FROM quotes"
        ).fetchone()[0]
        fresh = self._conn.execute(
            "SELECT COUNT(DISTINCT market_hash_name) FROM quotes WHERE fetched_at >= ?",
            (time.time() - self.ttl,),
        ).fetchone()[0]
        return {"releves": total, "objets": distinct, "frais": fresh}

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "QuoteCache":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _row_to_quote(row: tuple) -> Quote:
    return Quote(
        market_hash_name=row[0],
        source=row[1],
        lowest_price=row[2],
        median_price=row[3],
        volume=row[4],
        currency=row[5],
        fetched_at=row[6],
    )
