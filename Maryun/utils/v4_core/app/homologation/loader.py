"""Load ``homologacion_productos.xlsx`` with validation.

Excel layout (sheet ``homologacion``)::

    sku_id_importado | sku_id_nacional | factor_conversion |
    usar_analitico   | usar_operacional | vigente_desde    |
    vigente_hasta    | responsable

Rules:
  * ``sku_id_importado`` must exist in products and be ``procedencia=importado``.
  * ``sku_id_nacional``  must exist in products and be ``procedencia=nacional``.
  * ``factor_conversion > 0``.
  * ``usar_analitico`` and ``usar_operacional`` are booleans; at least one
    must be true (otherwise the row is meaningless).
  * Date range, when present, must contain ``process_date``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from app.connectors.excel import ExcelError, ExcelReader
from app.normalize.canonical import canonical_sku
from app.normalize.schemas import HomologacionSchema
from app.utils.logging import get_logger


SHEET = "homologacion"
REQUIRED = [
    "sku_id_importado", "sku_id_nacional", "factor_conversion",
    "usar_analitico", "usar_operacional",
]


class HomologationError(ValueError):
    """Raised when the homologation file fails validation."""


@dataclass(frozen=True)
class HomologacionTable:
    """Validated in-memory view of the homologation file."""

    df: pd.DataFrame                       # schema-compliant rows (active only)
    pairs: list[tuple[str, str]]           # (importado, nacional) active pairs
    factor: dict[tuple[str, str], float]   # (imp, nac) -> factor_conversion
    analitico: set[tuple[str, str]]        # subset flagged usar_analitico
    operacional: set[tuple[str, str]]      # subset flagged usar_operacional

    def national_of(self, importado_sku: str) -> str | None:
        for (imp, nac) in self.pairs:
            if imp == importado_sku:
                return nac
        return None

    def is_empty(self) -> bool:
        return self.df.empty


def load_homologacion(
    path: Path,
    products: pd.DataFrame | None,
    process_date: date,
    *,
    strict: bool = True,
) -> HomologacionTable:
    """Load + validate the homologation Excel.

    If ``path`` does not exist and ``strict=False``, returns an empty table
    (useful for setups without homologation data yet).
    """
    log = get_logger("homologation.loader")

    if not path.exists():
        if strict:
            raise HomologationError(f"Homologation file missing: {path}")
        log.info("homologation.loader.missing_optional", path=str(path))
        return _empty_table()

    reader = ExcelReader()
    try:
        raw = reader.read_sheet(path, sheet=SHEET, required_columns=REQUIRED)
    except ExcelError as exc:
        if strict:
            raise HomologationError(str(exc)) from exc
        log.warning("homologation.loader.read_failed", error=str(exc))
        return _empty_table()

    df = raw.copy()
    df["sku_id_importado"] = df["sku_id_importado"].map(canonical_sku)
    df["sku_id_nacional"] = df["sku_id_nacional"].map(canonical_sku)
    df["factor_conversion"] = pd.to_numeric(df["factor_conversion"], errors="coerce")
    df["usar_analitico"] = _to_bool(df["usar_analitico"])
    df["usar_operacional"] = _to_bool(df["usar_operacional"])

    for col in ("vigente_desde", "vigente_hasta"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
        else:
            df[col] = pd.NaT

    if "responsable" not in df.columns:
        df["responsable"] = pd.NA

    # Row-level validation -------------------------------------------------
    errors: list[str] = []
    df = df.dropna(subset=["sku_id_importado", "sku_id_nacional"])

    bad_factor = df[df["factor_conversion"].isna() | (df["factor_conversion"] <= 0)]
    if not bad_factor.empty:
        errors.append(f"{len(bad_factor)} rows with invalid factor_conversion (must be > 0)")

    neither = df[~(df["usar_analitico"] | df["usar_operacional"])]
    if not neither.empty:
        errors.append(f"{len(neither)} rows with neither usar_analitico nor usar_operacional")

    dup = df.duplicated(subset=["sku_id_importado"], keep=False)
    if dup.any():
        errors.append(f"{int(dup.sum())} rows with duplicated sku_id_importado")

    self_map = df[df["sku_id_importado"] == df["sku_id_nacional"]]
    if not self_map.empty:
        errors.append(f"{len(self_map)} rows map a SKU to itself")

    # Product presence + PROCEDENCIA
    if products is not None and not products.empty:
        prod_idx = products.set_index("sku_id")["procedencia"] \
            if "procedencia" in products.columns else pd.Series(dtype=str)
        if not prod_idx.empty:
            miss_imp = set(df["sku_id_importado"]) - set(prod_idx.index)
            miss_nac = set(df["sku_id_nacional"]) - set(prod_idx.index)
            if miss_imp:
                errors.append(f"sku_id_importado not in products: {sorted(miss_imp)[:5]}…")
            if miss_nac:
                errors.append(f"sku_id_nacional not in products: {sorted(miss_nac)[:5]}…")

            # Procedencia coherencia
            wrong_imp = df[
                df["sku_id_importado"].isin(prod_idx.index)
                & (df["sku_id_importado"].map(prod_idx) != "importado")
            ]
            wrong_nac = df[
                df["sku_id_nacional"].isin(prod_idx.index)
                & (df["sku_id_nacional"].map(prod_idx) != "nacional")
            ]
            if not wrong_imp.empty:
                errors.append(f"{len(wrong_imp)} sku_id_importado rows are not procedencia=importado")
            if not wrong_nac.empty:
                errors.append(f"{len(wrong_nac)} sku_id_nacional rows are not procedencia=nacional")

    if errors and strict:
        raise HomologationError("; ".join(errors))
    for msg in errors:
        log.warning("homologation.loader.warning", detail=msg)

    # Filter by effective date -------------------------------------------
    pd_ts = pd.Timestamp(process_date)
    active = df.copy()
    active = active[
        (active["vigente_desde"].isna() | (active["vigente_desde"] <= pd_ts))
        & (active["vigente_hasta"].isna() | (active["vigente_hasta"] >= pd_ts))
    ].reset_index(drop=True)

    # Schema validation ---------------------------------------------------
    out = HomologacionSchema.validate(active, lazy=True)

    pairs = list(zip(out["sku_id_importado"], out["sku_id_nacional"]))
    factor = {(i, n): float(f) for i, n, f in zip(out["sku_id_importado"], out["sku_id_nacional"], out["factor_conversion"])}
    analitico = {(i, n) for (i, n), flag in zip(pairs, out["usar_analitico"]) if bool(flag)}
    operacional = {(i, n) for (i, n), flag in zip(pairs, out["usar_operacional"]) if bool(flag)}

    log.info(
        "homologation.loader.ok",
        pairs_total=len(pairs),
        analitico=len(analitico),
        operacional=len(operacional),
    )
    return HomologacionTable(
        df=out, pairs=pairs, factor=factor,
        analitico=analitico, operacional=operacional,
    )


def _to_bool(s: pd.Series) -> pd.Series:
    if s.dtype == bool:
        return s
    truthy = {"true", "1", "yes", "si", "sí", "y", "t", "x", "✓"}
    return s.astype(str).str.strip().str.lower().isin(truthy)


def _empty_table() -> HomologacionTable:
    return HomologacionTable(
        df=pd.DataFrame(columns=HomologacionSchema.columns.keys()),
        pairs=[], factor={}, analitico=set(), operacional=set(),
    )
