"""Choose between the advanced engine, simple baseline, or a precomputed reader."""
from __future__ import annotations

import pandas as pd

from app.config.engine_params import EngineParams
from app.config.paths import ProjectPaths
from app.config.settings import Settings
from app.forecast.baseline import forecast_hw as _forecast_hw_baseline
from app.forecast.engine import ForecastEngineResult, run_forecast_engine
from app.forecast.reader import read_forecast_excel
from app.normalize.schemas import ForecastSchema
from app.temporal.gate import TemporalGate
from app.utils.logging import get_logger


def build_forecast(
    demand: pd.DataFrame,
    gate: TemporalGate,
    settings: Settings,
    params: EngineParams,
    paths: ProjectPaths,
    previous_forecast: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, ForecastEngineResult | None]:
    """Return ``(forecast_df_schema_compliant, engine_result)``.

    When the advanced engine is used, ``engine_result`` carries extra side
    tables (intervals, resumen, perfiles, correcciones) for the Export stage.
    It's ``None`` for the simple baseline and the reader mode.
    """
    log = get_logger("forecast")
    engine_result: ForecastEngineResult | None = None

    if settings.run.run_forecast:
        log.info("forecast.mode", mode="engine_v9")
        engine_result = run_forecast_engine(demand, gate, params, previous_forecast=previous_forecast)
        model_df = engine_result.forecast
    else:
        log.info("forecast.mode", mode="reader", path=str(paths.forecast_precomputed_xlsx))
        model_df = read_forecast_excel(paths.forecast_precomputed_xlsx)

    out = model_df.copy()
    # `forecast_override` must be a proper nullable float column (pandera requires float64),
    # not an object column full of pd.NA.
    out["forecast_override"] = pd.Series([float("nan")] * len(out), dtype="float64", index=out.index)
    out["forecast_final"] = out["forecast_modelo"].astype(float)
    out["forecast_fue_forzado"] = False
    out["motivo_override"] = pd.Series([pd.NA] * len(out), dtype="object", index=out.index)
    out["responsable_override"] = pd.Series([pd.NA] * len(out), dtype="object", index=out.index)

    return ForecastSchema.validate(out, lazy=True), engine_result


__all__ = ["build_forecast", "_forecast_hw_baseline"]
