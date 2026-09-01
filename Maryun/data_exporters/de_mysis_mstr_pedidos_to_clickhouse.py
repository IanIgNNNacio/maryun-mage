from mage_ai.io.config import ConfigFileLoader
import decimal
import pandas as pd
import clickhouse_connect

if 'data_exporter' not in globals():
    from mage_ai.data_preparation.decorators import data_exporter

TABLE = 'dwh.mysis_mstr_pedidos'
CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

INSERT_COLS = [
    'pid', 'cliente_id', 'direccion_id', 'direccion_id2', 'dt_in', 'id_externo',
    'dt_registro', 'dt_cierre', 'usr_in', 'observacion', 'documento', 'guia', 'guia_pdf',
    'factura', 'factura_pdf', 'fpago', 'voucher', 'total_pedido', 'dt_out', 'bultos',
    'elpdf', 'neto', 'iva', 'total', 'pagado', 'fin', 'usr_fin', 'refoc', 'fechaoc',
    'rehes', 'fechahes', 'sucursal_id', 'destino_id', 'entregado', 'retira',
    'retiro_glosa', 'dt_vencimiento', 'padre', 'entrega_completa', 'dt_picking',
    'dt_ingresa', 'total_nv', 'deuda', 'dt_pk_out', 'reffac', 'send_dte', 'refactura',
    'estado', 'solicita_precio', 'autoriza_precio', 'send_xml', 'usr_entrega', 'dt_ruta',
    'dt_entregado', 'es_factura', 'es_entrega', 'fecha_a_facturar', 'logo_produccion',
    'logo_usr', 'logo_fabrica', 'logo_id', 'dt_asigna_logo', 'usr_logo', 'ini_pro_usr',
    'ini_pro', 'fin_pro_usr', 'fin_pro'
]

INT_COLS_NOTNULL     = {'entrega_completa', 'es_entrega', 'es_factura', 'padre', 'pid', 'send_dte'}
INT_COLS_NULLABLE    = {'autoriza_precio', 'bultos', 'cliente_id', 'destino_id', 'direccion_id', 'direccion_id2', 'fin_pro_usr', 'fpago', 'ini_pro_usr', 'logo_fabrica', 'logo_id', 'logo_usr', 'retira', 'sucursal_id', 'usr_entrega', 'usr_fin', 'usr_logo'}
DECIMAL_COLS_NOTNULL = {'deuda', 'pagado'}
DECIMAL_COLS_NULLABLE= {'iva', 'neto', 'total', 'total_nv', 'total_pedido'}
FLOAT_COLS_NOTNULL   = set()
STRING_COLS_NOTNULL  = set()
STRING_COLS_NULLABLE = {'documento', 'elpdf', 'estado', 'factura', 'factura_pdf', 'guia', 'guia_pdf', 'id_externo', 'observacion', 'refactura', 'reffac', 'refoc', 'rehes', 'retiro_glosa', 'usr_in', 'voucher'}

# Escala declarada por ClickHouse para cada columna Decimal (system.columns).
DECIMAL_SCALES = {
    'total_pedido': '1',       # Nullable(Decimal(18, 0))
    'neto': '0.01',            # Nullable(Decimal(18, 2))
    'iva': '0.01',             # Nullable(Decimal(18, 2))
    'total': '0.01',           # Nullable(Decimal(18, 2))
    'pagado': '0.01',          # Decimal(18, 2)
    'total_nv': '0.01',        # Nullable(Decimal(18, 2))
    'deuda': '0.01',           # Decimal(18, 2)
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
    _int_nn_defaults = {'entrega_completa': 1, 'es_entrega': 1, 'es_factura': 1, 'padre': 0, 'send_dte': 0}
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
    for c in ['dt_in', 'dt_registro', 'dt_cierre', 'dt_out']:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors='coerce')
    for c in ['fin', 'entregado', 'dt_vencimiento', 'dt_picking', 'dt_ingresa', 'dt_pk_out', 'solicita_precio', 'send_xml', 'dt_ruta', 'dt_entregado', 'logo_produccion', 'dt_asigna_logo', 'ini_pro', 'fin_pro']:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors='coerce')
    for c in ['fechaoc', 'fechahes', 'fecha_a_facturar']:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors='coerce')
    return df


def _coerce_dates_for_clickhouse(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in ['dt_in', 'dt_registro', 'dt_cierre', 'dt_out', 'fin', 'entregado', 'dt_vencimiento', 'dt_picking', 'dt_ingresa', 'dt_pk_out', 'solicita_precio', 'send_xml', 'dt_ruta', 'dt_entregado', 'logo_produccion', 'dt_asigna_logo', 'ini_pro', 'fin_pro']:
        if c in df.columns:
            s = pd.to_datetime(df[c], errors='coerce')
            df[c] = s.apply(
                lambda x: x.to_pydatetime().replace(tzinfo=None) if pd.notna(x) else None
            ).astype('object')
    for c in ['fechaoc', 'fechahes', 'fecha_a_facturar']:
        if c in df.columns:
            s = pd.to_datetime(df[c], errors='coerce')
            df[c] = s.apply(
                lambda x: x.date() if pd.notna(x) else None
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
