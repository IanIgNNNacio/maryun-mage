import warnings
warnings.filterwarnings("ignore")

import os
import sys
import json
import logging
import argparse
import datetime
import importlib
import collections
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats
from statsmodels.tsa.holtwinters import ExponentialSmoothing

try:
    from statsmodels.tsa.seasonal import STL
    HAS_STL = True
except ImportError:
    HAS_STL = False

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

try:
    from joblib import Parallel, delayed, dump as jdump, load as jload
    HAS_JOBLIB = True
except ImportError:
    HAS_JOBLIB = False

VERSION_SCRIPT = "v10"

# ═══════════════════════════════════════════════════════════════════
# 1. ARGUMENTOS CLI
# ═══════════════════════════════════════════════════════════════════
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Sistema de Pronóstico de Demanda v10",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--input", "-i",
        default=r"C:\Users\user\OneDrive\Escritorio\DEMANDA 2024-2025\DEMANDA ESTANDARIZADA.xlsx",
        help="Ruta al archivo Excel de entrada",
    )
    p.add_argument(
        "--output", "-o",
        default=r"C:\Users\user\OneDrive\Escritorio\DEMANDA 2024-2025\PRONOSTICOS_DEMANDA_v10.xlsx",
        help="Ruta al archivo Excel de salida",
    )
    p.add_argument(
        "--sheet", default="Hoja2",
        help="Nombre de la hoja en el Excel de entrada",
    )
    p.add_argument(
        "--id-col", default="SKU 2.0",
        help="Nombre de la columna que actúa como ID_KEY (default: 'SKU 2.0')",
    )
    p.add_argument(
        "--target-start", default=None,
        help="Primer mes a pronosticar YYYY-MM-DD (default: auto-detectado)",
    )
    p.add_argument(
        "--log-file", default=None,
        help="Archivo de log (default: solo consola)",
    )
    p.add_argument(
        "--horizonte", type=int, default=12,
        help="Meses a pronosticar",
    )
    return p


# ═══════════════════════════════════════════════════════════════════
# 2. LOGGING
# ═══════════════════════════════════════════════════════════════════
def setup_logging(log_file=None):
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
# 3. CONSTANTES GLOBALES (no rutas, no TARGET_START)
# ═══════════════════════════════════════════════════════════════════
M_SEASON      = 12
MIN_NZ        = 1
SEED          = 7
N_BOOTSTRAP   = 300
MAX_CV_FOLDS  = 4
MIN_TRAIN     = 14
TOP_K         = 3
BIAS_ALPHA    = 0.5
MIN_OBS_CORR  = 2
CHECKPOINT_SIZE = 500
DRIFT_THRESHOLD = 0.15   # 15% degradación = alerta
ML_LAGS       = [1, 2, 3, 6, 12]
ML_WINDOWS    = [3, 6, 12]
MIN_OBS_ML    = 18
MIN_SERIES_ML = 10
ML_CV_FOLDS   = 3
WINTER_MONTHS = {10, 11, 12, 1, 2, 3}
SUMMER_MONTHS = {4,  5,  6,  7,  8,  9}
MONTH_NAMES   = ['Ene','Feb','Mar','Abr','May','Jun',
                 'Jul','Ago','Sep','Oct','Nov','Dic']

np.random.seed(SEED)

# ═══════════════════════════════════════════════════════════════════
# 4. DETECCIÓN AUTOMÁTICA DE BACKEND ML
# ═══════════════════════════════════════════════════════════════════
GBM_BACKEND = "none"
GBM_CLASS   = None
HAS_ML      = False
for _name, _mod_path, _cls in [
    ("LightGBM",         "lightgbm",         "LGBMRegressor"),
    ("XGBoost",          "xgboost",          "XGBRegressor"),
    ("GradientBoosting", "sklearn.ensemble", "GradientBoostingRegressor"),
    ("RandomForest",     "sklearn.ensemble", "RandomForestRegressor"),
]:
    try:
        _mod      = importlib.import_module(_mod_path)
        GBM_CLASS = getattr(_mod, _cls)
        GBM_BACKEND = _name
        HAS_ML    = True
        break
    except (ImportError, AttributeError):
        pass

# ═══════════════════════════════════════════════════════════════════
# 5. UTILIDADES BÁSICAS
# ═══════════════════════════════════════════════════════════════════
def parse_header_to_year_month(cell):
    import datetime as _dt
    try:
        if isinstance(cell, (pd.Timestamp, np.datetime64, pd.Period,
                             _dt.datetime, _dt.date)):
            ts = pd.Timestamp(cell).to_period('M')
            return int(ts.year), int(ts.month)
    except Exception:
        pass
    try:
        ts = pd.to_datetime(str(cell), errors='raise').to_period('M')
        return int(ts.year), int(ts.month)
    except Exception:
        return None


def make_unique(cols):
    seen, out = {}, []
    for c in cols:
        c0 = str(c)
        if c0 not in seen:
            seen[c0] = 1; out.append(c0)
        else:
            seen[c0] += 1; out.append(f"{c0}__{seen[c0]}")
    return out


def complete_monthly_index(ts: pd.Series) -> pd.Series:
    ts = ts.sort_index()
    if ts.empty:
        return ts
    idx = pd.date_range(
        start=ts.index.min().to_period('M').to_timestamp(),
        end  =ts.index.max().to_period('M').to_timestamp(), freq='MS')
    ts2 = ts.reindex(idx).fillna(0.0)
    ts2.index.freq = 'MS'
    return ts2


def cut_to_target(ts: pd.Series, target_start: pd.Timestamp):
    """
    Recorta la serie hasta el mes anterior a target_start.
    Si hay un gap entre el último dato y target_start, lo rellena con ceros.
    Retorna (ts_cut, 0) — gap siempre 0 después del relleno.
    """
    ts     = complete_monthly_index(ts)
    cutoff = target_start - pd.offsets.MonthBegin(1)
    ts_cut = ts[ts.index <= cutoff]
    if ts_cut.empty:
        ts_cut = ts.copy()
        cutoff = ts_cut.index[-1]
    # Detectar y rellenar gap
    nxt = ts_cut.index[-1] + pd.offsets.MonthBegin(1)
    gap = max(0, (target_start.year - nxt.year) * 12
                 + (target_start.month - nxt.month))
    if gap > 0:
        gap_idx  = pd.date_range(start=nxt, periods=gap, freq='MS')
        gap_vals = pd.Series(0.0, index=gap_idx)
        ts_cut   = pd.concat([ts_cut, gap_vals])
        ts_cut.index.freq = 'MS'
    return ts_cut, 0


def future_index_from_last(ts: pd.Series, h: int) -> pd.DatetimeIndex:
    return pd.date_range(
        start=ts.index[-1] + pd.offsets.MonthBegin(1), periods=h, freq='MS')


def robust_median(x):
    x = np.asarray(x, dtype=float); x = x[~np.isnan(x)]
    return float(np.median(x)) if x.size > 0 else 0.0


def recent_stats(ts: pd.Series) -> dict:
    if ts.empty:
        return dict(sum_last12=0., med_last12=0., med_last6=0.,
                    med_last3=0., share_last12=0.)
    l12 = ts[-12:] if len(ts) >= 12 else ts
    l6  = ts[-6:]  if len(ts) >= 6  else ts
    l3  = ts[-3:]  if len(ts) >= 3  else ts
    return dict(
        sum_last12   = float(l12.sum()),
        med_last12   = robust_median(l12[l12 > 0]),
        med_last6    = robust_median(l6[l6 > 0]),
        med_last3    = robust_median(l3[l3 > 0]),
        share_last12 = float((l12 > 0).mean()),
    )


def detect_target_start_auto(df_long: pd.DataFrame) -> pd.Timestamp:
    """Último mes con datos + 1 mes."""
    last = df_long['Fecha'].max()
    return last + pd.offsets.MonthBegin(1)

# ═══════════════════════════════════════════════════════════════════
# 6. VALIDACIÓN DE INPUTS
# ═══════════════════════════════════════════════════════════════════
def validate_inputs(df_long: pd.DataFrame, date_cols: list, id_col_name: str):
    """Lanza ValueError descriptivo ante cualquier problema estructural."""
    if not date_cols:
        raise ValueError(
            "No se detectaron columnas de fecha en el Excel de entrada. "
            "Verifique que las cabeceras sean fechas válidas.")

    if df_long['ID_KEY'].isnull().any():
        n_null = df_long['ID_KEY'].isnull().sum()
        raise ValueError(
            f"La columna ID_KEY ('{id_col_name}') contiene {n_null} valores nulos. "
            "Elimine o complete las filas sin ID antes de ejecutar.")

    # Mínimo 3 meses de historia por alguna serie
    meses_por_serie = (df_long[df_long['Demanda'] > 0]
                       .groupby('ID_KEY')['Fecha'].nunique())
    if meses_por_serie.empty or meses_por_serie.max() < 3:
        raise ValueError(
            "Ninguna serie tiene al menos 3 meses con demanda > 0. "
            "Se requiere más historia para pronosticar.")

    # Fechas duplicadas por clave — solo informar, se agregan por suma antes de llegar aquí
    dup = (df_long.groupby(['ID_KEY', 'Fecha']).size().reset_index(name='n')
           .query('n > 1'))
    if not dup.empty:
        ejemplos = dup.head(3)[['ID_KEY', 'Fecha']].to_string(index=False)
        log.warning(
            f"Se detectaron {len(dup)} combinaciones (ID_KEY, Fecha) duplicadas. "
            f"Se agregaron por suma automáticamente.\nEjemplos:\n{ejemplos}")

    log.info("Validación de inputs: OK")

