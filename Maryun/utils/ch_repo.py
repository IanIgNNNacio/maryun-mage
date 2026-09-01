from __future__ import annotations

import os
from datetime import date
from typing import Any

import pandas as pd


def _require_clickhouse_client():
    try:
        import clickhouse_connect  # type: ignore
    except Exception as exc:
        raise RuntimeError(
            "Falta dependencia clickhouse-connect. Instálala en requirements.txt"
        ) from exc
    return clickhouse_connect


def canonical_sku(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    if s == "":
        return ""
    if s.isdigit():
        s = s.lstrip("0")
        return s or "0"
    return s.upper()


def canonical_location(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().upper()


class ClickHouseRepo:
    def __init__(self) -> None:
        clickhouse_connect = _require_clickhouse_client()
        self.database = os.getenv("CLICKHOUSE_DATABASE", "logistica")
        self.client = clickhouse_connect.get_client(
            host=os.getenv("CLICKHOUSE_HOST", "localhost"),
            port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
            username=os.getenv("CLICKHOUSE_USER", "default"),
            password=os.getenv("CLICKHOUSE_PASSWORD", ""),
            database=self.database,
            secure=os.getenv("CLICKHOUSE_SECURE", "false").lower() == "true",
        )

    def query_df(self, sql: str) -> pd.DataFrame:
        return self.client.query_df(sql)

    def insert_df(self, table: str, df: pd.DataFrame) -> None:
        if df is None or df.empty:
            return
        self.client.insert_df(f"{self.database}.{table}", df)

    def leer_override_forecast(self, process_date: date) -> pd.DataFrame:
        sql = f"""
        SELECT sku_id, ubicacion, mes, forecast_override, motivo, responsable
        FROM {self.database}.logistica_vw_override_forecast_activo
        WHERE toDate('{process_date.isoformat()}') BETWEEN vigente_desde AND vigente_hasta
        """
        df = self.query_df(sql)
        if df.empty:
            return pd.DataFrame(columns=["sku_id", "ubicacion", "mes", "forecast_override", "motivo", "responsable"])
        df["sku_id"] = df["sku_id"].map(canonical_sku)
        df["ubicacion"] = df["ubicacion"].map(canonical_location)
        df["mes"] = pd.to_datetime(df["mes"]).dt.date
        return df

    def leer_override_clasificacion(self, process_date: date) -> pd.DataFrame:
        sql = f"""
        SELECT sku_id, ubicacion, abc_override, xyz_override, motivo, responsable
        FROM {self.database}.logistica_vw_override_clasificacion_activo
        WHERE toDate('{process_date.isoformat()}') BETWEEN vigente_desde AND vigente_hasta
        """
        df = self.query_df(sql)
        if df.empty:
            return pd.DataFrame(columns=["sku_id", "ubicacion", "abc_override", "xyz_override", "motivo", "responsable"])
        df["sku_id"] = df["sku_id"].map(canonical_sku)
        df["ubicacion"] = df["ubicacion"].map(canonical_location)
        return df

    def leer_automatizacion(self) -> pd.DataFrame:
        sql = f"""
        SELECT sku_id, ubicacion, automatizar
        FROM {self.database}.logistica_automatizacion_sku_sucursal
        """
        df = self.query_df(sql)
        if df.empty:
            return pd.DataFrame(columns=["sku_id", "ubicacion", "automatizar"])
        df["sku_id"] = df["sku_id"].map(canonical_sku)
        df["ubicacion"] = df["ubicacion"].map(canonical_location)
        df["automatizar"] = df["automatizar"].astype(int)
        return df

    def leer_homologacion(self) -> pd.DataFrame:
        sql = f"""
        SELECT sku_id_importado, sku_id_nacional, factor_conversion, usar_analitico, usar_operacional
        FROM {self.database}.logistica_vw_homologacion_activa
        """
        df = self.query_df(sql)
        if df.empty:
            return pd.DataFrame(columns=["sku_id_importado", "sku_id_nacional", "factor_conversion", "usar_analitico", "usar_operacional"])
        df["sku_id_importado"] = df["sku_id_importado"].map(canonical_sku)
        df["sku_id_nacional"] = df["sku_id_nacional"].map(canonical_sku)
        return df

    def leer_proveedores(self) -> pd.DataFrame:
        sql = f"""
        SELECT sku_id, proveedor, ubicacion, lead_time_dias, costo_unitario_clp, prioridad
        FROM {self.database}.logistica_politica_proveedores
        """
        df = self.query_df(sql)
        if df.empty:
            return pd.DataFrame(columns=["sku_id", "proveedor", "ubicacion", "lead_time_dias", "costo_unitario_clp", "prioridad"])
        df["sku_id"] = df["sku_id"].map(canonical_sku)
        df["ubicacion"] = df["ubicacion"].fillna("").map(canonical_location)
        return df

    def leer_silencio(self) -> pd.DataFrame:
        sql = f"""
        SELECT sku_id, ubicacion, last_suggested_date, last_necesidad, last_run_id
        FROM {self.database}.logistica_silencio_sugerencias
        """
        df = self.query_df(sql)
        if df.empty:
            return pd.DataFrame(columns=["sku_id", "ubicacion", "last_suggested_date", "last_necesidad", "last_run_id"])
        df["sku_id"] = df["sku_id"].map(canonical_sku)
        df["ubicacion"] = df["ubicacion"].map(canonical_location)
        df["last_suggested_date"] = pd.to_datetime(df["last_suggested_date"]).dt.date
        return df

    def guardar_carga(self, df: pd.DataFrame) -> None:
        self.insert_df("logistica_salida_carga_maryun", df)

    def guardar_auditoria_needs(self, df: pd.DataFrame) -> None:
        self.insert_df("logistica_auditoria_needs", df)

    def guardar_auditoria_plan(self, df: pd.DataFrame) -> None:
        self.insert_df("logistica_auditoria_plan", df)

    def guardar_auditoria_overrides(self, df: pd.DataFrame) -> None:
        self.insert_df("logistica_auditoria_overrides", df)

    def guardar_ejecucion(self, df: pd.DataFrame) -> None:
        self.insert_df("logistica_ejecuciones_pipeline", df)

    def actualizar_silencio(self, df_pairs: pd.DataFrame) -> None:
        # Upsert lógico con ReplacingMergeTree (updated_at más reciente).
        self.insert_df("logistica_silencio_sugerencias", df_pairs)
