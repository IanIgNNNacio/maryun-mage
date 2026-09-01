from mage_ai.io.config import ConfigFileLoader
import pandas as pd
import clickhouse_connect

if 'data_exporter' not in globals():
    from mage_ai.data_preparation.decorators import data_exporter

TABLE = 'dwh.mysis_mstr_ingresos_aux'
STAGING = 'dwh.mysis_mstr_ingresos_aux_stg_full'
CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

# Recarga completa atomica: el incremental por dt_in solo ve altas y nunca
# propaga las bajas ni las lineas reinsertadas al editar un ingreso. Con
# staging + EXCHANGE la tabla queda identica al origen en cada corrida.
MIN_RATIO = 0.5  # aborta si el origen trae menos de la mitad de lo ya cargado

# Orden EXACTO de SHOW CREATE TABLE dwh.mysis_mstr_ingresos_aux.
# ingested_at queda fuera a proposito: lo llena el DEFAULT now().
INSERT_COLS = ['posicion', 'hid', 'sku', 'qty', 'usr_in', 'dt_in', 'mda', 'pu']

INT_COLS_NOTNULL = {'posicion'}
INT_COLS_NULLABLE = {'hid', 'qty', 'mda'}
DECIMAL_COLS_NOTNULL = set()
DECIMAL_COLS_NULLABLE = {'pu'}
STRING_COLS_NOTNULL = set()
STRING_COLS_NULLABLE = {'sku', 'usr_in'}

DEC_PLACES = {}
DEC_NN_DEFAULTS = {}
INT_NN_DEFAULTS = {}

DATETIME_COLS = ['dt_in']


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


def _prepare_chunk(df: pd.DataFrame) -> pd.DataFrame:
    """Tipa un trozo, no el frame entero, para no duplicar memoria."""
    df = df.copy()
    for c in INSERT_COLS:
        if c not in df.columns:
            df[c] = pd.NA
    df = df[INSERT_COLS]
    df = df.dropna(subset=['posicion'])
    for c in INT_COLS_NOTNULL.intersection(df.columns):
        df[c] = pd.to_numeric(df[c], errors='coerce').fillna(
            INT_NN_DEFAULTS.get(c, 0)).astype('int32')
    for c in INT_COLS_NULLABLE.intersection(df.columns):
        df[c] = pd.to_numeric(df[c], errors='coerce').astype('Int32')
    for c in DECIMAL_COLS_NOTNULL.intersection(df.columns):
        df[c] = pd.to_numeric(df[c], errors='coerce').fillna(
            DEC_NN_DEFAULTS.get(c, 0.0)).round(DEC_PLACES.get(c, 2)).astype('float64')
    for c in DECIMAL_COLS_NULLABLE.intersection(df.columns):
        df[c] = pd.to_numeric(df[c], errors='coerce').round(
            DEC_PLACES.get(c, 2)).astype('float64')
    for c in STRING_COLS_NOTNULL.intersection(df.columns):
        df[c] = df[c].where(pd.notna(df[c]), '').astype('object')
    for c in STRING_COLS_NULLABLE.intersection(df.columns):
        df[c] = df[c].where(pd.notna(df[c]), None).astype('object')
    for c in DATETIME_COLS:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors='coerce')
    return df


def _coerce_dates_for_clickhouse(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in DATETIME_COLS:
        if c in df.columns:
            s = pd.to_datetime(df[c], errors='coerce')
            df[c] = s.apply(
                lambda x: x.to_pydatetime().replace(tzinfo=None) if pd.notna(x) else None
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
        rows.append(tuple(_to_python_native(chunk.at[i, c], c) for c in INSERT_COLS))
    client.insert(destino, rows, column_names=INSERT_COLS)


@data_exporter
def export_data_to_clickhouse(df: pd.DataFrame, *args, **kwargs):
    """Full load atomico: carga en staging y hace EXCHANGE TABLES."""
    client = _client()
    total = len(df)

    if total == 0:
        raise Exception(
            'Origen sin filas para {}: se aborta sin tocar la tabla final.'.format(TABLE))

    actual = client.query('SELECT count() FROM {}'.format(TABLE)).result_rows[0][0]
    if actual > 0 and total < actual * MIN_RATIO:
        raise Exception(
            'Carga sospechosa en {}: origen trae {} filas contra {} ya cargadas '
            '(menos del {:.0%}). Abortado sin tocar la tabla final.'.format(
                TABLE, total, actual, MIN_RATIO)
        )

    client.command('DROP TABLE IF EXISTS {}'.format(STAGING))
    client.command('CREATE TABLE {} AS {}'.format(STAGING, TABLE))

    try:
        chunk_size = int(kwargs.get('chunk_size') or 50_000)
        rows_inserted = 0
        for i in range(0, total, chunk_size):
            chunk = _prepare_chunk(df.iloc[i:i + chunk_size])
            if not chunk.empty:
                _insert_rows(client, STAGING, chunk)
                rows_inserted += len(chunk)
                print('Staging {}: {}/{}'.format(STAGING, rows_inserted, total))

        en_staging = client.query('SELECT count() FROM {}'.format(STAGING)).result_rows[0][0]
        if en_staging != rows_inserted:
            raise Exception(
                'Staging {} tiene {} filas y se enviaron {}. No se hace el swap.'.format(
                    STAGING, en_staging, rows_inserted)
            )

        client.command('EXCHANGE TABLES {} AND {}'.format(TABLE, STAGING))
        print('EXCHANGE ok: {} reemplazada ({} -> {} filas)'.format(TABLE, actual, rows_inserted))
    except Exception:
        client.command('DROP TABLE IF EXISTS {}'.format(STAGING))
        raise

    client.command('DROP TABLE IF EXISTS {}'.format(STAGING))
    return {
        'table': TABLE,
        'rows_sent': total,
        'rows_inserted': rows_inserted,
        'rows_before': actual,
        'swapped': True,
    }
