"""Analytical homologation — strengthen Maryun forecasts with nacional demand.

Given a forecast DataFrame ``[sku_id, ubicacion, mes, forecast_modelo, …]``
and a homologation table, for every ``(importado → nacional)`` pair flagged
``usar_analitico`` we blend the importado's model forecast with the
nacional's one:

    forecast_importado_new = (1 - w) * forecast_importado
                            + w * factor_conversion * forecast_nacional

where ``w`` is ``params.homologation.analytical_weight_national``.

The pre-blend value is preserved in ``forecast_modelo_antes_homologacion``
for audit. We DO NOT touch ``forecast_final`` here — that's the Overrides
layer's job. We update ``forecast_modelo`` so downstream stages see the
reinforced value and ``forecast_final`` is recomputed by the override
resolver (which defaults to ``forecast_modelo`` if no override exists).
"""
from __future__ import annotations

import pandas as pd

from app.config.engine_params import EngineParams
from app.utils.logging import get_logger


def reinforce_forecast_with_national(
    forecast: pd.DataFrame,
    homologacion,
    params: EngineParams,
) -> pd.DataFrame:
    """Return a new forecast frame with analytical reinforcement applied."""
    log = get_logger("homologation.analytical")

    if not params.homologation.enabled or homologacion.is_empty() or forecast.empty:
        return forecast

    w = float(params.homologation.analytical_weight_national)
    if w <= 0:
        return forecast

    out = forecast.copy()
    out["forecast_modelo_antes_homologacion"] = out["forecast_modelo"].astype(float)
    out["homologado_analiticamente"] = False

    # Index nacional forecasts by (sku_id, ubicacion, mes) for fast lookup.
    nac_idx = (
        out.set_index(["sku_id", "ubicacion", "mes"])["forecast_modelo"]
        .to_dict()
    )

    n_changed = 0
    for (imp_sku, nac_sku) in homologacion.analitico:
        factor = homologacion.factor[(imp_sku, nac_sku)]
        mask = out["sku_id"] == imp_sku
        if not mask.any():
            continue
        for ix in out.index[mask]:
            key = (nac_sku, out.at[ix, "ubicacion"], out.at[ix, "mes"])
            nac_val = nac_idx.get(key)
            if nac_val is None:
                continue
            old = float(out.at[ix, "forecast_modelo"])
            new = (1 - w) * old + w * factor * float(nac_val)
            if new != old:
                out.at[ix, "forecast_modelo"] = new
                out.at[ix, "forecast_final"] = new
                out.at[ix, "homologado_analiticamente"] = True
                n_changed += 1

    log.info("homologation.analytical.done", rows_reinforced=n_changed,
             weight_national=w, pairs=len(homologacion.analitico))
    return out
