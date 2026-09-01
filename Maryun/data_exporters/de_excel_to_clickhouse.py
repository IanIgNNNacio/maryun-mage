# data_exporter_sku_proveedores.py
import clickhouse_connect
import pandas as pd

if 'data_exporter' not in globals():
    from mage_ai.data_preparation.decorators import data_exporter
from mage_ai.io.config import ConfigFileLoader

SKU_PROVEEDORES_TABLE = 'dwh.sku_proveedores'
CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

# Columnas en el mismo orden de la tabla ClickHouse (sin created_at/updated_at: usan DEFAULT)
SKU_PROV_INSERT_COLS = [
    'sku',
    'proveedor',
    'rut_proveedor',
    'costo',
    'divisa',
    'fuente',
    'estado',
    'categoria',
]

DECIMAL_COLS = {'costo'}
UINT8_COLS = {'estado'}
STRING_COLS = {'sku', 'proveedor', 'rut_proveedor', 'divisa', 'fuente', 'categoria'}


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


def _prepare_for_insert(df: pd.DataFrame) -> pd.DataFrame:
    """
    Asegura columnas requeridas, tipos y orden antes de insertar.
    """
    df = df.copy()

    # asegurar columnas
    for c in SKU_PROV_INSERT_COLS:
        if c not in df.columns:
            df[c] = pd.NA

    # orden exacto
    df = df[SKU_PROV_INSERT_COLS]

    # decimales
    for c in DECIMAL_COLS.intersection(df.columns):
        s = pd.to_numeric(df[c], errors='coerce').fillna(0)
        df[c] = s.astype('float64')

    # uint8
    for c in UINT8_COLS.intersection(df.columns):
        s = pd.to_numeric(df[c], errors='coerce').fillna(1)
        df[c] = s.astype('int64').clip(lower=0, upper=1).astype('uint8')

    # strings
    for c in STRING_COLS.intersection(df.columns):
        df[c] = df[c].astype('string').fillna('')

    # Limpieza mínima
    df['sku'] = df['sku'].astype('string').str.strip().fillna('')
    df['rut_proveedor'] = df['rut_proveedor'].astype('string').str.strip().fillna('')

    # Filtrar filas inválidas (evitar basura)
    df = df[(df['sku'] != '') & (df['rut_proveedor'] != '')].copy()

    return df


def _insert_rows(client, chunk: pd.DataFrame):
    chunk = chunk.copy()

    # NA -> None
    chunk_py = chunk.where(pd.notna(chunk), None)

    rows = [tuple(chunk_py.loc[i, SKU_PROV_INSERT_COLS]) for i in chunk_py.index]
    client.insert(SKU_PROVEEDORES_TABLE, rows, column_names=SKU_PROV_INSERT_COLS)


@data_exporter
def export_sku_proveedores(data: pd.DataFrame, *args, **kwargs):
    """
    Inserta maestro SKU-Proveedor en ClickHouse.

    Espera columnas:
      sku, proveedor, rut_proveedor, costo, divisa
    Y opcionalmente:
      fuente (default "mage"), estado (default 1)
    """
    if data is None or data.empty:
        print('[sku_proveedores] No hay filas para procesar.')
        return pd.DataFrame([{'rows_total': 0, 'rows_inserted': 0}])

    df = data.copy()

    # defaults si no vienen
    if 'fuente' not in df.columns:
        df['fuente'] = 'mage'
    if 'estado' not in df.columns:
        df['estado'] = 1

    df_insert = _prepare_for_insert(df)
    rows_total = len(df_insert)

    if rows_total == 0:
        print('[sku_proveedores] No quedaron filas válidas tras preparar.')
        return pd.DataFrame([{'rows_total': 0, 'rows_inserted': 0}])

    client = _client()

    chunk_size = int(kwargs.get('chunk_size') or 10000)
    rows_inserted = 0
    inserted_chunks = 0
    rows_sent = 0

    for i in range(0, rows_total, chunk_size):
        chunk = df_insert.iloc[i:i + chunk_size].copy()
        if chunk.empty:
            continue

        _insert_rows(client, chunk)
        rows_inserted += len(chunk)
        inserted_chunks += 1
        rows_sent += len(chunk)

        print(
            f'[sku_proveedores] Chunk {inserted_chunks}: '
            f'enviados {rows_sent}, insertados {rows_inserted}'
        )

    print(
        f'[sku_proveedores] Resumen final -> total filas: {rows_total}, insertadas: {rows_inserted}'
    )

    return pd.DataFrame([{
        'rows_total': rows_total,
        'rows_inserted': rows_inserted,
    }])
