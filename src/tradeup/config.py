"""Configuration et secrets.

La cle API CSFloat ne doit jamais atterrir dans le code ni dans un commit. Elle
est lue depuis la variable d'environnement `CSFLOAT_API_KEY`, ou a defaut depuis
un fichier `.env` a la racine du projet (ignore par git).
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"

CSFLOAT_KEY_VAR = "CSFLOAT_API_KEY"


def load_env_file(path: Path | None = None) -> dict[str, str]:
    """Lit un fichier `.env` simple (KEY=value, # pour les commentaires)."""
    p = path or ENV_FILE
    if not p.exists():
        return {}
    values: dict[str, str] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def csfloat_api_key(explicit: str | None = None) -> str | None:
    """Cle API CSFloat, ou None si non configuree.

    Ordre de priorite : argument explicite, variable d'environnement, `.env`.
    """
    if explicit:
        return explicit
    from_env = os.getenv(CSFLOAT_KEY_VAR)
    if from_env:
        return from_env
    return load_env_file().get(CSFLOAT_KEY_VAR)


def mask(secret: str | None) -> str:
    """Represente une cle sans la divulguer, pour les logs et messages."""
    if not secret:
        return "(absente)"
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}...{secret[-4:]}"
