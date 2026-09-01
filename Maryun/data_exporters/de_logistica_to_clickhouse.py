import datetime as dt
from typing import Any, Dict, List

import clickhouse_connect
from mage_ai.io.config import ConfigFileLoader

if 'data_exporter' not in globals():
    from mage_ai.data_preparation.decorators import data_exporter

CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'
CH_DB = 'logistica_v2'

# ── Mapeo coleccion Mongo -> tabla ClickHouse ──────────────────────────────
# kind por columna:
#   str      String              (None/'' -> '')
#   nstr     Nullable(String)    (None/'' -> None)
#   float    Float64             (None -> 0.0)
#   nfloat   Nullable(Float64)   (None -> None)
#   int      Int/UInt entero     (None -> 0)
#   uint8    UInt8 desde bool    (true -> 1, false -> 0)
#   date_vd  Date  (None -> 1970-01-01)  vigente_desde / mes / fechas requeridas
#   date_vh  Date  (None -> 2100-12-31)  vigente_hasta requerido
#   ndate    Nullable(Date)      (None -> None)
#   updated  DateTime  = modificadoEn ?? creadoEn ?? now()
SPECS: Dict[str, Dict[str, Any]] = {
    'logistica_override_forecast': {
        'mongo': 'logistica_overrides_forecast',
        'cols': [
            ('sku_id', 'skuId', 'str'),
            ('ubicacion', 'ubicacion', 'str'),
            ('mes', 'mes', 'date_vd'),
            ('forecast_override', 'forecastOverride', 'float'),
            ('motivo', 'motivo', 'str'),
            ('responsable', 'responsable', 'str'),
            ('vigente_desde', 'vigenteDesde', 'date_vd'),
            ('vigente_hasta', 'vigenteHasta', 'date_vh'),
            ('activo', 'activo', 'uint8'),
            ('updated_at', None, 'updated'),
        ],
    },
    'logistica_override_clasificacion': {
        'mongo': 'logistica_overrides_clasificacion',
        'cols': [
            ('sku_id', 'skuId', 'str'),
            ('ubicacion', 'ubicacion', 'str'),
            ('abc_override', 'abcOverride', 'nstr'),
            ('xyz_override', 'xyzOverride', 'nstr'),
            ('motivo', 'motivo', 'str'),
            ('responsable', 'responsable', 'str'),
            ('vigente_desde', 'vigenteDesde', 'date_vd'),
            ('vigente_hasta', 'vigenteHasta', 'date_vh'),
            ('activo', 'activo', 'uint8'),
            ('updated_at', None, 'updated'),
        ],
    },
    'logistica_automatizacion_sku_sucursal': {
        'mongo': 'logistica_automatizacion_sku_sucursal',
        'cols': [
            ('sku_id', 'skuId', 'str'),
            ('ubicacion', 'ubicacion', 'str'),
            ('automatizar', 'automatizar', 'uint8'),
            ('fuente', 'fuente', 'str'),
            ('updated_at', None, 'updated'),
        ],
    },
    'logistica_homologacion_productos': {
        'mongo': 'logistica_homologacion_productos',
        'cols': [
            ('sku_id_importado', 'skuIdImportado', 'str'),
            ('sku_id_nacional', 'skuIdNacional', 'str'),
            ('factor_conversion', 'factorConversion', 'float'),
            ('usar_analitico', 'usarAnalitico', 'uint8'),
            ('usar_operacional', 'usarOperacional', 'uint8'),
            ('vigente_desde', 'vigenteDesde', 'ndate'),
            ('vigente_hasta', 'vigenteHasta', 'ndate'),
            ('responsable', 'responsable', 'str'),
            ('updated_at', None, 'updated'),
        ],
    },
    'logistica_politica_proveedores': {
        'mongo': 'logistica_politica_proveedores',
        'cols': [
            ('sku_id', 'skuId', 'str'),
            ('proveedor', 'proveedor', 'str'),
            ('ubicacion', 'ubicacion', 'nstr'),
            ('lead_time_dias', 'leadTimeDias', 'int'),
            ('moq', 'moq', 'float'),
            ('multiplo_compra', 'multiploCompra', 'float'),
            ('incoterm', 'incoterm', 'str'),
            ('procedencia', 'procedencia', 'str'),
            ('costo_unitario_clp', 'costoUnitarioClp', 'float'),
            ('prioridad', 'prioridad', 'int'),
            ('updated_at', None, 'updated'),
        ],
    },
    'logistica_politica_costos_transporte': {
        'mongo': 'logistica_politica_costos_transporte',
        'cols': [
            ('origen', 'origen', 'str'),
            ('destino', 'destino', 'str'),
            ('costo_clp_por_unidad', 'costoClpPorUnidad', 'float'),
            ('updated_at', None, 'updated'),
        ],
    },
    'logistica_politica_distancias': {
        'mongo': 'logistica_politica_distancias',
        'cols': [
            ('origen', 'origen', 'str'),
            ('destino', 'destino', 'str'),
            ('km', 'km', 'float'),
            ('updated_at', None, 'updated'),
        ],
    },
    'logistica_politica_prioridad_cd': {
        'mongo': 'logistica_politica_prioridad_cd',
        'cols': [
            ('ubicacion', 'ubicacion', 'str'),
            ('cd', 'cd', 'str'),
            ('prioridad', 'prioridad', 'int'),
            ('updated_at', None, 'updated'),
        ],
    },
    'logistica_politica_reglas_sku_sucursal': {
        'mongo': 'logistica_politica_reglas_sku_sucursal',
        'cols': [
            ('sku_id', 'skuId', 'str'),
            ('ubicacion', 'ubicacion', 'str'),
            ('bloqueado', 'bloqueado', 'uint8'),
            ('stock_minimo', 'stockMinimo', 'nfloat'),
            ('stock_maximo', 'stockMaximo', 'nfloat'),
            ('solo_desde_cd', 'soloDesdeCd', 'nstr'),
            ('nota', 'nota', 'str'),
            ('updated_at', None, 'updated'),
        ],
    },
    'logistica_silencio_sugerencias': {
        'mongo': 'logistica_silencio_sugerencias',
        'cols': [
            ('sku_id', 'skuId', 'str'),
            ('ubicacion', 'ubicacion', 'str'),
            ('last_suggested_date', 'lastSuggestedDate', 'date_vd'),
            ('last_necesidad', 'lastNecesidad', 'float'),
            ('last_run_id', 'lastRunId', 'str'),
            ('updated_at', None, 'updated'),
        ],
    },
}


