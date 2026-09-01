from mage_ai.io.config import ConfigFileLoader
import pandas as pd
import clickhouse_connect
from typing import Set
from decimal import Decimal, ROUND_HALF_UP

if 'data_exporter' not in globals():
    from mage_ai.data_preparation.decorators import data_exporter

TABLE = 'dwh.mysis_almacenaje'
CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

INSERT_COLS = [
    'wrknre', 'hid', 'sku', 'qty', 'bodega_id',
    'subbodega_id', 'rack_id', 'piso_id', 'posicion_id', 'subposicion_id',
    'caja_id', 'pallet_id', 'dt_in', 'pk'
]

INT_COLS_NOTNULL  = {'wrknre', 'hid', 'qty', 'bodega_id', 'pk'}
INT_COLS_NULLABLE = {'pallet_id'}
DECIMAL_COLS_NULLABLE = {'caja_id'}
DECIMAL_COLS = DECIMAL_COLS_NULLABLE
STRING_COLS_NOTNULL  = {'sku', 'subbodega_id', 'rack_id', 'piso_id', 'posicion_id', 'subposicion_id'}
DATE_COLS_DT64 = ['dt_in']

# Escala declarada en ClickHouse (system.columns): caja_id es Nullable(Decimal(18, 0)).
DECIMAL_QUANT = {'caja_id': Decimal('1')}


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
    df = df.dropna(subset=['wrknre', 'hid', 'sku', 'qty'])

    # Enteros NOT NULL
    defaults = {'bodega_id': 0, 'pk': 0}
    for c in INT_COLS_NOTNULL.intersection(df.columns):
        df[c] = pd.to_numeric(df[c], errors='coerce').fillna(defaults.get(c, 0)).astype('int32')

    # Enteros Nullable
    for c in INT_COLS_NULLABLE.intersection(df.columns):
        df[c] = pd.to_numeric(df[c], errors='coerce').astype('Int32')

    # Decimales Nullable (caja_id es Decimal(18,0)): se conservan como decimal.Decimal
    # (nunca float64, que es inexacto). La cuantizacion ocurre en _to_python_native.
    for c in DECIMAL_COLS_NULLABLE.intersection(df.columns):
        df[c] = df[c].astype('object')

    # Strings NOT NULL -> fillna('') para respetar el NOT NULL del DDL
    for c in STRING_COLS_NOTNULL.intersection(df.columns):
        df[c] = df[c].fillna('').astype(str)

    # DateTime64(3) Nullable
    for c in DATE_COLS_DT64:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors='coerce')

    return df


def _coerce_dates_for_clickhouse(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in DATE_COLS_DT64:
        if c in df.columns:
            s = pd.to_datetime(df[c], errors='coerce')
            df[c] = s.apply(
                lambda x: x.to_pydatetime().replace(tzinfo=None) if pd.notna(x) else None
            ).astype('object')
    return df


def _to_decimal(val, col: str):
    q = DECIMAL_QUANT.get(col, Decimal('1'))
    d = val if isinstance(val, Decimal) else Decimal(str(val))
    return d.quantize(q, rounding=ROUND_HALF_UP)


# Los decimales van como decimal.Decimal: el float trunca el ultimo digito al insertar en Decimal.
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
    if col in DECIMAL_COLS:
        return _to_decimal(val, col)
    if col in STRING_COLS_NOTNULL:
        return str(val)
    return val


def _fetch_existing_ids(client, ids: list) -> Set[int]:
    existing: Set[int] = set()
    step = 5000
    for i in range(0, len(ids), step):
        sub = ids[i:i + step]
        ids_txt = ','.join(str(int(x)) for x in sub)
        q = f"SELECT wrknre FROM {TABLE} WHERE wrknre IN ({ids_txt})"
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
    """FULL REFRESH: `almacenaje` es estado ACTUAL del stock (las filas que ya
    no estan en estante desaparecen en mryn_data). Un ingest append-only por
    wrknre acumula stock fantasma (filas vendidas/movidas que nunca se borran)
    e infla el stock. Por eso se hace TRUNCATE + recarga completa del snapshot.
    """
    client = _client()

    dfp = _prepare_for_insert(df)
    total = len(dfp)
    if total == 0:
        # Sin filas: NO truncar (evita dejar la tabla vacia por un fallo de lectura).
        print('No hay filas para exportar — se conserva el snapshot anterior.')
        return {'inserted_chunks': 0, 'rows_sent': 0, 'rows_inserted': 0}

    # Reemplazo total del snapshot.
    client.command(f'TRUNCATE TABLE {TABLE}')

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
        'mode': 'full_refresh',
    }