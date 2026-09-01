"""External-sources sync stage.

Mantiene los archivos derivados del pipeline en sincronia con las fuentes
externas (OneDrive / red). Se ejecuta al inicio de cada run.

Esta etapa SOLO loguea las rutas externas para auditoria. Los archivos
se leen directamente en sus stages correspondientes:

- forecast: lee MARYUN_EXTERNAL__FORECAST_XLSX en stage_forecast
- clasificacion v3: lee MARYUN_EXTERNAL__ABC_XYZ_XLSX en stage_classification
  como fuente PRIMARIA (sobre el baseline del engine_v3)

El archivo override_clasificacion.xlsx YA NO se regenera automaticamente.
Se reserva exclusivamente para overrides manuales aplicados en stage_overrides.
"""
from __future__ import annotations

from pathlib import Path

from app.utils.logging import get_logger


def _file_mtime(path: Path) -> float:
    """Retorna mtime del archivo, o 0.0 si no existe."""
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return 0.0


def sync_external_sources(ctx) -> dict:
    """Loguea las fuentes externas que se usaran en este run.

    Devuelve un dict con metadata para auditoria.
    """
    log = get_logger("pipeline.external_sync")
    result = {
        "forecast_source": None,
        "forecast_external_used": False,
        "abc_xyz_source": None,
        "abc_xyz_external_used": False,
    }

    # ----- Forecast ---------------------------------------------------------
    ext_forecast = ctx.paths.external_forecast_xlsx
    if ext_forecast and ext_forecast.exists():
        result["forecast_source"] = str(ext_forecast)
        result["forecast_external_used"] = True
        log.info(
            "external_sync.forecast.external",
            path=str(ext_forecast),
            mtime=_file_mtime(ext_forecast),
        )
    else:
        local = ctx.paths.reference_dir / "forecast_precomputado.xlsx"
        result["forecast_source"] = str(local)
        log.info("external_sync.forecast.local", path=str(local))

    # ----- Clasificacion ABC/XYZ (v3) --------------------------------------
    # El v3 se lee DIRECTAMENTE en stage_classification como fuente primaria.
    # Aqui solo logueamos para trazabilidad.
    ext_abc = ctx.paths.external_abc_xyz_xlsx
    if ext_abc and ext_abc.exists():
        result["abc_xyz_source"] = str(ext_abc)
        result["abc_xyz_external_used"] = True
        log.info(
            "external_sync.abc_xyz.external",
            path=str(ext_abc),
            mtime=_file_mtime(ext_abc),
            nota="v3 se aplicara como fuente primaria en stage_classification",
        )
    else:
        log.info(
            "external_sync.abc_xyz.no_external",
            nota="solo se usara engine_v3 baseline + overrides manuales",
        )

    # ----- Override manual (informativo) -----------------------------------
    override_local = ctx.paths.override_clasificacion_xlsx
    if override_local.exists():
        log.info(
            "external_sync.override_manual.detected",
            path=str(override_local),
            nota="se aplicara en stage_overrides (solo cambios manuales)",
        )

    return result