def _client():
    cfg = ConfigFileLoader(CONFIG_PATH, PROFILE)
    use_https = str(cfg['CLICKHOUSE_INTERFACE']).lower() == 'https'
    return clickhouse_connect.get_client(
        host=cfg['CLICKHOUSE_HOST'],
        port=int(cfg['CLICKHOUSE_PORT']),
        username=cfg['CLICKHOUSE_USERNAME'],
        password=cfg['CLICKHOUSE_PASSWORD'],
        database=cfg['CLICKHOUSE_DATABASE'],
        secure=use_https,
    )


def _to_str(v) -> str:
    return '' if v is None else str(v)


def _to_nstr(v):
    if v is None or (isinstance(v, str) and v.strip() == ''):
        return None
    return str(v)


def _to_float(v) -> float:
    if v is None or v == '':
        return 0.0
    try:
        return float(v)
    except Exception:
        return 0.0


def _to_nfloat(v):
    if v is None or v == '':
        return None
    try:
        return float(v)
    except Exception:
        return None


def _to_int(v) -> int:
    if v is None or v == '':
        return 0
    try:
        return int(float(v))
    except Exception:
        return 0


def _to_uint8(v) -> int:
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, str):
        return 1 if v.strip().lower() in ('1', 'true', 't', 'yes') else 0
    return 1 if _to_int(v) == 1 else 0


def _to_date(v, default):
    if v is None or v == '':
        return default
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    try:
        return dt.datetime.fromisoformat(str(v)[:19]).date()
    except Exception:
        return default


def _to_dt(v):
    if v is None or v == '':
        return None
    if isinstance(v, dt.datetime):
        return v.replace(tzinfo=None)
    if isinstance(v, dt.date):
        return dt.datetime(v.year, v.month, v.day)
    try:
        return dt.datetime.fromisoformat(str(v)[:19])
    except Exception:
        return None


_DATE_MIN = dt.date(1970, 1, 1)
_DATE_MAX = dt.date(2100, 12, 31)


def _convert(kind, raw, doc):
    if kind == 'str':
        return _to_str(raw)
    if kind == 'nstr':
        return _to_nstr(raw)
    if kind == 'float':
        return _to_float(raw)
    if kind == 'nfloat':
        return _to_nfloat(raw)
    if kind == 'int':
        return _to_int(raw)
    if kind == 'uint8':
        return _to_uint8(raw)
    if kind == 'date_vd':
        return _to_date(raw, _DATE_MIN)
    if kind == 'date_vh':
        return _to_date(raw, _DATE_MAX)
    if kind == 'ndate':
        return _to_date(raw, None)
    if kind == 'updated':
        return _to_dt(doc.get('modificadoEn')) or _to_dt(doc.get('creadoEn')) \
            or dt.datetime.utcnow().replace(microsecond=0)
    return _to_str(raw)


def _build_rows(docs: List[Dict[str, Any]], cols):
    rows = []
    for d in docs:
        rows.append(tuple(_convert(kind, d.get(mfield), d) for _, mfield, kind in cols))
    return rows


@data_exporter
def export_data(data, *args, **kwargs):
    """TRUNCATE + INSERT completo por tabla: ClickHouse queda identico a Mongo.

    - Existe en Mongo -> existe en ClickHouse tras la corrida.
    - Eliminado en Mongo -> desaparece (truncate borra todo y solo se reinsertan
      los docs actuales).
    - Editado en Mongo -> se reinserta con el valor actual.
    """
    if not isinstance(data, dict):
        raise ValueError('Se esperaba un dict desde el data_loader')

    client = _client()
    summary: Dict[str, int] = {}

    for table, spec in SPECS.items():
        coll = spec['mongo']
        cols = spec['cols']
        col_names = [c[0] for c in cols]
        docs = data.get(coll, []) or []
        full = f'{CH_DB}.{table}'

        client.command(f'TRUNCATE TABLE {full}')

        rows = _build_rows(docs, cols)
        if rows:
            client.insert(full, rows, column_names=col_names)

        # Colapsa cualquier colision de clave dentro del snapshot (ReplacingMergeTree)
        client.command(f'OPTIMIZE TABLE {full} FINAL')

        summary[table] = len(rows)
        print(f'{table}: truncado + {len(rows)} filas insertadas (Mongo: {coll})')

    print(f'Total tablas sincronizadas: {len(summary)}')
    return summary
