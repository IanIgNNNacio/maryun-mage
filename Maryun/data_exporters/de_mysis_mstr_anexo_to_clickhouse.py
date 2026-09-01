from mage_ai.io.config import ConfigFileLoader
import pandas as pd
import clickhouse_connect
from typing import Set

if 'data_exporter' not in globals():
    from mage_ai.data_preparation.decorators import data_exporter

TABLE = 'dwh.mysis_mstr_anexo'
CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

INSERT_COLS = [
    'id', 'sucursal_id', 'rut', 'descripcion', 'desde', 'diferencia', 'remuneracion'
]

INT_COLS_NOTNULL     = {'id'}
INT_COLS_NULLABLE    = {'sucursal_id'}
DECIMAL_COLS_NOTNULL = set()
DECIMAL_COLS_NULLABLE= {'diferencia'}
FLOAT_COLS_NOTNULL   = set()
STRING_COLS_NOTNULL  = {'remuneracion'}
STRING_COLS_NULLABLE = {'descripcion', 'rut'}


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
    df = df.dropna(subset=['id'])

    # Int32 NOT NULL
    _int_nn_defaults = {}
    for c in INT_COLS_NOTNULL.intersection(df.columns):
        df[c] = pd.to_numeric(df[c], errors='coerce').fillna(_int_nn_defaults.get(c, 0)).astype('int32')

    # Int32 Nullable
    for c in INT_COLS_NULLABLE.intersection(df.columns):
        df[c] = pd.to_numeric(df[c], errors='coerce').astype('Int32')

    # Decimal Nullable
    for c in DECIMAL_COLS_NULLABLE.intersection(df.columns):
        df[c] = pd.to_numeric(df[c], errors='coerce').round(2).astype('float64')

    # String NOT NULL
    _str_nn_defaults = {'remuneracion': 'SI'}
    for c in STRING_COLS_NOTNULL.intersection(df.columns):
        df[c] = df[c].fillna(_str_nn_defaults.get(c, '')).astype(str)

    # String Nullable
    for c in STRING_COLS_NULLABLE.intersection(df.columns):
        df[c] = df[c].where(pd.notna(df[c]), None).astype('object')

    # Date Nullable
    for c in ['desde']:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors='coerce')

    return df


def _coerce_dates_for_clickhouse(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in ['desde']:
        if c in df.columns:
            s = pd.to_datetime(df[c], errors='coerce')
            df[c] = s.apply(
                lambda x: x.date() if pd.notna(x) else None
            ).astype('object')
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
        q = f"SELECT id FROM dwh.mysis_mstr_anexo WHERE id IN ({ids_txt})"
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

        # Anti-join por id (PK)
        ids = chunk['id'].dropna().astype(int).tolist()
        existing = _fetch_existing_ids(client, ids)

        if existing:
            keep_mask = [int(x) not in existing for x in chunk['id']]
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
