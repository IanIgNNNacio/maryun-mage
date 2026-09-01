"""MariaDB implementation of the extraction source."""
from __future__ import annotations

from dateutil.relativedelta import relativedelta
import pandas as pd

from app.connectors.mariadb import MariaDBConnector
from app.extract.queries import load_sql
from app.normalize.canonical import (
    canonical_location,
    canonical_procedencia,
    canonical_sku,
)
from app.temporal.gate import TemporalGate
from app.utils.logging import get_logger


class MariaDBSource:
    """Pulls products/stock/demand from MariaDB and normalises them."""

    def __init__(self, connector: MariaDBConnector | None = None) -> None:
        self._conn = connector or MariaDBConnector()
        self._log = get_logger("extract.mariadb")

    def products(self) -> pd.DataFrame:
        df = self._conn.run_query(load_sql("products"))
        df["sku_id"] = df["sku_id"].map(canonical_sku)
        df["procedencia"] = df["procedencia"].map(canonical_procedencia)
        for bcol in ("critico", "sale_ok", "purchase_ok"):
            if bcol in df.columns:
                df[bcol] = df[bcol].astype("boolean").fillna(False).astype(bool)
        return df

    def stock(self) -> pd.DataFrame:
        df = self._conn.run_query(load_sql("stock"))
        df["sku_id"] = df["sku_id"].map(canonical_sku)
        df["ubicacion"] = df["ubicacion"].map(canonical_location)
        df["qty"] = df["qty"].astype(float)
        return df

    def demand(self, gate: TemporalGate) -> pd.DataFrame:
        # Open-month boundary is the first day of the month *after* the last
        # closed month → exclusive end.
        history_end = gate.last_closed_month + relativedelta(months=1)
        params = {
            "history_start": gate.history_start.isoformat(),
            "history_end": history_end.isoformat(),
        }
        df = self._conn.run_query(load_sql("sales"), params=params)
        df["sku_id"] = df["sku_id"].map(canonical_sku)
        df["ubicacion"] = df["ubicacion"].map(canonical_location)
        df["mes"] = pd.to_datetime(df["mes"])
        df["demanda"] = df["demanda"].astype(float)
        return df

    def ultima_venta(self) -> pd.DataFrame:
        """Ultima fecha de venta (facturacion) por (sku_id, ubicacion).

        Devuelve columnas: sku_id, ubicacion, ultima_venta (datetime).
        Usado para clasificar inmovilizado / sobrestock por dias sin venta.
        """
        df = self._conn.run_query(load_sql("ultima_venta"))
        df["sku_id"] = df["sku_id"].map(canonical_sku)
        df["ubicacion"] = df["ubicacion"].map(canonical_location)
        df["ultima_venta"] = pd.to_datetime(df["ultima_venta"], errors="coerce")
        return df.dropna(subset=["sku_id", "ubicacion", "ultima_venta"])