# ═══════════════════════════════════════════════════════════════════
# 7. CORRECCIÓN DE OUTLIERS
# ═══════════════════════════════════════════════════════════════════
def remove_outliers(ts: pd.Series, m: int = M_SEASON, k: float = 3.5) -> pd.Series:
    y = ts.values.astype(float).copy(); n = len(y)
    if n <= m:
        return ts
    for i in range(n):
        same = [j for j in range(i % m, n, m) if j != i]
        if len(same) < 2:
            continue
        sv  = y[same]; med = np.median(sv)
        mad = np.median(np.abs(sv - med))
        sigma = 1.4826 * mad if mad > 0 else (np.std(sv) or 1.0)
        if np.abs(y[i] - med) > k * sigma:
            y[i] = med
    return pd.Series(y, index=ts.index)

# ═══════════════════════════════════════════════════════════════════
# 8. CLASIFICACIÓN SBT
# ═══════════════════════════════════════════════════════════════════
def classify_sbt(ts: pd.Series):
    vals = ts.values.astype(float); nz = vals[vals > 0]
    n, nzn = len(vals), len(nz)
    if nzn == 0:
        return "lumpy", float('inf'), float('inf')
    adi = n / nzn
    cv2 = (np.std(nz, ddof=1) / np.mean(nz)) ** 2 if nzn > 1 else 0.0
    if   adi < 1.32 and cv2 < 0.49:  cat = "smooth"
    elif adi < 1.32 and cv2 >= 0.49: cat = "erratic"
    elif adi >= 1.32 and cv2 < 0.49: cat = "intermittent"
    else:                             cat = "lumpy"
    return cat, float(adi), float(cv2)

# ═══════════════════════════════════════════════════════════════════
# 9. ANÁLISIS ESTACIONAL (STL)
# ═══════════════════════════════════════════════════════════════════
def _seasonal_indices(ts: pd.Series, m: int = M_SEASON) -> np.ndarray:
    y = ts.values.astype(float); n = len(y)
    if n < m + 1:
        return np.ones(m)
    ma  = np.convolve(y, np.ones(m) / m, mode='valid')
    off = m // 2
    rat = np.full(n, np.nan)
    den = ma.copy(); den[den <= 0] = 1e-9
    rat[off: off + len(ma)] = y[off: off + len(ma)] / den
    months  = ts.index.month
    indices = np.ones(m)
    for s in range(m):
        v = rat[months == s + 1]; v = v[~np.isnan(v)]
        if len(v) > 0:
            indices[s] = np.median(v)
    mn = indices.mean()
    return (indices / mn) if mn > 0 else indices


def analyze_seasonality(ts: pd.Series, m: int = M_SEASON) -> dict:
    if len(ts) < 2 * m:
        return dict(F_s=0.0, seasonal_type="stable",
                    peak_months=[], indices=np.ones(m))
    F_s = 0.0
    if HAS_STL:
        try:
            res = STL(ts, period=m, robust=True).fit()
            vr  = np.var(res.resid); vsr = np.var(res.seasonal + res.resid)
            if vsr > 0: F_s = max(0.0, 1.0 - vr / vsr)
        except Exception:
            pass
    if F_s == 0.0:
        try:
            groups = [ts[ts.index.month == mm].values
                      for mm in range(1, m + 1)
                      if (ts.index.month == mm).any()]
            if len(groups) >= 3:
                _, p = scipy_stats.f_oneway(*groups)
                F_s  = max(0.0, min(0.9, 1.0 - p)) if np.isfinite(p) else 0.0
        except Exception:
            pass
    indices     = _seasonal_indices(ts, m)
    peak_months = [i + 1 for i in range(m) if indices[i] > 1.15]
    if F_s < 0.15 or not peak_months:
        stype = "stable" if F_s < 0.15 else "mixed"
    else:
        w = sum(1 for mm in peak_months if mm in WINTER_MONTHS)
        s = sum(1 for mm in peak_months if mm in SUMMER_MONTHS)
        if w >= 2 and w >= s:  stype = "winter"
        elif s >= 2 and s > w: stype = "summer"
        else:                  stype = "mixed"
    return dict(F_s=float(F_s), seasonal_type=stype,
                peak_months=peak_months, indices=indices)

# ═══════════════════════════════════════════════════════════════════
# 10. DETECCIÓN DE TENDENCIA
# ═══════════════════════════════════════════════════════════════════
def detect_trend(ts: pd.Series):
    y = ts.values.astype(float)
    if len(y) < 5:
        return 0.0, 1.0, False
    slope, _, _, p, _ = scipy_stats.linregress(
        np.arange(len(y), dtype=float), y)
    return float(slope), float(p), bool(p < 0.05)

# ═══════════════════════════════════════════════════════════════════
# 11. MÉTRICAS
# ═══════════════════════════════════════════════════════════════════
def mase(y_true, y_pred, y_ins, m=1) -> float:
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    y_ins  = np.asarray(y_ins,  dtype=float)
    scale  = (np.mean(np.abs(y_ins[m:] - y_ins[:-m])) if len(y_ins) > m
              else (np.mean(np.abs(np.diff(y_ins))) if len(y_ins) > 1 else 1.0))
    scale  = scale if (scale > 0 and np.isfinite(scale)) else 1.0
    return float(np.mean(np.abs(y_true - y_pred)) / scale)


def mae_metric(y_true, y_pred) -> float:
    return float(np.mean(np.abs(
        np.asarray(y_true, dtype=float) - np.asarray(y_pred, dtype=float))))


def rmse_metric(y_true, y_pred) -> float:
    return float(np.sqrt(np.mean((
        np.asarray(y_true, dtype=float) - np.asarray(y_pred, dtype=float)) ** 2)))

# ═══════════════════════════════════════════════════════════════════
# 12. MODELOS ESTADÍSTICOS
# ═══════════════════════════════════════════════════════════════════
def forecast_ses(tr, h):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        fit = ExponentialSmoothing(tr, trend=None, seasonal=None).fit(optimized=True)
    p = fit.forecast(h); p.index = future_index_from_last(tr, h); return p


def forecast_theta(tr, h):
    """
    Método Theta correcto (Assimakopoulos & Nikolopoulos 2000).
    Theta_0 (θ=0): extrapolación lineal pura (OLS).
    Theta_2 (θ=2): SES sobre la serie original (amplifica curvatura).
    Pronóstico final = promedio de ambas líneas.
    Matemáticamente equivalente a SES con drift.
    """
    y = tr.values.astype(float); n = len(y)
    t = np.arange(1, n + 1, dtype=float)
    # Theta_0: tendencia lineal pura
    slope, intercept = np.polyfit(t, y, 1)
    t_future  = np.arange(n + 1, n + h + 1, dtype=float)
    theta_0_fc = intercept + slope * t_future
    # Theta_2: SES sobre serie original
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ses_fit    = ExponentialSmoothing(
            tr, trend=None, seasonal=None).fit(optimized=True)
        theta_2_fc = ses_fit.forecast(h).values
    final = (theta_0_fc + theta_2_fc) / 2.0
    return pd.Series(final, index=future_index_from_last(tr, h))


def _fitted_ses(tr):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        fit = ExponentialSmoothing(tr, trend=None, seasonal=None).fit(optimized=True)
    return fit.fittedvalues.values.astype(float)


