from __future__ import annotations

from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Dict, List, Tuple, Union
import calendar
import datetime
import logging
import re
import warnings

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Configuracion
# ---------------------------------------------------------------------------

@dataclass
class Config:
    input_path: str
    output_filename: str = "RESULTADO_ESTRATEGICO_ABC_XYZ_v3.xlsx"
    sheet_name: Union[int, str] = 0

    # Columnas
    col_sku: str = "SKU 3.0"
    col_branch: str = ""  # dejar vacio si no existe columna de sucursal
    col_margin: str = "MARGEN"

    # Filtros
    exclude_keywords: Tuple[str, ...] = ("COSTO", "AJUSTE", "REBATE", "GASTO", "LOGO")
    require_positive_margin: bool = True
    clip_negative_demand_to_zero: bool = True

    # ABC thresholds
    abc_a: float = 0.80
    abc_b: float = 0.95
    abc_by_branch: bool = False

    # ABC por volumen (dual)
    abc_volume_enabled: bool = True
    abc_volume_a: float = 0.80
    abc_volume_b: float = 0.95

    # XYZ thresholds
    xyz_x: float = 0.50
    xyz_y: float = 1.00
    xyz_use_nonzero_cv: bool = True

    # Outlier detection para CV
    outlier_iqr_multiplier: float = 2.0  # IQR * este factor = limite
    outlier_min_datapoints: int = 4      # minimo puntos para detectar outliers

    # CV reciente (ventana de meses recientes para calcular CV_RECIENTE)
    cv_recent_months: int = 6

    # Ciclo de vida
    new_product_months: int = 3
    discontinued_months: int = 6

    # Estacionalidad
    season_top2_share_threshold: float = 0.60
    season_min_years_repeat: int = 2

    # Tendencia
    trend_window_months: int = 6
    trend_up_threshold: float = 0.20
    trend_down_threshold: float = -0.20

    # Rotacion constante
    rotation_constant_threshold: float = 0.90

    # Score de confianza
    confidence_min_months: int = 3     # debajo de esto, confianza = 0
    confidence_good_months: int = 12   # a partir de aqui, confianza maxima por historia

    # Score de automatizacion — pesos (suman 1.0)
    auto_weight_abc: float = 0.20
    auto_weight_xyz: float = 0.25
    auto_weight_confidence: float = 0.15
    auto_weight_forecast_error: float = 0.15
    auto_weight_frequency: float = 0.10
    auto_weight_lifecycle: float = 0.10
    auto_weight_cv_recent: float = 0.05

    # Umbrales de automatizacion
    auto_threshold_full: float = 75.0       # >= esto: AUTOMATIZAR
    auto_threshold_semi: float = 50.0       # >= esto: SEMI-AUTO
    auto_threshold_manual: float = 25.0     # >= esto: MANUAL, debajo: NO_AUTOMATIZAR

    # Diagnostico
    verbose_top_n: int = 5                  # cuantos SKUs top mostrar con calculos intermedios al final

    def __post_init__(self) -> None:
        # Umbrales ABC monotonos
        if not (0 < self.abc_a < self.abc_b < 1.0):
            raise ValueError(
                f"Umbrales ABC invalidos: abc_a={self.abc_a}, abc_b={self.abc_b}. "
                f"Debe cumplirse 0 < abc_a < abc_b < 1."
            )
        if not (0 < self.abc_volume_a < self.abc_volume_b < 1.0):
            raise ValueError(
                f"Umbrales ABC volumen invalidos: abc_volume_a={self.abc_volume_a}, "
                f"abc_volume_b={self.abc_volume_b}. Debe cumplirse 0 < a < b < 1."
            )
        # Umbrales XYZ monotonos
        if not (0 < self.xyz_x < self.xyz_y):
            raise ValueError(
                f"Umbrales XYZ invalidos: xyz_x={self.xyz_x}, xyz_y={self.xyz_y}. "
                f"Debe cumplirse 0 < xyz_x < xyz_y."
            )
        # Umbrales automatizacion monotonos
        if not (0 <= self.auto_threshold_manual < self.auto_threshold_semi < self.auto_threshold_full <= 100):
            raise ValueError(
                "Umbrales de automatizacion invalidos. Debe cumplirse "
                "0 <= manual < semi < full <= 100."
            )
        # Pesos suman ~1.0
        weight_sum = (
            self.auto_weight_abc + self.auto_weight_xyz + self.auto_weight_confidence
            + self.auto_weight_forecast_error + self.auto_weight_frequency
            + self.auto_weight_lifecycle + self.auto_weight_cv_recent
        )
        if abs(weight_sum - 1.0) > 1e-6:
            raise ValueError(
                f"Los pesos auto_weight_* deben sumar 1.0 (suman {weight_sum:.6f})."
            )
        # Outlier IQR
        if self.outlier_iqr_multiplier <= 0:
            raise ValueError(f"outlier_iqr_multiplier debe ser > 0 (es {self.outlier_iqr_multiplier}).")
        if self.outlier_min_datapoints < 4:
            raise ValueError(
                f"outlier_min_datapoints debe ser >= 4 para que IQR tenga sentido "
                f"(es {self.outlier_min_datapoints})."
            )
        # Ventanas
        if self.cv_recent_months < 1:
            raise ValueError(f"cv_recent_months debe ser >= 1 (es {self.cv_recent_months}).")
        if self.trend_window_months < 1:
            raise ValueError(f"trend_window_months debe ser >= 1 (es {self.trend_window_months}).")


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

def setup_logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    return logging.getLogger("abc_xyz_v3")


log = setup_logger()


# ---------------------------------------------------------------------------
# Utilidades basicas (sin cambios respecto a v2)
# ---------------------------------------------------------------------------

def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [c.strip() if isinstance(c, str) else c for c in df.columns]
    return df


