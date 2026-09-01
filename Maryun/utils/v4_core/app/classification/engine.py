"""Advanced ABC/XYZ classification engine — port of ``abc_xyz_mejorado_v3.py``.

This module preserves the full methodology of the user's script (outlier-robust
CV, SBT pattern, lifecycle, seasonality, confidence score, automation score)
and adapts the interface to the pipeline contract:

* Input: long-format ``demand`` DataFrame + ``products`` DataFrame (as coming
  out of the Extract stage).
* Output: a tuple ``(clasificacion, automatizacion_audit)`` where
  ``clasificacion`` matches :data:`normalize.ClassificationSchema` and
  ``automatizacion_audit`` is a rich side-table persisted by the Export stage.

Margin is not present in Maryun's ERP as a single column, so we approximate
``margen = costo * demanda_total`` (net-of-cost revenue proxy). When a true
margin column becomes available it can be injected via products.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass

import numpy as np
import pandas as pd

from app.config.engine_params import EngineParams
from app.temporal.gate import TemporalGate
from app.utils.logging import get_logger


# ---------------------------------------------------------------------------
# Internal parameters — mirror the defaults of the original v3 script.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _V3Params:
    abc_a: float = 0.80
    abc_b: float = 0.95
    abc_volume_a: float = 0.80
    abc_volume_b: float = 0.95

    xyz_x: float = 0.50
    xyz_y: float = 1.00

    outlier_iqr_multiplier: float = 2.0
    outlier_min_datapoints: int = 4
    cv_recent_months: int = 6

    new_product_months: int = 3
    discontinued_months: int = 6
    season_top2_share_threshold: float = 0.60
    season_min_years_repeat: int = 2

    trend_window_months: int = 6
    trend_up_threshold: float = 0.20
    trend_down_threshold: float = -0.20
    rotation_constant_threshold: float = 0.90

    confidence_min_months: int = 3
    confidence_good_months: int = 12

    auto_weight_abc: float = 0.20
    auto_weight_xyz: float = 0.25
    auto_weight_confidence: float = 0.15
    auto_weight_forecast_error: float = 0.15
    auto_weight_frequency: float = 0.10
    auto_weight_lifecycle: float = 0.10
    auto_weight_cv_recent: float = 0.05

    auto_threshold_full: float = 75.0
    auto_threshold_semi: float = 50.0
    auto_threshold_manual: float = 25.0


# ---------------------------------------------------------------------------
# Wide pivot helper
# ---------------------------------------------------------------------------
def _pivot_wide(demand: pd.DataFrame, gate: TemporalGate) -> tuple[pd.DataFrame, list[str]]:
    """Return ``(wide, demand_cols)`` with dense monthly columns inside the gate window."""
    if demand.empty:
        return pd.DataFrame(), []
    months = [pd.Timestamp(m) for m in gate.window.months]
    month_labels = [m.strftime("%Y-%m") for m in months]

    df = demand.copy()
    df["mes"] = pd.to_datetime(df["mes"])
    df["_label"] = df["mes"].dt.strftime("%Y-%m")

    wide = (
        df.groupby(["sku_id", "ubicacion", "_label"], sort=False)["demanda"]
        .sum()
        .unstack("_label", fill_value=0.0)
    )
    # Ensure every month column exists in correct order.
    for lbl in month_labels:
        if lbl not in wide.columns:
            wide[lbl] = 0.0
    wide = wide[month_labels]
    wide = wide.reset_index()
    return wide, month_labels


# ---------------------------------------------------------------------------
# IQR outlier helpers (vectorised per-row)
# ---------------------------------------------------------------------------
def _remove_outliers_iqr(mat: np.ndarray, multiplier: float, min_points: int) -> np.ndarray:
    clean = mat.copy()
    n_rows, n_cols = mat.shape
    for i in range(n_rows):
        row = mat[i]
        nz = row[row > 0]
        if len(nz) < min_points:
            continue
        q1, q3 = np.percentile(nz, [25, 75])
        iqr = q3 - q1
        lower = q1 - multiplier * iqr
        upper = q3 + multiplier * iqr
        for j in range(n_cols):
            if row[j] > 0 and (row[j] < lower or row[j] > upper):
                clean[i, j] = np.nan
    return clean


def _count_outliers(mat: np.ndarray, multiplier: float, min_points: int) -> np.ndarray:
    n_rows, n_cols = mat.shape
    counts = np.zeros(n_rows, dtype=int)
    for i in range(n_rows):
        row = mat[i]
        nz = row[row > 0]
        if len(nz) < min_points:
            continue
        q1, q3 = np.percentile(nz, [25, 75])
        iqr = q3 - q1
        lower = q1 - multiplier * iqr
        upper = q3 + multiplier * iqr
        counts[i] = int(np.sum((row > 0) & ((row < lower) | (row > upper))))
    return counts


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------
def run_classification_engine(
    demand: pd.DataFrame,
    products: pd.DataFrame,
    gate: TemporalGate,
    params: EngineParams,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return ``(clasificacion_schema_df, automatizacion_audit_df)``.

    - ``clasificacion_schema_df`` carries ABC/XYZ/clase for every
      ``(sku_id, ubicacion)`` with demand in the window. Matches
      :data:`ClassificationSchema` columns.
    - ``automatizacion_audit_df`` contains the rich v3 outputs (SBT pattern,
      lifecycle, seasonality, score de automatizacion, sub-scores, etc.)
      intended for the Export stage as a separate audit sheet.
    """
    log = get_logger("classification.engine")
    cp = _V3Params(
        abc_a=params.classification.abc_thresholds[0],
        abc_b=params.classification.abc_thresholds[1],
        xyz_x=params.classification.xyz_cv_thresholds[0],
        xyz_y=params.classification.xyz_cv_thresholds[1],
    )

    wide, demand_cols = _pivot_wide(demand, gate)
    if wide.empty:
        return (
            pd.DataFrame(columns=[
                "sku_id", "ubicacion", "abc_modelo", "xyz_modelo", "clase_modelo",
            ]),
            pd.DataFrame(),
        )

    # Cost lookup → margin proxy
    cost_map = (
        products.set_index("sku_id")["costo"].to_dict()
        if "costo" in products.columns else {}
    )
    wide["costo"] = wide["sku_id"].map(cost_map).fillna(1.0)

    mat = wide[demand_cols].to_numpy(dtype=float)
    n_rows, n_periods = mat.shape

    # Totals and moments
    total = mat.sum(axis=1)
    mean_all = mat.mean(axis=1)
    std_all = mat.std(axis=1, ddof=0)

    nz_mask = mat > 0
    nz_count = nz_mask.sum(axis=1)
    mean_nz = np.where(nz_count > 0, np.where(nz_mask, mat, 0).sum(axis=1) / np.maximum(nz_count, 1), 0.0)

    # CV (nonzero)
    mat_nz = np.where(nz_mask, mat, np.nan)
    std_nz = np.nanstd(mat_nz, axis=1, ddof=0)
    cv_all = np.where(mean_all > 0, std_all / mean_all, 0.0)
    cv_nz = np.where(mean_nz > 0, std_nz / mean_nz, 0.0)

    # Robust CV (outliers removed)
    mat_clean = _remove_outliers_iqr(mat, cp.outlier_iqr_multiplier, cp.outlier_min_datapoints)
    mat_clean_nz = np.where(mat_clean > 0, mat_clean, np.nan)
    mean_clean = np.nan_to_num(np.nanmean(mat_clean_nz, axis=1), nan=0.0)
    std_clean = np.nan_to_num(np.nanstd(mat_clean_nz, axis=1, ddof=0), nan=0.0)
    cv_robust = np.where(mean_clean > 0, std_clean / mean_clean, 0.0)
    n_outliers = _count_outliers(mat, cp.outlier_iqr_multiplier, cp.outlier_min_datapoints)

    # Recent CV
    recent_n = min(cp.cv_recent_months, n_periods)
    mat_recent = mat[:, -recent_n:]
    mat_recent_nz = np.where(mat_recent > 0, mat_recent, np.nan)
    mean_recent = np.nan_to_num(np.nanmean(mat_recent_nz, axis=1), nan=0.0)
    std_recent = np.nan_to_num(np.nanstd(mat_recent_nz, axis=1, ddof=0), nan=0.0)
    cv_recent = np.where(mean_recent > 0, std_recent / mean_recent, 0.0)

    # Forecast error proxy (MAD / mean)
    mad = np.zeros(n_rows, dtype=float)
    for i in range(n_rows):
        rnz = mat[i][mat[i] > 0]
        if len(rnz) >= 2:
            mad[i] = float(np.mean(np.abs(rnz - rnz.mean())))
    forecast_error_proxy = np.where(mean_nz > 0, mad / mean_nz, 0.0)

    # Frequency + first/last period
    freq = nz_count / max(n_periods, 1)
    any_sale = nz_mask.any(axis=1)
    first_idx = np.where(any_sale, nz_mask.argmax(axis=1), -1)
    last_idx = np.where(any_sale, (n_periods - 1) - np.flip(nz_mask, axis=1).argmax(axis=1), -1)
    periods_arr = np.array(demand_cols)
    first_p = np.where(first_idx >= 0, periods_arr[first_idx], None)
    last_p = np.where(last_idx >= 0, periods_arr[last_idx], None)

    def _ym_to_int(ym: str | None) -> float:
        if not ym:
            return np.nan
        y, m = ym.split("-")
        return int(y) * 12 + int(m)

    last_dataset = periods_arr[-1]
    last_int = _ym_to_int(last_dataset)
    first_int = np.array([_ym_to_int(p) for p in first_p])
    last_int_sale = np.array([_ym_to_int(p) for p in last_p])
    months_since_first = last_int - first_int
    months_since_last = last_int - last_int_sale

    # Trend
    w = max(1, min(cp.trend_window_months, n_periods // 2)) if n_periods >= 2 else 1
    recent_trend = mat[:, -w:].mean(axis=1)
    prev_trend = mat[:, -2 * w:-w].mean(axis=1) if n_periods >= 2 * w else mat[:, :w].mean(axis=1)
    growth = (recent_trend - prev_trend) / np.maximum(prev_trend, 1e-9)
    tendencia = np.full(n_rows, "ESTABLE", dtype=object)
    tendencia[growth >= cp.trend_up_threshold] = "ALZA"
    tendencia[growth <= cp.trend_down_threshold] = "BAJA"
    tendencia[(prev_trend == 0) & (recent_trend > 0)] = "RECIEN_ACTIVO"

    # Revenue + ABC by margin
    revenue = total * wide["costo"].to_numpy()
    order = np.argsort(-revenue)
    cum = np.cumsum(revenue[order])
    tot_rev = revenue.sum() if revenue.sum() > 0 else 1.0
    share = cum / tot_rev
    abc_m = np.full(n_rows, "C", dtype=object)
    abc_m[share <= cp.abc_b] = "B"
    abc_m[share <= cp.abc_a] = "A"
    abc_margin = np.empty(n_rows, dtype=object)
    abc_margin[order] = abc_m

    # ABC by volume
    order_v = np.argsort(-total)
    cum_v = np.cumsum(total[order_v])
    tot_vol = total.sum() if total.sum() > 0 else 1.0
    share_v = cum_v / tot_vol
    abc_v_sorted = np.full(n_rows, "C", dtype=object)
    abc_v_sorted[share_v <= cp.abc_volume_b] = "B"
    abc_v_sorted[share_v <= cp.abc_volume_a] = "A"
    abc_volume = np.empty(n_rows, dtype=object)
    abc_volume[order_v] = abc_v_sorted

    # Combined ABC (take the best of the two ranks)
    rank_map = {"A": 0, "B": 1, "C": 2}
    inv_map = {0: "A", 1: "B", 2: "C"}
    rm = np.array([rank_map[v] for v in abc_margin])
    rv = np.array([rank_map[v] for v in abc_volume])
    abc_combined = np.array([inv_map[i] for i in np.minimum(rm, rv)])

    # XYZ using robust CV
    xyz = np.full(n_rows, "Z", dtype=object)
    xyz[(mean_nz > 0) & (cv_robust <= cp.xyz_x)] = "X"
    xyz[(mean_nz > 0) & (cv_robust > cp.xyz_x) & (cv_robust <= cp.xyz_y)] = "Y"
    xyz[(mean_nz > 0) & (cv_robust > cp.xyz_y)] = "Z"

    clase = np.array([f"{a}{x}" for a, x in zip(abc_combined, xyz)])

    # Syntetos-Boylan
    adi = np.where(nz_count > 0, n_periods / np.maximum(nz_count, 1), np.inf)
    cv2 = np.square(cv_nz)
    patron = np.full(n_rows, "SIN_VENTA", dtype=object)
    mask = nz_count > 0
    patron[mask & (adi < 1.32) & (cv2 < 0.49)] = "SUAVE"
    patron[mask & (adi >= 1.32) & (cv2 < 0.49)] = "INTERMITENTE"
    patron[mask & (adi < 1.32) & (cv2 >= 0.49)] = "ERRATICA"
    patron[mask & (adi >= 1.32) & (cv2 >= 0.49)] = "IRREGULAR"

    # Lifecycle
    ciclo = np.where(
        total <= 0, "SIN_VENTA",
        np.where(months_since_first <= cp.new_product_months, "NUEVO",
                 np.where(months_since_last >= cp.discontinued_months,
                          "DESCONTINUADO", "ACTIVO")),
    )

    # Seasonality (per row)
    years = np.array([int(p.split("-")[0]) for p in demand_cols])
    months_of_cols = np.array([int(p.split("-")[1]) for p in demand_cols])
    idx_by_month = {m: np.where(months_of_cols == m)[0] for m in range(1, 13)}
    month_totals = np.zeros((n_rows, 12))
    years_repeat = np.zeros((n_rows, 12), dtype=int)
    for m in range(1, 13):
        idx = idx_by_month[m]
        if len(idx) == 0:
            continue
        mv = mat[:, idx]
        month_totals[:, m - 1] = mv.sum(axis=1)
        years_repeat[:, m - 1] = (mv > 0).sum(axis=1)
    unique_years = sorted(set(years.tolist()))
    year_sales_count = np.zeros(n_rows, dtype=int)
    for y in unique_years:
        idx = np.where(years == y)[0]
        if len(idx) == 0:
            continue
        year_sales_count += (mat[:, idx].sum(axis=1) > 0).astype(int)

    top1_idx = month_totals.argmax(axis=1)
    top1_total = month_totals[np.arange(n_rows), top1_idx]
    top2_idx = np.argpartition(month_totals, -2, axis=1)[:, -2:]
    top2_total = month_totals[np.arange(n_rows)[:, None], top2_idx].sum(axis=1)
    share_top2 = np.where(total > 0, top2_total / np.maximum(total, 1e-9), 0.0)
    repeat_top1 = years_repeat[np.arange(n_rows), top1_idx]
    estacional = (
        (total > 0)
        & (year_sales_count >= 2)
        & (share_top2 >= cp.season_top2_share_threshold)
        & (repeat_top1 >= cp.season_min_years_repeat)
    )
    peak_month = top1_idx + 1
    peak_name = np.array([calendar.month_name[m] for m in peak_month])

    # Confidence score
    hist_score = np.clip(months_since_first / cp.confidence_good_months, 0.0, 1.0)
    hist_score[months_since_first < cp.confidence_min_months] = 0.0
    active_months = np.maximum(months_since_first, 1)
    coverage = np.clip(nz_count / active_months, 0.0, 1.0)
    outlier_penalty = np.clip(1.0 - (n_outliers / np.maximum(nz_count, 1)) * 0.5, 0.3, 1.0)
    data_penalty = np.clip(nz_count / max(cp.confidence_min_months, 1), 0.0, 1.0)
    confidence = 0.35 * hist_score + 0.30 * coverage + 0.15 * outlier_penalty + 0.20 * data_penalty
    confidence[total <= 0] = 0.0

    # Automation score
    s_abc = np.array([{"A": 100.0, "B": 60.0, "C": 20.0}[v] for v in abc_combined])
    s_xyz = np.array([{"X": 100.0, "Y": 50.0, "Z": 10.0}[v] for v in xyz])
    s_conf = confidence * 100.0
    s_fe = np.clip(100.0 * (1.0 - forecast_error_proxy / 1.5), 0.0, 100.0)
    s_freq = freq * 100.0
    s_life = np.array([{"ACTIVO": 100.0, "NUEVO": 30.0,
                        "DESCONTINUADO": 10.0, "SIN_VENTA": 0.0}[v] for v in ciclo])
    s_cvr = np.clip(100.0 * (1.0 - cv_recent / 2.0), 0.0, 100.0)
    score = (
        cp.auto_weight_abc * s_abc
        + cp.auto_weight_xyz * s_xyz
        + cp.auto_weight_confidence * s_conf
        + cp.auto_weight_forecast_error * s_fe
        + cp.auto_weight_frequency * s_freq
        + cp.auto_weight_lifecycle * s_life
        + cp.auto_weight_cv_recent * s_cvr
    )
    score = np.where(estacional, score * 0.85, score)
    score = np.where(tendencia == "BAJA", score * 0.90, score)
    score = np.where(tendencia == "RECIEN_ACTIVO", score * 0.70, score)
    score = np.where(total <= 0, 0.0, score)
    auto_class = np.full(n_rows, "NO_AUTOMATIZAR", dtype=object)
    auto_class[score >= cp.auto_threshold_manual] = "MANUAL"
    auto_class[score >= cp.auto_threshold_semi] = "SEMI-AUTO"
    auto_class[score >= cp.auto_threshold_full] = "AUTOMATIZAR"

    # Schema output
    clasif = pd.DataFrame({
        "sku_id": wide["sku_id"].values,
        "ubicacion": wide["ubicacion"].values,
        "abc_modelo": abc_combined,
        "xyz_modelo": xyz,
        "clase_modelo": clase,
    })

    # Audit output (rich)
    audit = pd.DataFrame({
        "sku_id": wide["sku_id"].values,
        "ubicacion": wide["ubicacion"].values,
        "abc_margen": abc_margin,
        "abc_volumen": abc_volume,
        "abc": abc_combined,
        "xyz": xyz,
        "matriz_final": clase,
        "demanda_total": total,
        "meses_con_venta": nz_count,
        "pct_meses_con_venta": freq,
        "cv": cv_nz,
        "cv_robusto": cv_robust,
        "cv_reciente": cv_recent,
        "cv_incluyendo_ceros": cv_all,
        "n_outliers": n_outliers,
        "mad": mad,
        "forecast_error_proxy": forecast_error_proxy,
        "adi": adi,
        "cv2": cv2,
        "patron_demanda": patron,
        "ciclo_vida": ciclo,
        "flag_estacional": estacional,
        "mes_pico": peak_name,
        "share_top2_meses": share_top2,
        "repite_mes_pico_anios": repeat_top1,
        "tendencia": tendencia,
        "score_confianza": np.round(confidence, 4),
        "score_automatizacion": np.round(score, 2),
        "clase_automatizacion": auto_class,
        "sub_s_abc": np.round(s_abc, 1),
        "sub_s_xyz": np.round(s_xyz, 1),
        "sub_s_confianza": np.round(s_conf, 1),
        "sub_s_forecast": np.round(s_fe, 1),
        "sub_s_frecuencia": np.round(s_freq, 1),
        "sub_s_ciclovida": np.round(s_life, 1),
        "sub_s_cv_reciente": np.round(s_cvr, 1),
    })

    log.info(
        "classification.engine.done",
        rows=len(clasif),
        automatizables=int((auto_class == "AUTOMATIZAR").sum()),
        semi_auto=int((auto_class == "SEMI-AUTO").sum()),
    )
    return clasif, audit
