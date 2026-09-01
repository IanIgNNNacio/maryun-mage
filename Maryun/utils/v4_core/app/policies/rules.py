"""SKU × location rules — hard constraints + soft overrides."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from app.connectors.excel import ExcelError, ExcelReader
from app.normalize.canonical import canonical_location, canonical_sku
from app.utils.logging import get_logger


SHEET = "reglas_sku_sucursal"
# Acepta "sku" (nuevo) o "sku_id" (legado) — se resuelve en el loader.
REQUIRED = ["ubicacion"]   # sku / sku_id se valida manualmente
OPTIONAL = [
    "bloqueado",            # bool — hard ban; no movement can be generated
    "stock_minimo",         # float — overrides the engine-computed min
    "stock_maximo",         # float — overrides the engine-computed max
    "solo_desde_cd",        # string (canonical CD) or "" — if set, restrict origin
    "nota",                 # free text
]


@dataclass(frozen=True)
class Rule:
    bloqueado: bool = False
    stock_minimo: float | None = None
    stock_maximo: float | None = None
    solo_desde_cd: str | None = None
    nota: str = ""


@dataclass(frozen=True)
class SkuLocationRules:
    df: pd.DataFrame
    by_pair: dict[tuple[str, str], Rule]

    def rule(self, sku_id: str, ubicacion: str) -> Rule:
        return self.by_pair.get(
            (canonical_sku(sku_id), canonical_location(ubicacion)),
            Rule(),
        )

    def is_empty(self) -> bool:
        return self.df.empty


_TRUTHY = {"true", "1", "yes", "si", "sí", "y", "t", "x", "✓"}


def _to_bool(v) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s in _TRUTHY


def _to_float_or_none(v) -> float | None:
    try:
        x = float(v)
        if pd.isna(x):
            return None
        return x
    except (TypeError, ValueError):
        return None


def load_sku_location_rules(path: Path, *, strict: bool = False) -> SkuLocationRules:
    log = get_logger("policies.rules")
    columns = REQUIRED + OPTIONAL
    empty = pd.DataFrame(columns=columns)

    if not path.exists():
        if strict:
            raise FileNotFoundError(f"SKU/location rules file missing: {path}")
        log.info("policies.rules.optional_missing", path=str(path))
        return SkuLocationRules(df=empty, by_pair={})

    try:
        df = ExcelReader().read_sheet(path, sheet=SHEET, required_columns=REQUIRED)
    except ExcelError as exc:
        if strict:
            raise
        log.warning("policies.rules.read_failed", error=str(exc))
        return SkuLocationRules(df=empty, by_pair={})

    df = df.copy()
    # Normalizar columna identificadora: acepta "sku" (nuevo) o "sku_id" (legado).
    if "sku" in df.columns and "sku_id" not in df.columns:
        df = df.rename(columns={"sku": "sku_id"})
    if "sku_id" not in df.columns:
        log.warning("policies.rules.missing_sku_column", columns=list(df.columns))
        return SkuLocationRules(df=empty, by_pair={})
    df["sku_id"] = df["sku_id"].map(canonical_sku)
    df["ubicacion"] = df["ubicacion"].map(canonical_location)
    for col in OPTIONAL:
        if col not in df.columns:
            df[col] = pd.NA

    by_pair: dict[tuple[str, str], Rule] = {}
    for r in df.itertuples(index=False):
        solo_cd = r.solo_desde_cd
        solo_cd_clean = (
            canonical_location(solo_cd)
            if isinstance(solo_cd, str) and solo_cd.strip() != "" and not pd.isna(solo_cd)
            else None
        )
        rule = Rule(
            bloqueado=_to_bool(r.bloqueado),
            stock_minimo=_to_float_or_none(r.stock_minimo),
            stock_maximo=_to_float_or_none(r.stock_maximo),
            solo_desde_cd=solo_cd_clean,
            nota=str(r.nota) if r.nota is not None and not (isinstance(r.nota, float) and pd.isna(r.nota)) else "",
        )
        by_pair[(r.sku_id, r.ubicacion)] = rule

    log.info("policies.rules.loaded", pairs=len(by_pair), rows=len(df))
    return SkuLocationRules(df=df[columns].reset_index(drop=True), by_pair=by_pair)
