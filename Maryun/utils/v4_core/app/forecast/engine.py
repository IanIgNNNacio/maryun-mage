"""Advanced forecasting engine — port of the user's ``SISTEMA DE PRONOSTICO v9``.

Adapted from the monolithic script to:

* consume a long-format ``demand`` DataFrame (as produced by the Extract stage),
* honour :class:`TemporalGate` for the training cutoff (use only closed months),
* emit a DataFrame matching :data:`normalize.ForecastSchema` plus a set of
  audit side-tables for the Export stage.

Preserved features
------------------
* Syntetos-Boylan-Teunter (SBT) classification (smooth / erratic /
  intermittent / lumpy).
* STL-based seasonal strength + seasonal type (winter / summer / mixed / stable).
* MAD-based seasonal outlier cleaning.
* Model pool: SES, Theta, Holt-Damped, HW-Add, HW-Mul, NaiveSeasonal,
  Croston-SBA, TSB.
* Intelligent model selection driven by SBT × seasonal × trend signals.
* Walk-forward cross-validation with MASE; ensemble of top-K by 1/MASE.
* Optimistic floor per SBT class and adaptive bias correction vs the previous
  run's forecast.
* Bootstrap prediction intervals (Lo/Hi 80 and 95).

Skipped vs the original
-----------------------
* GlobalMLForecaster — the heavy cross-series LightGBM/XGB global model is
  behind the ``params.forecast.method == "ensemble_ml"`` flag; the default
  pipeline uses the statistical ensemble only (keeps tests light).
* Direct Excel I/O — handled by the Export stage.
* ``load_previous_forecasts`` / adaptive correction — reuses a DataFrame the
  orchestrator can pass in via ``previous_forecast``.
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass

import numpy as np
import pandas as pd
from dateutil.relativedelta import relativedelta
from scipy import stats as scipy_stats
from statsmodels.tsa.holtwinters import ExponentialSmoothing

try:  # optional — gracefully degrade
    from statsmodels.tsa.seasonal import STL
    HAS_STL = True
except ImportError:  # pragma: no cover
    HAS_STL = False

from app.config.engine_params import EngineParams
from app.temporal.gate import TemporalGate
from app.utils.logging import get_logger


# ---------------------------------------------------------------------------
# Configuration (mirrors the v9 script constants)
# ---------------------------------------------------------------------------
M_SEASON = 12
MIN_NZ = 1
SEED = 7
N_BOOTSTRAP = 300
MAX_CV_FOLDS = 4
MIN_TRAIN = 14
TOP_K = 3
BIAS_ALPHA = 0.5
MIN_OBS_CORR = 2
WINTER_MONTHS = {10, 11, 12, 1, 2, 3}
SUMMER_MONTHS = {4, 5, 6, 7, 8, 9}


# ---------------------------------------------------------------------------
# Small helpers (stateless, pure)
# ---------------------------------------------------------------------------
def _complete_monthly(ts: pd.Series) -> pd.Series:
    if ts.empty:
        return ts
    ts = ts.sort_index()
    idx = pd.date_range(
        start=ts.index.min().to_period("M").to_timestamp(),
        end=ts.index.max().to_period("M").to_timestamp(),
        freq="MS",
    )
    out = ts.reindex(idx).fillna(0.0)
    out.index.freq = "MS"
    return out


def _future_index(ts: pd.Series, h: int) -> pd.DatetimeIndex:
    return pd.date_range(start=ts.index[-1] + pd.offsets.MonthBegin(1), periods=h, freq="MS")


def _remove_outliers(ts: pd.Series, m: int = M_SEASON, k: float = 3.5) -> pd.Series:
    """Replace extreme points with the median of the same seasonal slot."""
    y = ts.values.astype(float).copy()
    n = len(y)
    if n <= m:
        return ts
    for i in range(n):
        same = [j for j in range(i % m, n, m) if j != i]
        if len(same) < 2:
            continue
        sv = y[same]
        med = np.median(sv)
        mad = np.median(np.abs(sv - med))
        sigma = 1.4826 * mad if mad > 0 else (np.std(sv) or 1.0)
        if np.abs(y[i] - med) > k * sigma:
            y[i] = med
    return pd.Series(y, index=ts.index)


def _classify_sbt(ts: pd.Series) -> tuple[str, float, float]:
    vals = ts.values.astype(float)
    nz = vals[vals > 0]
    n, nzn = len(vals), len(nz)
    if nzn == 0:
        return "lumpy", float("inf"), float("inf")
    adi = n / nzn
    cv2 = (np.std(nz, ddof=1) / np.mean(nz)) ** 2 if nzn > 1 else 0.0
    if adi < 1.32 and cv2 < 0.49:
        cat = "smooth"
    elif adi < 1.32:
        cat = "erratic"
    elif cv2 < 0.49:
        cat = "intermittent"
    else:
        cat = "lumpy"
    return cat, float(adi), float(cv2)


def _seasonal_indices(ts: pd.Series, m: int = M_SEASON) -> np.ndarray:
    y = ts.values.astype(float)
    n = len(y)
    if n < m + 1:
        return np.ones(m)
    ma = np.convolve(y, np.ones(m) / m, mode="valid")
    off = m // 2
    rat = np.full(n, np.nan)
    den = ma.copy()
    den[den <= 0] = 1e-9
    rat[off:off + len(ma)] = y[off:off + len(ma)] / den
    months = ts.index.month
    indices = np.ones(m)
    for s in range(m):
        v = rat[months == s + 1]
        v = v[~np.isnan(v)]
        if len(v) > 0:
            indices[s] = np.median(v)
    mn = indices.mean()
    return indices / mn if mn > 0 else indices


def _analyze_seasonality(ts: pd.Series, m: int = M_SEASON) -> dict:
    if len(ts) < 2 * m:
        return {"F_s": 0.0, "seasonal_type": "stable", "peak_months": [], "indices": np.ones(m)}
    F_s = 0.0
    if HAS_STL:
        try:
            res = STL(ts, period=m, robust=True).fit()
            vr = np.var(res.resid)
            vsr = np.var(res.seasonal + res.resid)
            if vsr > 0:
                F_s = max(0.0, 1.0 - vr / vsr)
        except Exception:
            pass
    if F_s == 0.0:
        try:
            groups = [ts[ts.index.month == mm].values for mm in range(1, m + 1) if (ts.index.month == mm).any()]
            if len(groups) >= 3:
                _, p = scipy_stats.f_oneway(*groups)
                F_s = max(0.0, min(0.9, 1.0 - p)) if np.isfinite(p) else 0.0
        except Exception:
            pass
    indices = _seasonal_indices(ts, m)
    peak_months = [i + 1 for i in range(m) if indices[i] > 1.15]
    if F_s < 0.15 or not peak_months:
        stype = "stable" if F_s < 0.15 else "mixed"
    else:
        w = sum(1 for mm in peak_months if mm in WINTER_MONTHS)
        s = sum(1 for mm in peak_months if mm in SUMMER_MONTHS)
        if w >= 2 and w >= s:
            stype = "winter"
        elif s >= 2 and s > w:
            stype = "summer"
        else:
            stype = "mixed"
    return {"F_s": float(F_s), "seasonal_type": stype, "peak_months": peak_months, "indices": indices}


def _detect_trend(ts: pd.Series) -> tuple[float, float, bool]:
    y = ts.values.astype(float)
    if len(y) < 5:
        return 0.0, 1.0, False
    slope, _, _, p, _ = scipy_stats.linregress(np.arange(len(y), dtype=float), y)
    return float(slope), float(p), bool(p < 0.05)


def _mase(y_true, y_pred, y_ins, m=1) -> float:
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    y_ins = np.asarray(y_ins, dtype=float)
    scale = (
        np.mean(np.abs(y_ins[m:] - y_ins[:-m]))
        if len(y_ins) > m
        else (np.mean(np.abs(np.diff(y_ins))) if len(y_ins) > 1 else 1.0)
    )
    scale = scale if (scale > 0 and np.isfinite(scale)) else 1.0
    return float(np.mean(np.abs(y_true - y_pred)) / scale)


# ---------------------------------------------------------------------------
# Model pool
# ---------------------------------------------------------------------------
def _ses(tr: pd.Series, h: int) -> pd.Series:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        fit = ExponentialSmoothing(tr, trend=None, seasonal=None).fit(optimized=True)
    p = fit.forecast(h)
    p.index = _future_index(tr, h)
    return p


def _theta(tr: pd.Series, h: int) -> pd.Series:
    y = tr.values.astype(float)
    t = np.arange(1, len(y) + 1, dtype=float)
    b, a = np.polyfit(t, y, 1)
    detr = y - (a + b * t)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ses = ExponentialSmoothing(detr, trend=None, seasonal=None).fit(optimized=True).forecast(h)
    t2 = np.arange(len(y) + 1, len(y) + h + 1, dtype=float)
    return pd.Series(ses + (a + b * t2), index=_future_index(tr, h))


def _holt_damped(tr: pd.Series, h: int) -> pd.Series:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        fit = ExponentialSmoothing(tr, trend="add", damped_trend=True, seasonal=None).fit(optimized=True)
    p = fit.forecast(h)
    p.index = _future_index(tr, h)
    return p


def _hw_add(tr: pd.Series, h: int, m: int = M_SEASON) -> pd.Series:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        fit = ExponentialSmoothing(tr, trend="add", seasonal="add", seasonal_periods=m).fit(optimized=True)
    p = fit.forecast(h)
    p.index = _future_index(tr, h)
    return p


def _hw_mul(tr: pd.Series, h: int, m: int = M_SEASON) -> pd.Series:
    if (tr.values <= 0).any():
        return _hw_add(tr, h, m)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        fit = ExponentialSmoothing(tr, trend="add", seasonal="mul", seasonal_periods=m).fit(optimized=True)
    p = fit.forecast(h)
    p.index = _future_index(tr, h)
    return p


def _naive_seasonal(tr: pd.Series, h: int, m: int = M_SEASON) -> pd.Series:
    y = tr.values.astype(float)
    cycle = y[-m:] if len(y) >= m else y
    if len(cycle) == 0:
        tiled = np.zeros(h)
    else:
        tiled = np.tile(cycle, (h // len(cycle)) + 2)[:h]
    return pd.Series(tiled, index=_future_index(tr, h))


def _croston_sba(tr: pd.Series, h: int, alpha: float = 0.2) -> pd.Series:
    y = np.asarray(tr.values, dtype=float)
    nz = np.where(y > 0)[0]
    if len(nz) == 0:
        return pd.Series(np.zeros(h), index=_future_index(tr, h))
    z = float(y[nz[0]])
    p = float(nz[0] + 1 if nz[0] > 0 else 1)
    prev = nz[0]
    for i in range(nz[0] + 1, len(y)):
        if y[i] > 0:
            z = alpha * y[i] + (1 - alpha) * z
            p = alpha * (i - prev) + (1 - alpha) * p
            prev = i
    f = max(0.0, (1.0 - alpha / 2.0) * z / p)
    return pd.Series(np.repeat(f, h), index=_future_index(tr, h))


def _tsb(tr: pd.Series, h: int, alpha: float = 0.3, beta: float = 0.3) -> pd.Series:
    y = np.asarray(tr.values, dtype=float)
    p = z = None
    for t in range(len(y)):
        I = 1.0 if y[t] > 0 else 0.0
        p = I if p is None else p + alpha * (I - p)
        if I > 0:
            z = y[t] if z is None else z + beta * (y[t] - z)
    f = 0.0 if (p is None or z is None) else max(0.0, p * z)
    return pd.Series(np.repeat(f, h), index=_future_index(tr, h))


_MODELS = {
    "SES": _ses, "Theta": _theta, "Holt-Damped": _holt_damped,
    "HW-Add": _hw_add, "HW-Mul": _hw_mul, "NaiveSeasonal": _naive_seasonal,
    "Croston-SBA": _croston_sba, "TSB": _tsb,
}


def _select_candidates(sbt: str, F_s: float, stype: str, has_trend: bool, n: int) -> list[str]:
    can_seasonal = n >= M_SEASON + 2
    if sbt in ("intermittent", "lumpy"):
        pool = ["Croston-SBA", "TSB", "SES"]
        if has_trend:
            pool.append("Theta")
        return pool
    if F_s >= 0.45 and can_seasonal:
        return (["HW-Mul", "HW-Add", "NaiveSeasonal", "Theta"]
                if stype in ("winter", "summer")
                else ["HW-Mul", "HW-Add", "Theta", "Holt-Damped"])
    if F_s >= 0.20 and can_seasonal:
        return (["HW-Add", "HW-Mul", "Theta", "Holt-Damped"]
                if stype in ("winter", "summer")
                else ["HW-Add", "Theta", "Holt-Damped", "SES"])
    pool = ["Holt-Damped", "Theta", "SES"] if has_trend else ["SES", "Theta", "Holt-Damped"]
    if sbt == "erratic" and "SES" not in pool:
        pool.append("SES")
    return pool


def _walk_forward(ts: pd.Series, models: list[str], h: int) -> dict[str, float]:
    L = len(ts)
    scores: dict[str, list[float]] = {m: [] for m in models}
    for fold in range(1, MAX_CV_FOLDS + 1):
        cut = L - fold * h
        if cut < MIN_TRAIN:
            break
        tr, te = ts.iloc[:cut], ts.iloc[cut:cut + h]
        if len(te) == 0:
            continue
        for m in models:
            try:
                pred = _MODELS[m](tr, h)
                n = min(len(te), len(pred))
                sc = _mase(te.values[:n], pred.values[:n], tr.values, m=M_SEASON)
                if np.isfinite(sc):
                    scores[m].append(sc)
            except Exception:
                pass
    return {m: float(np.nanmean(v)) for m, v in scores.items() if v}


def _ensemble(scores: dict[str, float], forecasts: dict[str, pd.Series], top_k: int = TOP_K) -> pd.Series | None:
    if not scores or not forecasts:
        return None
    top = sorted(scores.items(), key=lambda x: x[1])[:top_k]
    top = [(m, s) for m, s in top if m in forecasts]
    if not top:
        return None
    mods, errs = zip(*top)
    errs = np.clip(np.array(errs, dtype=float), 1e-9, None)
    w = (1.0 / errs) / (1.0 / errs).sum()
    result = None
    for wi, m in zip(w, mods):
        arr = forecasts[m].values.astype(float)
        result = wi * arr if result is None else result + wi * arr
    return pd.Series(result, index=forecasts[mods[0]].index)


def _apply_floor(pred: pd.Series, tr: pd.Series, dclass: str) -> tuple[pd.Series, float]:
    """Optimistic floor per SBT class — matches v9."""
    if tr.empty:
        return pred, 0.0
    l12 = tr[-12:] if len(tr) >= 12 else tr
    l6 = tr[-6:] if len(tr) >= 6 else tr
    l3 = tr[-3:] if len(tr) >= 3 else tr

    def _med(s: pd.Series) -> float:
        x = s[s > 0].values
        return float(np.median(x)) if len(x) else 0.0

    med6, med3, med12 = _med(l6), _med(l3), _med(l12)
    fv = {
        "smooth": max(1.0, 0.70 * max(med6, med3, med12)),
        "erratic": max(1.0, 0.50 * max(med6, med12)),
        "intermittent": max(0.0, 0.30 * med12),
        "lumpy": max(0.0, 0.15 * med12),
    }.get(dclass, 0.0)
    arr = np.maximum(pred.values, fv) if fv > 0 else pred.values
    return pd.Series(arr, index=pred.index), float(fv)


def _bootstrap_pi(
    tr: pd.Series,
    final_pred: pd.Series,
    proxy_model: str,
    h: int,
    n_boot: int = N_BOOTSTRAP,
) -> dict[float, tuple[np.ndarray, np.ndarray]]:
    y = tr.values.astype(float)
    try:
        ins = _MODELS[proxy_model](tr, len(y))
        resid = y - ins.values[:len(y)]
    except Exception:
        resid = y - np.median(y)
    resid = resid[np.isfinite(resid)]
    if len(resid) == 0:
        resid = np.array([0.0])
    rng = np.random.RandomState(SEED)
    paths = np.array([
        np.maximum(0.0, final_pred.values + rng.choice(resid, size=h, replace=True))
        for _ in range(n_boot)
    ])
    q_map = {0.80: (10, 90), 0.95: (2.5, 97.5)}
    return {lvl: (np.maximum(0.0, np.percentile(paths, q_map[lvl][0], axis=0)),
                  np.percentile(paths, q_map[lvl][1], axis=0)) for lvl in q_map}


def _apply_bias_correction(
    pred: pd.Series,
    correction: dict | None,
    alpha: float = BIAS_ALPHA,
) -> tuple[pd.Series, float]:
    if not correction or correction.get("n_obs", 0) < MIN_OBS_CORR:
        return pred, 0.0
    if abs(correction.get("bias_pct", 0.0)) < 0.05:
        return pred, 0.0
    corr_val = alpha * correction["bias"]
    return pd.Series(np.maximum(0.0, pred.values + corr_val), index=pred.index), float(corr_val)


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------
@dataclass
class ForecastEngineResult:
    forecast: pd.DataFrame         # schema-compliant: sku_id, ubicacion, mes, forecast_modelo
    intervals: pd.DataFrame        # lo80/hi80/lo95/hi95 + metodo + clase
    resumen: pd.DataFrame          # one row per series — metadata + chosen method
    perfiles: pd.DataFrame         # seasonal indices per month
    correcciones: pd.DataFrame     # adaptive bias corrections applied


def _compute_bias_corrections(
    previous_forecast: pd.DataFrame | None,
    demand: pd.DataFrame,
) -> dict[tuple[str, str], dict]:
    """Compare last-run's forecast against the now-realised demand."""
    if previous_forecast is None or previous_forecast.empty:
        return {}
    req = {"sku_id", "ubicacion", "mes", "forecast_modelo"}
    if not req.issubset(previous_forecast.columns):
        return {}
    prev = previous_forecast[["sku_id", "ubicacion", "mes", "forecast_modelo"]].copy()
    prev["mes"] = pd.to_datetime(prev["mes"])
    actuals = demand.groupby(["sku_id", "ubicacion", "mes"], sort=False)["demanda"].sum().reset_index()
    merged = prev.merge(actuals, on=["sku_id", "ubicacion", "mes"], how="inner")
    if merged.empty:
        return {}
    merged["error"] = merged["demanda"] - merged["forecast_modelo"]
    out: dict[tuple[str, str], dict] = {}
    for (sku, ub), g in merged.groupby(["sku_id", "ubicacion"]):
        mu = float(g["demanda"].mean())
        bias = float(g["error"].mean())
        out[(sku, ub)] = {
            "bias": bias,
            "bias_pct": bias / mu if mu > 0 else 0.0,
            "n_obs": int(len(g)),
            "rmse": float(np.sqrt((g["error"] ** 2).mean())),
            "mean_actual": mu,
        }
    return out


