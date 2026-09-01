"""Supplier table — SKU → lista de proveedores con lead time por sucursal.

El Excel ``proveedores.xlsx`` soporta:

* **Múltiples proveedores por SKU** vía columna ``prioridad``
  (1 = principal, 2 = alternativo, etc.).
* **Lead time por sucursal** vía columna opcional ``ubicacion``.
  - Fila sin ``ubicacion`` (vacío / NaN) → aplica como *default* a todas las
    sucursales que no tengan fila específica.
  - Fila con ``ubicacion`` → sobreescribe el default para esa sucursal concreta.

Ejemplo:

    sku            | proveedor   | ubicacion  | lead_time_dias | prioridad
    000131000131   | Proveedor A |            | 5              | 1   ← default
    000131000131   | Proveedor A | QUELLON    | 20             | 1   ← override Quellon
    000131000131   | Proveedor A | CASTRO     | 25             | 1   ← override Castro
    000131000131   | Proveedor B |            | 15             | 2   ← alternativo

Columna identificadora: ``sku`` (código operacional).
Se acepta también ``sku_id`` por retrocompatibilidad.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from app.connectors.excel import ExcelError, ExcelReader
from app.normalize.canonical import canonical_location, canonical_sku
from app.utils.logging import get_logger


SHEET = "proveedores"
REQUIRED_BASE = ["proveedor", "lead_time_dias"]
OPTIONAL = [
    "ubicacion",          # si se especifica → lead time por sucursal
    "moq",
    "multiplo_compra",
    "incoterm",
    "procedencia",
    "costo_unitario_clp",
    "prioridad",
]


@dataclass(frozen=True)
class SupplierInfo:
    proveedor: str
    lead_time_dias: int
    moq: float
    multiplo_compra: float
    incoterm: str
    procedencia: str
    costo_unitario_clp: float
    prioridad: int = 1
    ubicacion: str | None = None   # None = aplica a todas las sucursales


@dataclass(frozen=True)
class SupplierTable:
    df: pd.DataFrame
    # Clave: (sku_code, ubicacion_canonica | None)
    # None = default para todas las sucursales no especificadas
    by_sku_loc: dict[tuple[str, str | None], list[SupplierInfo]]

    def info(self, sku_id: str, ubicacion: str | None = None) -> SupplierInfo | None:
        """Devuelve el proveedor principal para el SKU+sucursal dados.

        Lógica de resolución (en orden):
        1. Busca (sku, ubicacion_canonica) → fila específica para la sucursal.
        2. Busca (sku, None)              → fila default para todas las sucursales.
        3. Devuelve ``None`` si el SKU no está en la tabla.
        """
        sku = canonical_sku(sku_id)
        ub = canonical_location(ubicacion) if ubicacion else None

        # 1. Intento específico por sucursal
        if ub is not None:
            specific = self.by_sku_loc.get((sku, ub))
            if specific:
                return specific[0]

        # 2. Fallback al default (None = todas las sucursales)
        default = self.by_sku_loc.get((sku, None))
        return default[0] if default else None

    def all_suppliers(
        self, sku_id: str, ubicacion: str | None = None
    ) -> list[SupplierInfo]:
        """Devuelve todos los proveedores para el SKU+sucursal, en orden de prioridad."""
        sku = canonical_sku(sku_id)
        ub = canonical_location(ubicacion) if ubicacion else None

        specific = self.by_sku_loc.get((sku, ub), []) if ub else []
        default = self.by_sku_loc.get((sku, None), [])

        # Combina: específicos primero, luego los default que no dupliquen proveedor
        seen = {s.proveedor for s in specific}
        combined = list(specific) + [s for s in default if s.proveedor not in seen]
        return sorted(combined, key=lambda s: s.prioridad)

    def is_empty(self) -> bool:
        return self.df.empty


def _to_float(v, default: float = 0.0) -> float:
    try:
        x = float(v)
        return default if pd.isna(x) else x
    except (TypeError, ValueError):
        return default


def _to_int(v, default: int = 0) -> int:
    try:
        x = float(v)
        return default if pd.isna(x) else int(x)
    except (TypeError, ValueError):
        return default


def load_suppliers(path: Path, *, strict: bool = False) -> SupplierTable:
    log = get_logger("policies.suppliers")
    empty = pd.DataFrame(columns=["sku_id", "proveedor", "lead_time_dias"])

    if not path.exists():
        if strict:
            raise FileNotFoundError(f"Suppliers file missing: {path}")
        log.info("policies.suppliers.optional_missing", path=str(path))
        return SupplierTable(df=empty, by_sku_loc={})

    try:
        # dtype=str en las columnas sku/sku_id: pandas inferiría int64 desde
        # "000329000247" → 329000247, perdiendo los ceros iniciales.
        raw = ExcelReader().read_sheet(
            path, sheet=SHEET, required_columns=REQUIRED_BASE,
            dtype={"sku": str, "sku_id": str},
        )
    except ExcelError as exc:
        if strict:
            raise
        log.warning("policies.suppliers.read_failed", error=str(exc))
        return SupplierTable(df=empty, by_sku_loc={})

    df = raw.copy()

    # ── Columna identificadora ──────────────────────────────────────────────
    if "sku" in df.columns and "sku_id" not in df.columns:
        df = df.rename(columns={"sku": "sku_id"})
    if "sku_id" not in df.columns:
        log.warning("policies.suppliers.missing_sku_column", columns=list(df.columns))
        return SupplierTable(df=empty, by_sku_loc={})

    df["sku_id"] = df["sku_id"].map(canonical_sku)
    df["proveedor"] = df["proveedor"].astype("string").str.strip()
    df["lead_time_dias"] = (
        pd.to_numeric(df["lead_time_dias"], errors="coerce").fillna(0).astype(int)
    )

    # ── Columna ubicacion (opcional) ────────────────────────────────────────
    if "ubicacion" in df.columns:
        df["ubicacion"] = df["ubicacion"].apply(
            lambda v: canonical_location(str(v)) if pd.notna(v) and str(v).strip() != "" else None
        )
    else:
        df["ubicacion"] = None

    # ── Columnas numéricas opcionales ───────────────────────────────────────
    for col, default in (("moq", 0.0), ("multiplo_compra", 1.0), ("costo_unitario_clp", 0.0)):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(default).astype(float)
        else:
            df[col] = default

    for col, default in (("incoterm", ""), ("procedencia", "")):
        if col in df.columns:
            df[col] = df[col].astype("string").fillna(default).str.strip()
        else:
            df[col] = default

    if "prioridad" in df.columns:
        df["prioridad"] = (
            pd.to_numeric(df["prioridad"], errors="coerce").fillna(1).astype(int)
        )
    else:
        df["prioridad"] = 1

    df = df.dropna(subset=["sku_id", "proveedor"])
    # Ordenar: primero específicos (ubicacion != None), luego default; dentro de cada
    # grupo, por prioridad ascendente.
    df = df.sort_values(
        ["sku_id", "prioridad"],
        ascending=True,
        na_position="last",
    )

    # ── Construir índice de búsqueda ────────────────────────────────────────
    by_sku_loc: dict[tuple[str, str | None], list[SupplierInfo]] = {}
    for r in df.itertuples(index=False):
        key = (r.sku_id, r.ubicacion)   # ubicacion puede ser None (default)
        info = SupplierInfo(
            proveedor=str(r.proveedor),
            lead_time_dias=_to_int(r.lead_time_dias),
            moq=_to_float(r.moq),
            multiplo_compra=_to_float(r.multiplo_compra, default=1.0),
            incoterm=str(r.incoterm or ""),
            procedencia=str(r.procedencia or ""),
            costo_unitario_clp=_to_float(r.costo_unitario_clp),
            prioridad=_to_int(r.prioridad, default=1),
            ubicacion=r.ubicacion,
        )
        by_sku_loc.setdefault(key, []).append(info)

    skus_total = len({k[0] for k in by_sku_loc})
    skus_con_loc = len({k[0] for k in by_sku_loc if k[1] is not None})
    log.info(
        "policies.suppliers.loaded",
        rows=len(df),
        skus=skus_total,
        skus_con_lead_time_por_sucursal=skus_con_loc,
    )

    export_cols = [
        "sku_id", "proveedor", "ubicacion", "lead_time_dias",
        "moq", "multiplo_compra", "incoterm", "procedencia",
        "costo_unitario_clp", "prioridad",
    ]
    present = [c for c in export_cols if c in df.columns]
    return SupplierTable(df=df[present].reset_index(drop=True), by_sku_loc=by_sku_loc)