def parse_margin(series: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_numeric(series, errors="coerce").fillna(0.0)

    s = series.astype(str).str.strip()
    s = s.str.replace(r"[^\d,.\-]", "", regex=True)

    def _to_float(x: str) -> float:
        if x in ("", "-", ".", ","):
            return np.nan
        if "," in x and "." in x:
            if x.rfind(",") > x.rfind("."):
                x = x.replace(".", "").replace(",", ".")
            else:
                x = x.replace(",", "")
        else:
            if "," in x and "." not in x:
                parts = x.split(",")
                if len(parts[-1]) == 3 and all(p.isdigit() for p in parts):
                    x = x.replace(",", "")
                else:
                    x = x.replace(",", ".")
            if "." in x:
                parts = x.split(".")
                if len(parts) > 1 and len(parts[-1]) == 3 and all(p.isdigit() for p in parts):
                    x = x.replace(".", "")
        try:
            return float(x)
        except Exception:
            return np.nan

    out = s.apply(_to_float)
    return pd.to_numeric(out, errors="coerce").fillna(0.0)


def detect_date_columns(df: pd.DataFrame) -> List[pd.Timestamp]:
    date_cols: List[pd.Timestamp] = []
    for c in df.columns:
        if isinstance(c, (pd.Timestamp, datetime.datetime, datetime.date)):
            date_cols.append(pd.to_datetime(c))
        elif isinstance(c, str):
            parsed = pd.to_datetime(c, errors="coerce", dayfirst=True)
            if not pd.isna(parsed) and parsed.year >= 2000:
                date_cols.append(parsed)
    seen = set()
    out = []
    for d in date_cols:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return sorted(out)


def rename_date_columns(df: pd.DataFrame, date_cols: List[pd.Timestamp]) -> Tuple[pd.DataFrame, Dict]:
    df = df.copy()
    mapping: Dict = {}
    for c in df.columns:
        if isinstance(c, (pd.Timestamp, datetime.datetime, datetime.date)):
            ts = pd.to_datetime(c)
            if ts in date_cols:
                mapping[c] = ts.strftime("%Y-%m")
        elif isinstance(c, str):
            parsed = pd.to_datetime(c, errors="coerce", dayfirst=True)
            if not pd.isna(parsed) and parsed in date_cols:
                mapping[c] = parsed.strftime("%Y-%m")
    df = df.rename(columns=mapping)
    return df, mapping


def clean_demand(df: pd.DataFrame, date_cols: List[pd.Timestamp], clip_negative: bool) -> Tuple[pd.DataFrame, List[str]]:
    df = df.copy()
    demand_cols = [d.strftime("%Y-%m") for d in date_cols]
    demand_cols = [c for c in demand_cols if c in df.columns]
    for c in demand_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
        if clip_negative:
            df[c] = df[c].clip(lower=0.0)
    return df, demand_cols


# ---------------------------------------------------------------------------
# Filtrado con auditoria (sin cambios)
# ---------------------------------------------------------------------------

def build_keyword_pattern(keywords: Tuple[str, ...], capture: bool) -> str:
    escaped = [re.escape(k) for k in keywords]
    group_open = "(" if capture else "(?:"
    # Sin \b de cierre: permite que la sucursal esté concatenada sin separador al nombre del SKU
    # (ej. "REBATEPUERTO MONTT" debe filtrarse aunque no haya espacio tras REBATE).
    # El \b de apertura sigue protegiendo contra falsos positivos dentro de palabras (ej. "GEOLOGO" != "LOGO").
    return r"(?i)\b" + group_open + "|".join(escaped) + r")"


def filter_products_with_audit(
    df: pd.DataFrame, cfg: Config, demand_cols: List[str]
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    df = df.copy()
    sku = df[cfg.col_sku].astype(str)

    if cfg.exclude_keywords:
        pattern_contains = build_keyword_pattern(cfg.exclude_keywords, capture=False)
        pattern_extract = build_keyword_pattern(cfg.exclude_keywords, capture=True)
        mask_keyword = sku.str.contains(pattern_contains, regex=True, na=False)
    else:
        pattern_extract = None
        mask_keyword = pd.Series(False, index=df.index)

    mask_margin_bad = (df[cfg.col_margin] <= 0) if cfg.require_positive_margin else pd.Series(False, index=df.index)
    mask_keep = ~(mask_keyword | mask_margin_bad)

    df_keep = df.loc[mask_keep].copy()
    excl_cols = [cfg.col_sku, cfg.col_margin]
    if cfg.col_branch and cfg.col_branch in df.columns:
        excl_cols.insert(1, cfg.col_branch)
    df_excl = df.loc[~mask_keep, excl_cols].copy()

    # Reportar demanda perdida en las exclusiones (filas con demanda > 0 que igual se descartan)
    demand_total = df[demand_cols].sum(axis=1)
    months_with_sale = (df[demand_cols] > 0).sum(axis=1)
    df_excl["Demanda_Total"] = demand_total.loc[df_excl.index].astype(float)
    df_excl["Meses_Con_Venta"] = months_with_sale.loc[df_excl.index].astype(int)

    df_excl["Motivo"] = np.select(
        [mask_margin_bad.loc[df_excl.index], mask_keyword.loc[df_excl.index]],
        ["MARGEN<=0", "KEYWORD_BASURA"],
        default="OTRO",
    )
    if pattern_extract:
        df_excl["Keyword_Match"] = sku.loc[df_excl.index].str.extract(pattern_extract, expand=False)

    excl_with_demand = (df_excl["Meses_Con_Venta"] >= 1).sum()
    units_lost = float(df_excl.loc[df_excl["Meses_Con_Venta"] >= 1, "Demanda_Total"].sum())

    log.info("Excluidos MARGEN<=0: %s | KEYWORD: %s | Total excl: %s | Incluidos: %s",
             int(mask_margin_bad.sum()), int(mask_keyword.sum()), len(df_excl), len(df_keep))
    log.info("De los excluidos, con demanda >= 1: %d filas (%.0f unidades no clasificadas)",
             int(excl_with_demand), units_lost)
    return df_keep, df_excl


def validate_inputs(df: pd.DataFrame, cfg: Config, demand_cols: List[str]) -> None:
    """Validaciones tempranas de calidad de datos. Registra warnings y lanza si es fatal."""
    n_rows = len(df)
    if n_rows == 0:
        raise ValueError("El dataset esta vacio.")

    # Duplicados de SKU (y sucursal si existe)
    dup_subset = [cfg.col_sku]
    if cfg.col_branch and cfg.col_branch in df.columns:
        dup_subset.append(cfg.col_branch)
    dup_mask = df.duplicated(subset=dup_subset, keep=False)
    n_dup = int(dup_mask.sum())
    if n_dup > 0:
        log.warning("Hay %d filas duplicadas en %s. Las metricas se calcularan por fila tal cual.",
                    n_dup, dup_subset)

    # SKUs vacios o NaN
    sku_str = df[cfg.col_sku].astype(str).str.strip()
    n_empty_sku = int(((sku_str == "") | (sku_str.str.lower() == "nan")).sum())
    if n_empty_sku > 0:
        log.warning("Hay %d filas con %s vacio/NaN.", n_empty_sku, cfg.col_sku)

    # MARGEN: porcentaje de NaN/0/negativo
    margen = df[cfg.col_margin]
    n_nan = int(margen.isna().sum())
    n_neg = int((margen < 0).sum())
    n_zero = int((margen == 0).sum())
    if n_nan > 0:
        log.warning("MARGEN tiene %d NaN (rellenados con 0 al parsear).", n_nan)
    if (n_neg + n_zero) / max(n_rows, 1) > 0.30:
        log.warning("Mas del 30%% de las filas tienen MARGEN<=0 (%d/%d). "
                    "Revisar parseo o calidad del dato.", n_neg + n_zero, n_rows)

    # Demanda: cobertura
    n_sin_demanda = int((df[demand_cols].sum(axis=1) <= 0).sum())
    if n_sin_demanda > 0:
        log.info("Filas sin demanda en todo el horizonte: %d/%d (entran como SIN_VENTA).",
                 n_sin_demanda, n_rows)

    # Periodos minimos para algunas metricas
    if len(demand_cols) < cfg.outlier_min_datapoints:
        log.warning("Solo hay %d periodos de demanda; deteccion de outliers IQR requiere >= %d. "
                    "CV_Robusto = CV crudo en la mayoria de filas.",
                    len(demand_cols), cfg.outlier_min_datapoints)

    log.info("Validacion de inputs OK: %d filas, %d periodos de demanda (%s..%s).",
             n_rows, len(demand_cols), demand_cols[0], demand_cols[-1])


# ---------------------------------------------------------------------------
# NUEVO: Deteccion de outliers por IQR (por fila)
# ---------------------------------------------------------------------------

def remove_outliers_iqr(mat: np.ndarray, multiplier: float = 2.0, min_points: int = 4) -> np.ndarray:
    """
    Reemplaza outliers con NaN usando IQR sobre valores > 0 de cada fila.
    Retorna una copia limpia (outliers -> NaN) para calcular CV robusto.
    """
    clean = mat.copy()
    n_rows, n_cols = mat.shape

    for i in range(n_rows):
        row = mat[i]
        nz = row[row > 0]
        if len(nz) < min_points:
            continue

        q1 = np.percentile(nz, 25)
        q3 = np.percentile(nz, 75)
        iqr = q3 - q1
        lower = q1 - multiplier * iqr
        upper = q3 + multiplier * iqr

        for j in range(n_cols):
            if row[j] > 0 and (row[j] < lower or row[j] > upper):
                clean[i, j] = np.nan

    return clean


def count_outliers_per_row(mat: np.ndarray, multiplier: float = 2.0, min_points: int = 4) -> np.ndarray:
    """Cuenta cuantos outliers tiene cada fila."""
    n_rows, n_cols = mat.shape
    counts = np.zeros(n_rows, dtype=int)

    for i in range(n_rows):
        row = mat[i]
        nz = row[row > 0]
        if len(nz) < min_points:
            continue
        q1 = np.percentile(nz, 25)
        q3 = np.percentile(nz, 75)
        iqr = q3 - q1
        lower = q1 - multiplier * iqr
        upper = q3 + multiplier * iqr
        counts[i] = int(np.sum((row > 0) & ((row < lower) | (row > upper))))

    return counts


# ---------------------------------------------------------------------------
# ABC por margen (igual que v2)
# ---------------------------------------------------------------------------

def compute_abc_margin(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    df = df.copy()
    if cfg.abc_by_branch and cfg.col_branch and cfg.col_branch in df.columns:
        df = df.sort_values([cfg.col_branch, cfg.col_margin], ascending=[True, False])
        grp_total = df.groupby(cfg.col_branch)[cfg.col_margin].transform("sum").replace(0, np.nan)
        df["_ACUM"] = df.groupby(cfg.col_branch)[cfg.col_margin].cumsum()
        df["%_Acum_Margen"] = df["_ACUM"] / grp_total
    else:
        df = df.sort_values(cfg.col_margin, ascending=False)
        total = df[cfg.col_margin].sum()
        df["%_Acum_Margen"] = df[cfg.col_margin].cumsum() / (total if total != 0 else np.nan)

    def cat(p):
        if pd.isna(p):
            return "C"
        if p <= cfg.abc_a:
            return "A"
        if p <= cfg.abc_b:
            return "B"
        return "C"

    df["ABC_MARGEN"] = df["%_Acum_Margen"].apply(cat)
    total_margin = df[cfg.col_margin].sum()
    df["%_Margen"] = df[cfg.col_margin] / (total_margin if total_margin != 0 else np.nan)
    return df.drop(columns=[c for c in ["_ACUM"] if c in df.columns])


# ---------------------------------------------------------------------------
# NUEVO: ABC por volumen
# ---------------------------------------------------------------------------

def compute_abc_volume(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """ABC basado en demanda total (volumen), complementario al de margen."""
    df = df.copy()
    if not cfg.abc_volume_enabled:
        df["ABC_VOLUMEN"] = df.get("ABC_MARGEN", "C")
        df["ABC"] = df["ABC_MARGEN"]
        return df

    col = "Demanda_Total"
    if col not in df.columns:
        df["ABC_VOLUMEN"] = "C"
        df["ABC"] = df.get("ABC_MARGEN", "C")
        return df

    df = df.sort_values(col, ascending=False)
    total = df[col].sum()
    if total == 0:
        df["%_Acum_Volumen"] = 0.0
        df["ABC_VOLUMEN"] = "C"
    else:
        df["%_Acum_Volumen"] = df[col].cumsum() / total

        def cat(p):
            if pd.isna(p):
                return "C"
            if p <= cfg.abc_volume_a:
                return "A"
            if p <= cfg.abc_volume_b:
                return "B"
            return "C"

        df["ABC_VOLUMEN"] = df["%_Acum_Volumen"].apply(cat)

    # --- ABC combinado: el MEJOR de ambos (mas conservador para no perder productos importantes) ---
    rank_map = {"A": 0, "B": 1, "C": 2}
    rank_margin = df["ABC_MARGEN"].map(rank_map).fillna(2).astype(int)
    rank_volume = df["ABC_VOLUMEN"].map(rank_map).fillna(2).astype(int)
    combined_rank = np.minimum(rank_margin.to_numpy(), rank_volume.to_numpy())
    inv_map = {0: "A", 1: "B", 2: "C"}
    df["ABC"] = pd.Series(combined_rank, index=df.index).map(inv_map)

    return df


# ---------------------------------------------------------------------------
# Demand features + outlier-robust CV + CV reciente
# ---------------------------------------------------------------------------

def demand_features(df: pd.DataFrame, demand_cols: List[str], cfg: Config) -> Tuple[pd.DataFrame, np.ndarray, np.ndarray, np.ndarray, int]:
    df = df.copy()
    mat = df[demand_cols].to_numpy(dtype=float, copy=True)
    n_periods = mat.shape[1]

    total = mat.sum(axis=1)
    mean_all = mat.mean(axis=1)
    std_all = mat.std(axis=1, ddof=0)

    nz_mask = mat > 0
    nz_count = nz_mask.sum(axis=1)
    nz_sum = np.where(nz_mask, mat, 0.0).sum(axis=1)
    mean_nz = np.where(nz_count > 0, nz_sum / nz_count, 0.0)

    # --- CV crudo (como v2) ---
    mat_nz = np.where(nz_mask, mat, np.nan)
    std_nz = np.nanstd(mat_nz, axis=1, ddof=0)
    cv_all = np.where(mean_all > 0, std_all / mean_all, 0.0)
    cv_nz = np.where(mean_nz > 0, std_nz / mean_nz, 0.0)

    # --- NUEVO: CV robusto (sin outliers IQR) ---
    mat_clean = remove_outliers_iqr(mat, multiplier=cfg.outlier_iqr_multiplier, min_points=cfg.outlier_min_datapoints)
    mat_clean_nz = np.where(mat_clean > 0, mat_clean, np.nan)
    mean_clean = np.nanmean(mat_clean_nz, axis=1)
    mean_clean = np.where(np.isnan(mean_clean), 0.0, mean_clean)
    std_clean = np.nanstd(mat_clean_nz, axis=1, ddof=0)
    std_clean = np.where(np.isnan(std_clean), 0.0, std_clean)
    cv_robust = np.where(mean_clean > 0, std_clean / mean_clean, 0.0)

    n_outliers = count_outliers_per_row(mat, multiplier=cfg.outlier_iqr_multiplier, min_points=cfg.outlier_min_datapoints)

    # --- NUEVO: CV reciente (ultimos N meses con datos) ---
    # Si en la ventana reciente todos los valores son cero, nanmean/nanstd levantan
    # RuntimeWarning sobre slices vacios -> los suprimimos porque el resultado correcto es 0.
    recent_n = min(cfg.cv_recent_months, n_periods)
    mat_recent = mat[:, -recent_n:]
    mat_recent_nz = np.where(mat_recent > 0, mat_recent, np.nan)
    with np.errstate(invalid="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        mean_recent_nz = np.nanmean(mat_recent_nz, axis=1)
        std_recent_nz = np.nanstd(mat_recent_nz, axis=1, ddof=0)
    mean_recent_nz = np.where(np.isnan(mean_recent_nz), 0.0, mean_recent_nz)
    std_recent_nz = np.where(np.isnan(std_recent_nz), 0.0, std_recent_nz)
    cv_recent = np.divide(
        std_recent_nz, mean_recent_nz,
        out=np.zeros_like(std_recent_nz, dtype=float),
        where=mean_recent_nz > 0,
    )

    # --- NUEVO: Forecast Error Proxy (MAD / Mean) ---
    # MAD = Mean Absolute Deviation respecto a la media movil simple
    mad = np.zeros(mat.shape[0])
    for i in range(mat.shape[0]):
        row_nz = mat[i][mat[i] > 0]
        if len(row_nz) >= 2:
            m = row_nz.mean()
            mad[i] = np.mean(np.abs(row_nz - m))
        else:
            mad[i] = 0.0
    forecast_error_proxy = np.where(mean_nz > 0, mad / mean_nz, 0.0)

    # Frecuencia y periodos
    freq = nz_count / np.maximum(n_periods, 1)

    any_sale = nz_mask.any(axis=1)
    first_idx = np.where(any_sale, nz_mask.argmax(axis=1), -1)
    last_idx = np.where(any_sale, (n_periods - 1) - np.flip(nz_mask, axis=1).argmax(axis=1), -1)

    periods = np.array(demand_cols)
    first_period = np.where(first_idx >= 0, periods[first_idx], None)
    last_period = np.where(last_idx >= 0, periods[last_idx], None)

    last_dataset_period = periods[-1]

    def ym_to_int(ym: str) -> int:
        y, m = ym.split("-")
        return int(y) * 12 + int(m)

    last_int = ym_to_int(last_dataset_period)
    first_int = np.array([ym_to_int(p) if p is not None else np.nan for p in first_period])
    last_int_sale = np.array([ym_to_int(p) if p is not None else np.nan for p in last_period])

    months_since_first = last_int - first_int
    months_since_last = last_int - last_int_sale

    # Tendencia
    w = max(1, min(cfg.trend_window_months, n_periods // 2)) if n_periods >= 2 else 1
    recent_trend = mat[:, -w:].mean(axis=1)
    prev_trend = mat[:, -2 * w:-w].mean(axis=1) if n_periods >= 2 * w else mat[:, :w].mean(axis=1)
    growth = (recent_trend - prev_trend) / np.maximum(prev_trend, 1e-9)

    # Asignar todo al dataframe
    df["Demanda_Total"] = total
    df["Meses_Con_Venta"] = nz_count
    df["%_Meses_Con_Venta"] = freq
    df["Venta_Promedio"] = mean_all
    df["Venta_Promedio_NoCero"] = mean_nz
    df["Desv_Std"] = std_all
    df["CV"] = cv_nz
    df["CV_Incluyendo_Ceros"] = cv_all
    df["CV_Robusto"] = cv_robust
    df["CV_Reciente"] = cv_recent
    df["N_Outliers"] = n_outliers
    df["MAD"] = mad
    df["Forecast_Error_Proxy"] = forecast_error_proxy
    df["Primer_Mes_Venta"] = first_period
    df["Ultimo_Mes_Venta"] = last_period
    df["Meses_Desde_Primera_Venta"] = months_since_first
    df["Meses_Desde_Ultima_Venta"] = months_since_last
    df[f"Promedio_Ultimos_{w}m"] = recent_trend
    df[f"Promedio_Anteriores_{w}m"] = prev_trend
    df[f"Crecimiento_{w}m_vs_prev"] = growth

    return df, mat, nz_mask, periods, w


# ---------------------------------------------------------------------------
# Tendencia (sin cambios)
# ---------------------------------------------------------------------------

def add_trend_label(df: pd.DataFrame, w: int, cfg: Config) -> pd.DataFrame:
    df = df.copy()
    gcol = f"Crecimiento_{w}m_vs_prev"
    prev_col = f"Promedio_Anteriores_{w}m"
    recent_col = f"Promedio_Ultimos_{w}m"

    growth = df[gcol].to_numpy()
    prev = df[prev_col].to_numpy()
    recent = df[recent_col].to_numpy()

    trend = np.full(len(df), "ESTABLE", dtype=object)
    trend[growth >= cfg.trend_up_threshold] = "ALZA"
    trend[growth <= cfg.trend_down_threshold] = "BAJA"
    trend[(prev == 0) & (recent > 0)] = "RECIEN_ACTIVO"

    df["TENDENCIA"] = trend
    return df


# ---------------------------------------------------------------------------
# XYZ — ahora usa CV_Robusto por defecto
# ---------------------------------------------------------------------------

def compute_xyz(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    df = df.copy()
    # Usar CV robusto (sin outliers) en lugar del CV crudo
    cv_col = "CV_Robusto"
    mean_col = "Venta_Promedio_NoCero" if cfg.xyz_use_nonzero_cv else "Venta_Promedio"

    mean = df[mean_col].to_numpy()
    cv = df[cv_col].to_numpy()

    xyz = np.full(len(df), "Z", dtype=object)
    mask = mean > 0
    xyz[mask & (cv <= cfg.xyz_x)] = "X"
    xyz[mask & (cv > cfg.xyz_x) & (cv <= cfg.xyz_y)] = "Y"
    xyz[mask & (cv > cfg.xyz_y)] = "Z"

    df["XYZ"] = xyz
    df["MATRIZ_FINAL"] = df["ABC"].astype(str) + df["XYZ"].astype(str)
    return df


# ---------------------------------------------------------------------------
# Syntetos-Boylan (sin cambios)
# ---------------------------------------------------------------------------

def demand_pattern_syntetos(df: pd.DataFrame, n_periods: int) -> pd.DataFrame:
    df = df.copy()
    nz = df["Meses_Con_Venta"].to_numpy()
    adi = np.where(nz > 0, n_periods / nz, np.inf)
    cv2 = np.square(df["CV"].to_numpy())

    pattern = np.full(len(df), "SIN_VENTA", dtype=object)
    mask = nz > 0
    pattern[mask & (adi < 1.32) & (cv2 < 0.49)] = "SUAVE"
    pattern[mask & (adi >= 1.32) & (cv2 < 0.49)] = "INTERMITENTE"
    pattern[mask & (adi < 1.32) & (cv2 >= 0.49)] = "ERRATICA"
    pattern[mask & (adi >= 1.32) & (cv2 >= 0.49)] = "IRREGULAR"

    df["ADI"] = adi
    df["CV2"] = cv2
    df["PATRON_DEMANDA"] = pattern
    return df


# ---------------------------------------------------------------------------
# Ciclo de vida (sin cambios)
# ---------------------------------------------------------------------------

def lifecycle_flags(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    df = df.copy()
    ms_first = df["Meses_Desde_Primera_Venta"]
    ms_last = df["Meses_Desde_Ultima_Venta"]
    total = df["Demanda_Total"]

    df["FLAG_SIN_VENTA"] = total <= 0
    df["FLAG_NUEVO"] = (total > 0) & (ms_first <= cfg.new_product_months)
    df["FLAG_DESCONTINUADO"] = (total > 0) & (ms_last >= cfg.discontinued_months)

    lifecycle = np.where(
        df["FLAG_SIN_VENTA"], "SIN_VENTA",
        np.where(df["FLAG_NUEVO"], "NUEVO",
                 np.where(df["FLAG_DESCONTINUADO"], "DESCONTINUADO", "ACTIVO"))
    )
    df["CICLO_VIDA"] = lifecycle
    return df


# ---------------------------------------------------------------------------
# Estacionalidad (sin cambios)
# ---------------------------------------------------------------------------

def compute_seasonality(df: pd.DataFrame, mat: np.ndarray, periods: np.ndarray, cfg: Config) -> pd.DataFrame:
    df = df.copy()

    years = np.array([int(p.split("-")[0]) for p in periods])
    months = np.array([int(p.split("-")[1]) for p in periods])

    unique_years = sorted(set(years.tolist()))
    idx_by_month = {m: np.where(months == m)[0] for m in range(1, 13)}
    idx_by_year = {y: np.where(years == y)[0] for y in unique_years}

    n_rows = mat.shape[0]
    total = df["Demanda_Total"].to_numpy()

    month_totals = np.zeros((n_rows, 12), dtype=float)
    years_repeat = np.zeros((n_rows, 12), dtype=int)

    for m in range(1, 13):
        idx = idx_by_month[m]
        if len(idx) == 0:
            continue
        month_vals = mat[:, idx]
        month_totals[:, m - 1] = month_vals.sum(axis=1)
        years_repeat[:, m - 1] = (month_vals > 0).sum(axis=1)

    year_sales_count = np.zeros(n_rows, dtype=int)
    for y in unique_years:
        idx = idx_by_year[y]
        if len(idx) == 0:
            continue
        year_total = mat[:, idx].sum(axis=1)
        year_sales_count += (year_total > 0).astype(int)

    top1_idx = month_totals.argmax(axis=1)
    top1_total = month_totals[np.arange(n_rows), top1_idx]
    top1_share = np.where(total > 0, top1_total / total, 0.0)

    top2_idx = np.argpartition(month_totals, -2, axis=1)[:, -2:]
    top2_total = month_totals[np.arange(n_rows)[:, None], top2_idx].sum(axis=1)
    top2_share = np.where(total > 0, top2_total / total, 0.0)

    repeat_top1 = years_repeat[np.arange(n_rows), top1_idx]

    seasonal = (
        (total > 0)
        & (year_sales_count >= 2)
        & (top2_share >= cfg.season_top2_share_threshold)
        & (repeat_top1 >= cfg.season_min_years_repeat)
    )

    peak_month = top1_idx + 1
    peak_month_name = [calendar.month_name[m] for m in peak_month]

    df["Anios_Con_Venta"] = year_sales_count
    df["Mes_Pico_Num"] = peak_month
    df["Mes_Pico"] = peak_month_name
    df["Share_Top1_Mes"] = top1_share
    df["Share_Top2_Meses"] = top2_share
    df["Repite_Mes_Pico_Anios"] = repeat_top1
    df["FLAG_ESTACIONAL"] = seasonal

    return df


# ---------------------------------------------------------------------------
# NUEVO: Score de confianza
# ---------------------------------------------------------------------------

def compute_confidence_score(df: pd.DataFrame, n_total_periods: int, cfg: Config) -> pd.DataFrame:
    """
    Score 0-1 que indica que tan fiable es la clasificacion del producto.
    Factores:
      - Meses con historia (mas historia = mas confianza)
      - Meses con venta vs total (si vendio 2 de 24 meses, baja confianza)
      - Si tiene outliers extremos, penaliza
      - Si es nuevo, penaliza
    """
    df = df.copy()

    meses_venta = df["Meses_Con_Venta"].to_numpy(dtype=float)
    meses_desde_primera = df["Meses_Desde_Primera_Venta"].to_numpy(dtype=float)
    meses_desde_primera = np.where(np.isnan(meses_desde_primera), 0, meses_desde_primera)
    n_outliers = df["N_Outliers"].to_numpy(dtype=float)

    # Factor 1: historia disponible (meses desde primera venta)
    hist_score = np.clip(meses_desde_primera / cfg.confidence_good_months, 0.0, 1.0)
    # Productos con menos de confidence_min_months -> 0
    hist_score[meses_desde_primera < cfg.confidence_min_months] = 0.0

    # Factor 2: cobertura (% meses con venta respecto a meses desde primera venta)
    active_months = np.maximum(meses_desde_primera, 1)
    coverage = np.clip(meses_venta / active_months, 0.0, 1.0)

    # Factor 3: penalizacion por outliers (mas de 2 outliers en la serie penaliza)
    outlier_penalty = np.clip(1.0 - (n_outliers / np.maximum(meses_venta, 1)) * 0.5, 0.3, 1.0)

    # Factor 4: penalizacion por pocos datos absolutos
    data_penalty = np.clip(meses_venta / max(cfg.confidence_min_months, 1), 0.0, 1.0)

    # Score final (promedio ponderado)
    confidence = (
        0.35 * hist_score +
        0.30 * coverage +
        0.15 * outlier_penalty +
        0.20 * data_penalty
    )

    # Productos sin venta = 0
    confidence[df["Demanda_Total"].to_numpy() <= 0] = 0.0

    df["SCORE_CONFIANZA"] = np.round(confidence, 4)
    return df


# ---------------------------------------------------------------------------
# NUEVO: Score de automatizacion (0-100)
# ---------------------------------------------------------------------------

def compute_automation_score(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """
    Score 0-100 que indica que tan seguro es automatizar la reposicion de este producto.

    Componentes (pesos configurables):
      - ABC: A=100, B=60, C=20
      - XYZ: X=100, Y=50, Z=10
      - Confianza: score * 100
      - Forecast Error: inverso (menos error = mas automatizable)
      - Frecuencia de venta: mas frecuente = mas automatizable
      - Ciclo de vida: ACTIVO=100, NUEVO=30, DESCONTINUADO=10, SIN_VENTA=0
      - CV reciente: menos variabilidad reciente = mejor
    """
    df = df.copy()

    # --- Sub-scores ---
    abc_score_map = {"A": 100.0, "B": 60.0, "C": 20.0}
    s_abc = df["ABC"].map(abc_score_map).fillna(0.0).to_numpy()

    xyz_score_map = {"X": 100.0, "Y": 50.0, "Z": 10.0}
    s_xyz = df["XYZ"].map(xyz_score_map).fillna(0.0).to_numpy()

    s_confidence = df["SCORE_CONFIANZA"].to_numpy() * 100.0

    # Forecast error: invertir (0 error = 100, error >= 1.5 = 0)
    fe = df["Forecast_Error_Proxy"].to_numpy()
    s_forecast = np.clip(100.0 * (1.0 - fe / 1.5), 0.0, 100.0)

    s_freq = df["%_Meses_Con_Venta"].to_numpy() * 100.0

    lifecycle_map = {"ACTIVO": 100.0, "NUEVO": 30.0, "DESCONTINUADO": 10.0, "SIN_VENTA": 0.0}
    s_lifecycle = df["CICLO_VIDA"].map(lifecycle_map).fillna(0.0).to_numpy()

    # CV reciente: invertir (CV=0 -> 100, CV >= 2 -> 0)
    cv_rec = df["CV_Reciente"].to_numpy()
    s_cv_recent = np.clip(100.0 * (1.0 - cv_rec / 2.0), 0.0, 100.0)

    # --- Score ponderado ---
    score = (
        cfg.auto_weight_abc * s_abc +
        cfg.auto_weight_xyz * s_xyz +
        cfg.auto_weight_confidence * s_confidence +
        cfg.auto_weight_forecast_error * s_forecast +
        cfg.auto_weight_frequency * s_freq +
        cfg.auto_weight_lifecycle * s_lifecycle +
        cfg.auto_weight_cv_recent * s_cv_recent
    )

    # --- Penalizaciones duras ---
    # Estacionalidad fuerte sin gestion especial -> penalizar
    seasonal = df["FLAG_ESTACIONAL"].to_numpy().astype(bool)
    score[seasonal] *= 0.85  # reducir 15% si es estacional (requiere atencion manual)

    # Tendencia BAJA fuerte -> penalizar
    baja = (df["TENDENCIA"] == "BAJA").to_numpy()
    score[baja] *= 0.90

    # Productos nuevos o recien activos -> penalizar mas
    recien = (df["TENDENCIA"] == "RECIEN_ACTIVO").to_numpy()
    score[recien] *= 0.70

    # Sin venta = 0
    sin_venta = (df["Demanda_Total"] <= 0).to_numpy()
    score[sin_venta] = 0.0

    df["SCORE_AUTOMATIZACION"] = np.round(score, 2)

    # --- Clasificacion ---
    auto_class = np.full(len(df), "NO_AUTOMATIZAR", dtype=object)
    auto_class[score >= cfg.auto_threshold_manual] = "MANUAL"
    auto_class[score >= cfg.auto_threshold_semi] = "SEMI-AUTO"
    auto_class[score >= cfg.auto_threshold_full] = "AUTOMATIZAR"

    df["CLASE_AUTOMATIZACION"] = auto_class

    # --- Sub-scores para transparencia ---
    df["_S_ABC"] = np.round(s_abc, 1)
    df["_S_XYZ"] = np.round(s_xyz, 1)
    df["_S_CONFIANZA"] = np.round(s_confidence, 1)
    df["_S_FORECAST"] = np.round(s_forecast, 1)
    df["_S_FRECUENCIA"] = np.round(s_freq, 1)
    df["_S_CICLOVIDA"] = np.round(s_lifecycle, 1)
    df["_S_CV_RECIENTE"] = np.round(s_cv_recent, 1)

    return df


# ---------------------------------------------------------------------------
# Estrategias (ampliadas con automatizacion)
# ---------------------------------------------------------------------------

BASE_ESTRATEGIAS = {
    "AX": "Alta rentabilidad + demanda estable: automatizar reposicion (min-max).",
    "AY": "Alta rentabilidad + demanda variable: stock de seguridad medio, revision mensual.",
    "AZ": "Alta rentabilidad + demanda erratica: revision manual frecuente, compras cortas.",
    "BX": "Rentabilidad media + estable: lotes economicos y reposicion periodica.",
    "BY": "Rentabilidad media + variable: revision periodica y ajuste de parametros.",
    "BZ": "Rentabilidad media + erratica: comprar bajo pedido / stock minimo.",
    "CX": "Baja rentabilidad + estable: compras masivas infrecuentes (si hay espacio).",
    "CY": "Baja rentabilidad + variable: reducir inventario y priorizar rotacion.",
    "CZ": "Baja rentabilidad + erratica: evaluar eliminacion, sustitucion o liquidacion.",
}


def build_recommendations(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ESTRATEGIA_BASE"] = df["MATRIZ_FINAL"].map(BASE_ESTRATEGIAS).fillna("Sin clasificacion")

    def _row_rec(r: pd.Series) -> str:
        notes = []

        # Automatizacion
        auto_class = str(r.get("CLASE_AUTOMATIZACION", ""))
        auto_score = r.get("SCORE_AUTOMATIZACION", 0)

        if auto_class == "AUTOMATIZAR":
            notes.append(f"[AUTOMATIZAR score={auto_score:.0f}] Apto para reposicion automatica sin intervencion.")
        elif auto_class == "SEMI-AUTO":
            notes.append(f"[SEMI-AUTO score={auto_score:.0f}] Automatizar con revision periodica.")
        elif auto_class == "MANUAL":
            notes.append(f"[MANUAL score={auto_score:.0f}] Requiere gestion manual; datos insuficientes o alta variabilidad.")

        if bool(r.get("FLAG_NUEVO", False)):
            notes.append("NUEVO: clasificar con cautela; definir stock inicial y revisar semanal/quincenal.")
        if bool(r.get("FLAG_DESCONTINUADO", False)):
            notes.append("DESCONTINUADO: revisar obsolescencia, redistribucion y plan de liquidacion.")
        if bool(r.get("FLAG_ESTACIONAL", False)):
            notes.append(f"ESTACIONAL: planificar pico ({r.get('Mes_Pico','')}); anticipar compras y ajustar safety stock.")

        patron = str(r.get("PATRON_DEMANDA", ""))
        if patron in ("INTERMITENTE", "IRREGULAR"):
            notes.append("INTERMITENTE/IRREGULAR: considerar Croston/SBA, reponer bajo pedido.")

        tendencia = str(r.get("TENDENCIA", ""))
        if tendencia == "ALZA":
            notes.append("TENDENCIA ALZA: aumentar frecuencia de revision y validar capacidad de abastecimiento.")
        elif tendencia == "BAJA":
            notes.append("TENDENCIA BAJA: reducir cobertura y evitar sobrestock.")
        elif tendencia == "RECIEN_ACTIVO":
            notes.append("RECIEN ACTIVO: validar si es relanzamiento o alta puntual.")

        if bool(r.get("ALERTA_CONST_PERO_Z", False)):
            notes.append("ALERTA: rota constante pero CV alto -> revisar outliers/picos.")

        confidence = r.get("SCORE_CONFIANZA", 0)
        if 0 < confidence < 0.4:
            notes.append(f"BAJA CONFIANZA ({confidence:.2f}): pocos datos, clasificacion provisional.")

        n_outliers = r.get("N_Outliers", 0)
        if n_outliers >= 3:
            notes.append(f"OUTLIERS ({n_outliers} detectados): la demanda tiene picos anomalos que distorsionan.")

        if notes:
            return str(r["ESTRATEGIA_BASE"]) + " | " + " ".join(notes)
        return str(r["ESTRATEGIA_BASE"])

    df["ESTRATEGIA"] = df.apply(_row_rec, axis=1)
    return df


# ---------------------------------------------------------------------------
# Resumen de automatizacion
# ---------------------------------------------------------------------------

def build_summaries(df: pd.DataFrame, cfg: Config) -> Dict[str, pd.DataFrame]:
    summaries: Dict[str, pd.DataFrame] = {}

    summaries["Resumen_Matriz"] = (
        df.groupby("MATRIZ_FINAL", dropna=False)
        .agg(
            SKUs=(cfg.col_sku, "count"),
            Margen_Total=(cfg.col_margin, "sum"),
            Demanda_Total=("Demanda_Total", "sum"),
            Margen_Promedio=(cfg.col_margin, "mean"),
            CV_Promedio=("CV", "mean"),
            CV_Robusto_Promedio=("CV_Robusto", "mean"),
            Score_Auto_Promedio=("SCORE_AUTOMATIZACION", "mean"),
            Frecuencia_Promedio=("%_Meses_Con_Venta", "mean"),
        )
        .sort_values("Margen_Total", ascending=False)
    )

    summaries["Resumen_ABC"] = (
        df.groupby("ABC")
        .agg(
            SKUs=(cfg.col_sku, "count"),
            Margen_Total=(cfg.col_margin, "sum"),
            Demanda_Total=("Demanda_Total", "sum"),
        )
        .sort_index()
    )

    summaries["Resumen_XYZ"] = (
        df.groupby("XYZ")
        .agg(
            SKUs=(cfg.col_sku, "count"),
            Margen_Total=(cfg.col_margin, "sum"),
            Demanda_Total=("Demanda_Total", "sum"),
        )
        .sort_index()
    )

    summaries["Matriz_Conteo"] = pd.crosstab(df["ABC"], df["XYZ"])

    # NUEVO: Resumen por clase de automatizacion
    summaries["Resumen_Automatizacion"] = (
        df.groupby("CLASE_AUTOMATIZACION", dropna=False)
        .agg(
            SKUs=(cfg.col_sku, "count"),
            Margen_Total=(cfg.col_margin, "sum"),
            Demanda_Total=("Demanda_Total", "sum"),
            Score_Promedio=("SCORE_AUTOMATIZACION", "mean"),
            Score_Min=("SCORE_AUTOMATIZACION", "min"),
            Score_Max=("SCORE_AUTOMATIZACION", "max"),
            Confianza_Promedio=("SCORE_CONFIANZA", "mean"),
        )
        .sort_values("Score_Promedio", ascending=False)
    )

    # NUEVO: Matriz cruzada ABC x Automatizacion
    summaries["ABC_vs_Auto"] = pd.crosstab(df["ABC"], df["CLASE_AUTOMATIZACION"])

    flag_cols = [c for c in ["FLAG_NUEVO", "FLAG_ESTACIONAL", "FLAG_DESCONTINUADO", "FLAG_SIN_VENTA",
                             "ROTACION_CONSTANTE", "ALERTA_CONST_PERO_Z"] if c in df.columns]
    if flag_cols:
        summaries["Resumen_Flags"] = pd.DataFrame({
            "Flag": flag_cols,
            "Cantidad": [int(df[c].sum()) for c in flag_cols],
            "%": [float(df[c].mean()) for c in flag_cols],
        })

    summaries["Top_Margen"] = df.sort_values(cfg.col_margin, ascending=False).head(200)

    # NUEVO: Top automatizables
    summaries["Top_Automatizables"] = (
        df[df["CLASE_AUTOMATIZACION"] == "AUTOMATIZAR"]
        .sort_values("SCORE_AUTOMATIZACION", ascending=False)
        .head(200)
    )

    return summaries


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def write_output_excel(
    df: pd.DataFrame,
    df_excluidos: pd.DataFrame,
    summaries: Dict[str, pd.DataFrame],
    output_path: Path,
    cfg: Config,
    demand_cols: List[str],
) -> None:
    df = df.copy()
    if cfg.col_branch and cfg.col_branch in df.columns:
        df.insert(0, "SKU_SUCURSAL",
                  df[cfg.col_sku].astype(str).str.strip() + " | " + df[cfg.col_branch].astype(str).str.strip())

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        id_cols = ["SKU_SUCURSAL"] if (cfg.col_branch and cfg.col_branch in df.columns) else []
        id_cols += [cfg.col_sku]
        if cfg.col_branch and cfg.col_branch in df.columns:
            id_cols.append(cfg.col_branch)
        base_cols = id_cols + [cfg.col_margin, "%_Margen", "%_Acum_Margen",
            "%_Acum_Volumen",
            "ABC_MARGEN", "ABC_VOLUMEN", "ABC", "XYZ", "MATRIZ_FINAL",
            "SCORE_AUTOMATIZACION", "CLASE_AUTOMATIZACION", "SCORE_CONFIANZA",
            "ESTRATEGIA",
            "Demanda_Total", "Meses_Con_Venta", "%_Meses_Con_Venta",
            "FRECUENCIA_VENTA", "ROTACION_CONSTANTE", "ALERTA_CONST_PERO_Z",
            "Venta_Promedio", "Venta_Promedio_NoCero",
            "CV", "CV_Robusto", "CV_Reciente", "CV_Incluyendo_Ceros",
            "N_Outliers", "MAD", "Forecast_Error_Proxy",
            "ADI", "CV2", "PATRON_DEMANDA",
            "CICLO_VIDA", "FLAG_NUEVO", "FLAG_DESCONTINUADO", "FLAG_SIN_VENTA",
            "FLAG_ESTACIONAL", "Mes_Pico", "Share_Top2_Meses", "Repite_Mes_Pico_Anios",
            "TENDENCIA",
            # Sub-scores de automatizacion (para transparencia)
            "_S_ABC", "_S_XYZ", "_S_CONFIANZA", "_S_FORECAST", "_S_FRECUENCIA", "_S_CICLOVIDA", "_S_CV_RECIENTE",
        ]
        cols = [c for c in base_cols if c in df.columns] + demand_cols
        df[cols].to_excel(writer, sheet_name="Analisis", index=False)

        df_excluidos.to_excel(writer, sheet_name="Excluidos", index=False)

        for name, sdf in summaries.items():
            sdf.to_excel(writer, sheet_name=name[:31])

        pd.DataFrame({
            "Parametro": list(asdict(cfg).keys()),
            "Valor": list(asdict(cfg).values()),
        }).to_excel(writer, sheet_name="Parametros", index=False)


# ---------------------------------------------------------------------------
# Pipeline principal
# ---------------------------------------------------------------------------

def procesar_abc_xyz_v3(cfg: Config) -> Path:
    input_path = Path(cfg.input_path)
    if not input_path.exists():
        raise FileNotFoundError(f"No existe el archivo: {input_path}")

    output_path = input_path.parent / cfg.output_filename

    log.info("Leyendo: %s", input_path)
    df = pd.read_excel(input_path, sheet_name=cfg.sheet_name)
    df = normalize_columns(df)

    required = [cfg.col_sku, cfg.col_margin]
    if cfg.col_branch:
        required.append(cfg.col_branch)
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Faltan columnas: {missing}. Detectadas: {list(df.columns)}")

    df[cfg.col_margin] = parse_margin(df[cfg.col_margin])

    date_cols = detect_date_columns(df)
    if not date_cols:
        raise ValueError("No se detectaron columnas de fecha para demanda.")

    df, _ = rename_date_columns(df, date_cols)
    df, demand_cols = clean_demand(df, date_cols, clip_negative=cfg.clip_negative_demand_to_zero)

    validate_inputs(df, cfg, demand_cols)

    df, df_excluidos = filter_products_with_audit(df, cfg, demand_cols)
    if len(df) == 0:
        raise ValueError("No quedaron productos luego del filtrado.")

    # ABC por margen
    df = compute_abc_margin(df, cfg)

    # Metricas de demanda (incluye outliers, CV robusto, CV reciente, forecast error)
    df, mat, nz_mask, periods, w = demand_features(df, demand_cols, cfg)
    df["Demanda_Total"] = mat.sum(axis=1)  # asegurar que existe antes de ABC volumen

    # ABC por volumen + ABC combinado
    df = compute_abc_volume(df, cfg)

    # Tendencia
    df = add_trend_label(df, w, cfg)

    # Frecuencia / rotacion
    df["FRECUENCIA_VENTA"] = df["%_Meses_Con_Venta"]
    df["ROTACION_CONSTANTE"] = df["FRECUENCIA_VENTA"] >= cfg.rotation_constant_threshold

    # XYZ (ahora con CV robusto)
    df = compute_xyz(df, cfg)
    df["ALERTA_CONST_PERO_Z"] = df["ROTACION_CONSTANTE"] & (df["XYZ"] == "Z")

    # Syntetos-Boylan
    df = demand_pattern_syntetos(df, n_periods=len(demand_cols))

    # Ciclo de vida
    df = lifecycle_flags(df, cfg)

    # Estacionalidad
    df = compute_seasonality(df, mat, periods, cfg)

    # Score de confianza
    df = compute_confidence_score(df, n_total_periods=len(demand_cols), cfg=cfg)

    # Score de automatizacion
    df = compute_automation_score(df, cfg)

    # Estrategias
    df = build_recommendations(df)

    # Resumenes
    summaries = build_summaries(df, cfg)

    log.info("Guardando: %s", output_path)
    write_output_excel(df, df_excluidos, summaries, output_path, cfg, demand_cols)
    log.info("OK. Archivo: %s", output_path)

    # ----- RESUMEN FINAL EN CONSOLA -----
    n = len(df)
    log.info("=== DISTRIBUCION ABC (combinado margen+volumen) ===")
    for cat, count in df["ABC"].value_counts().sort_index().items():
        log.info("  %s: %d SKUs (%.1f%%)", cat, count, count / n * 100)

    log.info("=== DISTRIBUCION XYZ (basada en CV_Robusto) ===")
    for cat, count in df["XYZ"].value_counts().sort_index().items():
        log.info("  %s: %d SKUs (%.1f%%)", cat, count, count / n * 100)

    log.info("=== MATRIZ ABC x XYZ ===")
    matriz = pd.crosstab(df["ABC"], df["XYZ"])
    for line in matriz.to_string().splitlines():
        log.info("  %s", line)

    log.info("=== DISTRIBUCION CICLO DE VIDA ===")
    for cat, count in df["CICLO_VIDA"].value_counts().items():
        log.info("  %s: %d SKUs (%.1f%%)", cat, count, count / n * 100)

    auto_counts = df["CLASE_AUTOMATIZACION"].value_counts()
    log.info("=== RESUMEN AUTOMATIZACION ===")
    for clase, count in auto_counts.items():
        pct = count / n * 100
        log.info("  %s: %d SKUs (%.1f%%)", clase, count, pct)

    # ----- LOG DE CALCULOS INTERMEDIOS POR SKU (top-N por margen) -----
    if cfg.verbose_top_n > 0:
        top = df.nlargest(cfg.verbose_top_n, cfg.col_margin)
        log.info("=== TOP %d POR MARGEN — calculos intermedios ===", cfg.verbose_top_n)
        for _, r in top.iterrows():
            log.info(
                "  [%s | %s] MARGEN=%.0f  %%Acum_Margen=%.4f  Demanda=%.0f  "
                "Meses_Venta=%d  CV=%.4f  CV_Robusto=%.4f  N_Outliers=%d  "
                "ABC_M=%s  ABC_V=%s  ABC=%s  XYZ=%s  MATRIZ=%s",
                str(r[cfg.col_sku])[:60],
                r[cfg.col_branch] if (cfg.col_branch and cfg.col_branch in r.index) else "-",
                r[cfg.col_margin], r["%_Acum_Margen"], r["Demanda_Total"],
                int(r["Meses_Con_Venta"]), r["CV"], r["CV_Robusto"], int(r["N_Outliers"]),
                r["ABC_MARGEN"], r["ABC_VOLUMEN"], r["ABC"], r["XYZ"], r["MATRIZ_FINAL"],
            )

    return output_path


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Clasificacion ABC-XYZ v3 — Potenciado para automatizacion.")
    parser.add_argument("--input", required=False,
                        default=r"C:\Users\user\OneDrive\Escritorio\DEMANDA 2024-2025\CLASIFICACION ABC-XYZ.xlsx")
    parser.add_argument("--output", required=False, default="RESULTADO_ESTRATEGICO_ABC_XYZ_v3.xlsx")
    parser.add_argument("--abc-by-branch", action="store_true")
    parser.add_argument("--keep-negative-demand", action="store_true")
    parser.add_argument("--no-volume-abc", action="store_true", help="Desactiva ABC dual (solo margen).")
    args = parser.parse_args()

    cfg = Config(
        input_path=args.input,
        output_filename=args.output,
        abc_by_branch=args.abc_by_branch,
        clip_negative_demand_to_zero=not args.keep_negative_demand,
        abc_volume_enabled=not args.no_volume_abc,
    )

    procesar_abc_xyz_v3(cfg)


if __name__ == "__main__":
    main()

