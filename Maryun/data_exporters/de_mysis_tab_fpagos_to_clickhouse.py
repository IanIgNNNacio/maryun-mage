from mage_ai.io.config import ConfigFileLoader
import pandas as pd
import clickhouse_connect

if 'data_exporter' not in globals():
    from mage_ai.data_preparation.decorators import data_exporter

TABLE = 'dwh.mysis_tab_fpagos'
STAGING = 'dwh.mysis_tab_fpagos_stg'
CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

# Full load atomico. El insert directo no propagaba las bajas del origen:
# una forma de pago eliminada en MySis quedaria viva en ClickHouse para
# siempre. Con staging + EXCHANGE TABLES la tabla queda identica al origen en
# cada corrida (altas, ediciones y bajas), sin ventana en que quede vacia.
MIN_RATIO = 0.5  # aborta si el origen trae menos de la mitad de lo que ya hay

INSERT_COLS = [
    'fpago_id', 'fpago_nombre', 'oa'
]

INT_COLS_NOTNULL     = {'fpago_id', 'oa'}
INT_COLS_NULLABLE    = set()
DECIMAL_COLS_NOTNULL = set()
DECIMAL_COLS_NULLABLE= set()
FLOAT_COLS_NOTNULL   = set()
STRING_COLS_NOTNULL  = set()
STRING_COLS_NULLABLE = {'fpago_nombre'}

DATETIME_COLS = []
DATE_COLS     = []


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
    df = df.dropna(subset=['fpago_id'])
    _int_nn_defaults = {'oa': 1}
    for c in INT_COLS_NOTNULL.intersection(df.columns):
        df[c] = pd.to_numeric(df[c], errors='coerce').fillna(_int_nn_defaults.get(c, 0)).astype('int32')
    for c in STRING_COLS_NULLABLE.intersection(df.columns):
        df[c] = df[c].where(pd.notna(df[c]), None).astype('object')
    return df


def _coerce_dates_for_clickhouse(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    return df  # no date columns


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
    if col in STRING_COLS_NOTNULL:
        return str(val)
    return val


def _insert_rows(client, destino: str, chunk: pd.DataFrame):
    chunk = _coerce_dates_for_clickhouse(chunk)
    rows = []
    for i in chunk.index:
        row = tuple(_to_python_native(chunk.at[i, c], c) for c in INSERT_COLS)
        rows.append(row)
    client.insert(destino, rows, column_names=INSERT_COLS)


@data_exporter
def export_data_to_clickhouse(df: pd.DataFrame, *args, **kwargs):
    """Full load atomico: carga en staging y hace EXCHANGE TABLES.

    La tabla final nunca queda vacia ni a medio cargar. Si algo falla, el
    swap no ocurre y los datos anteriores siguen intactos.
    """
    client = _client()
    dfp = _prepare_for_insert(df)
    total = len(dfp)

    if total == 0:
        print('Origen sin filas: se aborta sin tocar la tabla final.')
        return {'rows_sent': 0, 'rows_inserted': 0, 'swapped': False}

    # Control de volumen: evita reemplazar datos buenos por una carga parcial
    actual = client.query(f'SELECT count() FROM {TABLE}').result_rows[0][0]
    if actual > 0 and total < actual * MIN_RATIO:
        raise Exception(
            f'Carga sospechosa: origen trae {total} filas contra {actual} ya '
            f'cargadas (menos del {MIN_RATIO:.0%}). Abortado sin tocar la tabla final.'
        )

    # Staging limpio con la misma estructura y engine que la tabla final
    client.command(f'DROP TABLE IF EXISTS {STAGING}')
    client.command(f'CREATE TABLE {STAGING} AS {TABLE}')

    try:
        chunk_size = int(kwargs.get('chunk_size') or 50_000)
        rows_inserted = 0
        for i in range(0, total, chunk_size):
            chunk = dfp.iloc[i:i + chunk_size].copy()
            if not chunk.empty:
                _insert_rows(client, STAGING, chunk)
                rows_inserted += len(chunk)
                print(f'Staging: {rows_inserted}/{total}')

        # Swap atomico: los lectores nunca ven la tabla vacia
        client.command(f'EXCHANGE TABLES {TABLE} AND {STAGING}')
        print(f'EXCHANGE ok: {TABLE} reemplazada ({actual} -> {rows_inserted} filas)')
    except Exception:
        client.command(f'DROP TABLE IF EXISTS {STAGING}')
        raise

    client.command(f'DROP TABLE IF EXISTS {STAGING}')
    return {
        'rows_sent': total,
        'rows_inserted': rows_inserted,
        'rows_before': actual,
        'swapped': True,
    }
