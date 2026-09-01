"""Ahorro generado por traslados vs. compras externas.

Lee todos los ``carga_maryun.csv`` historicos en ``data/output/`` y entrega
agregados por día / semana / mes calendario. El cálculo por fila vive en
``app.export.carga_maryun`` (columna ``ahorro_clp``); aquí solo se consolida.
"""
from app.savings.aggregator import (
    AhorroResumen,
    compute_ahorro_resumen,
    load_historical_carga,
)

__all__ = [
    "AhorroResumen",
    "compute_ahorro_resumen",
    "load_historical_carga",
]
