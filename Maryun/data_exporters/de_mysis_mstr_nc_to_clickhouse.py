from mage_ai.io.config import ConfigFileLoader
import pandas as pd
import clickhouse_connect
import decimal

if 'data_exporter' not in globals():
    from mage_ai.data_preparation.decorators import data_exporter

TABLE = 'dwh.mysis_mstr_nc'
CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

INSERT_COLS = [
    'pid', 'cliente_id', 'direccion_id', 'direccion_id2', 'dt_in', 'id_externo',
    'dt_registro', 'dt_cierre', 'usr_in', 'observacion', 'documento', 'guia', 'guia_pdf',
    'factura', 'factura_pdf', 'fpago', 'voucher', 'total_pedido', 'dt_out', 'bultos',
    'elpdf', 'neto', 'iva', 'total', 'pagado', 'fin', 'usr_fin', 'refoc', 'fechaoc',
    'rehes', 'fechahes', 'sucursal_id', 'entregado', 'retira', 'retiro_glosa',
    'dt_vencimiento', 'padre', 'entrega_completa', 'dt_picking', 'send_dte', 'reffac',
    'tipo', 'usado', 'send_xml'
]

INT_COLS_NOTNULL     = {'entrega_completa', 'padre', 'pid', 'send_dte', 'usado'}
INT_COLS_NULLABLE    = {'bultos', 'cliente_id', 'direccion_id', 'direccion_id2', 'fpago', 'id_externo', 'retira', 'sucursal_id', 'usr_fin'}
DECIMAL_COLS_NOTNULL = {'pagado'}
DECIMAL_COLS_NULLABLE= {'iva', 'neto', 'total', 'total_pedido'}
FLOAT_COLS_NOTNULL   = set()
STRING_COLS_NOTNULL  = {'tipo'}
STRING_COLS_NULLABLE = {'documento', 'elpdf', 'factura', 'factura_pdf', 'guia', 'guia_pdf', 'observacion', 'reffac', 'refoc', 'rehes', 'retiro_glosa', 'usr_in', 'voucher'}

# Escala declarada por ClickHouse para cada columna decimal del destino
# (system.columns): total_pedido = Decimal(18, 0); neto/iva/total/pagado = Decimal(18, 2).
DECIMAL_SCALES = {
    'total_pedido': decimal.Decimal('1'),
    'neto':         decimal.Decimal('0.01'),
    'iva':          decimal.Decimal('0.01'),
    'total':        decimal.Decimal('0.01'),
    'pagado':       decimal.Decimal('0.01'),
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
    df = df.dropna(subset=['pid'])
    _int_nn_defaults = {'entrega_completa': 1, 'padre': 0, 'send_dte': 0, 'usado': 0}
    for c in INT_COLS_NOTNULL.intersection(df.columns):
        df[c] = pd.to_numeric(df[c], errors='coerce').fillna(_int_nn_defaults.get(c, 0)).astype('int32')
    for c in INT_COLS_NULLABLE.intersection(df.columns):
        df[c] = pd.to_numeric(df[c], errors='coerce').astype('Int32')
    # Se conserva el decimal.Decimal exacto que entrega MySQL: pasar por float64
    # aqui ya introduce el error que luego trunca el centavo.
    _dec_nn_defaults = {'pagado': decimal.Decimal('0')}
    for c in DECIMAL_COLS_NOTNULL.intersection(df.columns):
        df[c] = df[c].where(pd.notna(df[c]), _dec_nn_defaults.get(c, decimal.Decimal('0'))).astype('object')
    for c in DECIMAL_COLS_NULLABLE.intersection(df.columns):
        df[c] = df[c].where(pd.notna(df[c]), None).astype('object')
    _str_nn_defaults = {'tipo': 'S'}
    for c in STRING_COLS_NOTNULL.intersection(df.columns):
        df[c] = df[c].fillna(_str_nn_defaults.get(c, '')).astype(str)
    for c in STRING_COLS_NULLABLE.intersection(df.columns):
        df[c] = df[c].where(pd.notna(df[c]), None).astype('object')
    for c in ['dt_in', 'dt_registro', 'dt_cierre', 'dt_out']:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors='coerce')
    for c in ['fin', 'entregado', 'dt_vencimiento', 'dt_picking', 'send_xml']:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors='coerce')
    for c in ['fechaoc', 'fechahes']:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors='coerce')
    return df


def _coerce_dates_for_clickhouse(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in ['dt_in', 'dt_registro', 'dt_cierre', 'dt_out', 'fin', 'entregado', 'dt_vencimiento', 'dt_picking', 'send_xml']:
        if c in df.columns:
            s = pd.to_datetime(df[c], errors='coerce')
            df[c] = s.apply(
                lambda x: x.to_pydatetime().replace(tzinfo=None) if pd.notna(x) else None
            ).astype('object')
    for c in ['fechaoc', 'fechahes']:
        if c in df.columns:
            s = pd.to_datetime(df[c], errors='coerce')
            df[c] = s.apply(
                lambda x: x.date() if pd.notna(x) else None
            ).astype('object')
    return df


# Los decimales viajan como decimal.Decimal cuantizado a la escala del destino:
# convertirlos a float hace que clickhouse_connect trunque el centavo al insertar
# en Decimal(18, 2) (p. ej. 1039.05 -> 1039.0499999... -> 1039.04).
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
        d = val if isinstance(val, decimal.Decimal) else decimal.Decimal(str(val))
        return d.quantize(DECIMAL_SCALES[col], rounding=decimal.ROUND_HALF_UP)
    if col in STRING_COLS_NOTNULL:
        return str(val)
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
    """Insercion directa con ReplacingMergeTree. ClickHouse deduplica por pid."""
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