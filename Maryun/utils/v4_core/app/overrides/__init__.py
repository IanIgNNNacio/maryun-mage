"""Manual overrides for forecast and classification.

Two Excel files, one logic:

* ``override_forecast.xlsx`` — rows ``[sku_id, ubicacion, mes,
  forecast_override, motivo, responsable]`` force a specific forecast value.
* ``override_clasificacion.xlsx`` — rows ``[sku_id, ubicacion,
  abc_override, xyz_override, motivo, responsable]`` force ABC/XYZ.

The resolver merges these into the model frames, setting ``*_final``,
``*_fue_forzado/forzada``, motivo and responsable columns as per the
schemas. An audit DataFrame captures each applied override for the export
stage.
"""
from app.overrides.loader import (
    load_forecast_overrides,
    load_classification_overrides,
    OverrideError,
)
from app.overrides.resolver import (
    apply_forecast_overrides,
    apply_classification_overrides,
    build_overrides_audit,
)

__all__ = [
    "load_forecast_overrides",
    "load_classification_overrides",
    "OverrideError",
    "apply_forecast_overrides",
    "apply_classification_overrides",
    "build_overrides_audit",
]
