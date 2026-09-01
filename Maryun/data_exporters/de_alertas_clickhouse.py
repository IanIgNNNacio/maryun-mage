# data_exporter_alertas_silencio.py
import clickhouse_connect
import pandas as pd
from typing import Iterable, Set
from datetime import datetime, timedelta

if 'data_exporter' not in globals():
    from mage_ai.data_preparation.decorators import data_exporter
from mage_ai.io.config import ConfigFileLoader


# --- Configuración base (ajusta a tu realidad) ---

ALERTAS_TABLE = 'alertas_silencio'  # o 'dwh.alertas_silencio' si usas esquema
CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

# Duración de silencio en días (hardcodeado como pediste)
SILENCIO_DIAS = 7

# Columnas a insertar, en el mismo orden que la tabla ClickHouse
ALERTAS_INSERT_COLS = [
    'sku2',
    'sku_original',
    'sucursal_destino',
    'sucursal_origen',
    'accion',
    'accion_original',
    'cantidad',
    'fecha_corte',
    'hash_clave',
    'fecha_alerta',
    'valida_hasta',
    'api_source',   # 👈 NUEVA COLUMNA
    'estado',
]

# Tipos para preparar datos
DECIMAL_COLS = {'cantidad'}
STRING_COLS = {
    'sku2',
    'sku_original',
    'sucursal_destino',
    'sucursal_origen',
    'accion',
    'accion_original',
    'hash_clave',
    'api_source',   # 👈 NUEVA COLUMNA
    'estado',
}
DATE_COLS_DT = ['fecha_corte', 'fecha_alerta', 'valida_hasta']


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
    for c in ALERTAS_INSERT_COLS:
        if c not in df.columns:
            df[c] = pd.NA

    # Orden exacto
    df = df[ALERTAS_INSERT_COLS]

    # Decimales (cantidad)
    for c in DECIMAL_COLS.intersection(df.columns):
        s = pd.to_numeric(df[c], errors='coerce')

        # No permitimos NaN porque en la tabla no está Nullable
        if s.isna().any():
            idx = df[s.isna()].index[:5].tolist()
            raise ValueError(
                f'NaN en decimal "{c}" y la columna no es Nullable '
                f'(ej: filas {idx})'
            )

        df[c] = s.astype('float64')  # clickhouse-connect se lleva bien con float64

    # Strings
    for c in STRING_COLS.intersection(df.columns):
        df[c] = df[c].astype('string').fillna('')

    # Fechas → pandas datetime
    for c in DATE_COLS_DT:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors='coerce')

    return df


def _coerce_dates_for_clickhouse(df: pd.DataFrame) -> pd.DataFrame:
    """
    Convierte DateTime -> datetime.datetime o None (dtype object),
    evitando NaT para clickhouse-connect.
    """
    df = df.copy()

    for c in DATE_COLS_DT:
        if c in df.columns:
            s = pd.to_datetime(df[c], errors='coerce')
            df[c] = s.apply(
                lambda x: x.to_pydatetime().replace(tzinfo=None) if pd.notna(x) else None
            ).astype('object')

    return df


def _hashes_from_chunk(chunk: pd.DataFrame) -> Iterable[str]:
    """
    Devuelve los hash_clave del chunk como lista de strings.
    """
    return list(chunk['hash_clave'].astype(str).unique())


def _fetch_existing_hashes(client, hashes: Iterable[str]) -> Set[str]:
    """
    Consulta ClickHouse para saber qué hash_clave ya existen
    como silencios activos y vigentes (estado='Activa' y valida_hasta >= now()).
    """
    hashes = list(hashes)
    if not hashes:
        return set()

    existing: Set[str] = set()
    step = 1000

    now_str = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')

    for i in range(0, len(hashes), step):
        sub = hashes[i:i+step]
        hashes_txt = ','.join("'{}'".format(str(h).replace("'", "\\'")) for h in sub)

        q = f"""
            SELECT hash_clave
            FROM {ALERTAS_TABLE}
            WHERE hash_clave IN ({hashes_txt})
              AND lower(estado) = 'activa'
              AND valida_hasta >= toDateTime('{now_str}')
        """
        res = client.query(q)
        for row in res.result_rows:
            existing.add(str(row[0]))

    return existing


