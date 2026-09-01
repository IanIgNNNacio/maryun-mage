"""Lector primario de RESULTADO_ESTRATEGICO_ABC_XYZ_v3.xlsx.

Este modulo es la UNICA fuente de verdad de la clasificacion de productos.
NO se calcula ni se infiere clasificacion desde otras fuentes — todo viene
exclusivamente del archivo v3.

Flujo:
    1. Cargar v3 (hoja 'Analisis') con TODOS los campos estrategicos
    2. Cruzar con catalogo de productos (por nombre exacto) para obtener sku_id
    3. (Opcional) Aplicar override manual SOLO a pares (sku, sucursal)
       que YA existen en la clasificacion base v3

Productos NO presentes en v3:
    - No reciben clasificacion
    - No pasan a las etapas posteriores del pipeline
    - El usuario debe agregarlos al v3 si necesita clasificarlos
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.utils.logging import get_logger


# ---------------------------------------------------------------------------
# Configuracion de columnas estrategicas a extraer del v3
# ---------------------------------------------------------------------------

# Identificacion (obligatorias para el cruce)
# SUCURSAL fue removida del archivo v3 en jun-2026; se auto-detecta desde SKU 3.0.
_ID_COLS = ["SKU 3.0"]

# Clasificaciones (obligatorias - se validan)
_CLASS_COLS = ["ABC", "XYZ", "MATRIZ_FINAL"]

# Campos estrategicos adicionales (opcionales - se cargan si existen)
_STRATEGIC_COLS = [
    "ABC_MARGEN",           # ABC basado solo en margen
    "ABC_VOLUMEN",          # ABC basado solo en volumen
    "MARGEN",               # margen total
    "%_Margen",             # contribucion al margen
    "SCORE_AUTOMATIZACION", # score 0-100 de automatizacion
    "CLASE_AUTOMATIZACION", # AUTOMATIZAR / SEMI-AUTO / MANUAL
    "SCORE_CONFIANZA",      # confianza del modelo
    "ESTRATEGIA",           # estrategia textual recomendada
    "PATRON_DEMANDA",       # ERRATICA / SUAVE / etc.
    "CICLO_VIDA",           # ACTIVO / NUEVO / DESCONTINUADO
    "FRECUENCIA_VENTA",     # % meses con venta
    "ROTACION_CONSTANTE",   # bool
    "TENDENCIA",            # ALZA / BAJA / ESTABLE
    "FLAG_NUEVO",
    "FLAG_DESCONTINUADO",
    "FLAG_SIN_VENTA",
    "FLAG_ESTACIONAL",
    "Demanda_Total",
    "CV_Reciente",
]

_ABC_RANK = {"A": 0, "B": 1, "C": 2}


# ---------------------------------------------------------------------------
# Carga del archivo v3
# ---------------------------------------------------------------------------

def _load_v3_raw(path: Path) -> pd.DataFrame:
    """Lee y valida la hoja Analisis del v3."""
    log = get_logger("classification.v3_loader")
    df = pd.read_excel(path, sheet_name="Analisis", dtype=str)

    # Validar columnas obligatorias
    required = set(_ID_COLS + _CLASS_COLS)
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"v3 file missing required columns: {missing}. "
            f"File: {path}"
        )

    # Normalizar clasificaciones (uppercase + strip)
    for col in _CLASS_COLS:
        df[col] = df[col].str.strip().str.upper()

    # Validar valores ABC/XYZ
    bad_abc = ~df["ABC"].isin(["A", "B", "C"])
    bad_xyz = ~df["XYZ"].isin(["X", "Y", "Z"])
    n_bad = int(bad_abc.sum() + bad_xyz.sum())
    if n_bad > 0:
        log.warning("v3.invalid_class_dropped", count=n_bad)
        df = df[~bad_abc & ~bad_xyz].copy()

    return df


def _extract_nombre_base(df: pd.DataFrame) -> pd.DataFrame:
    """Quita el sufijo SUCURSAL de SKU 3.0 para recuperar el nombre del producto.

    Si la columna SUCURSAL existe, la usa directamente.
    Si no existe (archivo v3 jun-2026+), detecta la sucursal desde SUCURSALES_CANONICAS.
    """
    from app.normalize.canonical import SUCURSALES_CANONICAS

    df = df.copy()
    df["SKU 3.0"] = df["SKU 3.0"].astype(str).str.strip()

    if "SUCURSAL" in df.columns:
        df["SUCURSAL"] = df["SUCURSAL"].astype(str).str.strip()

        def _extract(row: pd.Series) -> str | None:
            sku3 = row["SKU 3.0"]
            suc = row["SUCURSAL"]
            if sku3.endswith(suc):
                base = sku3[: -len(suc)].strip()
                return base if base else None
            return None

        df["nombre_base"] = df.apply(_extract, axis=1)
    else:
        # Ordenar por largo desc para que "LOS ANGELES EXPRESS" gane sobre "EXPRESSS"
        sucursales_sorted = sorted(SUCURSALES_CANONICAS, key=len, reverse=True)

        def _detect(sku3: str) -> tuple[str | None, str | None]:
            upper = sku3.upper()
            for suc in sucursales_sorted:
                if upper.endswith(suc):
                    base = sku3[: len(sku3) - len(suc)].strip()
                    return (base if base else None), suc
            return None, None

        detected = df["SKU 3.0"].apply(_detect)
        df["nombre_base"] = detected.apply(lambda x: x[0])
        df["SUCURSAL"] = detected.apply(lambda x: x[1])

    return df[df["nombre_base"].notna()].copy()


# ---------------------------------------------------------------------------
# Construccion de la clasificacion base desde v3
# ---------------------------------------------------------------------------

def load_v3_classification(
    path: Path,
    products: pd.DataFrame,
) -> pd.DataFrame:
    """Construye la clasificacion base desde v3 (FUENTE PRIMARIA Y UNICA).

    Args:
        path: Ruta a RESULTADO_ESTRATEGICO_ABC_XYZ_v3.xlsx.
        products: Catalogo de productos con columnas (sku_id, nombre).

    Returns:
        DataFrame con una fila por par (sku_id, ubicacion) presente en v3.
        Columnas (compatibles con ClassificationSchema + campos estrategicos):
            sku_id, ubicacion,
            abc_modelo, xyz_modelo, clase_modelo,
            abc_margen, abc_volumen, margen, pct_margen,
            score_automatizacion, clase_automatizacion,
            score_confianza, estrategia, patron_demanda,
            ciclo_vida, frecuencia_venta, rotacion_constante,
            tendencia, flag_nuevo, flag_descontinuado,
            flag_sin_venta, flag_estacional,
            demanda_total, cv_reciente,
            fuente_clasificacion='v3_estrategico'

    Raises:
        FileNotFoundError: si el archivo v3 no existe.
        ValueError: si faltan columnas obligatorias en v3.
    """
    log = get_logger("classification.v3_loader")

    if not path.exists():
        raise FileNotFoundError(
            f"Archivo v3 no encontrado: {path}. "
            f"Es la fuente primaria de clasificacion - el pipeline no puede continuar."
        )

    log.info("v3.loading", path=str(path))
    df_ext = _load_v3_raw(path)
    df_ext = _extract_nombre_base(df_ext)

    # Detectar que columnas estrategicas existen en el archivo
    extra_cols = [c for c in _STRATEGIC_COLS if c in df_ext.columns]
    log.info("v3.strategic_columns_found", count=len(extra_cols), cols=extra_cols[:5])

    # Normalizar para el join con products
    df_ext["_nombre_upper"] = df_ext["nombre_base"].str.strip().str.upper()
    df_ext["_ubicacion_upper"] = df_ext["SUCURSAL"].str.strip().str.upper()

    # Resolver duplicados v3 (mismo nombre+sucursal): preferir mejor ABC
    df_ext["_abc_rank"] = df_ext["ABC"].map(_ABC_RANK).fillna(9)
    df_ext = (
        df_ext.sort_values("_abc_rank")
              .drop_duplicates(subset=["_nombre_upper", "_ubicacion_upper"], keep="first")
    )

    prod = products.copy()
    prod["_nombre_upper"] = prod["nombre"].astype(str).str.strip().str.upper()

    # Join exacto por nombre - un nombre puede tener varias variantes/tallas
    keep_cols = ["_nombre_upper", "_ubicacion_upper"] + _CLASS_COLS + extra_cols
    merged = prod.merge(
        df_ext[keep_cols],
        on="_nombre_upper",
        how="inner",
    )

    # Renombrar a snake_case y schema-compatible
    rename_map = {
        "_ubicacion_upper": "ubicacion",
        "ABC": "abc_modelo",
        "XYZ": "xyz_modelo",
        "MATRIZ_FINAL": "clase_modelo",
        "ABC_MARGEN": "abc_margen",
        "ABC_VOLUMEN": "abc_volumen",
        "MARGEN": "margen",
        "%_Margen": "pct_margen",
        "SCORE_AUTOMATIZACION": "score_automatizacion",
        "CLASE_AUTOMATIZACION": "clase_automatizacion",
        "SCORE_CONFIANZA": "score_confianza",
        "ESTRATEGIA": "estrategia",
        "PATRON_DEMANDA": "patron_demanda",
        "CICLO_VIDA": "ciclo_vida",
        "FRECUENCIA_VENTA": "frecuencia_venta",
        "ROTACION_CONSTANTE": "rotacion_constante",
        "TENDENCIA": "tendencia",
        "FLAG_NUEVO": "flag_nuevo",
        "FLAG_DESCONTINUADO": "flag_descontinuado",
        "FLAG_SIN_VENTA": "flag_sin_venta",
        "FLAG_ESTACIONAL": "flag_estacional",
        "Demanda_Total": "demanda_total",
        "CV_Reciente": "cv_reciente",
    }
    out = merged.rename(columns=rename_map)

    # Mantener solo lo necesario + estrategicos
    base_cols = ["sku_id", "ubicacion", "abc_modelo", "xyz_modelo", "clase_modelo"]
    strategic_renamed = [rename_map.get(c, c) for c in extra_cols]
    out = out[base_cols + strategic_renamed].copy()

    # Deduplicar a nivel (sku_id, ubicacion)
    out = out.drop_duplicates(subset=["sku_id", "ubicacion"], keep="first")

    # Marca de origen
    out["fuente_clasificacion"] = "v3_estrategico"

    log.info(
        "v3.loaded",
        pares_sku_ubicacion=len(out),
        skus_unicos=out["sku_id"].nunique(),
        sucursales_unicas=out["ubicacion"].nunique(),
        campos_estrategicos=len(strategic_renamed),
    )
    return out


# ---------------------------------------------------------------------------
# Aplicacion estricta del override (solo a pares ya existentes en v3)
# ---------------------------------------------------------------------------

def apply_manual_override(
    base: pd.DataFrame,
    override_path: Path,
) -> tuple[pd.DataFrame, dict]:
    """Aplica override manual SOLO a pares (sku, ubicacion) presentes en v3.

    Reglas:
    - Solo se modifican pares que YA existen en la base v3.
    - Filas del override con pares NO presentes en v3 son IGNORADAS (con warning).
    - El override NO puede crear filas nuevas ni clasificar productos sin v3.

    Args:
        base: Clasificacion base desde v3 (output de load_v3_classification).
        override_path: Ruta a override_clasificacion.xlsx.

    Returns:
        (DataFrame con overrides aplicados, dict de metricas).
    """
    log = get_logger("classification.v3_loader")
    metrics = {
        "override_rows_input": 0,
        "override_applied": 0,
        "override_ignored_not_in_v3": 0,
    }

    if not override_path.exists():
        log.info("override.no_file", path=str(override_path))
        return base, metrics

    try:
        ov = pd.read_excel(override_path, sheet_name="override_clasificacion", dtype=str)
    except Exception as exc:
        log.warning("override.read_failed", error=str(exc), path=str(override_path))
        return base, metrics

    if ov.empty:
        log.info("override.empty", path=str(override_path))
        return base, metrics

    metrics["override_rows_input"] = len(ov)

    # Validar columnas minimas del override
    needed = {"sku_id", "ubicacion", "abc_override", "xyz_override"}
    missing = needed - set(ov.columns)
    if missing:
        log.warning("override.missing_columns", missing=list(missing))
        return base, metrics

    # Normalizar
    ov = ov.copy()
    ov["_sku_key"] = ov["sku_id"].astype(str).str.strip()
    ov["_ubic_key"] = ov["ubicacion"].astype(str).str.strip().str.upper()
    ov["abc_override"] = ov["abc_override"].astype(str).str.strip().str.upper()
    ov["xyz_override"] = ov["xyz_override"].astype(str).str.strip().str.upper()

    # Validar valores
    bad = ~(ov["abc_override"].isin(["A", "B", "C"]) & ov["xyz_override"].isin(["X", "Y", "Z"]))
    if bad.any():
        log.warning("override.invalid_values_dropped", count=int(bad.sum()))
        ov = ov[~bad].copy()

    # Index de base por (sku, ubicacion) — solo aceptamos pares que ya existen
    base_work = base.copy()
    base_work["_sku_key"] = base_work["sku_id"].astype(str).str.strip()
    base_work["_ubic_key"] = base_work["ubicacion"].astype(str).str.strip().str.upper()
    base_keys = set(zip(base_work["_sku_key"], base_work["_ubic_key"]))

    ov["_in_base"] = list(zip(ov["_sku_key"], ov["_ubic_key"]))
    ov["_in_base"] = ov["_in_base"].isin(base_keys)

    in_base = ov[ov["_in_base"]]
    not_in_base = ov[~ov["_in_base"]]
    metrics["override_applied"] = len(in_base)
    metrics["override_ignored_not_in_v3"] = len(not_in_base)

    if len(not_in_base) > 0:
        log.warning(
            "override.ignored_pairs_not_in_v3",
            count=len(not_in_base),
            ejemplos=not_in_base[["sku_id", "ubicacion"]].head(5).to_dict("records"),
        )

    if in_base.empty:
        log.info("override.no_applicable_rows")
        return base, metrics

    # Aplicar override (sobrescribir solo las filas que matchean)
    ov_apply = in_base[["_sku_key", "_ubic_key", "abc_override", "xyz_override"]].copy()
    ov_apply["clase_override"] = ov_apply["abc_override"] + ov_apply["xyz_override"]

    merged = base_work.merge(
        ov_apply,
        on=["_sku_key", "_ubic_key"],
        how="left",
    )

    mask = merged["abc_override"].notna()
    merged.loc[mask, "abc_modelo"] = merged.loc[mask, "abc_override"]
    merged.loc[mask, "xyz_modelo"] = merged.loc[mask, "xyz_override"]
    merged.loc[mask, "clase_modelo"] = merged.loc[mask, "clase_override"]
    merged.loc[mask, "fuente_clasificacion"] = "override_manual"

    merged = merged.drop(columns=["_sku_key", "_ubic_key", "abc_override",
                                   "xyz_override", "clase_override"])

    log.info("override.applied", **metrics)
    return merged, metrics
