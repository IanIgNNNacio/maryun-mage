from mage_ai.io.config import ConfigFileLoader
import pandas as pd
import clickhouse_connect
import datetime as _dt

if 'data_exporter' not in globals():
    from mage_ai.data_preparation.decorators import data_exporter


TABLE_CURRENT = 'dwh.costcenters_jp_mongo'
TABLE_HISTORY = 'dwh.costcenters_jp_mongo_history'

CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

INSERT_COLS = [
    'invoice_key',
    'folio',
    'rutProveedor',
    'cost_center_id',
    'cost_center_name',
    'cost_center_percent',
    'ingested_at',
]

HISTORY_COLS = ['snapshot_at'] + INSERT_COLS

FLOAT_COLS = {'cost_center_percent'}
DATETIME_COLS = {'ingested_at', 'snapshot_at'}
STRING_COLS = set(INSERT_COLS) - FLOAT_COLS - {'ingested_at'}


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


def _is_missing(v) -> bool:
    try:
        return v is None or pd.isna(v)
    except Exception:
        return v is None


def _to_clean_string(v):
    if _is_missing(v):
        return ''
    return str(v)


def _to_nullable_float(v, decimals=None):
    if _is_missing(v):
        return None

    n = pd.to_numeric(pd.Series([v]), errors='coerce').iloc[0]
    if pd.isna(n):
        return None

    n = float(n)
    if decimals is not None:
        n = round(n, decimals)

    return float(n)


def _to_naive_datetime(v):
    if _is_missing(v):
        return None

    dtv = pd.to_datetime(v, errors='coerce')
    if pd.isna(dtv):
        return None

    py = dtv.to_pydatetime()
    return py.replace(tzinfo=None)


def _prepare_for_insert(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    for c in INSERT_COLS:
        if c not in df.columns:
            df[c] = pd.NA

    df['invoice_key'] = df['invoice_key'].astype('string')
    df = df[df['invoice_key'].notna() & (df['invoice_key'].astype(str).str.strip() != '')].copy()

    for c in STRING_COLS:
        df[c] = df[c].apply(_to_clean_string)

    for c in FLOAT_COLS:
        df[c] = df[c].apply(lambda x: _to_nullable_float(x, decimals=4))

    now = _dt.datetime.utcnow().replace(tzinfo=None)
    df['ingested_at'] = now

    return df[INSERT_COLS]


def _build_rows(df: pd.DataFrame, cols: list):
    rows = []
    for row in df[cols].itertuples(index=False, name=None):
        row_out = []
        for col, val in zip(cols, row):
            if col in FLOAT_COLS:
                row_out.append(_to_nullable_float(val, decimals=4))
            elif col in DATETIME_COLS:
                row_out.append(_to_naive_datetime(val))
            else:
                row_out.append(_to_clean_string(val))
        rows.append(tuple(row_out))
    return rows


def _truncate_table(client, table: str):
    client.command(f'TRUNCATE TABLE {table}')


def _insert_rows(client, table: str, cols: list, chunk: pd.DataFrame):
    rows = _build_rows(chunk, cols)
    client.insert(table, rows, column_names=cols)


@data_exporter
def export_data_to_clickhouse(df: pd.DataFrame, *args, **kwargs):
    """
    Estrategia:
      - current: TRUNCATE + INSERT completo
      - history: INSERT acumulativo
    """
    client = _client()

    dfp = _prepare_for_insert(df)
    total = len(dfp)

    if total == 0:
        print('No hay filas para exportar.')
        return {
            'truncated_current': False,
            'inserted_chunks': 0,
            'rows_sent': 0,
            'rows_inserted_current': 0,
            'rows_inserted_history': 0,
        }

    hist = dfp.copy()
    hist['snapshot_at'] = hist['ingested_at'].apply(_to_naive_datetime)
    hist = hist[HISTORY_COLS]

    dfp['ingested_at'] = dfp['ingested_at'].apply(_to_naive_datetime)

    _truncate_table(client, TABLE_CURRENT)
    print(f'Tabla truncada: {TABLE_CURRENT}')

    chunk_size = int(kwargs.get('chunk_size') or 10000)
    inserted_chunks = 0
    rows_sent = 0
    rows_ins_cur = 0
    rows_ins_hist = 0

    for i in range(0, total, chunk_size):
        chunk_cur = dfp.iloc[i:i + chunk_size].copy()
        chunk_hist = hist.iloc[i:i + chunk_size].copy()

        if not chunk_cur.empty:
            _insert_rows(client, TABLE_CURRENT, INSERT_COLS, chunk_cur)
            rows_ins_cur += len(chunk_cur)

        if not chunk_hist.empty:
            _insert_rows(client, TABLE_HISTORY, HISTORY_COLS, chunk_hist)
            rows_ins_hist += len(chunk_hist)

        inserted_chunks += 1
        rows_sent += len(chunk_cur)
        print(
            f'Chunk {inserted_chunks}: enviados {rows_sent}, '
            f'current {rows_ins_cur}, history {rows_ins_hist}'
        )

    return {
        'truncated_current': True,
        'inserted_chunks': inserted_chunks,
        'rows_sent': rows_sent,
        'rows_inserted_current': rows_ins_cur,
        'rows_inserted_history': rows_ins_hist,
    }