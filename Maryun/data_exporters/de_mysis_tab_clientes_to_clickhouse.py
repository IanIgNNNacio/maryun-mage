from mage_ai.io.config import ConfigFileLoader
import pandas as pd
import clickhouse_connect
from datetime import date, datetime

if 'data_exporter' not in globals():
    from mage_ai.data_preparation.decorators import data_exporter

TABLE = 'dwh.mysis_tab_clientes'
CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

INSERT_COLS = [
    'cliente_id', 'rut', 'rso', 'telefono', 'correo', 'correo_valido', 'contacto',
    'giro', 'tipo_factura', 'dt_in', 'usr_in', 'direccion', 'comuna', 'region', 'ciudad',
    'descuento', 'vencimiento_descuentos', 'autorizado', 'adias', 'edias', 'oa',
    'bloqueado', 'credito', 'limite', 'usaguia', 'parcial', 'direccion_id',
    'vendedor_id', 'sii_update', 'prospecto', 'rubro_id', 'updates', 'registro',
    'comentarios'
]

INT_COLS_NOTNULL     = {'adias', 'autorizado', 'bloqueado', 'cliente_id', 'correo_valido', 'credito', 'edias', 'oa', 'parcial', 'prospecto', 'sii_update', 'usaguia', 'vendedor_id'}
INT_COLS_NULLABLE    = {'direccion_id', 'rubro_id', 'usr_in'}
DECIMAL_COLS_NOTNULL = {'limite'}
DECIMAL_COLS_NULLABLE= set()
FLOAT_COLS_NOTNULL   = {'descuento'}
STRING_COLS_NOTNULL  = set()
STRING_COLS_NULLABLE = {'ciudad', 'comentarios', 'comuna', 'contacto', 'correo', 'direccion', 'giro', 'region', 'registro', 'rso', 'rut', 'telefono', 'tipo_factura'}

CH_DATE_MIN = date(1970, 1, 1)
CH_DATE_MAX = date(2149, 6, 6)


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


def _normalize_clickhouse_date(val):
    if val is None:
        return None
    try:
        if pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass
    try:
        ts = pd.to_datetime(val, errors='coerce')
    except Exception:
        return None
    if pd.isna(ts):
        return None
    d = ts.date()
    if d < CH_DATE_MIN or d > CH_DATE_MAX:
        return None
    return d


def _prepare_for_insert(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in INSERT_COLS:
        if c not in df.columns:
            df[c] = pd.NA
    df = df[INSERT_COLS]
    df = df.dropna(subset=['cliente_id'])
    _int_nn_defaults = {
        'adias': 0, 'autorizado': 0, 'bloqueado': 0, 'correo_valido': 0,
        'credito': 0, 'edias': 0, 'oa': 1, 'parcial': 1, 'prospecto': 0,
        'sii_update': 0, 'usaguia': 0, 'vendedor_id': 0
    }
    for c in INT_COLS_NOTNULL.intersection(df.columns):
        df[c] = pd.to_numeric(df[c], errors='coerce').fillna(_int_nn_defaults.get(c, 0)).astype('int32')
    for c in INT_COLS_NULLABLE.intersection(df.columns):
        df[c] = pd.to_numeric(df[c], errors='coerce').astype('Int32')
    _dec_nn_defaults = {'limite': 0}
    for c in DECIMAL_COLS_NOTNULL.intersection(df.columns):
        df[c] = pd.to_numeric(df[c], errors='coerce').fillna(_dec_nn_defaults.get(c, 0.0)).round(2).astype('float64')
    _float_nn_defaults = {'descuento': 0.0}
    for c in FLOAT_COLS_NOTNULL.intersection(df.columns):
        df[c] = pd.to_numeric(df[c], errors='coerce').fillna(_float_nn_defaults.get(c, 0.0)).astype('float64')
    for c in STRING_COLS_NULLABLE.intersection(df.columns):
        df[c] = df[c].where(pd.notna(df[c]), None).astype('object')
    for c in ['dt_in']:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors='coerce')
    for c in ['updates']:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors='coerce')
    for c in ['vencimiento_descuentos']:
        if c in df.columns:
            df[c] = df[c].apply(_normalize_clickhouse_date).astype('object')
    return df


def _coerce_dates_for_clickhouse(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in ['dt_in', 'updates']:
        if c in df.columns:
            s = pd.to_datetime(df[c], errors='coerce')
            df[c] = s.apply(
                lambda x: x.to_pydatetime().replace(tzinfo=None) if pd.notna(x) else None
            ).astype('object')
    for c in ['vencimiento_descuentos']:
        if c in df.columns:
            df[c] = df[c].apply(_normalize_clickhouse_date).astype('object')
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
    if col in FLOAT_COLS_NOTNULL:
        return float(val)
    if col == 'vencimiento_descuentos':
        return _normalize_clickhouse_date(val)
    if col in ['dt_in', 'updates']:
        if isinstance(val, pd.Timestamp):
            return val.to_pydatetime().replace(tzinfo=None)
        if isinstance(val, datetime):
            return val.replace(tzinfo=None)
        return val
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
    """Insercion directa con ReplacingMergeTree. ClickHouse deduplica por cliente_id."""
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