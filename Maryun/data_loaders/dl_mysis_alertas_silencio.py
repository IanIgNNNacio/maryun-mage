from mage_ai.io.config import ConfigFileLoader
import clickhouse_connect
import pandas as pd

if 'data_loader' not in globals():
    from mage_ai.data_preparation.decorators import data_loader
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test

TABLE = 'dwh.alertas_silencio'
CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'


@data_loader
def load_data(*args, **kwargs):
    """
    Carga los silencios activos desde ClickHouse.
    """

    client = _client()
    query = """
        SELECT 
            hash_clave,
            sku2,
            sucursal_destino,
            accion,
            accion_original,
            cantidad,
            fecha_corte,
            fecha_alerta,
            valida_hasta,
            estado,
            sucursal_origen,
            sku_original
        FROM dwh.alertas_silencio
        WHERE lower(estado) = 'activa'
          AND valida_hasta >= now()
    """

    result = client.query(query)
    rows = getattr(result, 'result_rows', None)
    if rows is None:
        # fallback por si la versión usa otro atributo
        rows = getattr(result, 'result_set', [])

    columns = [
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

    df = pd.DataFrame(list(rows), columns=columns)
    df.columns = [str(c) for c in df.columns]
    return df


@test
def test_output(output, *args) -> None:
    """
    Test del output del bloque.
    """
    assert output is not None, 'The output is undefined'
    assert isinstance(output, pd.DataFrame), 'Output debe ser un DataFrame'
    assert len(output.columns) > 0, 'El DataFrame no tiene columnas'
    # opcional: revisar algunas columnas esperadas
    for col in [
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
    ]:
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