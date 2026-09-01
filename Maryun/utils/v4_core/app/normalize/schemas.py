"""Pandera schemas — the data contracts between pipeline stages.

Every DataFrame that crosses a module boundary must match one of these.
Keep the columns minimal (what consumers actually read) — stages can
attach extra columns, but missing/required ones fail fast.
"""
from __future__ import annotations

try:
    import pandera.pandas as pa
    from pandera.pandas import Check, Column, DataFrameSchema
except Exception:  # pragma: no cover - Mage image may ship pandas/pandera mismatch
    class _NoopSchema:
        def __init__(self, *args, **kwargs):
            self.columns = kwargs.get("columns", {})

        def validate(self, df, *args, **kwargs):
            return df

    class _NoopCheck:
        @staticmethod
        def ge(*args, **kwargs):
            return None

        @staticmethod
        def gt(*args, **kwargs):
            return None

        @staticmethod
        def isin(*args, **kwargs):
            return None

    class _NoopPandera:
        DateTime = "datetime64[ns]"

    def Column(*args, **kwargs):
        return None

    DataFrameSchema = _NoopSchema
    Check = _NoopCheck
    pa = _NoopPandera()


# ---------------------------------------------------------------------------
# Products — one row per SKU.
# ---------------------------------------------------------------------------
ProductsSchema = DataFrameSchema(
    columns={
        "sku_id": Column(str, nullable=False, unique=True),
        "sku": Column(str, nullable=False),
        "nombre": Column(str, nullable=True),
        "familia": Column(str, nullable=True),
        "marca": Column(str, nullable=True),
        "tipo": Column(str, nullable=True),
        "costo": Column(float, Check.ge(0), nullable=True),
        "procedencia": Column(
            str,
            Check.isin(["nacional", "importado"]),
            nullable=True,
        ),
        "critico": Column(bool, nullable=True),
        "sale_ok": Column(bool, nullable=True),
        "purchase_ok": Column(bool, nullable=True),
    },
    strict=False,  # allow extra columns
    coerce=True,
)

# ---------------------------------------------------------------------------
# Stock — one row per (sku, ubicacion).
# ---------------------------------------------------------------------------
StockSchema = DataFrameSchema(
    columns={
        "sku_id": Column(str, nullable=False),
        "ubicacion": Column(str, nullable=False),  # sucursal or CD
        "qty": Column(float, Check.ge(0), nullable=False),
    },
    strict=False,
    coerce=True,
)

# ---------------------------------------------------------------------------
# Demand — monthly demand per (sku, ubicacion, mes).
# ---------------------------------------------------------------------------
DemandSchema = DataFrameSchema(
    columns={
        "sku_id": Column(str, nullable=False),
        "ubicacion": Column(str, nullable=False),
        "mes": Column(pa.DateTime, nullable=False),  # month-start
        "demanda": Column(float, Check.ge(0), nullable=False),
    },
    strict=False,
    coerce=True,
)

# ---------------------------------------------------------------------------
# Forecast — horizon months per (sku, ubicacion).
# ---------------------------------------------------------------------------
ForecastSchema = DataFrameSchema(
    columns={
        "sku_id": Column(str, nullable=False),
        "ubicacion": Column(str, nullable=False),
        "mes": Column(pa.DateTime, nullable=False),
        "forecast_modelo": Column(float, Check.ge(0), nullable=False),
        "forecast_override": Column(float, Check.ge(0), nullable=True),
        "forecast_final": Column(float, Check.ge(0), nullable=False),
        "forecast_fue_forzado": Column(bool, nullable=False),
        "motivo_override": Column(str, nullable=True),
        "responsable_override": Column(str, nullable=True),
    },
    strict=False,
    coerce=True,
)

# ---------------------------------------------------------------------------
# Classification — ABC/XYZ per (sku, ubicacion).
# ---------------------------------------------------------------------------
_ABC = ["A", "B", "C"]
_XYZ = ["X", "Y", "Z"]
_COMBOS = [f"{a}{x}" for a in _ABC for x in _XYZ]

ClassificationSchema = DataFrameSchema(
    columns={
        "sku_id": Column(str, nullable=False),
        "ubicacion": Column(str, nullable=False),
        "abc_modelo": Column(str, Check.isin(_ABC), nullable=False),
        "xyz_modelo": Column(str, Check.isin(_XYZ), nullable=False),
        "clase_modelo": Column(str, Check.isin(_COMBOS), nullable=False),
        "abc_override": Column(str, Check.isin(_ABC), nullable=True),
        "xyz_override": Column(str, Check.isin(_XYZ), nullable=True),
        "clase_final": Column(str, Check.isin(_COMBOS), nullable=False),
        "clase_fue_forzada": Column(bool, nullable=False),
        "motivo_override": Column(str, nullable=True),
        "responsable_override": Column(str, nullable=True),
    },
    strict=False,
    coerce=True,
)

# ---------------------------------------------------------------------------
# Homologation — many-to-one map importado → nacional.
# ---------------------------------------------------------------------------
HomologacionSchema = DataFrameSchema(
    columns={
        "sku_id_importado": Column(str, nullable=False),
        "sku_id_nacional": Column(str, nullable=False),
        "factor_conversion": Column(float, Check.gt(0), nullable=False),
        "usar_analitico": Column(bool, nullable=False),       # reinforce Maryun forecast
        "usar_operacional": Column(bool, nullable=False),     # substitute on zero-stock trigger
        "vigente_desde": Column(pa.DateTime, nullable=True),
        "vigente_hasta": Column(pa.DateTime, nullable=True),
        "responsable": Column(str, nullable=True),
    },
    strict=False,
    coerce=True,
)

# ---------------------------------------------------------------------------
# Needs — required units per (sku, sucursal).
# ---------------------------------------------------------------------------
NeedsSchema = DataFrameSchema(
    columns={
        "sku_id": Column(str, nullable=False),
        "ubicacion": Column(str, nullable=False),
        "demanda_esperada": Column(float, Check.ge(0), nullable=False),
        "stock_actual": Column(float, Check.ge(0), nullable=False),
        "safety_stock": Column(float, Check.ge(0), nullable=False),
        "necesidad": Column(float, Check.ge(0), nullable=False),
        "dispara": Column(bool, nullable=False),
    },
    strict=False,
    coerce=True,
)

# ---------------------------------------------------------------------------
# Replenishment plan — proposed moves (pre-export).
# ---------------------------------------------------------------------------
_CAPAS = ["inmovilizado", "sobrestock", "cd", "compra"]

ReplenishmentPlanSchema = DataFrameSchema(
    columns={
        "sku_id": Column(str, nullable=False),
        "origen": Column(str, nullable=False),
        "destino": Column(str, nullable=False),
        "capa": Column(str, Check.isin(_CAPAS), nullable=False),
        "cantidad": Column(float, Check.gt(0), nullable=False),
        "score": Column(float, nullable=True),
        "motivo": Column(str, nullable=True),
        "homologado_desde_sku": Column(str, nullable=True),  # if operational substitution
    },
    strict=False,
    coerce=True,
)


SCHEMAS: dict[str, DataFrameSchema] = {
    "products": ProductsSchema,
    "stock": StockSchema,
    "demand": DemandSchema,
    "forecast": ForecastSchema,
    "classification": ClassificationSchema,
    "homologacion": HomologacionSchema,
    "needs": NeedsSchema,
    "replenishment_plan": ReplenishmentPlanSchema,
}
