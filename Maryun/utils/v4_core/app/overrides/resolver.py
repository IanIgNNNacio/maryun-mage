"""Apply loaded overrides onto forecast and classification frames."""
from __future__ import annotations

import pandas as pd

from app.config.engine_params import EngineParams
from app.normalize.schemas import ClassificationSchema, ForecastSchema
from app.utils.logging import get_logger


def apply_forecast_overrides(
    forecast: pd.DataFrame,
    overrides: pd.DataFrame,
    params: EngineParams,
) -> pd.DataFrame:
    """Return a new forecast frame with overrides materialised in the final columns."""
    log = get_logger("overrides.forecast")
    if forecast.empty:
        return forecast

    out = forecast.copy()
    # Ensure date dtype consistency for the join.
    out["mes"] = pd.to_datetime(out["mes"])
    if overrides.empty:
        out["forecast_final"] = out["forecast_modelo"].astype(float)
        out["forecast_fue_forzado"] = False
        out["fuente_dato"] = "excel_v10"  # origen: Excel oficial de pronostico
        out["forecast_override"] = pd.to_numeric(
            out.get("forecast_override", pd.Series([pd.NA] * len(out))),
            errors="coerce",
        ).astype(float)
        return ForecastSchema.validate(out, lazy=True)

    ov = overrides.copy()
    ov["mes"] = pd.to_datetime(ov["mes"])

    merged = out.merge(
        ov[["sku_id", "ubicacion", "mes", "forecast_override", "motivo", "responsable"]],
        on=["sku_id", "ubicacion", "mes"],
        how="left",
        suffixes=("", "_ov"),
    )

    # If the model produced an override column already, prefer the Excel value.
    if "forecast_override_ov" in merged.columns:
        merged["forecast_override"] = merged["forecast_override_ov"].combine_first(merged["forecast_override"])
        merged = merged.drop(columns=["forecast_override_ov"])
    if "motivo_ov" in merged.columns:
        merged["motivo_override"] = merged["motivo_ov"].combine_first(merged.get("motivo_override"))
        merged = merged.drop(columns=["motivo_ov"])
    if "responsable_ov" in merged.columns:
        merged["responsable_override"] = merged["responsable_ov"].combine_first(merged.get("responsable_override"))
        merged = merged.drop(columns=["responsable_ov"])
    if "motivo" in merged.columns:
        merged["motivo_override"] = merged["motivo_override"].combine_first(merged["motivo"])
        merged = merged.drop(columns=["motivo"])
    if "responsable" in merged.columns:
        merged["responsable_override"] = merged["responsable_override"].combine_first(merged["responsable"])
        merged = merged.drop(columns=["responsable"])

    has_ov = merged["forecast_override"].notna()
    merged["forecast_fue_forzado"] = has_ov
    merged["forecast_final"] = merged["forecast_modelo"].astype(float)
    merged.loc[has_ov, "forecast_final"] = merged.loc[has_ov, "forecast_override"].astype(float)

    # Trazabilidad de origen por registro: Excel base vs override
    merged["fuente_dato"] = "excel_v10"
    merged.loc[has_ov, "fuente_dato"] = "override_forecast"

    log.info("overrides.forecast.applied", rows_affected=int(has_ov.sum()))
    return ForecastSchema.validate(merged, lazy=True)


def apply_classification_overrides(
    classification: pd.DataFrame,
    overrides: pd.DataFrame,
    params: EngineParams,
) -> pd.DataFrame:
    log = get_logger("overrides.classification")
    if classification.empty:
        return classification

    out = classification.copy()
    if overrides.empty:
        out["clase_final"] = out["clase_modelo"].astype(str)
        out["clase_fue_forzada"] = False
        return ClassificationSchema.validate(out, lazy=True)

    ov = overrides.copy()

    merged = out.merge(
        ov[["sku_id", "ubicacion", "abc_override", "xyz_override", "motivo", "responsable"]],
        on=["sku_id", "ubicacion"],
        how="left",
        suffixes=("", "_ov"),
    )

    if "abc_override_ov" in merged.columns:
        merged["abc_override"] = merged["abc_override_ov"].combine_first(merged["abc_override"])
        merged = merged.drop(columns=["abc_override_ov"])
    if "xyz_override_ov" in merged.columns:
        merged["xyz_override"] = merged["xyz_override_ov"].combine_first(merged["xyz_override"])
        merged = merged.drop(columns=["xyz_override_ov"])
    if "motivo" in merged.columns:
        merged["motivo_override"] = merged["motivo_override"].combine_first(merged["motivo"])
        merged = merged.drop(columns=["motivo"])
    if "responsable" in merged.columns:
        merged["responsable_override"] = merged["responsable_override"].combine_first(merged["responsable"])
        merged = merged.drop(columns=["responsable"])

    abc_final = merged["abc_override"].combine_first(merged["abc_modelo"]).astype(str)
    xyz_final = merged["xyz_override"].combine_first(merged["xyz_modelo"]).astype(str)
    merged["clase_final"] = abc_final + xyz_final
    merged["clase_fue_forzada"] = (
        merged["abc_override"].notna() | merged["xyz_override"].notna()
    )

    log.info("overrides.classification.applied", rows_affected=int(merged["clase_fue_forzada"].sum()))
    return ClassificationSchema.validate(merged, lazy=True)


def build_overrides_audit(
    forecast_final: pd.DataFrame,
    classification_final: pd.DataFrame,
) -> pd.DataFrame:
    """Return one-row-per-override log combining both kinds."""
    parts: list[pd.DataFrame] = []

    if not forecast_final.empty and "forecast_fue_forzado" in forecast_final.columns:
        f = forecast_final[forecast_final["forecast_fue_forzado"]].copy()
        if not f.empty:
            f["tipo"] = "forecast"
            f["valor_modelo"] = f["forecast_modelo"].astype(float)
            f["valor_final"] = f["forecast_final"].astype(float)
            parts.append(f[[
                "tipo", "sku_id", "ubicacion", "mes",
                "valor_modelo", "valor_final",
                "motivo_override", "responsable_override",
            ]])

    if not classification_final.empty and "clase_fue_forzada" in classification_final.columns:
        c = classification_final[classification_final["clase_fue_forzada"]].copy()
        if not c.empty:
            c["tipo"] = "clasificacion"
            c["mes"] = pd.NaT
            c["valor_modelo"] = c["clase_modelo"].astype(str)
            c["valor_final"] = c["clase_final"].astype(str)
            parts.append(c[[
                "tipo", "sku_id", "ubicacion", "mes",
                "valor_modelo", "valor_final",
                "motivo_override", "responsable_override",
            ]])

    if not parts:
        return pd.DataFrame(columns=[
            "tipo", "sku_id", "ubicacion", "mes",
            "valor_modelo", "valor_final",
            "motivo_override", "responsable_override",
        ])
    return pd.concat(parts, ignore_index=True)
