"""Holt-Winters baseline forecast.

This is a *placeholder* that can be swapped out when Victor hands over the
real script. The interface is what matters: ``forecast_hw(demand, gate, params)``
returning ``(sku_id, ubicacion, mes, forecast_modelo)`` for ``horizon_months``
month-starts after the last closed month.

Fallback hierarchy:
1. If a series has >= ``min_history_months`` points: Holt-Winters additive.
2. Else if ``fallback_to_naive_below_min``: last observed value (or 0).
3. Else: mean of history.
"""
from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
from dateutil.relativedelta import relativedelta

from app.config.engine_params import EngineParams
from app.temporal.gate import TemporalGate
from app.utils.logging import get_logger


def forecast_hw(
    demand: pd.DataFrame,
    gate: TemporalGate,
    params: EngineParams,
) -> pd.DataFrame:
    """Return a DataFrame ``[sku_id, ubicacion, mes, forecast_modelo]``."""
    log = get_logger("forecast.baseline")
    fp = params.forecast
    horizon_months = fp.horizon_months
    min_hist = fp.min_history_months
    first_future = gate.last_closed_month + relativedelta(months=1)
    future_months = [first_future + relativedelta(months=i) for i in range(horizon_months)]

    # Only build forecasts for (sku, ubicacion) pairs that actually have any
    # demand in the window. Zero-demand series get skipped.
    if demand.empty:
        return pd.DataFrame(columns=["sku_id", "ubicacion", "mes", "forecast_modelo"])

    out: list[dict] = []
    groups = demand.groupby(["sku_id", "ubicacion"], sort=False)

    for (sku_id, ubicacion), g in groups:
        series = _dense_monthly(g, gate)
        preds = _predict_series(series, horizon_months, min_hist, fp)
        for m, v in zip(future_months, preds):
            out.append({
                "sku_id": sku_id,
                "ubicacion": ubicacion,
                "mes": pd.Timestamp(m),
                "forecast_modelo": max(0.0, float(v)),
            })

    log.info("forecast.baseline.done", series=len(groups), horizon=horizon_months)
    return pd.DataFrame(out)


def _dense_monthly(g: pd.DataFrame, gate: TemporalGate) -> pd.Series:
    """Build a dense month-indexed series inside the gate window."""
    months = [pd.Timestamp(m) for m in gate.window.months]
    s = g.groupby("mes", sort=True)["demanda"].sum()
    s = s.reindex(months, fill_value=0.0)
    s.index = pd.DatetimeIndex(months, name="mes")
    return s.astype(float)


def _predict_series(
    series: pd.Series,
    horizon: int,
    min_hist: int,
    fp,
) -> np.ndarray:
    n_obs = int((series > 0).sum())
    method = fp.method

    if method == "mean":
        return np.repeat(series.mean() if len(series) else 0.0, horizon)

    if method == "naive" or (n_obs < min_hist and fp.fallback_to_naive_below_min):
        last = float(series.iloc[-1]) if len(series) else 0.0
        return np.repeat(last, horizon)

    if method == "holt_winters" and n_obs >= min_hist:
        try:
            from statsmodels.tsa.holtwinters import ExponentialSmoothing

            seasonal = "add" if len(series) >= 2 * fp.seasonal_periods else None
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                model = ExponentialSmoothing(
                    series,
                    trend="add",
                    seasonal=seasonal,
                    seasonal_periods=fp.seasonal_periods if seasonal else None,
                    initialization_method="estimated",
                ).fit(optimized=True)
            return np.asarray(model.forecast(horizon), dtype=float)
        except Exception:  # pragma: no cover — fall through to naive
            pass

    last = float(series.iloc[-1]) if len(series) else 0.0
    return np.repeat(last, horizon)
