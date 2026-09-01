from mage_ai.io.config import ConfigFileLoader
import clickhouse_connect
import pandas as pd

if 'data_loader' not in globals():
    from mage_ai.data_preparation.decorators import data_loader
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test

TABLE = 'logistica_v2.mysis_v2_alertas_silencio'
CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

COLUMNS = [
    "run_id",
    "hash_clave",
    "sku2",
    "sucursal_destino",
    "accion",
    "accion_original",
    "cantidad",
    "fecha_corte",
    "fecha_alerta",
    "valida_hasta",
    "estado",
    "sucursal_origen",
    "sku_original",
]


@data_loader
def load_data(*args, **kwargs):
    """Carga los silencios de EJECUCION vigentes (estado activa, no vencidos).

    El anti-join downstream es por hash_clave; como hash_clave embebe run_id,
    una run_id distinta nunca matchea estos silencios (solo bloquea la
    re-ejecucion de la MISMA run_id).
    """
    client = _client()
    query = f"""
        SELECT
            run_id, hash_clave, sku2, sucursal_destino, accion, accion_original,
            cantidad, fecha_corte, fecha_alerta, valida_hasta, estado,
            sucursal_origen, sku_original
        FROM {TABLE}
        WHERE lower(estado) = 'activa'
          AND valida_hasta >= now()
    """
    result = client.query(query)
    rows = getattr(result, 'result_rows', None)
    if rows is None:
        rows = getattr(result, 'result_set', [])

    df = pd.DataFrame(list(rows), columns=COLUMNS)
    df.columns = [str(c) for c in df.columns]
    return df


@test
def test_output(output, *args) -> None:
    assert output is not None, 'The output is undefined'
    assert isinstance(output, pd.DataFrame), 'Output debe ser un DataFrame'
    for col in ["hash_clave", "run_id", "estado", "valida_hasta"]:
        assert col in output.columns, f'Falta columna esperada: {col}'


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