def _insert_rows(client, chunk: pd.DataFrame):
    """
    Inserta las filas del chunk en ClickHouse usando insert nativo.
    """
    # 1) Fechas a objetos Python
    chunk = _coerce_dates_for_clickhouse(chunk)

    # 2) Reemplazar NA por None
    chunk_py = chunk.where(pd.notna(chunk), None)

    # 3) Construir filas en el orden exacto
    rows = [tuple(chunk_py.loc[i, ALERTAS_INSERT_COLS]) for i in chunk_py.index]

    # 4) Insert nativo
    client.insert(ALERTAS_TABLE, rows, column_names=ALERTAS_INSERT_COLS)


@data_exporter
def export_alertas_silencio(df: pd.DataFrame, *args, **kwargs):
    """
    Recibe un DataFrame de alertas NUEVAS (en principio) y:
      - Completa fecha_alerta y valida_hasta.
      - Calcula api_source (oc/traspaso) según 'accion'.
      - Hace un anti-join por hash_clave contra alertas_silencio
        (estado='Activa' y valida_hasta >= now()).
      - Inserta solo las realmente nuevas.
      - Devuelve resumen: filas nuevas insertadas y filas que ya existían.
    """
    if df is None or df.empty:
        print('[alertas_silencio] No hay filas para procesar.')
        return {'rows_total': 0, 'rows_inserted': 0, 'rows_existing_valid': 0}

    # Añadir fecha_alerta y valida_hasta (hardcode 7 días)
    ahora = datetime.utcnow()
    valida = ahora + timedelta(days=SILENCIO_DIAS)

    df = df.copy()
    df['fecha_alerta'] = ahora
    df['valida_hasta'] = valida
    df['estado'] = 'Activa'

    # ✅ NUEVO: api_source según accion
    # - contiene 'generar' => 'oc'
    # - contiene 'traspaso' => 'traspaso'
    # - si no calza => ''
    acc = df['accion'].astype('string').fillna('')
    df['api_source'] = ''
    df.loc[acc.str.contains('generar', case=False, na=False), 'api_source'] = 'oc'
    df.loc[acc.str.contains('despachar', case=False, na=False), 'api_source'] = 'traspaso'
    df.loc[
        (df['api_source'] == '') & acc.str.contains('transferir', case=False, na=False),
        'api_source'
    ] = 'traspaso'

    # Asegurar columnas y tipos
    dfp = _prepare_for_insert(df)
    total = len(dfp)

    client = _client()

    chunk_size = int(kwargs.get('chunk_size') or 10000)
    inserted_chunks = 0
    rows_sent = 0
    rows_inserted = 0
    rows_existing_valid = 0

    for i in range(0, total, chunk_size):
        chunk = dfp.iloc[i:i+chunk_size].copy()

        # Anti-join: obtener hashes ya existentes y vigentes
        hashes = _hashes_from_chunk(chunk)
        existing_hashes = _fetch_existing_hashes(client, hashes)

        if existing_hashes:
            hashes_chunk = chunk['hash_clave'].astype(str).values
            keep_mask = [h not in existing_hashes for h in hashes_chunk]
            rows_existing_valid += len(chunk) - sum(keep_mask)
            chunk = chunk.loc[keep_mask]

        if not chunk.empty:
            _insert_rows(client, chunk)
            rows_inserted += len(chunk)

        inserted_chunks += 1
        rows_sent += len(dfp.iloc[i:i+chunk_size])
        print(
            f'[alertas_silencio] Chunk {inserted_chunks}: '
            f'enviados {rows_sent}, insertados {rows_inserted}, '
            f'ya existentes y vigentes {rows_existing_valid}'
        )

    print(
        f'[alertas_silencio] Resumen final -> '
        f'total recibidas: {total}, '
        f'nuevas insertadas: {rows_inserted}, '
        f'ya existentes y vigentes: {rows_existing_valid}'
    )

    out = {
        'rows_total': total,
        'rows_inserted': rows_inserted,
        'rows_existing_valid': rows_existing_valid,
    }

    print(out)
    return out