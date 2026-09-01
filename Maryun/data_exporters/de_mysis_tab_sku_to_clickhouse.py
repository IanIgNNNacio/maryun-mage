from mage_ai.io.config import ConfigFileLoader
import pandas as pd
import clickhouse_connect
from decimal import Decimal, ROUND_HALF_UP

if 'data_exporter' not in globals():
    from mage_ai.data_preparation.decorators import data_exporter

TABLE = 'dwh.mysis_tab_sku'
CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

INSERT_COLS = [
    'sku_id', 'sku', 'producto_id', 'nombre', 'descripcion', 'comentario',
    'oa', 'dt_in', 'critico', 'minimo', 'familia_id', 'barcode', 'user_in',
    'pvfinal', 'sku_id_externo', 'producto_id_externo', 'marca_id', 'color',
    'talla', 'costo', 'foto', 'tipo_id', 'descripcionlarga', 'paraweb', 'pmp',
    'sale_ok', 'purchase_ok', 'ps_id', 'pack', 'largo', 'ancho', 'alto',
    'factor', 'volumetrico', 'nombre_web', 'descripcion_web', 'clave',
    'clave_extra', 'promocionar', 'procedencia', 'area'
]

INT_COLS_NOTNULL = {'costo', 'paraweb', 'pmp', 'sale_ok', 'purchase_ok', 'pack', 'promocionar'}

INT_COLS_NULLABLE = {
    'sku_id', 'producto_id', 'oa', 'critico', 'minimo', 'familia_id',
    'user_in', 'pvfinal', 'sku_id_externo', 'producto_id_externo',
    'marca_id', 'tipo_id', 'ps_id', 'largo', 'ancho', 'alto'
}

INT_COLS = INT_COLS_NOTNULL | INT_COLS_NULLABLE

DECIMAL_COLS = {'factor', 'volumetrico'}

# Escala declarada en ClickHouse (system.columns): ambas son Nullable(Decimal(18, 2)).
DECIMAL_QUANT = {'factor': Decimal('0.01'), 'volumetrico': Decimal('0.01')}

STRING_COLS_NULLABLE = {
    'sku', 'nombre', 'descripcion', 'comentario', 'barcode', 'color',
    'talla', 'foto', 'descripcionlarga', 'nombre_web', 'descripcion_web',
    'clave', 'clave_extra', 'procedencia', 'area'
}

DATE_COLS_DT = ['dt_in']


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
    df = df.dropna(subset=['sku_id'])

    for c in INT_COLS_NOTNULL.intersection(df.columns):
        df[c] = pd.to_numeric(df[c], errors='coerce')
        defaults = {'costo': 0, 'paraweb': 0, 'pmp': 0, 'sale_ok': 1, 'purchase_ok': 1, 'pack': 0, 'promocionar': 0}
        df[c] = df[c].fillna(defaults.get(c, 0)).astype('int32')

    for c in INT_COLS_NULLABLE.intersection(df.columns):
        df[c] = pd.to_numeric(df[c], errors='coerce')
        df[c] = df[c].astype('Int32')

    # Decimales: se conservan como decimal.Decimal (nunca float64, que es inexacto
    # y hace perder el centavo). La cuantizacion ocurre en _to_python_native.
    for c in DECIMAL_COLS.intersection(df.columns):
        df[c] = df[c].astype('object')

    for c in STRING_COLS_NULLABLE.intersection(df.columns):
        df[c] = df[c].where(pd.notna(df[c]), None)
        df[c] = df[c].astype('object')

    for c in DATE_COLS_DT:
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
    return df


def _to_decimal(val, col: str):
    q = DECIMAL_QUANT.get(col, Decimal('0.01'))
    d = val if isinstance(val, Decimal) else Decimal(str(val))
    return d.quantize(q, rounding=ROUND_HALF_UP)


# Los decimales van como decimal.Decimal: el float trunca el centavo al insertar en Decimal(18,2).
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
    if col in INT_COLS_NOTNULL:
        return int(val)
    if col in DECIMAL_COLS:
        return _to_decimal(val, col)
    if col in STRING_COLS_NULLABLE:
        return str(val) if val is not None else None
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
    """Insercion directa con ReplacingMergeTree. ClickHouse deduplica por sku_id."""
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