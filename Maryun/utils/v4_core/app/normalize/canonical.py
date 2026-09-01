"""Canonical string normalisation — lowercase, trim, strip accents.

Used whenever a raw column arrives from MariaDB or Excel with inconsistent
casing ("cd santiago" vs "CD SANTIAGO") and we want to compare or group.
"""
from __future__ import annotations

import unicodedata

# Locations in canonical upper form. Comparison should use ``canonical_location``.
SUCURSALES_CANONICAS: tuple[str, ...] = (
    "CASTRO",
    "CONCEPCION",
    "OSORNO",
    "PUERTO MONTT",
    "PUERTO VARAS",
    "QUELLON",
    "VALDIVIA",
    "LOS ANGELES EXPRESS",
    "MARKETPLACE",
    "SANTIAGO",
)

CDS_CANONICOS: tuple[str, ...] = ("CD SANTIAGO", "CD SUR")


def _strip_accents(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def canonical_location(value: str | None) -> str | None:
    """Uppercase, trim, remove accents. Returns ``None`` for null-ish input."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    return _strip_accents(s).upper()


def canonical_sku(value: str | int | None) -> str | None:
    """Normaliza un código SKU a su forma canónica.

    Elimina ceros iniciales de SKUs puramente numéricos para que la
    representación de la BD ("000329000247") y la del Excel leído sin
    ``dtype=str`` (329000247 como int64) sean idénticas al comparar.

    Ejemplos:
        "000329000247" → "329000247"
        329000247      → "329000247"
        "329000247"    → "329000247"
        "ABC-01"       → "ABC-01"   (no numérico: sin cambio)
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    # SKU puramente numérico → quitar ceros iniciales vía int()
    if s.isdigit():
        return str(int(s))
    return s


def canonical_procedencia(value: str | None) -> str | None:
    """Normalise PROCEDENCIA — only ``'nacional'`` / ``'importado'`` are valid."""
    c = canonical_location(value)
    if c is None:
        return None
    c = c.lower()
    if c.startswith("nac"):
        return "nacional"
    if c.startswith("imp"):
        return "importado"
    return c  # unknown; let downstream validation flag it


def is_sucursal(name: str | None) -> bool:
    c = canonical_location(name)
    return c in SUCURSALES_CANONICAS


def is_cd(name: str | None) -> bool:
    c = canonical_location(name)
    return c in CDS_CANONICOS
