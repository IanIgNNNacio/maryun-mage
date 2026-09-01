from __future__ import annotations

import clickhouse_connect  # type: ignore
from mage_ai.io.config import ConfigFileLoader

CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'
# logistica_v2 = CLICKHOUSE_LOG_DATABASE (la DB de outputs logisticos). Antes
# apuntaba a 'logistica' (no existe en el io_config). modelos_trimestral lee
# estas tablas desde logistica_v2.
TABLA_DEMANDA = 'logistica_v2.logistica_demanda_estandarizada_mensual'
TABLA_CLASIFICACION_BASE = 'logistica_v2.logistica_clasificacion_base_mensual'

if 'data_exporter' not in globals():
    from mage_ai.data_preparation.decorators import data_exporter
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test


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


@data_exporter
def de_refresh_demanda_mensual(**kwargs):
    """
    Refresca demanda estandarizada mensual desde dwh.ventas_mysis.
    Regla de negocio:
    - Si pu > 0: qty_filtrada = qty
    - Si pu <= 0: qty_filtrada = -qty
    - Base fija desde 2024-01-01
    - Considera meses cerrados: hasta inicio del mes actual (exclusivo)
    """
    c = _client()

    c.command(f"""
    CREATE TABLE IF NOT EXISTS {TABLA_DEMANDA}
    (
      sku_2_0 String,
      sucursal String,
      mes Date,
      demanda_neta Float64,
      fecha_carga DateTime DEFAULT now()
    )
    ENGINE = MergeTree
    PARTITION BY toYYYYMM(mes)
    ORDER BY (sku_2_0, sucursal, mes)
    """)

    c.command(f"""
    CREATE TABLE IF NOT EXISTS {TABLA_CLASIFICACION_BASE}
    (
      sku_3_0 String,
      sucursal String,
      nombre String,
      mes Date,
      demanda_neta Float64,
      margen_total Float64,
      fecha_carga DateTime DEFAULT now()
    )
    ENGINE = MergeTree
    PARTITION BY toYYYYMM(mes)
    ORDER BY (sku_3_0, sucursal, mes)
    """)

    c.command(f"""
    TRUNCATE TABLE {TABLA_DEMANDA}
    """)

    c.command(f"""
    TRUNCATE TABLE {TABLA_CLASIFICACION_BASE}
    """)

    c.command(f"""
    INSERT INTO {TABLA_DEMANDA}
    SELECT
      concat(toString(sku), upper(trimBoth(sucursal))) AS sku_2_0,
      upper(trimBoth(sucursal)) AS sucursal,
      toStartOfMonth(toDate(entregado)) AS mes,
      sum(if(toFloat64(pu) > 0, toFloat64(qty), -toFloat64(qty))) AS demanda_neta,
      now() AS fecha_carga
    FROM dwh.ventas_mysis_2 FINAL
    WHERE entregado >= toDate('2024-01-01')
      AND entregado < toStartOfMonth(toDate(now('America/Santiago')))
      AND entregado IS NOT NULL
    GROUP BY
      sku_2_0,
      sucursal,
      mes
    """)

    c.command(f"""
    INSERT INTO {TABLA_CLASIFICACION_BASE}
    SELECT
      concat(toString(nombre), upper(trimBoth(sucursal))) AS sku_3_0,
      upper(trimBoth(sucursal)) AS sucursal,
      toString(nombre) AS nombre,
      toStartOfMonth(toDate(entregado)) AS mes,
      sum(if(toFloat64(pu) > 0, toFloat64(qty), -toFloat64(qty))) AS demanda_neta,
      sum(toFloat64(margen_final)) AS margen_total,
      now() AS fecha_carga
    FROM dwh.ventas_mysis_2 FINAL
    WHERE entregado >= toDate('2024-01-01')
      AND entregado < toStartOfMonth(toDate(now('America/Santiago')))
      AND entregado IS NOT NULL
    GROUP BY
      sku_3_0,
      sucursal,
      nombre,
      mes
    """)

    rows_demanda = c.query(f"SELECT count() AS n FROM {TABLA_DEMANDA}").result_rows[0][0]
    rows_cls = c.query(f"SELECT count() AS n FROM {TABLA_CLASIFICACION_BASE}").result_rows[0][0]
    return {
        'tabla_demanda': TABLA_DEMANDA,
        'rows_demanda': int(rows_demanda),
        'tabla_clasificacion_base': TABLA_CLASIFICACION_BASE,
        'rows_clasificacion_base': int(rows_cls),
        'status': 'ok',
    }


@test
def test_output(output, *args):
    assert output['tabla_demanda'] == TABLA_DEMANDA
    assert output['status'] == 'ok'