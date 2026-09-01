"""High-level ``extract_all`` — picks MariaDB or Mock based on Settings."""
from __future__ import annotations

from typing import TypedDict

import pandas as pd

from app.config.settings import Settings, get_settings
from app.extract.mock_source import MockSource
from app.normalize.schemas import DemandSchema, ProductsSchema, StockSchema
from app.temporal.gate import TemporalGate
from app.temporal.validators import assert_within_window
from app.utils.logging import get_logger


class ExtractResult(TypedDict):
    products: pd.DataFrame
    stock: pd.DataFrame
    demand: pd.DataFrame
    ultima_venta: pd.DataFrame


def extract_all(gate: TemporalGate, settings: Settings | None = None) -> ExtractResult:
    """Run extraction + normalization + schema validation.

    Enforces the temporal window on the demand DataFrame — any rows outside
    the gate's window raise ``TemporalViolation``.
    """
    s = settings or get_settings()
    log = get_logger("extract")
    log.info("extract.start", source=s.run.source, window=repr(gate.window))

    ultima_venta = pd.DataFrame(columns=["sku_id", "ubicacion", "ultima_venta"])
    if s.run.source == "mariadb":
        from app.extract.mariadb_source import MariaDBSource  # lazy import

        src = MariaDBSource()
        products = src.products()
        stock = src.stock()
        demand = src.demand(gate)
        try:
            ultima_venta = src.ultima_venta()
        except Exception as exc:  # no debe romper el pipeline si falta la tabla
            log.warning("extract.ultima_venta_failed", error=str(exc))
    else:
        mock = MockSource().build(gate)
        products, stock, demand = mock.products, mock.stock, mock.demand

    # Validate temporal rule before pandera so we get a helpful message.
    assert_within_window(demand, "mes", gate, context="extract.demand")

    products = ProductsSchema.validate(products, lazy=True)
    stock = StockSchema.validate(stock, lazy=True)
    demand = DemandSchema.validate(demand, lazy=True)

    log.info(
        "extract.done",
        n_products=len(products),
        n_stock=len(stock),
        n_demand=len(demand),
        n_ultima_venta=len(ultima_venta),
    )
    return {"products": products, "stock": stock, "demand": demand,
            "ultima_venta": ultima_venta}
