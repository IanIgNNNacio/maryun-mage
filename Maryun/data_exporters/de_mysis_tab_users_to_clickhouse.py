from mage_ai.io.config import ConfigFileLoader
import pandas as pd
import clickhouse_connect
from typing import Set

if 'data_exporter' not in globals():
    from mage_ai.data_preparation.decorators import data_exporter

TABLE = 'dwh.mysis_tab_users'
CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

INSERT_COLS = [
    'user_id', 'user_name', 'user_apellido', 'user_login', 'password', 'acceso', 
    'objeto_activo', 'cliente', 'user_rut', 'id_almacen', 'sucursal_id', 'sucursal', 
    'descuentos_max', 'lista', 'id_externo', 'token', 'area', 'correo', 'resetpass', 
    'tdesc', 'tcomision', 'saldo', 'centrocosto', 'pipe_id'
]

INT_COLS_NOTNULL     = {'descuentos_max', 'resetpass', 'user_id'}
INT_COLS_NULLABLE    = {'id_almacen', 'objeto_activo', 'sucursal_id'}
DECIMAL_COLS_NOTNULL = {'saldo'}
DECIMAL_COLS_NULLABLE= set()
FLOAT_COLS_NOTNULL   = set()
STRING_COLS_NOTNULL  = {'tcomision'}
STRING_COLS_NULLABLE = {'acceso', 'area', 'centrocosto', 'cliente', 'correo', 'id_externo', 'lista', 'password', 'pipe_id', 'sucursal', 'tdesc', 'token', 'user_apellido', 'user_login', 'user_name', 'user_rut'}


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
    df = df.copy()

    for c in INSERT_COLS:
        if c not in df.columns:
            df[c] = pd.NA

    df = df[INSERT_COLS]
    df = df.dropna(subset=['user_id'])

    # Int32 NOT NULL
    _int_nn_defaults = {'descuentos_max': 0, 'resetpass': 0}
    for c in INT_COLS_NOTNULL.intersection(df.columns):
        df[c] = pd.to_numeric(df[c], errors='coerce').fillna(_int_nn_defaults.get(c, 0)).astype('int32')

    # Int32 Nullable
    for c in INT_COLS_NULLABLE.intersection(df.columns):
        df[c] = pd.to_numeric(df[c], errors='coerce').astype('Int32')

    # Decimal NOT NULL
    _dec_nn_defaults = {'saldo': 0}
    for c in DECIMAL_COLS_NOTNULL.intersection(df.columns):
        df[c] = pd.to_numeric(df[c], errors='coerce').fillna(_dec_nn_defaults.get(c, 0.0)).round(2).astype('float64')

    # String NOT NULL
    _str_nn_defaults = {'tcomision': 'NO'}
    for c in STRING_COLS_NOTNULL.intersection(df.columns):
        df[c] = df[c].fillna(_str_nn_defaults.get(c, '')).astype(str)

    # String Nullable
    for c in STRING_COLS_NULLABLE.intersection(df.columns):
        df[c] = df[c].where(pd.notna(df[c]), None).astype('object')

    return df


def _coerce_dates_for_clickhouse(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    pass  # no date columns
    return df


def _to_python_native(val, col: str):
    if val is None:
        return None
    try:
        if pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass

    if col in INT_COLS_NOTNULL or col in INT_COLS_NULLABLE:
        return int(val)
    if col in DECIMAL_COLS_NOTNULL or col in DECIMAL_COLS_NULLABLE:
        return float(val)
    if col in STRING_COLS_NOTNULL:
        return str(val)
    return val  # fechas ya vienen como datetime/date o None


def _fetch_existing_ids(client, ids: list) -> Set[int]:
    existing: Set[int] = set()
    step = 5000
    for i in range(0, len(ids), step):
        sub = ids[i:i + step]
        ids_txt = ','.join(str(int(x)) for x in sub)
        q = f"SELECT user_id FROM dwh.mysis_tab_users WHERE user_id IN ({ids_txt})"
        res = client.query(q)
        for row in res.result_rows:
            existing.add(int(row[0]))
    return existing


def _insert_rows(client, chunk: pd.DataFrame):
    chunk = _coerce_dates_for_clickhouse(chunk)
    rows = []
    for i in chunk.index:
        row = tuple(_to_python_native(chunk.at[i, c], c) for c in INSERT_COLS)
        rows.append(row)
    client.insert(TABLE, rows, column_names=INSERT_COLS)


@data_exporter
def export_data_to_clickhouse(df: pd.DataFrame, *args, **kwargs):
    client = _client()

    dfp = _prepare_for_insert(df)
    total = len(dfp)
    if total == 0:
        print('No hay filas para exportar.')
        return {'inserted_chunks': 0, 'rows_sent': 0, 'rows_inserted': 0}

    chunk_size = int(kwargs.get('chunk_size') or 10_000)
    inserted_chunks = 0
    rows_sent = 0
    rows_inserted = 0

    for i in range(0, total, chunk_size):
        chunk = dfp.iloc[i:i + chunk_size].copy()

        # Anti-join por user_id (PK)
        ids = chunk['user_id'].dropna().astype(int).tolist()
        existing = _fetch_existing_ids(client, ids)

        if existing:
            keep_mask = [int(x) not in existing for x in chunk['user_id']]
            chunk = chunk.loc[keep_mask]

        if not chunk.empty:
            _insert_rows(client, chunk)
            rows_inserted += len(chunk)

        inserted_chunks += 1
        rows_sent += len(dfp.iloc[i:i + chunk_size])
        print(f'Chunk {inserted_chunks}: enviados {rows_sent}, insertados {rows_inserted}')

    return {
        'inserted_chunks': inserted_chunks,
        'rows_sent': rows_sent,
        'rows_inserted': rows_inserted,
    }
