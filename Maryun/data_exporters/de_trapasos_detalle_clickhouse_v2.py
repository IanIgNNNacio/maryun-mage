# data_exporter_traspasos_detalle.py
import clickhouse_connect
import pandas as pd

if 'data_exporter' not in globals():
    from mage_ai.data_preparation.decorators import data_exporter
from mage_ai.io.config import ConfigFileLoader

# --- Configuración base ---

TRASPASOS_DETALLE_TABLE = 'traspasos_detalle'  # o 'dwh.traspasos_detalle'
CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

# Columnas a insertar (mismo orden que la tabla ClickHouse)
TRASPASOS_INSERT_COLS = [
    'id',
    'sucursal_origen',
    'sucursal_destino',
    'sku',
    'accion',
    'cantidad',
]

DECIMAL_COLS = {'cantidad'}
STRING_COLS = {'id', 'sucursal_origen', 'sucursal_destino', 'sku', 'accion'}


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
    Asegura columnas requeridas, tipos y orden de columnas
    antes de insertar en ClickHouse.
    """
    df = df.copy()

    # Asegurar columnas requeridas
    for c in TRASPASOS_INSERT_COLS:
        if c not in df.columns:
            df[c] = pd.NA

    # Orden exacto
    df = df[TRASPASOS_INSERT_COLS]

    # Decimales (cantidad)
    for c in DECIMAL_COLS.intersection(df.columns):
        s = pd.to_numeric(df[c], errors='coerce').fillna(0)
        df[c] = s.astype('float64')

    # Strings
    for c in STRING_COLS.intersection(df.columns):
        df[c] = df[c].astype('string').fillna('')

    return df


def _insert_rows(client, chunk: pd.DataFrame):
    """
    Inserta las filas del chunk en ClickHouse usando insert nativo.
    """
    chunk = chunk.copy()

    # Reemplazar NA por None
    chunk_py = chunk.where(pd.notna(chunk), None)

    # Construir filas en el orden exacto
    rows = [tuple(chunk_py.loc[i, TRASPASOS_INSERT_COLS]) for i in chunk_py.index]

    # Insert nativo
    client.insert(TRASPASOS_DETALLE_TABLE, rows, column_names=TRASPASOS_INSERT_COLS)


@data_exporter
def export_traspasos_detalle(data: pd.DataFrame, *args, **kwargs):
    """
    Recibe el DF AGRUPADO de traspasos (origen/destino)
    y lo "desarma" a detalle fila a fila para guardarlo en ClickHouse.

    Espera columnas:
      - sucursal_origen (str)
      - sucursal_destino (str)
      - sku_original (list[str])
      - accion (list[str])
      - cantidad (list[num])
      - comentario (str)  -> se usará como ID
    """
    if data is None or data.empty:
        print('[traspasos_detalle] No hay filas para procesar.')
        return pd.DataFrame([{
            'rows_total': 0,
            'rows_inserted': 0,
        }])

    df = data.copy()

    # Flatten: construir detalle fila a fila
    rows_out = []

    for _, row in df.iterrows():
        suc_origen = str(row['sucursal_origen'])
        suc_destino = str(row['sucursal_destino'])
        comentario = str(row.get('comentario', ''))

        skus = row['sku_original']
        acciones = row['accion']
        cantidades = row['cantidad']

        # Seguridad: si algo viene como NaN, lo tratamos como lista vacía
        if not isinstance(skus, (list, tuple)):
            continue
        if not isinstance(acciones, (list, tuple)):
            continue
        if not isinstance(cantidades, (list, tuple)):
            continue

        for sku, acc, qty in zip(skus, acciones, cantidades):
            rows_out.append({
                'id': comentario,             # ID = comentario del df
                'sucursal_origen': suc_origen,
                'sucursal_destino': suc_destino,
                'sku': str(sku),
                'accion': str(acc),
                'cantidad': qty,
            })

    if not rows_out:
        print('[traspasos_detalle] Después de aplanar no quedaron filas.')
        return pd.DataFrame([{
            'rows_total': 0,
            'rows_inserted': 0,
        }])

    df_detalle = pd.DataFrame(rows_out)
    rows_total = len(df_detalle)

    # Preparar tipos/orden
    df_insert = _prepare_for_insert(df_detalle)

    client = _client()

    chunk_size = int(kwargs.get('chunk_size') or 10000)
    rows_inserted = 0
    inserted_chunks = 0
    rows_sent = 0

    for i in range(0, rows_total, chunk_size):
        chunk = df_insert.iloc[i:i+chunk_size].copy()
        if chunk.empty:
            continue

        _insert_rows(client, chunk)
        rows_inserted += len(chunk)
        inserted_chunks += 1
        rows_sent += len(chunk)

        print(
            f'[traspasos_detalle] Chunk {inserted_chunks}: '
            f'enviados {rows_sent}, insertados {rows_inserted}'
        )

    print(
        f'[traspasos_detalle] Resumen final -> '
        f'total filas detalle: {rows_total}, '
        f'insertadas: {rows_inserted}'
    )

    resumen_df = pd.DataFrame([{
        'rows_total': rows_total,
        'rows_inserted': rows_inserted,
    }])

    return resumen_df
