# data_exporter_oc_detalle.py
import clickhouse_connect
import pandas as pd

if 'data_exporter' not in globals():
    from mage_ai.data_preparation.decorators import data_exporter
from mage_ai.io.config import ConfigFileLoader

# --- Configuración base ---
OC_DETALLE_TABLE = 'dwh.oc_detalle'
CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

# Mapeo sucursal -> id (destino)
SUCURSAL_TO_ID = {
    "SANTIAGO": 1,
    "PUERTO MONTT": 2,
    "CONCEPCION": 3,
    "QUELLON": 4,
    "OSORNO": 5,
    "LOS ANGELES": 6,
    "CASTRO": 7,
    "PUERTO VARAS": 8,
    "CARDONAL": 9,
    "ADMINISTRACION": 10,
    "PENDIENTES": 11,
    "CD SUR": 12,
    "CD SANTIAGO": 13,
    "MUESTRA SIN RETORNO": 14,
    "DISTRIBUCION TOTAL": 15,
    "ZONA SUR TOTAL": 16,
    "ZONA SUR AUSTRAL": 17,
    "ISLA CHILOE": 18,
    "ZONA BIO BIO": 19,
    "PROVINCIA LLANQUIHUE": 20,
    "INVENTARIO STGO": 21,
    "LOS ANGELES EXPRESS": 22,
    "CONSUMOS INTERNOS": 23,
    "BORDADOS": 24,
    "VALDIVIA": 25,
    "MARKETPLACE": 26,
}

# Columnas a insertar (mismo orden que la tabla ClickHouse)
OC_INSERT_COLS = [
    'id',
    'rut_proveedor',
    'proveedor',
    'destino',
    'destino_id',
    'sku',
    'cantidad',
    'precio',
    'comentario_payload',
]

FLOAT_COLS = {'cantidad', 'precio'}
STRING_COLS = {'id', 'rut_proveedor', 'proveedor', 'destino', 'sku', 'comentario_payload'}


def map_sucursal_to_id(nombre: str):
    if nombre is None:
        return None
    key = str(nombre).strip().upper()
    return SUCURSAL_TO_ID.get(key)


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
    Asegura columnas requeridas, tipos y orden.
    """
    df = df.copy()

    # Asegurar columnas requeridas
    for c in OC_INSERT_COLS:
        if c not in df.columns:
            df[c] = pd.NA

    # Orden exacto
    df = df[OC_INSERT_COLS]

    # Floats
    for c in FLOAT_COLS.intersection(df.columns):
        s = pd.to_numeric(df[c], errors='coerce').fillna(0)
        df[c] = s.astype('float64')

    # Strings
    for c in STRING_COLS.intersection(df.columns):
        df[c] = df[c].astype('string').fillna('')

    # destino_id (UInt16)
    df['destino_id'] = pd.to_numeric(df['destino_id'], errors='coerce').fillna(0).astype('int64')
    df['destino_id'] = df['destino_id'].clip(lower=0, upper=65535).astype('uint16')

    return df


def _insert_rows(client, chunk: pd.DataFrame):
    """
    Inserta filas en ClickHouse usando insert nativo.
    """
    chunk = chunk.copy()

    # Reemplazar NA por None
    chunk_py = chunk.where(pd.notna(chunk), None)

    rows = [tuple(chunk_py.loc[i, OC_INSERT_COLS]) for i in chunk_py.index]
    client.insert(OC_DETALLE_TABLE, rows, column_names=OC_INSERT_COLS)


@data_exporter
def export_oc_detalle(data: pd.DataFrame, *args, **kwargs):
    """
    Recibe DF del transformer de OC con columnas:
      - cabecera (dict): rut_proveedor, destino (str), comentario (id base), ...
      - detalle (list[dict]): [{sku, cantidad, precio}, ...]
      - proveedor (str)

    Flatten a 1 fila por SKU y lo inserta en ClickHouse.
    """
    if data is None or data.empty:
        print('[oc_detalle] No hay filas para procesar.')
        return pd.DataFrame([{
            'rows_total': 0,
            'rows_inserted': 0,
        }])

    df = data.copy()

    comentario_prefix = str(kwargs.get("comentario_prefix") or "detalle de OC en ID:").strip()

    rows_out = []

    for _, row in df.iterrows():
        cab = row.get('cabecera') or {}
        det = row.get('detalle') or []
        proveedor = str(row.get('proveedor', '') or '')

        if not isinstance(cab, dict):
            continue
        if not isinstance(det, (list, tuple)) or len(det) == 0:
            continue

        rut_proveedor = str(cab.get('rut_proveedor', '') or '').strip()
        destino = str(cab.get('destino', '') or '').strip()
        comentario_col = str(cab.get('comentario', '') or '').strip()

        if rut_proveedor == '' or destino == '' or comentario_col == '':
            continue

        destino_id = map_sucursal_to_id(destino)
        if destino_id is None:
            # Si no mapea, lo dejamos en 0 para no romper el insert (puedes cambiar a continue)
            destino_id = 0

        comentario_payload = f"{comentario_prefix} {comentario_col}".strip()

        # detalle: lista de dicts {sku,cantidad,precio}
        for item in det:
            if not isinstance(item, dict):
                continue

            sku = str(item.get('sku', '') or '').strip()
            qty = item.get('cantidad', 0)
            precio = item.get('precio', 0)

            if sku == '':
                continue

            rows_out.append({
                'id': comentario_col,
                'rut_proveedor': rut_proveedor,
                'proveedor': proveedor,
                'destino': destino,
                'destino_id': destino_id,
                'sku': sku,
                'cantidad': qty,
                'precio': precio,
                'comentario_payload': comentario_payload,
            })

    if not rows_out:
        print('[oc_detalle] Después de aplanar no quedaron filas.')
        return pd.DataFrame([{
            'rows_total': 0,
            'rows_inserted': 0,
        }])

    df_detalle = pd.DataFrame(rows_out)
    rows_total = len(df_detalle)

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
            f'[oc_detalle] Chunk {inserted_chunks}: '
            f'enviados {rows_sent}, insertados {rows_inserted}'
        )

    print(
        f'[oc_detalle] Resumen final -> total filas detalle: {rows_total}, insertadas: {rows_inserted}'
    )

    return pd.DataFrame([{
        'rows_total': rows_total,
        'rows_inserted': rows_inserted,
    }])