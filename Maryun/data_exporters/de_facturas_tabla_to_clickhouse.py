from mage_ai.io.config import ConfigFileLoader
import pandas as pd
import clickhouse_connect
import datetime as _dt

if 'data_exporter' not in globals():
    from mage_ai.data_preparation.decorators import data_exporter


TABLE_CURRENT = 'dwh.facturas_jp_mongo'
TABLE_HISTORY = 'dwh.facturas_jp_mongo_history'

CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

INSERT_COLS = [
    'invoice_key',
    'folio',
    'rutProveedor',
    'razon_social',
    'periodo',
    'fechaDocto',
    'fechaRecepcion',
    'operacion',
    'tipoDoc',
    'montoTotal',
    'estadoContab',
    'status',
    'creditDays',
    'Nominas',
    'nomina_name',
    'nomina_type',
    'nomina_status',
    'nomina_created_at',
    'nomina_updated_at',
    'nominaInstallmentNumber',
    'installment_number',
    'installment_amount',
    'installment_due_date',
    'installmentCount',
    'Installments',
    'paidAmount',
    'openAmount',
    'isCreditNote',
    'costCenters',
    'codigo_cuenta_contable',
    'cuenta_contable',
    'tipo_cuenta_contable',
    'ingested_at',
]

HISTORY_COLS = ['snapshot_at'] + INSERT_COLS

INT32_NULLABLE_COLS = {
    'creditDays',
}

UINT32_NULLABLE_COLS = {
    'nominaInstallmentNumber',
    'installment_number',
    'installmentCount',
}

UINT8_COLS = {
    'isCreditNote',
}

DECIMAL_COLS = {
    'montoTotal',
    'installment_amount',
    'paidAmount',
    'openAmount',
}

DATE_COLS = {
    'fechaDocto',
    'fechaRecepcion',
    'installment_due_date',
}

DATETIME_COLS = {
    'ingested_at',
    'snapshot_at',
}

STRING_COLS = set(INSERT_COLS) - INT32_NULLABLE_COLS - UINT32_NULLABLE_COLS - UINT8_COLS - DECIMAL_COLS - DATE_COLS - {'ingested_at'}


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


def _to_nullable_int32(v):
    if _is_missing(v):
        return None

    n = pd.to_numeric(pd.Series([v]), errors='coerce').iloc[0]
    if pd.isna(n):
        return None

    if float(n) % 1 != 0:
        raise ValueError(f'Valor no entero para Int32 nullable: {v}')

    n = int(n)
    if n < -2147483648 or n > 2147483647:
        raise ValueError(f'Valor fuera de rango Int32: {v}')

    return int(n)


def _to_nullable_uint32(v):
    if _is_missing(v):
        return None

    n = pd.to_numeric(pd.Series([v]), errors='coerce').iloc[0]
    if pd.isna(n):
        return None

    if float(n) % 1 != 0:
        raise ValueError(f'Valor no entero para UInt32 nullable: {v}')

    n = int(n)
    if n < 0:
        raise ValueError(f'Valor negativo para UInt32: {v}')

    return int(n)


def _to_uint8_boolish(v):
    if _is_missing(v):
        return 0

    if isinstance(v, bool):
        return 1 if v else 0

    s = str(v).strip().lower()
    if s in {'1', 'true', 't', 'yes', 'y'}:
        return 1
    if s in {'0', 'false', 'f', 'no', 'n', ''}:
        return 0

    n = pd.to_numeric(pd.Series([v]), errors='coerce').iloc[0]
    if pd.isna(n):
        return 0

    if float(n) % 1 != 0:
        raise ValueError(f'Valor no entero para UInt8: {v}')

    n = int(n)
    if n < 0:
        return 0
    if n > 1:
        return 1
    return int(n)


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


def _to_nullable_date(v):
    if _is_missing(v):
        return None

    dtv = pd.to_datetime(v, errors='coerce')
    if pd.isna(dtv):
        return None

    return dtv.date()


def _to_naive_datetime(v):
    if _is_missing(v):
        return None

    dtv = pd.to_datetime(v, errors='coerce')
    if pd.isna(dtv):
        return None

    py = dtv.to_pydatetime()
    return py.replace(tzinfo=None)


def _force_python_float_or_none(series: pd.Series) -> pd.Series:
    return series.apply(lambda x: None if _is_missing(x) else float(x)).astype('object')


def _force_python_date_or_none(series: pd.Series) -> pd.Series:
    return series.apply(lambda x: None if _is_missing(x) else x).astype('object')