def run_forecast_engine(
    demand: pd.DataFrame,
    gate: TemporalGate,
    params: EngineParams,
    previous_forecast: pd.DataFrame | None = None,
) -> ForecastEngineResult:
    """Run the full v9 pipeline series by series and return bundled outputs."""
    log = get_logger("forecast.engine")
    H = params.forecast.horizon_months
    target_start = pd.Timestamp(gate.last_closed_month + relativedelta(months=1))

    if demand.empty:
        empty_cols = ["sku_id", "ubicacion", "mes", "forecast_modelo"]
        return ForecastEngineResult(
            pd.DataFrame(columns=empty_cols), pd.DataFrame(), pd.DataFrame(),
            pd.DataFrame(), pd.DataFrame(),
        )

    demand = demand.copy()
    demand["mes"] = pd.to_datetime(demand["mes"])
    demand["sku_id"] = demand["sku_id"].astype(str)
    demand["ubicacion"] = demand["ubicacion"].astype(str)

    corrections = _compute_bias_corrections(previous_forecast, demand)

    rows_fcst: list[pd.DataFrame] = []
    rows_int: list[pd.DataFrame] = []
    rows_sum: list[dict] = []
    rows_prof: list[dict] = []
    rows_corr: list[dict] = []

    groups = demand.groupby(["sku_id", "ubicacion"], sort=False)
    for (sku_id, ubicacion), g in groups:
        ts_full = _complete_monthly(g.set_index("mes")["demanda"].astype(float))
        if ts_full.empty or ts_full.sum() == 0:
            continue

        # Truncate at last closed month
        cutoff = target_start - pd.offsets.MonthBegin(1)
        tr = ts_full[ts_full.index <= cutoff]
        if tr.empty or len(tr) < 3:
            base = float(tr.iloc[-1]) if len(tr) > 0 else 0.0
            idx = pd.date_range(start=target_start, periods=H, freq="MS")
            pred = pd.Series(np.repeat(max(0.0, base), H), index=idx)
            metodo, dclass = "SimpleAvg", "lumpy"
            sinfo = {"F_s": 0.0, "seasonal_type": "stable", "peak_months": [], "indices": np.ones(12)}
            adi = cv2 = float("inf")
            slope = 0.0
            p_val = 1.0
            best_single = "SES"
        else:
            ts_clean = _remove_outliers(tr)
            dclass, adi, cv2 = _classify_sbt(ts_clean)
            sinfo = _analyze_seasonality(ts_clean)
            slope, p_val, has_trend = _detect_trend(ts_clean)
            candidates = _select_candidates(dclass, sinfo["F_s"], sinfo["seasonal_type"], has_trend, len(ts_clean))
            scores = _walk_forward(ts_clean, candidates, h=H)
            if not scores:
                scores = {candidates[0]: 999.0}
            best_single = min(scores, key=scores.get)

            forecasts: dict[str, pd.Series] = {}
            for m in scores:
                try:
                    forecasts[m] = _MODELS[m](ts_clean, H)
                except Exception:
                    continue

            if len(forecasts) >= 2:
                base_pred = _ensemble(scores, forecasts, top_k=TOP_K)
                metodo = f"Ensemble(top{min(TOP_K, len(forecasts))})"
                if base_pred is None:
                    metodo, base_pred = best_single, forecasts.get(best_single)
            else:
                metodo, base_pred = best_single, forecasts.get(best_single)

            if base_pred is None:
                base_pred = pd.Series(np.repeat(float(ts_clean.iloc[-1]), H),
                                      index=pd.date_range(start=target_start, periods=H, freq="MS"))
                metodo = "Naive-Fallback"
            else:
                base_pred.index = pd.date_range(start=target_start, periods=H, freq="MS")

            pred, floor_val = _apply_floor(base_pred, ts_clean, dclass)
            pred, bias_val = _apply_bias_correction(pred, corrections.get((sku_id, ubicacion)))

            try:
                pi = _bootstrap_pi(ts_clean, pred, best_single, H)
                lo80, hi80 = pi[0.80]
                lo95, hi95 = pi[0.95]
            except Exception:
                sigma = np.std(ts_clean.values) if len(ts_clean) > 1 else 1.0
                lo80 = np.maximum(0.0, pred.values - 1.28 * sigma)
                hi80 = pred.values + 1.28 * sigma
                lo95 = np.maximum(0.0, pred.values - 1.96 * sigma)
                hi95 = pred.values + 1.96 * sigma

        df_fc = pd.DataFrame({
            "sku_id": sku_id,
            "ubicacion": ubicacion,
            "mes": pred.index,
            "forecast_modelo": np.maximum(0.0, pred.values),
        })
        rows_fcst.append(df_fc)

        if len(tr) >= 3:
            rows_int.append(pd.DataFrame({
                "sku_id": sku_id, "ubicacion": ubicacion, "mes": pred.index,
                "lo80": lo80, "hi80": hi80, "lo95": lo95, "hi95": hi95,
                "metodo": metodo, "clase_sbt": dclass,
            }))

        rows_sum.append({
            "sku_id": sku_id,
            "ubicacion": ubicacion,
            "metodo": metodo,
            "clase_sbt": dclass,
            "tipo_estacion": sinfo["seasonal_type"],
            "f_estacional": round(sinfo["F_s"], 3),
            "peak_months": str(sinfo["peak_months"]),
            "adi": round(adi, 3) if np.isfinite(adi) else 999,
            "cv2": round(cv2, 3) if np.isfinite(cv2) else 999,
            "trend_slope": round(slope, 4),
            "trend_pval": round(p_val, 4),
            "n_meses_hist": int(len(ts_full)),
            "pronostico_total": float(df_fc["forecast_modelo"].sum()),
        })

        rows_prof.append({
            "sku_id": sku_id,
            "ubicacion": ubicacion,
            "clase_sbt": dclass,
            "tipo_estacion": sinfo["seasonal_type"],
            "f_estacional": round(sinfo["F_s"], 3),
            **{f"idx_{i+1:02d}": float(sinfo["indices"][i]) for i in range(12)},
        })

        corr = corrections.get((sku_id, ubicacion))
        if corr and corr["n_obs"] >= MIN_OBS_CORR:
            rows_corr.append({
                "sku_id": sku_id,
                "ubicacion": ubicacion,
                "n_obs_comparadas": corr["n_obs"],
                "bias_unidades": round(corr["bias"], 2),
                "bias_pct": round(corr["bias_pct"] * 100, 1),
                "rmse": round(corr["rmse"], 2),
                "media_real": round(corr["mean_actual"], 2),
                "correccion_activa": abs(corr["bias_pct"]) >= 0.05,
            })

    forecast_df = (
        pd.concat(rows_fcst, ignore_index=True)
        if rows_fcst else pd.DataFrame(columns=["sku_id", "ubicacion", "mes", "forecast_modelo"])
    )
    intervals_df = pd.concat(rows_int, ignore_index=True) if rows_int else pd.DataFrame()
    resumen_df = pd.DataFrame(rows_sum)
    perfiles_df = pd.DataFrame(rows_prof)
    correcciones_df = pd.DataFrame(rows_corr)

    log.info(
        "forecast.engine.done",
        series=len(groups),
        horizon=H,
        target_start=str(target_start.date()),
        with_bias_corr=len(correcciones_df),
    )
    return ForecastEngineResult(forecast_df, intervals_df, resumen_df, perfiles_df, correcciones_df)
