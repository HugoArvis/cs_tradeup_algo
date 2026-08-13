"""Chargement et interrogation de la base statique collections / skins."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .models import Collection, Rarity, Skin

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "collections.json"


@dataclass(slots=True)
class SkinDatabase:
    """Index en memoire des collections et de leurs skins."""

    collections: dict[str, Collection]
    _by_key: dict[str, Skin]
    _by_market_name: dict[str, Skin]
    source_version: str = "unknown"

    @classmethod
    def load(cls, path: Path | str | None = None) -> "SkinDatabase":
        p = Path(path) if path else DEFAULT_DB_PATH
        if not p.exists():
            raise FileNotFoundError(
                f"Base introuvable : {p}\n"
                "Genere-la avec :  python -m scripts.build_db"
            )
        raw = json.loads(p.read_text(encoding="utf-8"))
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict) -> "SkinDatabase":
        collections: dict[str, Collection] = {}
        by_key: dict[str, Skin] = {}
        by_market: dict[str, Skin] = {}

        for cdata in raw.get("collections", []):
            skins: list[Skin] = []
            for sdata in cdata.get("skins", []):
                skin = Skin(
                    key=sdata["key"],
                    name=sdata["name"],
                    collection_id=cdata["id"],
                    rarity=Rarity.from_label(sdata["rarity"]),
                    min_float=float(sdata["min_float"]),
                    max_float=float(sdata["max_float"]),
                    stattrak=bool(sdata.get("stattrak", False)),
                    souvenir=bool(sdata.get("souvenir", False)),
                )
                skins.append(skin)
                by_key[skin.key] = skin
                by_market[skin.name] = skin
            col = Collection(id=cdata["id"], name=cdata["name"], skins=tuple(skins))
            collections[col.id] = col

        return cls(
            collections=collections,
            _by_key=by_key,
            _by_market_name=by_market,
            source_version=raw.get("version", "unknown"),
        )

    # --- Acces ---

    def skin(self, key: str) -> Skin:
        return self._by_key[key]

    def find(self, name: str) -> Skin | None:
        """Recherche par nom exact ('AK-47 | Redline') puis approximative."""
        if name in self._by_market_name:
            return self._by_market_name[name]
        needle = name.strip().lower()
        for n, s in self._by_market_name.items():
            if n.lower() == needle:
                return s
        matches = [s for n, s in self._by_market_name.items() if needle in n.lower()]
        return matches[0] if len(matches) == 1 else None

    def collection(self, cid: str) -> Collection:
        return self.collections[cid]

    def find_collection(self, name: str) -> Collection | None:
        needle = name.strip().lower()
        for c in self.collections.values():
            if c.id.lower() == needle or c.name.lower() == needle:
                return c
        matches = [c for c in self.collections.values() if needle in c.name.lower()]
        return matches[0] if len(matches) == 1 else None

    def __iter__(self) -> Iterator[Collection]:
        return iter(self.collections.values())

    def __len__(self) -> int:
        return len(self.collections)

    @property
    def skin_count(self) -> int:
        return len(self._by_key)

    # --- Requetes utiles au scan ---

    def tradeable_collections(self, input_rarity: Rarity) -> list[Collection]:
        """Collections utilisables en entree ET ayant des sorties a la rarete cible.

        Une collection sans sortie a la rarete superieure ne produit rien : ses
        entrees seraient du cout pur. On les ecarte d'office.
        """
        return [
            c
            for c in self.collections.values()
            if c.by_rarity(input_rarity) and c.outcomes_for_input_rarity(input_rarity)
        ]

    def outcomes_map(
        self, collections: Iterable[Collection], input_rarity: Rarity
    ) -> dict[str, tuple[Skin, ...]]:
        return {
            c.id: c.outcomes_for_input_rarity(input_rarity) for c in collections
        }