def _fitted_values(name, tr):
    """Valores in-sample ajustados para calcular residuos del ensemble."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            if name == "SES":
                fit = ExponentialSmoothing(tr, trend=None, seasonal=None).fit(optimized=True)
                return fit.fittedvalues.values.astype(float)
            elif name == "Holt-Damped":
                fit = ExponentialSmoothing(
                    tr, trend='add', damped_trend=True, seasonal=None).fit(optimized=True)
                return fit.fittedvalues.values.astype(float)
            elif name in ("HW-Add", "HW-Mul"):
                if (tr.values <= 0).any():
                    name = "HW-Add"
                seasonal = 'add' if name == "HW-Add" else 'mul'
                fit = ExponentialSmoothing(
                    tr, trend='add', seasonal=seasonal,
                    seasonal_periods=M_SEASON).fit(optimized=True)
                return fit.fittedvalues.values.astype(float)
            elif name == "Theta":
                y = tr.values.astype(float); n = len(y)
                t = np.arange(1, n + 1, dtype=float)
                slope, intercept = np.polyfit(t, y, 1)
                theta_0_ins = intercept + slope * t
                ses_fit = ExponentialSmoothing(
                    tr, trend=None, seasonal=None).fit(optimized=True)
                return ((theta_0_ins + ses_fit.fittedvalues.values) / 2.0).astype(float)
            else:
                return np.full(len(tr), float(np.mean(tr.values)))
        except Exception:
            return np.full(len(tr), float(np.mean(tr.values)))


def forecast_holt_damped(tr, h):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        fit = ExponentialSmoothing(
            tr, trend='add', damped_trend=True, seasonal=None).fit(optimized=True)
    p = fit.forecast(h); p.index = future_index_from_last(tr, h); return p


def forecast_hw_add(tr, h, m=M_SEASON):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        fit = ExponentialSmoothing(
            tr, trend='add', seasonal='add',
            seasonal_periods=m).fit(optimized=True)
    p = fit.forecast(h); p.index = future_index_from_last(tr, h); return p


def forecast_hw_mul(tr, h, m=M_SEASON):
    if (tr.values <= 0).any():
        return forecast_hw_add(tr, h, m)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        fit = ExponentialSmoothing(
            tr, trend='add', seasonal='mul',
            seasonal_periods=m).fit(optimized=True)
    p = fit.forecast(h); p.index = future_index_from_last(tr, h); return p


def forecast_naive_seasonal(tr, h, m=M_SEASON):
    y = tr.values.astype(float)
    cycle = y[-m:] if len(y) >= m else y
    tiled = np.tile(cycle, (h // len(cycle)) + 2)[:h]
    return pd.Series(tiled, index=future_index_from_last(tr, h))


def forecast_croston_sba(tr, h, alpha=0.2):
    y = np.asarray(tr.values, dtype=float)
    nz = np.where(y > 0)[0]
    if len(nz) == 0:
        return pd.Series(np.zeros(h), index=future_index_from_last(tr, h))
    z, p, prev = float(y[nz[0]]), float(nz[0] + 1 if nz[0] > 0 else 1), nz[0]
    for i in range(nz[0] + 1, len(y)):
        if y[i] > 0:
            z = alpha * y[i] + (1 - alpha) * z
            p = alpha * (i - prev) + (1 - alpha) * p; prev = i
    f = max(0.0, (1.0 - alpha / 2.0) * z / p)
    return pd.Series(np.repeat(f, h), index=future_index_from_last(tr, h))


def forecast_tsb(tr, h, alpha=0.3, beta=0.3):
    y = np.asarray(tr.values, dtype=float); p = z = None
    for t in range(len(y)):
        I = 1.0 if y[t] > 0 else 0.0
        p = I if p is None else p + alpha * (I - p)
        if I > 0: z = y[t] if z is None else z + beta * (y[t] - z)
    f = 0.0 if (p is None or z is None) else max(0.0, p * z)
    return pd.Series(np.repeat(f, h), index=future_index_from_last(tr, h))


def _predict(name, tr, h):
    return {
        "SES":           forecast_ses,
        "Theta":         forecast_theta,
        "Holt-Damped":   forecast_holt_damped,
        "HW-Add":        forecast_hw_add,
        "HW-Mul":        forecast_hw_mul,
        "NaiveSeasonal": forecast_naive_seasonal,
        "Croston-SBA":   forecast_croston_sba,
        "TSB":           forecast_tsb,
    }[name](tr, h)

# ═══════════════════════════════════════════════════════════════════
# 13. SELECCIÓN INTELIGENTE DE MODELOS
# ═══════════════════════════════════════════════════════════════════
def select_candidate_models(sbt_class, F_s, seasonal_type,
                            has_trend, n_obs) -> list:
    can_seasonal = n_obs >= M_SEASON + 2
    if sbt_class in ("intermittent", "lumpy"):
        pool = ["Croston-SBA", "TSB", "SES"]
        if has_trend: pool.append("Theta")
        return pool
    if F_s >= 0.45 and can_seasonal:
        pool = (["HW-Mul", "HW-Add", "NaiveSeasonal", "Theta"]
                if seasonal_type in ("winter", "summer")
                else ["HW-Mul", "HW-Add", "Theta", "Holt-Damped"])
    elif F_s >= 0.20 and can_seasonal:
        pool = (["HW-Add", "HW-Mul", "Theta", "Holt-Damped"]
                if seasonal_type in ("winter", "summer")
                else ["HW-Add", "Theta", "Holt-Damped", "SES"])
    else:
        pool = (["Holt-Damped", "Theta", "SES"] if has_trend
                else ["SES", "Theta", "Holt-Damped"])
    if sbt_class == "erratic" and "SES" not in pool:
        pool.append("SES")
    return pool

# ═══════════════════════════════════════════════════════════════════
# 14. WALK-FORWARD CV — con métricas MAE y RMSE por serie
# ═══════════════════════════════════════════════════════════════════
def walk_forward_cv(ts, models, h, min_train=MIN_TRAIN,
                    max_folds=MAX_CV_FOLDS) -> tuple:
    """
    Retorna:
      scores_mase : {modelo: MASE promedio}
      cv_summary  : {'MASE_CV': float, 'MAE_CV': float, 'RMSE_CV': float, 'N_Folds_CV': int}
                    calculados sobre el mejor modelo.
    """
    L = len(ts)
    all_mase = {m: [] for m in models}
    all_mae  = {m: [] for m in models}
    all_rmse = {m: [] for m in models}
    n_folds_used = 0

    for fold in range(1, max_folds + 1):
        cut = L - fold * h
        if cut < min_train:
            break
        tr, te = ts.iloc[:cut], ts.iloc[cut:cut + h]
        if len(te) == 0:
            continue
        n_folds_used = fold
        for m in models:
            try:
                pred = _predict(m, tr, h)
                n    = min(len(te), len(pred))
                yt, yp = te.values[:n], pred.values[:n]
                sc_mase = mase(yt, yp, tr.values, m=M_SEASON)
                if np.isfinite(sc_mase):
                    all_mase[m].append(sc_mase)
                    all_mae[m].append(mae_metric(yt, yp))
                    all_rmse[m].append(rmse_metric(yt, yp))
            except Exception:
                pass

    scores_mase = {m: float(np.nanmean(v)) for m, v in all_mase.items() if v}
    if scores_mase:
        best_m = min(scores_mase, key=scores_mase.get)
        cv_summary = {
            'MASE_CV':    round(float(np.nanmean(all_mase[best_m])), 4),
            'MAE_CV':     round(float(np.nanmean(all_mae[best_m])), 4),
            'RMSE_CV':    round(float(np.nanmean(all_rmse[best_m])), 4),
            'N_Folds_CV': n_folds_used,
        }
    else:
        cv_summary = {'MASE_CV': None, 'MAE_CV': None,
                      'RMSE_CV': None, 'N_Folds_CV': 0}
    return scores_mase, cv_summary

# ═══════════════════════════════════════════════════════════════════
# 15. ENSEMBLE PONDERADO (Bates-Granger)
# ═══════════════════════════════════════════════════════════════════
def ensemble_weighted(scores, forecasts, top_k=TOP_K):
    if not scores or not forecasts:
        return None, []
    top  = sorted(scores.items(), key=lambda x: x[1])[:top_k]
    top  = [(m, s) for m, s in top if m in forecasts]
    if not top:
        return None, []
    mods, errs = zip(*top)
    errs   = np.clip(np.array(errs, dtype=float), 1e-9, None)
    w      = (1.0 / errs) / (1.0 / errs).sum()
    result = None
    for wi, m in zip(w, mods):
        arr    = forecasts[m].values.astype(float)
        result = wi * arr if result is None else result + wi * arr
    return pd.Series(result, index=forecasts[mods[0]].index), list(zip(mods, w))

# ═══════════════════════════════════════════════════════════════════
# 16. PISO OPTIMISTA POR CLASE SBT
# ═══════════════════════════════════════════════════════════════════
def apply_floor(pred, ts_train, dclass, share_last12=None):
    """
    Aplica piso mínimo según clase SBT.
    Si share_last12 < 0.10 → producto descontinuado, piso = 0.
    """
    s = recent_stats(ts_train)
    if share_last12 is not None and share_last12 < 0.10:
        return pred, 0.0
    fv = {
        "smooth":       max(1.0, 0.70 * max(s['med_last6'],
                                            s['med_last3'], s['med_last12'])),
        "erratic":      max(1.0, 0.50 * max(s['med_last6'], s['med_last12'])),
        "intermittent": max(0.0, 0.30 * s['med_last12']),
        "lumpy":        max(0.0, 0.15 * s['med_last12']),
    }.get(dclass, 0.0)
    arr = np.maximum(pred.values, fv) if fv > 0 else pred.values
    return pd.Series(arr, index=pred.index), float(fv)

# ═══════════════════════════════════════════════════════════════════
# 17. INTERVALOS BOOTSTRAP — residuos del ensemble in-sample
# ═══════════════════════════════════════════════════════════════════
def bootstrap_pi(ts_train, final_pred, ensemble_components, h,
                 n_boot=N_BOOTSTRAP, levels=(0.80, 0.95)):
    """
    ensemble_components: list of (model_name, weight) del ensemble.
    Usa residuos in-sample del ensemble ponderado cuando es posible.
    """
    y = ts_train.values.astype(float)
    resid = None
    if ensemble_components:
        try:
            insample_ens = np.zeros(len(ts_train))
            for name, w in ensemble_components:
                if name == "GlobalML":
                    continue
                fv = _fitted_values(name, ts_train)
                n  = min(len(fv), len(insample_ens))
                insample_ens[:n] += w * fv[:n]
            resid = y - insample_ens
        except Exception:
            resid = None

    if resid is None or not np.any(np.isfinite(resid)):
        resid = y - np.median(y)

    resid = resid[np.isfinite(resid)]
    if len(resid) == 0:
        resid = np.array([0.0])

    rng   = np.random.RandomState(SEED)
    paths = np.array([
        np.maximum(0.0, final_pred.values +
                   rng.choice(resid, size=h, replace=True))
        for _ in range(n_boot)])

    q_map = {0.80: (10, 90), 0.95: (2.5, 97.5)}
    return {lvl: (np.maximum(0.0, np.percentile(paths, q_map[lvl][0], axis=0)),
                  np.percentile(paths, q_map[lvl][1], axis=0))
            for lvl in levels}

# ═══════════════════════════════════════════════════════════════════
# 18. SISTEMA ADAPTATIVO
# ═══════════════════════════════════════════════════════════════════
def load_previous_forecasts(output_path: str) -> pd.DataFrame:
    if not Path(output_path).exists():
        return pd.DataFrame()
    try:
        df  = pd.read_excel(output_path, sheet_name="Pronosticos",
                            parse_dates=['Fecha'])
        req = {'ID_KEY', 'Fecha', 'Pronostico'}
        if not req.issubset(df.columns):
            return pd.DataFrame()
        return df[['ID_KEY', 'Fecha', 'Pronostico']].copy()
    except Exception:
        return pd.DataFrame()


def compute_adaptive_corrections(prev_df: pd.DataFrame,
                                  df_long_actual: pd.DataFrame) -> dict:
    if prev_df.empty:
        return {}
    corrections = {}
    actual_map  = (df_long_actual.groupby(['ID_KEY', 'Fecha'])['Demanda']
                   .sum().reset_index())
    for key, grp in prev_df.groupby('ID_KEY'):
        act    = actual_map[actual_map['ID_KEY'] == key]
        if act.empty:
            continue
        merged = (grp.merge(act, on='Fecha', how='inner')
                     .dropna(subset=['Pronostico', 'Demanda']))
        if len(merged) < 1:
            continue
        errors = merged['Demanda'] - merged['Pronostico']
        mu     = float(merged['Demanda'].mean())
        bias   = float(errors.mean())
        rmse_v = float(np.sqrt((errors ** 2).mean()))
        # MASE aproximado usando escala naive
        scale  = float(merged['Demanda'].diff().abs().mean()) or 1.0
        mase_v = float(errors.abs().mean() / scale)
        over_fc = float((merged['Pronostico'] > merged['Demanda']).mean())
        corrections[key] = {
            'bias':              bias,
            'bias_pct':          bias / mu if mu > 0 else 0.0,
            'n_obs':             int(len(merged)),
            'rmse':              rmse_v,
            'mase_real':         mase_v if np.isfinite(mase_v) else None,
            'mean_actual':       mu,
            'error_direction':   float(np.sign(bias)),
            'over_forecast_rate': over_fc,
        }
    return corrections


def apply_adaptive_correction(pred: pd.Series, correction: dict,
                               alpha: float = BIAS_ALPHA):
    if not correction or correction.get('n_obs', 0) < MIN_OBS_CORR:
        return pred, 0.0
    if abs(correction.get('bias_pct', 0.0)) < 0.05:
        return pred, 0.0
    corr_val  = alpha * correction['bias']
    corrected = np.maximum(0.0, pred.values + corr_val)
    return pd.Series(corrected, index=pred.index), float(corr_val)

# ═══════════════════════════════════════════════════════════════════
# 19. GLOBALMLFORECASTER — modelo ML cross-series
# ═══════════════════════════════════════════════════════════════════
class GlobalMLForecaster:
    """
    Entrena UN modelo de gradient boosting sobre TODAS las series.
    Incluye features de error histórico para autocorrección sistemática.
    AVISO: cv_score evalúa el modelo entrenado con datos completos —
           existe data leakage. El score se reporta como (optimistic) y
           se penaliza con ×1.20 al asignar peso en el ensemble.
    """
    _PARAMS = {
        "LightGBM": dict(n_estimators=500, learning_rate=0.05,
                         num_leaves=31, min_child_samples=15,
                         subsample=0.8, colsample_bytree=0.8,
                         reg_alpha=0.05, reg_lambda=0.1,
                         n_jobs=-1, random_state=SEED, verbose=-1),
        "XGBoost":  dict(n_estimators=500, learning_rate=0.05,
                         max_depth=6, subsample=0.8, colsample_bytree=0.8,
                         reg_alpha=0.05, reg_lambda=1.0,
                         n_jobs=-1, random_state=SEED, verbosity=0),
        "GradientBoosting": dict(n_estimators=300, learning_rate=0.05,
                                 max_depth=5, subsample=0.8, random_state=SEED),
        "RandomForest": dict(n_estimators=300, max_depth=8,
                             min_samples_leaf=5, n_jobs=-1, random_state=SEED),
    }

    def __init__(self):
        self._model         = None
        self._feature_names = None
        self._is_fitted     = False

    @staticmethod
    def _make_features(y_hist: np.ndarray,
                       target_date: pd.Timestamp,
                       meta: dict = None,
                       correction: dict = None) -> dict:
        n    = len(y_hist)
        feat = {}
        for lag in ML_LAGS:
            feat[f"lag_{lag}"] = float(y_hist[-lag]) if lag <= n else 0.0
        for w in ML_WINDOWS:
            win = y_hist[-w:] if n >= w else y_hist
            nz  = win[win > 0]
            feat[f"roll_mean_{w}"]    = float(np.mean(win))
            feat[f"roll_std_{w}"]     = float(np.std(win))    if len(win) > 1 else 0.0
            feat[f"roll_median_{w}"]  = float(np.median(win))
            feat[f"roll_max_{w}"]     = float(np.max(win))    if len(win) > 0 else 0.0
            feat[f"roll_mean_nz_{w}"] = float(np.mean(nz))   if len(nz) > 0 else 0.0
            feat[f"roll_nz_rate_{w}"] = float(len(nz)/len(win)) if len(win) > 0 else 0.0
        feat["diff_1"]  = float(y_hist[-1] - y_hist[-2])  if n >= 2  else 0.0
        feat["diff_12"] = float(y_hist[-1] - y_hist[-12]) if n >= 12 else 0.0
        tw = y_hist[-12:] if n >= 12 else y_hist
        if len(tw) >= 3:
            sl, _, _, p, _ = scipy_stats.linregress(
                np.arange(len(tw), dtype=float), tw)
            feat["trend_slope"] = float(sl)
            feat["trend_pval"]  = float(p)
        else:
            feat["trend_slope"] = feat["trend_pval"] = 0.0
        m = target_date.month
        feat["month"]     = m
        feat["quarter"]   = (m - 1) // 3 + 1
        feat["month_sin"] = float(np.sin(2 * np.pi * m / 12))
        feat["month_cos"] = float(np.cos(2 * np.pi * m / 12))
        feat["is_q1"]     = int(m in {1, 2, 3})
        feat["is_q4"]     = int(m in {10, 11, 12})
        feat["is_winter"] = int(m in WINTER_MONTHS)
        feat["is_summer"] = int(m in SUMMER_MONTHS)
        feat["series_mean"]      = float(np.mean(y_hist)) if n > 0 else 0.0
        feat["series_std"]       = float(np.std(y_hist))  if n > 0 else 0.0
        feat["series_cv"]        = feat["series_std"] / (feat["series_mean"] + 1e-9)
        feat["series_zero_rate"] = float((y_hist == 0).mean()) if n > 0 else 1.0
        feat["n_obs"]            = float(n)
        if meta:
            adi = meta.get("adi", 2.0); cv2 = meta.get("cv2", 1.0)
            feat["adi"]             = float(adi) if np.isfinite(adi) else 10.0
            feat["cv2"]             = float(cv2) if np.isfinite(cv2) else 5.0
            feat["F_s"]             = float(meta.get("F_s", 0.0))
            dc = meta.get("dclass", "smooth")
            feat["is_smooth"]       = int(dc == "smooth")
            feat["is_erratic"]      = int(dc == "erratic")
            feat["is_intermittent"] = int(dc == "intermittent")
            feat["is_lumpy"]        = int(dc == "lumpy")
            st = meta.get("seasonal_type", "stable")
            feat["stype_winter"]    = int(st == "winter")
            feat["stype_summer"]    = int(st == "summer")
            feat["stype_mixed"]     = int(st == "mixed")
            idx_arr = meta.get("indices", np.ones(12))
            feat["seasonal_idx"]    = float(idx_arr[m - 1])
        else:
            for k in ["adi","cv2","F_s","is_smooth","is_erratic",
                      "is_intermittent","is_lumpy",
                      "stype_winter","stype_summer","stype_mixed","seasonal_idx"]:
                feat[k] = 0.0
        # Features de error histórico para autocorrección
        if correction:
            feat["error_bias_last"]    = float(correction.get('bias', 0.0))
            feat["error_rmse_last"]    = float(correction.get('rmse', 0.0))
            feat["error_direction"]    = float(correction.get('error_direction', 0.0))
            feat["over_forecast_rate"] = float(correction.get('over_forecast_rate', 0.5))
        else:
            feat["error_bias_last"]    = 0.0
            feat["error_rmse_last"]    = 0.0
            feat["error_direction"]    = 0.0
            feat["over_forecast_rate"] = 0.5
        return feat

    @staticmethod
    def build_panel(ts_dict: dict, meta_dict: dict,
                    target_start: pd.Timestamp,
                    corrections: dict = None) -> tuple:
        max_lag = max(ML_LAGS)
        rows_X, rows_y = [], []
        for key, ts_full in ts_dict.items():
            ts_train, _ = cut_to_target(ts_full, target_start)
            ts_clean    = remove_outliers(ts_train)
            n = len(ts_clean)
            if n < MIN_OBS_ML:
                continue
            y_arr = ts_clean.values.astype(float)
            meta  = meta_dict.get(key, {})
            corr  = (corrections or {}).get(key)
            for t in range(max_lag, n):
                feat = GlobalMLForecaster._make_features(
                    y_arr[:t], ts_clean.index[t],
                    meta=meta, correction=corr)
                rows_X.append(feat)
                rows_y.append(float(y_arr[t]))
        if not rows_X:
            return pd.DataFrame(), np.array([])
        X     = pd.DataFrame(rows_X).fillna(0.0)
        y_arr = np.array(rows_y, dtype=float)
        return X, y_arr

    def fit(self, X: pd.DataFrame, y: np.ndarray) -> "GlobalMLForecaster":
        if not HAS_ML or X.empty or len(X) < 30:
            log.warning("Dataset insuficiente o sin backend ML — GlobalML omitido.")
            return self
        params = self._PARAMS.get(GBM_BACKEND, {})
        try:
            self._model = GBM_CLASS(**params)
            self._model.fit(X, y)
            self._feature_names = list(X.columns)
            self._is_fitted     = True
            log.info(f"GlobalML ({GBM_BACKEND}) entrenado — "
                     f"{len(X):,} obs · {len(X.columns)} features")
        except Exception as e:
            log.error(f"Error entrenando GlobalML: {e}")
        return self

    def forecast(self, ts_train: pd.Series, h: int,
                 meta: dict = None, correction: dict = None,
                 _seed: np.ndarray = None) -> "pd.Series | None":
        if not self._is_fitted:
            return None
        y_hist = list((_seed if _seed is not None
                       else ts_train.values.astype(float)))
        preds  = []
        for step in range(1, h + 1):
            target_date = ts_train.index[-1] + pd.offsets.MonthBegin(step)
            feat        = self._make_features(
                np.array(y_hist), target_date,
                meta=meta, correction=correction)
            X_row = pd.DataFrame([feat])
            for col in self._feature_names:
                if col not in X_row.columns:
                    X_row[col] = 0.0
            X_row = X_row[self._feature_names]
            try:
                val = float(max(0.0, self._model.predict(X_row)[0]))
            except Exception:
                val = float(np.mean(y_hist[-3:]))
            preds.append(val)
            y_hist.append(val)
        return pd.Series(preds, index=future_index_from_last(ts_train, h))

    def cv_score(self, ts_clean: pd.Series, meta: dict = None,
                 correction: dict = None,
                 h: int = 12, min_train: int = MIN_TRAIN,
                 n_folds: int = ML_CV_FOLDS) -> "float | None":
        """
        AVISO — Data leakage: el modelo fue entrenado con datos completos.
        Este score es OPTIMISTA. Se penaliza ×1.20 en el ensemble.
        """
        if not self._is_fitted:
            return None
        L = len(ts_clean); scores = []
        for fold in range(1, n_folds + 1):
            cut = L - fold * h
            if cut < min_train:
                break
            tr, te = ts_clean.iloc[:cut], ts_clean.iloc[cut:cut + h]
            if len(te) == 0:
                continue
            try:
                pred = self.forecast(tr, len(te), meta=meta, correction=correction,
                                     _seed=tr.values.astype(float))
                if pred is None:
                    continue
                sc = mase(te.values, pred.values, tr.values, m=M_SEASON)
                if np.isfinite(sc):
                    scores.append(sc)
            except Exception:
                pass
        return float(np.mean(scores)) if scores else None

    def feature_importance(self) -> pd.DataFrame:
        if not self._is_fitted or self._model is None:
            return pd.DataFrame()
        try:
            raw = (self._model.booster_.feature_importance(importance_type="gain")
                   if GBM_BACKEND == "LightGBM"
                   else self._model.feature_importances_)
        except Exception:
            raw = getattr(self._model, 'feature_importances_',
                          np.ones(len(self._feature_names)))
        total = raw.sum() + 1e-12
        pct   = (raw / total) * 100.0
        df = (pd.DataFrame({"feature": self._feature_names,
                             "importance_pct": pct})
              .sort_values("importance_pct", ascending=False)
              .reset_index(drop=True))
        df["rank"]           = df.index + 1
        df["cumulative_pct"] = df["importance_pct"].cumsum().round(2)
        df["importance_pct"] = df["importance_pct"].round(3)
        return df

# ═══════════════════════════════════════════════════════════════════
# 20. PERSISTENCIA DEL MODELO ML
# ═══════════════════════════════════════════════════════════════════
def get_model_paths(output_path: str):
    base = Path(output_path).stem
    parent = Path(output_path).parent
    return (parent / f"{base}_model.pkl",
            parent / f"{base}_model_meta.json",
            parent / f"{base}_history.pkl")


def load_model_and_meta(pkl_path: Path, meta_path: Path):
    model = None; meta = {}
    if HAS_JOBLIB and pkl_path.exists():
        try:
            model = jload(str(pkl_path))
            log.info(f"Modelo ML cargado desde: {pkl_path.name}")
        except Exception as e:
            log.warning(f"No se pudo cargar modelo previo: {e}")
    if meta_path.exists():
        try:
            with open(meta_path, encoding='utf-8') as f:
                meta = json.load(f)
            log.info(f"Metadatos del modelo anterior — "
                     f"fecha: {meta.get('fecha_entrenamiento','?')}  "
                     f"MASE_CV: {meta.get('mase_cv_promedio','?')}  "
                     f"backend: {meta.get('backend_ml','?')}")
        except Exception as e:
            log.warning(f"No se pudieron cargar metadatos: {e}")
    return model, meta


def save_model_and_meta(model: GlobalMLForecaster, output_path: str,
                        n_series: int, n_obs: int,
                        mase_cv: float, fi_df: pd.DataFrame):
    pkl_path, meta_path, _ = get_model_paths(output_path)
    if HAS_JOBLIB and model is not None and model._is_fitted:
        try:
            jdump(model, str(pkl_path))
            log.info(f"Modelo ML guardado: {pkl_path.name}")
        except Exception as e:
            log.warning(f"No se pudo guardar modelo: {e}")
    top5 = (fi_df.head(5)['feature'].tolist()
            if not fi_df.empty else [])
    meta = {
        "fecha_entrenamiento": datetime.datetime.now().isoformat(timespec='seconds'),
        "n_series":            n_series,
        "n_observaciones":     n_obs,
        "backend_ml":          GBM_BACKEND,
        "mase_cv_promedio":    round(float(mase_cv), 4) if mase_cv else None,
        "top5_features":       top5,
        "version_script":      VERSION_SCRIPT,
    }
    try:
        with open(meta_path, 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        log.info(f"Metadatos del modelo guardados: {meta_path.name}")
    except Exception as e:
        log.warning(f"No se pudieron guardar metadatos: {e}")


def load_history(hist_path: Path) -> pd.DataFrame:
    if hist_path.exists():
        try:
            df = pd.read_pickle(str(hist_path))
            log.info(f"Historia acumulada cargada: {len(df):,} filas desde {hist_path.name}")
            return df
        except Exception as e:
            log.warning(f"No se pudo cargar historia acumulada: {e}")
    return pd.DataFrame()


def save_history(df_long: pd.DataFrame, hist_path: Path):
    try:
        df_long.to_pickle(str(hist_path))
        log.info(f"Historia acumulada guardada: {len(df_long):,} filas → {hist_path.name}")
    except Exception as e:
        log.warning(f"No se pudo guardar historia: {e}")

# ═══════════════════════════════════════════════════════════════════
# 21. PIPELINE PRINCIPAL (UNA SERIE)
# ═══════════════════════════════════════════════════════════════════
def forecast_one_safe(ts_full: pd.Series,
                      target_start: pd.Timestamp,
                      h: int,
                      correction: dict = None,
                      global_model: "GlobalMLForecaster | None" = None,
                      key_meta: dict = None,
                      prev_model_meta: dict = None) -> dict:
    """
    Orden correcto (v10):
      ensemble → adaptive_correction → floor → clip(0)
    """
    ts_full  = complete_monthly_index(ts_full.astype(float))
    ts_train, _ = cut_to_target(ts_full, target_start)
    EIDX    = pd.date_range(start=target_start, periods=h, freq='MS')
    _EMPTY  = dict(pred=pd.Series(np.zeros(h), index=EIDX),
                   metodo="ZeroHistory", dclass="lumpy",
                   sinfo=dict(F_s=0.0, seasonal_type="stable",
                              peak_months=[], indices=np.ones(M_SEASON)),
                   adi=float('inf'), cv2=float('inf'),
                   slope=0.0, p_val=1.0, floor_val=0.0,
                   bias_applied=0.0, best_single="SES",
                   ensemble_components=[],
                   cv_summary={'MASE_CV': None, 'MAE_CV': None,
                               'RMSE_CV': None, 'N_Folds_CV': 0},
                   ml_mase_score=None, ml_is_optimistic=False,
                   ml_drift_alert=False)

    if ts_train.sum() == 0:
        return _EMPTY
    if len(ts_train) <= 3:
        base  = max(float(ts_train.iloc[-1]), float(ts_train.mean()))
        pred  = pd.Series(np.repeat(base, h), index=EIDX)
        dc, adi, cv2 = classify_sbt(ts_train)
        stats = recent_stats(ts_train)
        pred, ba = apply_adaptive_correction(pred, correction)
        pred, fv = apply_floor(pred, ts_train, dc, stats['share_last12'])
        pred = pd.Series(np.clip(pred.values, 0, None), index=EIDX)
        return dict(pred=pred, metodo="SimpleAvg", dclass=dc,
                    sinfo=dict(F_s=0.0, seasonal_type="stable",
                               peak_months=[], indices=np.ones(M_SEASON)),
                    adi=adi, cv2=cv2, slope=0.0, p_val=1.0,
                    floor_val=fv, bias_applied=ba, best_single="SES",
                    ensemble_components=[],
                    cv_summary={'MASE_CV': None, 'MAE_CV': None,
                                'RMSE_CV': None, 'N_Folds_CV': 0},
                    ml_mase_score=None, ml_is_optimistic=False,
                    ml_drift_alert=False)

    ts_clean = remove_outliers(ts_train)
    dclass, adi, cv2 = classify_sbt(ts_clean)
    sinfo = analyze_seasonality(ts_clean)
    slope, p_val, has_trend = detect_trend(ts_clean)
    stats = recent_stats(ts_clean)

    candidates = select_candidate_models(
        dclass, sinfo['F_s'], sinfo['seasonal_type'],
        has_trend, len(ts_clean))
    scores, cv_summary = walk_forward_cv(
        ts_clean, candidates, h=h,
        min_train=max(MIN_TRAIN, M_SEASON + 2),
        max_folds=MAX_CV_FOLDS)
    if not scores:
        scores = {candidates[0]: 999.0}

    best_single = min(scores, key=scores.get)
    forecasts_all = {}
    for m in list(scores.keys()):
        try:
            forecasts_all[m] = _predict(m, ts_clean, h)
        except Exception:
            pass

    # GlobalML con peso dinámico
    ml_mase_score  = None
    ml_is_optimistic = False
    ml_drift_alert  = False
    if (global_model is not None
            and global_model._is_fitted
            and len(ts_clean) >= MIN_OBS_ML):
        try:
            ml_pred  = global_model.forecast(
                ts_clean, h, meta=key_meta, correction=correction)
            ml_score_cv = global_model.cv_score(
                ts_clean, meta=key_meta, correction=correction)

            # Peso dinámico: usa MASE real si está disponible
            mase_real = correction.get('mase_real') if correction else None
            if mase_real is not None and np.isfinite(mase_real):
                ml_weight_score = mase_real
                ml_is_optimistic = False
            else:
                # Sin historia real: penalizar CV por data leakage
                base_cv = ml_score_cv if ml_score_cv is not None else float(np.mean(list(scores.values())))
                ml_weight_score = base_cv * 1.20
                ml_is_optimistic = True

            ml_mase_score = ml_weight_score

            # Detección de drift vs run anterior
            prev_mase = (prev_model_meta or {}).get('mase_cv_promedio')
            if prev_mase and ml_score_cv is not None:
                if ml_score_cv > prev_mase * (1 + DRIFT_THRESHOLD):
                    ml_drift_alert = True

            if ml_pred is not None:
                ml_pred.index = EIDX
                forecasts_all["GlobalML"] = ml_pred
                scores["GlobalML"]        = ml_weight_score
        except Exception:
            pass

    # ── Ensemble ────────────────────────────────────────────────────
    if len(forecasts_all) >= 2:
        base_pred, ens_components = ensemble_weighted(
            scores, forecasts_all, top_k=TOP_K)
        metodo = f"Ensemble(top{min(TOP_K, len(forecasts_all))})"
        if base_pred is None:
            metodo = best_single
            base_pred = forecasts_all.get(best_single)
            ens_components = [(best_single, 1.0)]
    else:
        metodo = best_single
        base_pred = forecasts_all.get(best_single)
        ens_components = [(best_single, 1.0)] if base_pred is not None else []

    if base_pred is None:
        base_pred = pd.Series(np.repeat(float(ts_clean.iloc[-1]), h), index=EIDX)
        metodo    = "Naive-Fallback"
        ens_components = []
    else:
        base_pred.index = EIDX

    # ── Orden correcto v10: ensemble → adaptive_correction → floor → clip(0)
    pred, ba = apply_adaptive_correction(base_pred, correction)
    pred, fv = apply_floor(pred, ts_clean, dclass, stats['share_last12'])
    pred = pd.Series(np.clip(pred.values, 0, None), index=EIDX)

    return dict(pred=pred, metodo=metodo, dclass=dclass, sinfo=sinfo,
                adi=adi, cv2=cv2, slope=slope, p_val=p_val,
                floor_val=fv, bias_applied=ba, best_single=best_single,
                ensemble_components=ens_components,
                cv_summary=cv_summary,
                ml_mase_score=ml_mase_score,
                ml_is_optimistic=ml_is_optimistic,
                ml_drift_alert=ml_drift_alert)

# ═══════════════════════════════════════════════════════════════════
# 22. PRONÓSTICO COMPLETO POR CLAVE
# ═══════════════════════════════════════════════════════════════════
def forecast_for_key(df_long, key, target_start, h,
                     correction=None, global_model=None,
                     key_meta=None, prev_model_meta=None):
    grp     = df_long[df_long['ID_KEY'] == key].sort_values('Fecha')
    ts_full = complete_monthly_index(
        grp.set_index('Fecha')['Demanda'].astype(float))
    if ts_full.sum() == 0:
        return None, None, None

    ts_train, _ = cut_to_target(ts_full, target_start)
    res = forecast_one_safe(
        ts_full, target_start=target_start, h=h,
        correction=correction, global_model=global_model,
        key_meta=key_meta, prev_model_meta=prev_model_meta)

    pred, metodo, dclass = res['pred'], res['metodo'], res['dclass']
    sinfo, stats         = res['sinfo'], recent_stats(ts_train)
    EIDX = pd.date_range(start=target_start, periods=h, freq='MS')

    try:
        pi          = bootstrap_pi(ts_train, pred, res['ensemble_components'],
                                   h, n_boot=N_BOOTSTRAP)
        lo80, hi80  = pi[0.80]
        lo95, hi95  = pi[0.95]
    except Exception:
        sigma = np.std(ts_train.values) if len(ts_train) > 1 else 1.0
        lo80  = np.maximum(0.0, pred.values - 1.28 * sigma)
        hi80  = pred.values + 1.28 * sigma
        lo95  = np.maximum(0.0, pred.values - 1.96 * sigma)
        hi95  = pred.values + 1.96 * sigma

    df_det = (pred.to_frame('Pronostico')
                  .reset_index().rename(columns={'index': 'Fecha'}))
    for col, arr in [('Lo80',lo80),('Hi80',hi80),('Lo95',lo95),('Hi95',hi95)]:
        df_det[col] = arr
    df_det['Tipo']          = "Futuro"
    df_det['ID_KEY']        = key
    df_det['Metodo']        = metodo
    df_det['Clase_SBT']     = dclass
    df_det['Tipo_Estacion'] = sinfo['seasonal_type']

    cv  = res['cv_summary']
    ml_leak_note = "(optimistic)" if res['ml_is_optimistic'] else ""

    row_sum = {
        "ID_KEY":             key,
        "Metodo":             metodo,
        "Clase_SBT":          dclass,
        "Tipo_Estacion":      sinfo['seasonal_type'],
        "F_Estacional":       round(sinfo['F_s'], 3),
        "Peak_Months":        str(sinfo['peak_months']),
        "ADI":                round(res['adi'], 3) if np.isfinite(res['adi']) else 999,
        "CV2":                round(res['cv2'], 3) if np.isfinite(res['cv2']) else 999,
        "Trend_Slope":        round(res['slope'], 4),
        "Trend_Pval":         round(res['p_val'], 4),
        "SumHistorico":       float(ts_full.sum()),
        "UltimoMesHist":      float(ts_train.iloc[-1]),
        "MedianLast6":        stats['med_last6'],
        "MedianLast12":       stats['med_last12'],
        "ShareLast12":        stats['share_last12'],
        "FloorVal":           float(res['floor_val']),
        "BiasCorreccion":     float(res['bias_applied']),
        "NumMesesHist":       int(len(ts_full)),
        "PronosticoTotal":    float(pred.sum()),
        "TieneML":            int("GlobalML" in str(metodo)),
        "MASE_CV":            cv['MASE_CV'],
        "MAE_CV":             cv['MAE_CV'],
        "RMSE_CV":            cv['RMSE_CV'],
        "N_Folds_CV":         cv['N_Folds_CV'],
        "GlobalML_MASE":      round(res['ml_mase_score'], 4)
                              if res['ml_mase_score'] is not None else None,
        "GlobalML_Leakage":   ml_leak_note,
        "ML_Drift_Alert":     bool(res['ml_drift_alert']),
    }

    row_prof = {"ID_KEY": key, "Clase_SBT": dclass,
                "Tipo_Estacion": sinfo['seasonal_type'],
                "F_Estacional":  round(sinfo['F_s'], 3),
                "Peak_Months":   str(sinfo['peak_months'])}
    for j, mn in enumerate(MONTH_NAMES):
        row_prof[f"Idx_{mn}"] = round(float(sinfo['indices'][j]), 3)

    return df_det, row_sum, row_prof

# ═══════════════════════════════════════════════════════════════════
# 23. CHECKPOINT
# ═══════════════════════════════════════════════════════════════════
def save_checkpoint(rows_detail, rows_summary, rows_profile,
                    rows_corr, output_path: str, checkpoint_idx: int):
    ck_path = (Path(output_path).parent
               / f"{Path(output_path).stem}_checkpoint_{checkpoint_idx}.xlsx")
    try:
        with pd.ExcelWriter(str(ck_path), engine='openpyxl') as w:
            if rows_detail:
                pd.concat(rows_detail, ignore_index=True).to_excel(
                    w, sheet_name="Pronosticos", index=False)
            if rows_summary:
                pd.DataFrame(rows_summary).to_excel(
                    w, sheet_name="Resumen", index=False)
        log.info(f"Checkpoint guardado: {ck_path.name}")
    except Exception as e:
        log.warning(f"No se pudo guardar checkpoint: {e}")

# ═══════════════════════════════════════════════════════════════════
# 24. FUNCIÓN PRINCIPAL
# ═══════════════════════════════════════════════════════════════════
def main():
    parser = build_parser()
    args   = parser.parse_args()
    setup_logging(args.log_file)

    FILE_PATH   = args.input
    OUTPUT_PATH = args.output
    SHEET_NAME  = args.sheet
    H           = args.horizonte

    # Crear directorio de salida
    Path(OUTPUT_PATH).parent.mkdir(parents=True, exist_ok=True)

    # ── Verificar archivo de entrada ────────────────────────────────
    if not Path(FILE_PATH).exists():
        raise FileNotFoundError(f"No se encuentra el archivo de entrada: {FILE_PATH}")

    log.info(f"Archivo de entrada : {FILE_PATH}")
    log.info(f"Archivo de salida  : {OUTPUT_PATH}")
    log.info(f"Horizonte          : {H} meses")

    # ── Rutas del modelo ML ─────────────────────────────────────────
    pkl_path, meta_path, hist_path = get_model_paths(OUTPUT_PATH)

    # ── Lectura del Excel ───────────────────────────────────────────
    raw = pd.read_excel(FILE_PATH, header=None, sheet_name=SHEET_NAME)
    raw = raw.dropna(how="all").dropna(axis=1, how="all")

    max_hits, header_row = -1, 0
    for r in range(min(15, len(raw))):
        hits = sum(parse_header_to_year_month(v) is not None
                   for v in list(raw.iloc[r]))
        if hits > max_hits:
            max_hits, header_row = hits, r

    hdr_raw = list(raw.iloc[header_row])
    hdr = []
    for c in hdr_raw:
        if isinstance(c, (pd.Timestamp, np.datetime64, pd.Period,
                          datetime.datetime, datetime.date)):
            hdr.append(c)
        else:
            hdr.append(str(c).strip())

    df = raw.iloc[header_row + 1:].copy()
    df.columns = make_unique(hdr)

    date_cols = [str(c) for c in df.columns
                 if parse_header_to_year_month(c) is not None]

    # Buscar columna ID según --id-col; fallback a la primera columna no-fecha
    id_col_target = args.id_col
    if id_col_target in df.columns:
        id_col = id_col_target
    else:
        # Intentar match case-insensitive
        col_lower = {c.lower(): c for c in df.columns}
        id_col = col_lower.get(id_col_target.lower(),
                               df.columns[0])   # fallback: primera columna
        if id_col != id_col_target:
            log.warning(f"Columna '{id_col_target}' no encontrada. "
                        f"Usando '{id_col}' como ID_KEY.")

    df['ID_KEY'] = df[id_col].astype(str).str.strip()
    log.info(f"Columna ID_KEY      : '{id_col}'  (hoja: '{SHEET_NAME}')")

    df_long = df.melt(id_vars=['ID_KEY'],
                      value_vars=date_cols,
                      var_name='Fecha', value_name='Demanda')
    df_long['Fecha']   = (pd.to_datetime(df_long['Fecha'])
                            .dt.to_period('M').dt.to_timestamp())
    df_long['Demanda'] = (pd.to_numeric(df_long['Demanda'], errors='coerce')
                            .fillna(0.0).clip(lower=0.0))

    # ── Agregar duplicados por suma (múltiples filas con mismo ID+Fecha son válidas) ──
    n_antes = len(df_long)
    df_long = (df_long.groupby(['ID_KEY', 'Fecha'], as_index=False)['Demanda']
               .sum())
    n_dup = n_antes - len(df_long)
    if n_dup > 0:
        log.warning(f"Se encontraron {n_dup} filas duplicadas por (ID_KEY, Fecha). "
                    f"Se consolidaron por suma de Demanda.")

    # ── Validación de inputs ────────────────────────────────────────
    validate_inputs(df_long, date_cols, id_col)

    # ── TARGET_START auto-detectado ─────────────────────────────────
    if args.target_start:
        TARGET_START = pd.Timestamp(args.target_start)
        log.info(f"TARGET_START (manual) : {TARGET_START.strftime('%Y-%m-%d')}")
    else:
        TARGET_START = detect_target_start_auto(df_long)
        log.info(f"TARGET_START (auto)   : {TARGET_START.strftime('%Y-%m-%d')}")

    log.info(f"Fila de encabezado  : {header_row}")
    log.info(f"Columnas de fecha   : {len(date_cols)}  ({date_cols[0]} → {date_cols[-1]})")

    # ── Filtrar series con demanda ──────────────────────────────────
    meta_filter = (df_long.groupby('ID_KEY')['Demanda']
                   .agg(Demanda_total='sum',
                        nz=lambda s: (s > 0).sum(),
                        nobs='count')
                   .reset_index())
    meta_filter = (meta_filter[meta_filter['nz'] >= MIN_NZ]
                   .sort_values('Demanda_total', ascending=False))
    ids_keep    = meta_filter['ID_KEY'].tolist()
    df_long     = df_long[df_long['ID_KEY'].isin(ids_keep)].copy()
    total       = len(ids_keep)
    log.info(f"Variantes a procesar: {total}")
    log.info(f"Pronosticando {H} meses hasta "
             f"{(TARGET_START + pd.DateOffset(months=H-1)).strftime('%Y-%m')}")

    # ── Historia acumulada (reentrenamiento incremental) ────────────
    prev_df_long = load_history(hist_path)
    if not prev_df_long.empty:
        df_long_acum = (pd.concat([prev_df_long, df_long], ignore_index=True)
                        .drop_duplicates(subset=['ID_KEY', 'Fecha'])
                        .reset_index(drop=True))
        log.info(f"Dataset acumulado: {len(df_long_acum):,} filas "
                 f"(anterior: {len(prev_df_long):,} + actual: {len(df_long):,})")
    else:
        df_long_acum = df_long.copy()

    # ── Correcciones adaptativas ────────────────────────────────────
    log.info("[1/4] Cargando correcciones adaptativas del run anterior...")
    prev_forecasts = load_previous_forecasts(OUTPUT_PATH)
    corrections    = compute_adaptive_corrections(prev_forecasts, df_long)
    n_corr = sum(1 for c in corrections.values()
                 if c['n_obs'] >= MIN_OBS_CORR
                 and abs(c.get('bias_pct', 0)) >= 0.05)
    log.info(f"Series con sesgo material para corregir: {n_corr}")

    # ── Cargar modelo anterior ──────────────────────────────────────
    prev_model_raw, prev_model_meta = load_model_and_meta(pkl_path, meta_path)
    loaded_from_prev = prev_model_raw is not None

    # ── Pre-cómputo de metadatos ────────────────────────────────────
    log.info("[2/4] Pre-computando perfiles por serie...")
    meta_dict = {}
    ts_dict   = {}
    for key in ids_keep:
        grp     = df_long_acum[df_long_acum['ID_KEY'] == key].sort_values('Fecha')
        ts_full = complete_monthly_index(
            grp.set_index('Fecha')['Demanda'].astype(float))
        ts_dict[key] = ts_full
        ts_train, _ = cut_to_target(ts_full, TARGET_START)
        if len(ts_train) < 4:
            meta_dict[key] = {}; continue
        ts_clean     = remove_outliers(ts_train)
        dc, adi, cv2 = classify_sbt(ts_clean)
        sinfo        = analyze_seasonality(ts_clean)
        meta_dict[key] = {
            "dclass":        dc,   "adi":      adi,
            "cv2":           cv2,  "F_s":      sinfo['F_s'],
            "seasonal_type": sinfo['seasonal_type'],
            "indices":       sinfo['indices'],
        }

    # ── Entrenamiento del modelo ML global ──────────────────────────
    global_model = None
    ml_mase_cv_list = []
    if HAS_ML:
        log.info(f"[3/4] Construyendo dataset global ({GBM_BACKEND})...")
        log.warning(
            "GlobalML data leakage — cv_score evalúa el modelo entrenado con datos "
            "completos. El MASE_CV reportado es OPTIMISTA (optimistic) y se penaliza "
            "×1.20 en el ensemble cuando no hay MASE real disponible.")
        X_ml, y_ml = GlobalMLForecaster.build_panel(
            ts_dict, meta_dict, TARGET_START, corrections=corrections)
        n_series_ml = sum(
            1 for key in ts_dict
            if len(cut_to_target(ts_dict[key], TARGET_START)[0]) >= MIN_OBS_ML)
        log.info(f"Series elegibles ML: {n_series_ml}  |  Observaciones: {len(X_ml):,}")

        if n_series_ml >= MIN_SERIES_ML and len(X_ml) >= 50:
            if prev_model_raw is not None and isinstance(prev_model_raw, GlobalMLForecaster):
                # Reentrenar desde cero con datos acumulados
                global_model = GlobalMLForecaster().fit(X_ml, y_ml)
                log.info("Reentrenamiento incremental sobre dataset acumulado completo.")
            else:
                global_model = GlobalMLForecaster().fit(X_ml, y_ml)
        else:
            log.warning("Pocas series — GlobalML omitido esta vez.")
    else:
        log.info("[3/4] Sin backend ML (instala: pip install lightgbm)")

    # ── Loop principal ──────────────────────────────────────────────
    log.info(f"[4/4] Generando pronósticos para {total} variantes...")
    rows_detail  = []
    rows_summary = []
    rows_profile = []
    rows_corr    = []

    iterator = (tqdm(ids_keep, desc="Pronosticando") if HAS_TQDM
                else ids_keep)

    def _process_key(key):
        corr     = corrections.get(key)
        key_meta = meta_dict.get(key, {})
        try:
            return forecast_for_key(
                df_long, key, target_start=TARGET_START, h=H,
                correction=corr, global_model=global_model,
                key_meta=key_meta, prev_model_meta=prev_model_meta)
        except Exception as exc:
            log.warning(f"Error en {key}: {exc} — usando Naive-Error")
            grp     = df_long[df_long['ID_KEY'] == key].sort_values('Fecha')
            ts_full = complete_monthly_index(
                grp.set_index('Fecha')['Demanda'].astype(float))
            ts_train, _ = cut_to_target(ts_full, TARGET_START)
            last  = float(ts_train.iloc[-1]) if len(ts_train) > 0 else 0.0
            idx   = pd.date_range(start=TARGET_START, periods=H, freq='MS')
            pred  = pd.Series(np.repeat(last, H), index=idx)
            sigma = np.std(ts_train.values) if len(ts_train) > 1 else 1.0
            df_det = (pred.to_frame('Pronostico')
                          .reset_index().rename(columns={'index': 'Fecha'}))
            for col2, arr in [
                ('Lo80', np.maximum(0.0, pred.values - 1.28*sigma)),
                ('Hi80', pred.values + 1.28*sigma),
                ('Lo95', np.maximum(0.0, pred.values - 1.96*sigma)),
                ('Hi95', pred.values + 1.96*sigma)]:
                df_det[col2] = arr
            df_det['Tipo'] = "Futuro"; df_det['ID_KEY'] = key
            df_det['Metodo'] = "Naive-Error"; df_det['Clase_SBT'] = "fallback"
            df_det['Tipo_Estacion'] = "unknown"
            row_sum = {
                "ID_KEY": key, "Metodo": "Naive-Error",
                "Clase_SBT": "fallback", "Tipo_Estacion": "unknown",
                "F_Estacional": 0.0, "Peak_Months": "[]",
                "ADI": 0.0, "CV2": 0.0, "Trend_Slope": 0.0, "Trend_Pval": 1.0,
                "SumHistorico": float(ts_full.sum()), "UltimoMesHist": last,
                "MedianLast6": 0.0, "MedianLast12": 0.0, "ShareLast12": 0.0,
                "FloorVal": 0.0, "BiasCorreccion": 0.0,
                "NumMesesHist": int(len(ts_full)),
                "PronosticoTotal": float(pred.sum()), "TieneML": 0,
                "MASE_CV": None, "MAE_CV": None, "RMSE_CV": None, "N_Folds_CV": 0,
                "GlobalML_MASE": None, "GlobalML_Leakage": "",
                "ML_Drift_Alert": False,
            }
            return df_det, row_sum, None

    # Paralelizar si joblib disponible y hay suficientes series
    if HAS_JOBLIB and total > 10:
        all_results = Parallel(n_jobs=-1, prefer="threads")(
            delayed(_process_key)(key) for key in iterator)
    else:
        all_results = [_process_key(key) for key in iterator]

    for i, (df_det, row_sum, row_prof) in enumerate(all_results, 1):
        key  = ids_keep[i - 1]
        corr = corrections.get(key)

        if df_det  is not None: rows_detail.append(df_det)
        if row_sum is not None:
            rows_summary.append(row_sum)
            if row_sum.get('MASE_CV') is not None:
                ml_mase_cv_list.append(row_sum['MASE_CV'])
        if row_prof is not None: rows_profile.append(row_prof)

        if corr and corr.get('n_obs', 0) >= MIN_OBS_CORR:
            ba_v = (corr['bias'] * BIAS_ALPHA
                    if abs(corr.get('bias_pct', 0)) >= 0.05 else 0.0)
            rows_corr.append({
                "ID_KEY":            key,
                "N_Obs_Comparadas":  corr['n_obs'],
                "Bias_Unidades":     round(corr['bias'], 2),
                "Bias_Pct":          round(corr['bias_pct'] * 100, 1),
                "RMSE":              round(corr['rmse'], 2),
                "Media_Real":        round(corr['mean_actual'], 2),
                "MASE_Real":         round(corr['mase_real'], 4)
                                     if corr.get('mase_real') else None,
                "Over_Forecast_Rate": round(corr.get('over_forecast_rate', 0), 3),
                "Correccion_Aplic":  round(ba_v, 2),
                "Correccion_Activa": abs(corr.get('bias_pct', 0)) >= 0.05,
            })

        # Checkpoint cada CHECKPOINT_SIZE iteraciones
        if total > CHECKPOINT_SIZE and i % CHECKPOINT_SIZE == 0:
            save_checkpoint(rows_detail, rows_summary, rows_profile,
                            rows_corr, OUTPUT_PATH, i)

        if (not HAS_TQDM) and (i % 200 == 0 or i == total):
            log.info(f"[{i:>5}/{total}] procesadas...")

    # ── Detección de deriva del modelo ──────────────────────────────
    prev_mase_global = (prev_model_meta or {}).get('mase_cv_promedio')
    mase_cv_actual = float(np.mean(ml_mase_cv_list)) if ml_mase_cv_list else None
    if prev_mase_global and mase_cv_actual:
        if mase_cv_actual > prev_mase_global * (1 + DRIFT_THRESHOLD):
            log.warning(
                f"ADVERTENCIA — Posible degradación del modelo ML detectada. "
                f"MASE actual {mase_cv_actual:.4f} vs anterior {prev_mase_global:.4f}. "
                f"Considerar reentrenamiento desde cero eliminando el .pkl.")

    # ── Guardado del modelo ML ──────────────────────────────────────
    fi_df = pd.DataFrame()
    if global_model is not None and global_model._is_fitted:
        fi_df = global_model.feature_importance()
        save_model_and_meta(
            global_model, OUTPUT_PATH,
            n_series=total, n_obs=len(X_ml) if HAS_ML else 0,
            mase_cv=mase_cv_actual, fi_df=fi_df)

    # ── Guardar historia acumulada ──────────────────────────────────
    save_history(df_long_acum, hist_path)

    # ── Guardado en Excel — 5 hojas ─────────────────────────────────
    log.info(f"Escribiendo Excel: {OUTPUT_PATH}")
    with pd.ExcelWriter(OUTPUT_PATH, engine='openpyxl') as writer:
        if rows_detail:
            (pd.concat(rows_detail, ignore_index=True)
               .sort_values(['ID_KEY', 'Fecha'])
               .to_excel(writer, sheet_name="Pronosticos", index=False))
        if rows_summary:
            (pd.DataFrame(rows_summary)
               .sort_values('ID_KEY')
               .to_excel(writer, sheet_name="Resumen", index=False))
        if rows_profile:
            (pd.DataFrame(rows_profile)
               .sort_values('ID_KEY')
               .to_excel(writer, sheet_name="Perfiles_Estacionales", index=False))
        (pd.DataFrame(rows_corr if rows_corr
                      else [{"Mensaje": "Sin correcciones en este run."}])
           .to_excel(writer, sheet_name="Correcciones_Adaptativas", index=False))
        if not fi_df.empty:
            fi_df.to_excel(writer, sheet_name="ML_Feature_Importance", index=False)
        else:
            pd.DataFrame([{"Mensaje": "GlobalML no disponible en este run."}]).to_excel(
                writer, sheet_name="ML_Feature_Importance", index=False)

    # ── Resumen de calidad ──────────────────────────────────────────
    n_ml  = sum(1 for r in rows_summary if r.get('TieneML', 0) == 1)
    n_ca  = sum(1 for r in rows_summary
                if abs(r.get('BiasCorreccion', 0.0)) > 0)
    sbt_groups = collections.defaultdict(list)
    for r in rows_summary:
        cv_m = r.get('MASE_CV')
        if cv_m is not None:
            sbt_groups[r.get('Clase_SBT', 'unknown')].append(cv_m)

    log.info("─" * 60)
    log.info(f"Archivo            : {OUTPUT_PATH}")
    log.info(f"Series procesadas  : {total}")
    log.info(f"Con GlobalML       : {n_ml} ({n_ml/total*100:.1f}%)")
    log.info(f"Con correc. adapt. : {n_ca} ({n_ca/total*100:.1f}%)")
    log.info(f"Backend ML         : {GBM_BACKEND}")
    log.info(f"Modelo ML          : {'cargado de run anterior' if loaded_from_prev else 'entrenado desde cero'}")
    if mase_cv_actual:
        log.info(f"MASE_CV promedio   : {mase_cv_actual:.4f} (optimistic — ver aviso data leakage)")
    log.info("MASE_CV por clase SBT:")
    for clase, vals in sorted(sbt_groups.items()):
        log.info(f"  {clase:<14}: {np.mean(vals):.4f} (n={len(vals)})")
    if not fi_df.empty:
        log.info("Top-5 features ML:")
        for _, row in fi_df.head(5).iterrows():
            log.info(f"  #{int(row['rank']):>2}  {row['feature']:<30} {row['importance_pct']:.1f}%")
    log.info("─" * 60)


# ═══════════════════════════════════════════════════════════════════
# PUNTO DE ENTRADA
# ═══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    main()

