from mage_ai.io.config import ConfigFileLoader
import decimal
import pandas as pd
import clickhouse_connect

if 'data_exporter' not in globals():
    from mage_ai.data_preparation.decorators import data_exporter

TABLE = 'dwh.mysis_mstr_pedidos_aux'
CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

INSERT_COLS = [
    'posicion', 'pid', 'sku', 'qty', 'usr_in', 'dt_in', 'mda', 'pu', 'reserva',
    'picking', 'valor_2', 'descuento', 'tramo', 'especial', 'entrega', 'pmp',
    'facturado', 'dt_pmp', 'glosa', 'precio_solicitado'
]

INT_COLS_NOTNULL     = {'facturado', 'picking', 'posicion'}
INT_COLS_NULLABLE    = {'entrega', 'mda', 'pid', 'qty', 'reserva'}
DECIMAL_COLS_NOTNULL = {'descuento', 'pmp', 'precio_solicitado'}
DECIMAL_COLS_NULLABLE= {'pu', 'valor_2'}
FLOAT_COLS_NOTNULL   = set()
STRING_COLS_NOTNULL  = set()
STRING_COLS_NULLABLE = {'especial', 'glosa', 'sku', 'tramo', 'usr_in'}

# Escala declarada por ClickHouse para cada columna Decimal (system.columns).
DECIMAL_SCALES = {
    'pu': '0.01',                 # Nullable(Decimal(18, 2))
    'valor_2': '1',               # Nullable(Decimal(18, 0))
    'descuento': '0.01',          # Decimal(18, 2)
    'pmp': '0.01',                # Decimal(18, 2)
    'precio_solicitado': '0.01',  # Decimal(18, 2)
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


def _prepare_for_insert(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in INSERT_COLS:
        if c not in df.columns:
            df[c] = pd.NA
    df = df[INSERT_COLS]
    df = df.dropna(subset=['posicion'])
    _int_nn_defaults = {'facturado': 0, 'picking': 0}
    for c in INT_COLS_NOTNULL.intersection(df.columns):
        df[c] = pd.to_numeric(df[c], errors='coerce').fillna(_int_nn_defaults.get(c, 0)).astype('int32')
    for c in INT_COLS_NULLABLE.intersection(df.columns):
        df[c] = pd.to_numeric(df[c], errors='coerce').astype('Int32')
    # Los decimales NO pasan por pd.to_numeric/float64: se quedan como objeto
    # (decimal.Decimal exacto de MySQL) y se cuantizan en _to_python_native.
    for c in DECIMAL_COLS_NOTNULL.intersection(df.columns):
        df[c] = df[c].astype('object').where(pd.notna(df[c]), decimal.Decimal('0'))
    for c in DECIMAL_COLS_NULLABLE.intersection(df.columns):
        df[c] = df[c].astype('object').where(pd.notna(df[c]), None)
    for c in STRING_COLS_NULLABLE.intersection(df.columns):
        df[c] = df[c].where(pd.notna(df[c]), None).astype('object')
    for c in ['dt_in']:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors='coerce')
    for c in ['dt_pmp']:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors='coerce')
    return df


def _coerce_dates_for_clickhouse(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in ['dt_in', 'dt_pmp']:
        if c in df.columns:
            s = pd.to_datetime(df[c], errors='coerce')
            df[c] = s.apply(
                lambda x: x.to_pydatetime().replace(tzinfo=None) if pd.notna(x) else None
            ).astype('object')
    return df


def _to_decimal(val, col: str) -> decimal.Decimal:
    d = val if isinstance(val, decimal.Decimal) else decimal.Decimal(str(val))
    return d.quantize(decimal.Decimal(DECIMAL_SCALES.get(col, '0.01')),
                      rounding=decimal.ROUND_HALF_UP)


# Los decimales se devuelven como decimal.Decimal: pasar por float hace que
# clickhouse_connect trunque el centavo al insertar en Decimal (1039.05 -> 1039.04).
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
        return _to_decimal(val, col)
    return val


def _insert_rows(client, chunk: pd.DataFrame):
    chunk = _coerce_dates_for_clickhouse(chunk)
    rows = []
    for i in chunk.index:
        row = tuple(_to_python_native(chunk.at[i, c], c) for c in INSERT_COLS)
        rows.append(row)
    client.insert(TABLE, rows, column_names=INSERT_COLS)


@data_exporter
def export_data_to_clickhouse(df: pd.DataFrame, *args, **kwargs):
    """Insercion directa con ReplacingMergeTree. ClickHouse deduplica por posicion."""
    client = _client()
    dfp = _prepare_for_insert(df)
    total = len(dfp)
    if total == 0:
        print('No hay filas para exportar.')
        return {'inserted_chunks': 0, 'rows_sent': 0, 'rows_inserted': 0}
    chunk_size = int(kwargs.get('chunk_size') or 10_000)
    inserted_chunks = 0
    rows_inserted = 0
    for i in range(0, total, chunk_size):
        chunk = dfp.iloc[i:i + chunk_size].copy()
        if not chunk.empty:
            _insert_rows(client, chunk)
            rows_inserted += len(chunk)
        inserted_chunks += 1
        print(f'Chunk {inserted_chunks}: insertados {rows_inserted}/{total}')
    return {
        'inserted_chunks': inserted_chunks,
        'rows_sent': total,
        'rows_inserted': rows_inserted,
    }
