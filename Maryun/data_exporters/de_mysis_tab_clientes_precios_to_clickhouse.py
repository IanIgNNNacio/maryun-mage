from mage_ai.io.config import ConfigFileLoader
import pandas as pd
import clickhouse_connect

if 'data_exporter' not in globals():
    from mage_ai.data_preparation.decorators import data_exporter

TABLE = 'dwh.mysis_tab_clientes_precios'
CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

INSERT_COLS = ['cliente_id', 'sku_id', 'neto', 'final', 'dt_in', 'user_in', 'vencimiento']

INT_COLS_NULLABLE = {'cliente_id', 'sku_id', 'user_in'}
DECIMAL_COLS = {'neto', 'final'}
DATE_COLS_DT = ['dt_in']
DATE_COLS_D = ['vencimiento']


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
    for c in INT_COLS_NULLABLE.intersection(df.columns):
        df[c] = pd.to_numeric(df[c], errors='coerce').astype('Int32')
    for c in DECIMAL_COLS.intersection(df.columns):
        df[c] = pd.to_numeric(df[c], errors='coerce').round(2).astype('float64')
    for c in DATE_COLS_DT:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors='coerce')
    for c in DATE_COLS_D:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors='coerce')
    return df


def _coerce_dates_for_clickhouse(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in DATE_COLS_DT:
        if c in df.columns:
            s = pd.to_datetime(df[c], errors='coerce')
            df[c] = s.apply(
                lambda x: x.to_pydatetime().replace(tzinfo=None) if pd.notna(x) else None
            ).astype('object')
    for c in DATE_COLS_D:
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
    if col in INT_COLS_NULLABLE:
        return int(val)
    if col in DECIMAL_COLS:
        return float(val)
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
    """Insercion directa con ReplacingMergeTree. ClickHouse deduplica por (cliente_id, sku_id)."""
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
        'rows_inserted': rows_inserted
    }