def _force_python_datetime_or_none(series: pd.Series) -> pd.Series:
    return series.apply(lambda x: None if _is_missing(x) else x).astype('object')


def _prepare_for_insert(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    for c in INSERT_COLS:
        if c not in df.columns:
            df[c] = pd.NA

    df['invoice_key'] = df['invoice_key'].astype('string')
    df = df[df['invoice_key'].notna() & (df['invoice_key'].astype(str).str.strip() != '')].copy()

    for c in STRING_COLS:
        df[c] = df[c].apply(_to_clean_string)

    for c in INT32_NULLABLE_COLS:
        df[c] = df[c].apply(_to_nullable_int32)

    for c in UINT32_NULLABLE_COLS:
        df[c] = df[c].apply(_to_nullable_uint32)

    for c in UINT8_COLS:
        df[c] = df[c].apply(_to_uint8_boolish)

    for c in DECIMAL_COLS:
        df[c] = df[c].apply(lambda x: _to_nullable_float(x, decimals=2))
        df[c] = _force_python_float_or_none(df[c])

    for c in DATE_COLS:
        df[c] = df[c].apply(_to_nullable_date)
        df[c] = _force_python_date_or_none(df[c])

    now = _dt.datetime.utcnow().replace(tzinfo=None)
    df['ingested_at'] = now
    df['ingested_at'] = _force_python_datetime_or_none(df['ingested_at'])

    return df[INSERT_COLS]


def _build_rows(df: pd.DataFrame, cols: list):
    rows = []
    for row in df[cols].itertuples(index=False, name=None):
        row_out = []
        for col, val in zip(cols, row):
            if col in INT32_NULLABLE_COLS:
                row_out.append(_to_nullable_int32(val))
            elif col in UINT32_NULLABLE_COLS:
                row_out.append(_to_nullable_uint32(val))
            elif col in UINT8_COLS:
                row_out.append(_to_uint8_boolish(val))
            elif col in DECIMAL_COLS:
                row_out.append(_to_nullable_float(val, decimals=2))
            elif col in DATE_COLS:
                row_out.append(_to_nullable_date(val))
            elif col in DATETIME_COLS:
                row_out.append(_to_naive_datetime(val))
            else:
                row_out.append(_to_clean_string(val))
        rows.append(tuple(row_out))
    return rows


def _debug_validate_chunk(chunk: pd.DataFrame):
    for c in INT32_NULLABLE_COLS:
        bad_rows = []
        for v in chunk[c].head(len(chunk)):
            try:
                _to_nullable_int32(v)
            except Exception:
                bad_rows.append(v)
                if len(bad_rows) >= 10:
                    break
        if bad_rows:
            sample = [{'value': x, 'python_type': type(x).__name__} for x in bad_rows]
            raise ValueError(
                f'La columna {c} contiene valores no válidos para Nullable(Int32): {sample}'
            )

    for c in UINT32_NULLABLE_COLS:
        bad_rows = []
        for v in chunk[c].head(len(chunk)):
            try:
                _to_nullable_uint32(v)
            except Exception:
                bad_rows.append(v)
                if len(bad_rows) >= 10:
                    break
        if bad_rows:
            sample = [{'value': x, 'python_type': type(x).__name__} for x in bad_rows]
            raise ValueError(
                f'La columna {c} contiene valores no válidos para Nullable(UInt32): {sample}'
            )

    for c in UINT8_COLS:
        bad_rows = []
        for v in chunk[c].head(len(chunk)):
            try:
                _to_uint8_boolish(v)
            except Exception:
                bad_rows.append(v)
                if len(bad_rows) >= 10:
                    break
        if bad_rows:
            sample = [{'value': x, 'python_type': type(x).__name__} for x in bad_rows]
            raise ValueError(
                f'La columna {c} contiene valores no válidos para UInt8: {sample}'
            )


def _truncate_table(client, table: str):
    client.command(f'TRUNCATE TABLE {table}')


def _insert_rows(client, table: str, cols: list, chunk: pd.DataFrame):
    _debug_validate_chunk(chunk)
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
    hist['snapshot_at'] = _force_python_datetime_or_none(hist['snapshot_at'])
    hist = hist[HISTORY_COLS]

    dfp['ingested_at'] = dfp['ingested_at'].apply(_to_naive_datetime)
    dfp['ingested_at'] = _force_python_datetime_or_none(dfp['ingested_at'])

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