from __future__ import annotations

import clickhouse_connect  # type: ignore
from mage_ai.io.config import ConfigFileLoader

CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

# NOTA: abastecimiento_v4 LEE de logistica_v2.* (mismo esquema donde viven los
# DDL y donde escribe la salida). La regeneracion debe publicar AHI o el
# handoff del precomputado no llega. Si 'logistica' era intencional, revertir.
TABLA_FC = 'logistica_v2.logistica_forecast_precomputado'
TABLA_CLS = 'logistica_v2.logistica_clasificacion_precomputada'
STG_FC = 'logistica_v2.logistica_stg_forecast_precomputado'
STG_CLS = 'logistica_v2.logistica_stg_clasificacion_precomputada'

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
def de_publicar_snapshots_trimestral(prev: dict = None, **kwargs):
    """
    Publica snapshots trimestrales desde staging a tablas activas.
    Flujo esperado:
    1) Un proceso estrategico externo carga STG_FC y STG_CLS.
    2) Este bloque desactiva snapshot activo y publica el nuevo.
    """
    c = _client()

    # Validaciones rapidas de staging
    n_fc = int(c.query(f"SELECT count() FROM {STG_FC}").first_item)
    n_cls = int(c.query(f"SELECT count() FROM {STG_CLS}").first_item)
    if n_fc == 0 or n_cls == 0:
        raise ValueError(
            f"Staging vacio. {STG_FC}={n_fc}, {STG_CLS}={n_cls}. "
            "Carga primero los resultados del recalculo trimestral."
        )

    # Desactivar snapshot previo
    c.command(f"ALTER TABLE {TABLA_FC} UPDATE activo = 0 WHERE activo = 1")
    c.command(f"ALTER TABLE {TABLA_CLS} UPDATE activo = 0 WHERE activo = 1")

    # Publicar forecast
    c.command(f"""
    INSERT INTO {TABLA_FC}
    (
      sku_id, ubicacion, mes, forecast_modelo, forecast_override, forecast_final,
      forecast_fue_forzado, motivo_override, responsable_override, activo,
      vigente_desde, vigente_hasta, version_modelo, fecha_snapshot, updated_at
    )
    SELECT
      sku_id,
      upper(trimBoth(ubicacion)) AS ubicacion,
      toDate(mes) AS mes,
      toFloat64(forecast_modelo) AS forecast_modelo,
      forecast_override,
      toFloat64(forecast_final) AS forecast_final,
      toUInt8(ifNull(forecast_fue_forzado, 0)) AS forecast_fue_forzado,
      motivo_override,
      responsable_override,
      1 AS activo,
      toDate(now('America/Santiago')) AS vigente_desde,
      NULL AS vigente_hasta,
      ifNull(version_modelo, 'trimestral') AS version_modelo,
      now() AS fecha_snapshot,
      now() AS updated_at
    FROM {STG_FC}
    """)

    # Publicar clasificacion
    c.command(f"""
    INSERT INTO {TABLA_CLS}
    (
      sku_3_0, sku_id, ubicacion, abc_modelo, xyz_modelo, clase_final, score_automatizacion,
      clase_automatizacion, activo, vigente_desde, vigente_hasta,
      version_modelo, fecha_snapshot, updated_at
    )
    SELECT
      sku_3_0,
      sku_id,
      upper(trimBoth(ubicacion)) AS ubicacion,
      abc_modelo,
      xyz_modelo,
      clase_final,
      score_automatizacion,
      clase_automatizacion,
      1 AS activo,
      toDate(now('America/Santiago')) AS vigente_desde,
      NULL AS vigente_hasta,
      ifNull(version_modelo, 'trimestral') AS version_modelo,
      now() AS fecha_snapshot,
      now() AS updated_at
    FROM {STG_CLS}
    """)

    out = {
        'status': 'ok',
        'staging_forecast_rows': n_fc,
        'staging_clasificacion_rows': n_cls,
        'tabla_forecast': TABLA_FC,
        'tabla_clasificacion': TABLA_CLS,
    }
    if isinstance(prev, dict):
        return {**prev, **out}
    return out


@test
def test_output(output, *args):
    assert output['status'] == 'ok'