"""Normalize layer — pandera schemas + canonical DataFrame shapes.

All inter-module DataFrames must conform to one of the schemas in
``app.normalize.schemas``. Stages that produce such a frame should call the
schema's ``validate`` method before returning.
"""
from app.normalize.schemas import (
    SCHEMAS,
    ClassificationSchema,
    DemandSchema,
    ForecastSchema,
    HomologacionSchema,
    NeedsSchema,
    ProductsSchema,
    ReplenishmentPlanSchema,
    StockSchema,
)
from app.normalize.canonical import (
    canonical_location,
    canonical_procedencia,
    canonical_sku,
    is_cd,
    is_sucursal,
)

__all__ = [
    "SCHEMAS",
    "ClassificationSchema",
    "DemandSchema",
    "ForecastSchema",
    "HomologacionSchema",
    "NeedsSchema",
    "ProductsSchema",
    "ReplenishmentPlanSchema",
    "StockSchema",
    "canonical_location",
    "canonical_procedencia",
    "canonical_sku",
    "is_cd",
    "is_sucursal",
]
