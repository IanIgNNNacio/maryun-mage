from mage_ai.io.config import ConfigFileLoader
import pandas as pd
import clickhouse_connect
import datetime as _dt

if 'data_exporter' not in globals():
    from mage_ai.data_preparation.decorators import data_exporter
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test


TABLE_CURRENT = 'dwh.bank_scraping_jp'

CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

INSERT_COLS = [
    'fecha',
    'tipoMovimiento',
    'descripcion',
    'sucursal',
    'banco',
    'monto',
    'importe',
    'fechaContable',
    'nroMovimiento',
    'horaTransaccion',
    'codigoOperacion',
    'rutUsuario',
    'cuenta',
    'key',
    'createdAtUtc',
    'ingested_at',
]

FLOAT_COLS = {'monto', 'importe'}
DATETIME_COLS = {'fecha', 'createdAtUtc', 'ingested_at'}
DATE_COLS = {'fechaContable'}
STRING_COLS = {
    'tipoMovimiento',
    'descripcion',
    'banco',
    'nroMovimiento',
    'key',
}
NULLABLE_STRING_COLS = {
    'sucursal',
    'horaTransaccion',
    'codigoOperacion',
    'rutUsuario',
    'cuenta',
}


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


def _to_nullable_string(v):
    if _is_missing(v):
        return None
    s = str(v).strip()
    return s if s else None


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


def _to_date(v):
    if _is_missing(v):
        return None

    dtv = pd.to_datetime(v, errors='coerce')
    if pd.isna(dtv):
        return None

    return dtv.date()


def _prepare_for_insert(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    for c in INSERT_COLS:
        if c not in df.columns:
            df[c] = pd.NA

    df['key'] = df['key'].astype('string')
    df = df[df['key'].notna() & (df['key'].astype(str).str.strip() != '')].copy()

    for c in STRING_COLS:
        df[c] = df[c].apply(_to_clean_string)

    for c in NULLABLE_STRING_COLS:
        df[c] = df[c].apply(_to_nullable_string)

    for c in FLOAT_COLS:
        df[c] = df[c].apply(lambda x: _to_nullable_float(x, decimals=4))

    for c in DATE_COLS:
        df[c] = df[c].apply(_to_date)

    for c in DATETIME_COLS:
        df[c] = df[c].apply(_to_naive_datetime)

    now_utc = _dt.datetime.utcnow().replace(tzinfo=None)

    # Si createdAtUtc no viene informado, lo completamos.
    df['createdAtUtc'] = df['createdAtUtc'].where(df['createdAtUtc'].notna(), now_utc)

    # ingested_at siempre corresponde al momento de carga a CH, en UTC.
    df['ingested_at'] = now_utc

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
            elif col in DATE_COLS:
                row_out.append(_to_date(val))
            elif col in NULLABLE_STRING_COLS:
                row_out.append(_to_nullable_string(val))
            else:
                row_out.append(_to_clean_string(val))
        rows.append(tuple(row_out))
    return rows


def _insert_rows(client, table: str, cols: list, chunk: pd.DataFrame):
    rows = _build_rows(chunk, cols)
    client.insert(table, rows, column_names=cols)


@data_exporter
def export_data_to_clickhouse(df: pd.DataFrame, *args, **kwargs):
    client = _client()

    dfp = _prepare_for_insert(df)
    total = len(dfp)

    if total == 0:
        print('No hay filas para exportar.')
        return {
            'inserted_chunks': 0,
            'rows_sent': 0,
            'rows_inserted': 0,
        }

    chunk_size = int(kwargs.get('chunk_size') or 10000)
    inserted_chunks = 0
    rows_sent = 0
    rows_inserted = 0

    for i in range(0, total, chunk_size):
        chunk_cur = dfp.iloc[i:i + chunk_size].copy()

        if not chunk_cur.empty:
            _insert_rows(client, TABLE_CURRENT, INSERT_COLS, chunk_cur)
            rows_inserted += len(chunk_cur)

        inserted_chunks += 1
        rows_sent += len(chunk_cur)

        print(
            f'Chunk {inserted_chunks}: enviados {rows_sent}, '
            f'insertados {rows_inserted}'
        )

    return {
        'inserted_chunks': inserted_chunks,
        'rows_sent': rows_sent,
        'rows_inserted': rows_inserted,
    }


@test
def test_output(output, *args) -> None:
    """
    Template code for testing the output of the block.
    """
    assert output is not None, 'The output is undefined'