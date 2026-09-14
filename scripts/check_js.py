"""Verifie la syntaxe du JavaScript embarque dans les pages Python.

Le JS de `web.py` et `report.py` vit dans des chaines Python : aucun test ne
l'execute, une faute de frappe ne se voit qu'a l'ouverture du navigateur.

    python scripts/check_js.py

Necessite node. Sortie 0 si tout va bien.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path


def extraire(source: str, nom: str) -> list[tuple[str, str]]:
    return [(f"{nom}#{i}", m) for i, m in
            enumerate(re.findall(r"<script>(.*?)</script>", source, re.S), 1)]


def main() -> int:
    from tradeup.report import _JS
    from tradeup.web import PAGE

    blocs = extraire(PAGE, "web.PAGE") + [("report._JS", _JS)]
    if not blocs:
        print("aucun bloc <script> trouve", file=sys.stderr)
        return 1

    echecs = 0
    with tempfile.TemporaryDirectory() as tmp:
        for nom, js in blocs:
            f = Path(tmp) / "bloc.js"
            f.write_text(js, encoding="utf-8")
            r = subprocess.run(["node", "--check", str(f)],
                               capture_output=True, text=True)
            if r.returncode:
                echecs += 1
                print(f"[KO] {nom}\n{r.stderr.strip()}", file=sys.stderr)
            else:
                print(f"[OK] {nom} ({len(js)} octets)")
    return 1 if echecs else 0


if __name__ == "__main__":
    raise SystemExit(main())
