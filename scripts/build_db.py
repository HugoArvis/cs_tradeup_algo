"""Construit data/collections.json a partir du dataset communautaire CSGO-API.

Source : https://github.com/ByMykel/CSGO-API (derive des fichiers du jeu,
regenere a chaque mise a jour de CS2). C'est la reference la plus fiable pour
les couples (skin, collection, rarete, min_float, max_float).

Usage :
    python -m scripts.build_db                 # telecharge et ecrit data/collections.json
    python -m scripts.build_db --from skins.json  # depuis un fichier local

On ne conserve que ce qui est pertinent pour un trade-up :
  - categorie arme (les couteaux/gants ne sont ni entree ni sortie valide),
  - rarete dans l'echelle Consumer..Covert,
  - un range de float exploitable,
  - une collection connue.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

SOURCE_URL = (
    "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/skins.json"
)
OUT_PATH = Path(__file__).resolve().parents[1] / "data" / "collections.json"

VALID_RARITIES = {
    "Consumer Grade",
    "Industrial Grade",
    "Mil-Spec Grade",
    "Restricted",
    "Classified",
    "Covert",
}
# Les couteaux et gants portent des raretes speciales, mais on filtre aussi par
# categorie au cas ou la source changerait ses libelles.
EXCLUDED_CATEGORIES = {"Knives", "Gloves"}


def fetch(url: str) -> list[dict]:
    print(f"Telechargement de {url} ...", file=sys.stderr)
    req = urllib.request.Request(url, headers={"User-Agent": "cs-tradeup-algo/0.1"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def slugify(name: str) -> str:
    out = []
    for ch in name.lower():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-")


def normalize(raw_skins: list[dict]) -> dict:
    by_collection: dict[str, dict] = {}
    seen_keys: set[str] = set()
    stats: dict[str, int] = defaultdict(int)

    for item in raw_skins:
        rarity = (item.get("rarity") or {}).get("name")
        if rarity not in VALID_RARITIES:
            stats["rarete_hors_echelle"] += 1
            continue

        category = (item.get("category") or {}).get("name")
        if category in EXCLUDED_CATEGORIES:
            stats["categorie_exclue"] += 1
            continue

        min_f, max_f = item.get("min_float"), item.get("max_float")
        if min_f is None or max_f is None or not (0 <= min_f < max_f <= 1):
            stats["float_invalide"] += 1
            continue

        collections = item.get("collections") or []
        if not collections:
            stats["sans_collection"] += 1
            continue

        # Le nom marchand exclut l'usure : "AK-47 | Redline".
        name = item.get("name") or ""
        if "(" in name:
            name = name.split("(")[0].strip()
        if not name:
            stats["sans_nom"] += 1
            continue

        for col in collections:
            cid = col.get("id") or slugify(col.get("name", ""))
            cname = col.get("name") or cid
            bucket = by_collection.setdefault(cid, {"id": cid, "name": cname, "skins": []})

            key = f"{cid}::{slugify(name)}"
            if key in seen_keys:
                continue
            seen_keys.add(key)

            bucket["skins"].append(
                {
                    "key": key,
                    "name": name,
                    "rarity": rarity,
                    "min_float": round(float(min_f), 6),
                    "max_float": round(float(max_f), 6),
                    "stattrak": bool(item.get("stattrak", False)),
                    "souvenir": bool(item.get("souvenir", False)),
                }
            )
            stats["retenus"] += 1

    for bucket in by_collection.values():
        bucket["skins"].sort(key=lambda s: (s["rarity"], s["name"]))

    return {
        "version": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "source": SOURCE_URL,
        "collections": sorted(by_collection.values(), key=lambda c: c["name"]),
        "_stats": dict(stats),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from", dest="src", help="fichier skins.json local")
    ap.add_argument("--out", default=str(OUT_PATH), help="chemin de sortie")
    ap.add_argument("--url", default=SOURCE_URL)
    args = ap.parse_args()

    raw = (
        json.loads(Path(args.src).read_text(encoding="utf-8"))
        if args.src
        else fetch(args.url)
    )
    if isinstance(raw, dict):  # certaines versions renvoient un dict indexe
        raw = list(raw.values())

    db = normalize(raw)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(db, ensure_ascii=False, indent=1), encoding="utf-8")

    n_skins = sum(len(c["skins"]) for c in db["collections"])
    print(f"OK : {len(db['collections'])} collections, {n_skins} skins -> {out}")
    print(f"     filtres : {db['_stats']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
