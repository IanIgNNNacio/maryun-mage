"""Integracion con Supabase — backend en la nube del dashboard.

Dos usos:
  * PIPELINE: ``push_run(...)`` empuja el plan de la corrida (plan_semanal +
    plan_actual + historico_ahorro). Idempotente por run_id.
  * DASHBOARD: ``fetch_*`` (lectura) y ``update_*`` (escritura interactiva:
    estado, cantidad_ajustada, nota).

Degradacion elegante: si Supabase no esta configurado o el paquete no esta
instalado, las funciones de push se omiten (log) y las de lectura devuelven
DataFrame vacio — el pipeline local sigue funcionando sin Supabase.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

import pandas as pd

from app.config.settings import get_settings
from app.utils.logging import get_logger

_log = get_logger("export.supabase")

# Mapeo carga_maryun -> tabla plan_semanal
_PLAN_SEMANAL_COLS = {
    "run_id": "run_id", "fecha_generacion": "fecha_generacion", "sku_id": "sku_id",
    "nombre": "nombre", "variante": "variante", "origen": "origen", "destino": "destino",
    "capa": "capa", "cantidad": "cantidad_sugerida", "proveedor": "proveedor",
    "costo_unitario_clp": "costo_unitario_clp", "clase_abc_xyz": "clase_abc_xyz",
    "motivo": "motivo",
}
# Mapeo powerbi_view (MAYUSCULAS) -> tabla plan_actual
_PLAN_ACTUAL_COLS = {
    "RUN_ID": "run_id", "FECHA_GENERACION": "fecha_generacion", "SUCURSAL": "sucursal",
    "NOMBRE": "nombre", "MARCA": "marca", "FAMILIA": "familia", "TIPO": "tipo",
    "DESCRIPCION": "descripcion", "SKU": "sku", "SKU_CORRECTO": "sku_correcto",
    "CLASE_ABC_XYZ": "clase_abc_xyz", "STOCK": "stock", "DEMANDA_MES_ACTUAL": "demanda_mes_actual",
    "CANT_REQ": "cant_req", "CD_STGO": "cd_stgo", "CD_SUR": "cd_sur", "PROVEEDOR": "proveedor",
    "COSTO_UNITARIO_CLP": "costo_unitario_clp", "VALOR_STOCK_CLP": "valor_stock_clp",
    "COBERTURA_MESES": "cobertura_meses", "DESCUBIERTO": "descubierto",
}


def is_configured() -> bool:
    s = get_settings().supabase
    return bool(s.enabled and s.url and s.service_key)


@lru_cache(maxsize=1)
def _client():
    """Cliente Supabase (service_role). None si no esta configurado/instalado."""
    s = get_settings().supabase
    if not (s.enabled and s.url and s.service_key):
        _log.info("supabase.not_configured")
        return None
    try:
        from supabase import create_client
        return create_client(s.url, s.service_key)
    except Exception as exc:
        _log.warning("supabase.client_init_failed", error=str(exc))
        return None


def reset_client_cache() -> None:
    _client.cache_clear()


def _records(df: pd.DataFrame, colmap: dict[str, str]) -> list[dict[str, Any]]:
    """Selecciona+renombra columnas y convierte NaN -> None (JSON-safe)."""
    cols = [c for c in colmap if c in df.columns]
    out = df[cols].rename(columns={c: colmap[c] for c in cols}).copy()
    if "fecha_generacion" in out.columns:
        out["fecha_generacion"] = pd.to_datetime(
            out["fecha_generacion"], errors="coerce"
        ).dt.strftime("%Y-%m-%d")
    out = out.where(pd.notna(out), None)
    return out.to_dict(orient="records")


def _insert_chunked(client, table: str, records: list[dict], chunk: int = 500) -> int:
    n = 0
    for i in range(0, len(records), chunk):
        client.table(table).insert(records[i:i + chunk]).execute()
        n += len(records[i:i + chunk])
    return n


# ---------------------------------------------------------------------------
# PUSH (pipeline)
# ---------------------------------------------------------------------------
def push_run(
    *,
    carga_maryun: pd.DataFrame,
    powerbi_view: pd.DataFrame,
    resumen: dict,
    run_id: str,
    fecha_generacion,
) -> dict:
    """Empuja la corrida a Supabase. Idempotente por run_id (borra+reinserta)."""
    client = _client()
    if client is None:
        _log.info("supabase.push_skipped", reason="no configurado")
        return {"pushed": False}

    fecha = pd.to_datetime(fecha_generacion).strftime("%Y-%m-%d")
    result = {"pushed": True, "run_id": run_id}

    # --- plan_semanal (operacional + interactivo) ---
    if carga_maryun is not None and not carga_maryun.empty:
        # Idempotencia: limpiar filas previas de este run_id antes de insertar.
        client.table("plan_semanal").delete().eq("run_id", run_id).execute()
        # Consolidar por la llave unica para evitar choques con uq_plan_semanal_accion
        agg = (
            carga_maryun.groupby(
                ["run_id", "fecha_generacion", "sku_id", "nombre", "variante",
                 "origen", "destino", "capa", "proveedor", "costo_unitario_clp",
                 "clase_abc_xyz", "motivo"],
                dropna=False, as_index=False,
            )["cantidad"].sum()
        )
        recs = _records(agg, _PLAN_SEMANAL_COLS)
        result["plan_semanal_rows"] = _insert_chunked(client, "plan_semanal", recs)

    # --- plan_actual (analitica) ---
    if powerbi_view is not None and not powerbi_view.empty:
        client.table("plan_actual").delete().eq("run_id", run_id).execute()
        recs = _records(powerbi_view, _PLAN_ACTUAL_COLS)
        result["plan_actual_rows"] = _insert_chunked(client, "plan_actual", recs)

    # --- historico_ahorro (KPI, upsert por run_id) ---
    ahorro = float(carga_maryun["ahorro_clp"].sum()) if (
        carga_maryun is not None and "ahorro_clp" in carga_maryun.columns
    ) else 0.0
    fila = {
        "run_id": run_id, "fecha_generacion": fecha,
        "unidades_total": float(resumen.get("unidades_totales", 0.0)),
        "unidades_inmovilizado": float(resumen.get("unidades_inmovilizado", 0.0)),
        "unidades_sobrestock": float(resumen.get("unidades_sobrestock", 0.0)),
        "unidades_cd": float(resumen.get("unidades_cd", 0.0)),
        "unidades_compra": float(resumen.get("unidades_compra", 0.0)),
        "unidades_descubiertas": float(resumen.get("unidades_descubiertas", 0.0)),
        "ahorro_clp": ahorro,
    }
    client.table("historico_ahorro").upsert(fila, on_conflict="run_id").execute()
    result["historico_ahorro"] = True

    _log.info("supabase.push_done", **{k: v for k, v in result.items()})
    return result


# ---------------------------------------------------------------------------
# READ (dashboard)
# ---------------------------------------------------------------------------
def _latest_run_id(client) -> str | None:
    r = client.table("plan_semanal").select("run_id").order(
        "run_id", desc=True
    ).limit(1).execute()
    return r.data[0]["run_id"] if r.data else None


def fetch_plan_vigente() -> pd.DataFrame:
    """Plan operacional del ULTIMO run (con campos interactivos)."""
    client = _client()
    if client is None:
        return pd.DataFrame()
    rid = _latest_run_id(client)
    if rid is None:
        return pd.DataFrame()
    rows: list[dict] = []
    page, size = 0, 1000
    while True:
        r = (client.table("plan_semanal").select("*").eq("run_id", rid)
             .range(page * size, page * size + size - 1).execute())
        rows.extend(r.data or [])
        if not r.data or len(r.data) < size:
            break
        page += 1
    return pd.DataFrame(rows)


def fetch_plan_actual() -> pd.DataFrame:
    """Vista analitica del ULTIMO run."""
    client = _client()
    if client is None:
        return pd.DataFrame()
    r = client.table("plan_actual").select("run_id").order("run_id", desc=True).limit(1).execute()
    if not r.data:
        return pd.DataFrame()
    rid = r.data[0]["run_id"]
    rows: list[dict] = []
    page, size = 0, 1000
    while True:
        rr = (client.table("plan_actual").select("*").eq("run_id", rid)
              .range(page * size, page * size + size - 1).execute())
        rows.extend(rr.data or [])
        if not rr.data or len(rr.data) < size:
            break
        page += 1
    return pd.DataFrame(rows)


def fetch_historico_ahorro() -> pd.DataFrame:
    client = _client()
    if client is None:
        return pd.DataFrame()
    r = client.table("historico_ahorro").select("*").order("fecha_generacion").execute()
    return pd.DataFrame(r.data or [])


# ---------------------------------------------------------------------------
# WRITE (interactividad del dashboard)
# ---------------------------------------------------------------------------
def update_accion(
    plan_id: int,
    *,
    estado: str | None = None,
    cantidad_ajustada: float | None = None,
    nota: str | None = None,
    usuario: str | None = None,
) -> bool:
    """Actualiza estado/cantidad/nota de una fila de plan_semanal + log."""
    client = _client()
    if client is None:
        return False
    from datetime import datetime, timezone
    patch: dict[str, Any] = {"actualizado_en": datetime.now(timezone.utc).isoformat()}
    if estado is not None:
        patch["estado"] = estado
    if cantidad_ajustada is not None:
        patch["cantidad_ajustada"] = float(cantidad_ajustada)
    if nota is not None:
        patch["nota"] = nota
    if usuario is not None:
        patch["actualizado_por"] = usuario
    client.table("plan_semanal").update(patch).eq("id", plan_id).execute()
    try:
        client.table("acciones_log").insert({
            "plan_id": plan_id, "accion": estado or "edito",
            "valor_nuevo": patch, "usuario": usuario,
        }).execute()
    except Exception as exc:
        _log.warning("supabase.log_failed", error=str(exc))
    return True


def bulk_update_estado(plan_ids: list[int], estado: str, usuario: str | None = None) -> int:
    """Marca varias filas con un mismo estado (aprobacion/rechazo en lote).

    Se procesa por lotes de 200 ids para no exceder el limite del filtro IN
    de PostgREST cuando el filtro abarca miles de filas.
    """
    client = _client()
    if client is None or not plan_ids:
        return 0
    from datetime import datetime, timezone
    patch = {"estado": estado, "actualizado_en": datetime.now(timezone.utc).isoformat()}
    if usuario:
        patch["actualizado_por"] = usuario
    ids = [int(i) for i in plan_ids]
    n = 0
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        client.table("plan_semanal").update(patch).in_("id", chunk).execute()
        n += len(chunk)
    return n


# ---------------------------------------------------------------------------
# READ — Panel gerencial (tablas/vistas NUEVAS y aditivas).
# Funciones nuevas; NO modifican las existentes. Degradan a DataFrame vacío si
# las tablas/vistas aún no existen (p. ej. antes de correr supabase/gerencial.sql),
# para que el dashboard nunca se rompa.
# ---------------------------------------------------------------------------
def _fetch_paginated(table: str, select: str = "*", **eq) -> pd.DataFrame:
    client = _client()
    if client is None:
        return pd.DataFrame()
    try:
        rows: list[dict] = []
        page, size = 0, 1000
        while True:
            q = client.table(table).select(select)
            for k, v in eq.items():
                q = q.eq(k, v)
            r = q.range(page * size, page * size + size - 1).execute()
            rows.extend(r.data or [])
            if not r.data or len(r.data) < size:
                break
            page += 1
        return pd.DataFrame(rows)
    except Exception as exc:
        _log.warning("supabase.fetch_failed", table=table, error=str(exc))
        return pd.DataFrame()


def fetch_ventas_producto() -> pd.DataFrame:
    """Agregado de venta histórica por producto (vista v_ventas_producto)."""
    return _fetch_paginated("v_ventas_producto")


def fetch_productos_dim() -> pd.DataFrame:
    """Catálogo de productos: nombre, variante, color, talla, critico, etc."""
    return _fetch_paginated("productos_dim")


def fetch_ventas_mensual(sku_id: str) -> pd.DataFrame:
    """Serie mensual nacional de venta histórica de un sku (v_ventas_mensual)."""
    return _fetch_paginated("v_ventas_mensual", sku_id=str(sku_id))


def fetch_ultima_venta() -> pd.DataFrame:
    """Última fecha de venta por (sku_id, ubicacion) — tabla ultima_venta."""
    return _fetch_paginated("ultima_venta")


def fetch_margen_producto() -> pd.DataFrame:
    """Margen agregado por producto — tabla margen_producto."""
    return _fetch_paginated("margen_producto")


def fetch_inmovilizado_resumen() -> pd.DataFrame:
    """Inmovilizado/sobrestock CLP por sucursal×familia — vista v_inmovilizado_resumen."""
    return _fetch_paginated("v_inmovilizado_resumen")


def fetch_mape_categoria() -> pd.DataFrame:
    """Error de pronóstico (WAPE) por categoría — vista v_mape_categoria."""
    return _fetch_paginated("v_mape_categoria")


def fetch_mape_producto() -> pd.DataFrame:
    """Error de pronóstico (WAPE) por producto — vista v_mape_producto."""
    return _fetch_paginated("v_mape_producto")


def fetch_margen_categoria() -> pd.DataFrame:
    """Margen por categoría — vista v_margen_categoria."""
    return _fetch_paginated("v_margen_categoria")
