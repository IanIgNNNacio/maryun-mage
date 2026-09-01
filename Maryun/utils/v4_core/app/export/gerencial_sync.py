"""Sync INDEPENDIENTE para el Panel Gerencial — MariaDB (lectura) -> Supabase.

Trae a Supabase dos cosas que el dashboard en la nube no tenía:
  * Venta histórica mensual por (sku, sucursal)  -> tabla ``ventas_historicas``
  * Catálogo de productos (nombre + VARIANTE + ...) -> tabla ``productos_dim``

Es 100% ADITIVO y de SOLO LECTURA respecto al motor de abastecimiento:
  * Reutiliza ``MariaDBSource.demand()`` y ``.products()`` SIN modificarlas
    (mismo SQL read-only que usa el pipeline; no se altera ``sales.sql`` ni
    ``products.sql``).
  * Escribe únicamente a tablas NUEVAS de Supabase (no toca ``plan_*`` ni el motor).
  * NO se engancha al pipeline operativo (no se toca runner/stages). Se ejecuta
    como job aparte (``scripts/sync_gerencial.py``), idealmente semanal.

Requiere: túnel/credenciales MariaDB y Supabase habilitado (mismas que el pipeline).
"""
from __future__ import annotations

from datetime import date

import pandas as pd

# Reutilización read-only del cliente Supabase ya existente (sin modificarlo).
from app.export.supabase_sync import _client
from app.utils.logging import get_logger

_log = get_logger("export.gerencial_sync")

_PRODUCTOS_COLS = ["sku_id", "nombre", "variante", "color", "talla",
                   "familia", "marca", "tipo", "critico", "costo"]

# Margen por producto (nacional) desde reporte_ventas_completo. Mismas exclusiones
# de sucursal que sales.sql. neto = venta neta; margen_final = margen definitivo.
_MARGEN_SQL = """
SELECT v.sku                AS sku_id,
       SUM(v.qty)           AS unidades,
       SUM(v.neto)          AS venta_neto_clp,
       SUM(v.margen_final)  AS margen_clp
FROM reporte_ventas_completo v
WHERE v.entregado >= :history_start
  AND v.entregado <  :history_end
  AND v.qty > 0
  AND v.sku IS NOT NULL
  AND TRIM(v.sku) != ''
  AND v.sucursal NOT IN ('CONSUMOS INTERNOS', 'PENDIENTES', 'ADMINISTRACION',
                         'MUESTRA SIN RETORNO', 'INVENTARIO STGO', 'BORDADOS',
                         'CARDONAL')
GROUP BY v.sku;
"""


# ---------------------------------------------------------------------------
# Helpers de escritura (mismo patrón que supabase_sync, sin modificarlo)
# ---------------------------------------------------------------------------
def _to_records(df: pd.DataFrame, *, date_cols: tuple[str, ...] = ()) -> list[dict]:
    """DataFrame -> lista de dicts JSON-safe (NaN->None, fechas a 'YYYY-MM-DD')."""
    out = df.copy()
    for c in date_cols:
        if c in out.columns:
            out[c] = pd.to_datetime(out[c], errors="coerce").dt.strftime("%Y-%m-%d")
    if "critico" in out.columns:
        out["critico"] = out["critico"].map(lambda v: None if pd.isna(v) else bool(v))

    # NaN/NaT -> None de forma robusta (en columnas float, .where deja NaN).
    def _na(v):
        try:
            return None if pd.isna(v) else v
        except (TypeError, ValueError):
            return v

    return [{k: _na(v) for k, v in row.items()}
            for row in out.to_dict(orient="records")]


def _upsert_chunked(client, table: str, records: list[dict], on_conflict: str,
                    chunk: int = 500) -> int:
    n = 0
    for i in range(0, len(records), chunk):
        client.table(table).upsert(records[i:i + chunk],
                                   on_conflict=on_conflict).execute()
        n += len(records[i:i + chunk])
    return n


# ---------------------------------------------------------------------------
# EXTRACT (read-only desde MariaDB, reutilizando MariaDBSource)
# ---------------------------------------------------------------------------
def extract_ventas_historicas(process_date: date | None = None) -> pd.DataFrame:
    """Venta histórica mensual (sku_id, ubicacion, mes, demanda) desde MariaDB."""
    from app.config.engine_params import get_engine_params
    from app.extract.mariadb_source import MariaDBSource
    from app.temporal.gate import build_temporal_gate

    pdate = process_date or date.today()
    gate = build_temporal_gate(pdate, get_engine_params())
    df = MariaDBSource().demand(gate)  # read-only; no modifica nada
    cols = [c for c in ("sku_id", "ubicacion", "mes", "demanda") if c in df.columns]
    out = df[cols].copy()
    out = out.dropna(subset=["sku_id", "ubicacion", "mes"])
    return out


def extract_productos() -> pd.DataFrame:
    """Catálogo de productos (nombre, VARIANTE, color, talla, ..., critico)."""
    from app.extract.mariadb_source import MariaDBSource
    df = MariaDBSource().products()  # read-only
    keep = [c for c in _PRODUCTOS_COLS if c in df.columns]
    out = df[keep].copy()
    out = out.dropna(subset=["sku_id"]).drop_duplicates(subset=["sku_id"])
    return out.reset_index(drop=True)


# ---------------------------------------------------------------------------
# PUSH (upsert idempotente a tablas NUEVAS de Supabase)
# ---------------------------------------------------------------------------
def push_ventas_historicas(df: pd.DataFrame) -> dict:
    client = _client()
    if client is None:
        _log.info("gerencial_sync.skip", reason="supabase no configurado")
        return {"pushed": False, "reason": "no configurado"}
    if df is None or df.empty:
        return {"pushed": False, "reason": "sin datos"}
    recs = _to_records(df, date_cols=("mes",))
    n = _upsert_chunked(client, "ventas_historicas", recs, "sku_id,ubicacion,mes")
    _log.info("gerencial_sync.ventas_historicas", rows=n)
    return {"pushed": True, "ventas_historicas_rows": n}


def push_productos_dim(df: pd.DataFrame) -> dict:
    client = _client()
    if client is None:
        _log.info("gerencial_sync.skip", reason="supabase no configurado")
        return {"pushed": False, "reason": "no configurado"}
    if df is None or df.empty:
        return {"pushed": False, "reason": "sin datos"}
    recs = _to_records(df)
    n = _upsert_chunked(client, "productos_dim", recs, "sku_id")
    _log.info("gerencial_sync.productos_dim", rows=n)
    return {"pushed": True, "productos_dim_rows": n}


def extract_ultima_venta() -> pd.DataFrame:
    """Última fecha de venta por (sku_id, ubicacion) desde MariaDB."""
    from app.extract.mariadb_source import MariaDBSource
    df = MariaDBSource().ultima_venta()  # read-only
    cols = [c for c in ("sku_id", "ubicacion", "ultima_venta") if c in df.columns]
    return df[cols].copy()


def extract_margen(process_date: date | None = None) -> pd.DataFrame:
    """Margen por producto (nacional) en la misma ventana que la venta histórica."""
    from dateutil.relativedelta import relativedelta
    from app.config.engine_params import get_engine_params
    from app.connectors.mariadb import MariaDBConnector
    from app.normalize.canonical import canonical_sku
    from app.temporal.gate import build_temporal_gate

    pdate = process_date or date.today()
    gate = build_temporal_gate(pdate, get_engine_params())
    history_end = gate.last_closed_month + relativedelta(months=1)
    params = {"history_start": gate.history_start.isoformat(),
              "history_end": history_end.isoformat()}
    df = MariaDBConnector().run_query(_MARGEN_SQL, params=params)
    if df.empty:
        return df
    df["sku_id"] = df["sku_id"].map(canonical_sku)
    for c in ("unidades", "venta_neto_clp", "margen_clp"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    df = df.dropna(subset=["sku_id"]).groupby("sku_id", as_index=False)[
        ["unidades", "venta_neto_clp", "margen_clp"]].sum()
    df["margen_pct"] = (df["margen_clp"] / df["venta_neto_clp"]).where(
        df["venta_neto_clp"] > 0)
    return df


def extract_pronostico_historico(process_date: date | None = None) -> pd.DataFrame:
    """Serie de pronóstico de meses CERRADOS (para MAPE), desde el archivo oficial."""
    from app.config.engine_params import get_engine_params
    from app.config.paths import get_paths
    from app.forecast.reader import read_forecast_excel
    from app.temporal.gate import build_temporal_gate

    pdate = process_date or date.today()
    gate = build_temporal_gate(pdate, get_engine_params())
    df = read_forecast_excel(get_paths().forecast_precomputed_xlsx)  # read-only
    if df.empty:
        return df
    df = df.copy()
    df["mes"] = pd.to_datetime(df["mes"])
    cutoff = pd.Timestamp(gate.last_closed_month)
    df = df[df["mes"] <= cutoff]                       # solo meses cerrados
    df = df.rename(columns={"forecast_modelo": "pronostico"})
    df = df.groupby(["sku_id", "ubicacion", "mes"], as_index=False)["pronostico"].sum()
    return df


def push_ultima_venta(df: pd.DataFrame) -> dict:
    client = _client()
    if client is None or df is None or df.empty:
        return {"ultima_venta_rows": 0}
    recs = _to_records(df, date_cols=("ultima_venta",))
    n = _upsert_chunked(client, "ultima_venta", recs, "sku_id,ubicacion")
    _log.info("gerencial_sync.ultima_venta", rows=n)
    return {"ultima_venta_rows": n}


def push_margen_producto(df: pd.DataFrame) -> dict:
    client = _client()
    if client is None or df is None or df.empty:
        return {"margen_producto_rows": 0}
    recs = _to_records(df)
    n = _upsert_chunked(client, "margen_producto", recs, "sku_id")
    _log.info("gerencial_sync.margen_producto", rows=n)
    return {"margen_producto_rows": n}


def push_pronostico_historico(df: pd.DataFrame) -> dict:
    client = _client()
    if client is None or df is None or df.empty:
        return {"pronostico_historico_rows": 0}
    recs = _to_records(df, date_cols=("mes",))
    n = _upsert_chunked(client, "pronostico_historico", recs, "sku_id,ubicacion,mes")
    _log.info("gerencial_sync.pronostico_historico", rows=n)
    return {"pronostico_historico_rows": n}


def sync_all(process_date: date | None = None) -> dict:
    """Extrae de MariaDB + archivo de pronóstico (read-only) y puebla las tablas
    gerenciales en Supabase. Idempotente (upsert)."""
    vh = extract_ventas_historicas(process_date)
    pr = extract_productos()
    uv = extract_ultima_venta()
    mg = extract_margen(process_date)
    ph = extract_pronostico_historico(process_date)
    res = {
        "ventas_filas": len(vh), "productos_filas": len(pr),
        "ultima_venta_filas": len(uv), "margen_filas": len(mg),
        "pronostico_filas": len(ph),
    }
    res.update(push_ventas_historicas(vh))
    res.update(push_productos_dim(pr))
    res.update(push_ultima_venta(uv))
    res.update(push_margen_producto(mg))
    res.update(push_pronostico_historico(ph))
    return res
