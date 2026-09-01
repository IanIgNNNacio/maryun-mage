from decimal import Decimal

from mage_ai.io.config import ConfigFileLoader
import pandas as pd
import clickhouse_connect

if 'data_exporter' not in globals():
    from mage_ai.data_preparation.decorators import data_exporter

TABLE = 'dwh.mysis_mstr_pedidos_aux'
STAGING = 'dwh.mysis_mstr_pedidos_aux_stg_full'
CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

# Recarga completa atomica. El incremental por dt_in solo ve altas: la app borra
# y reinserta las lineas al editar un pedido (con posicion nueva) y archiva las
# bajas en mstr_pedidos_aux_borrados, asi que las filas viejas quedaban vivas en
# ClickHouse para siempre. Medido: 33.353 filas fantasma. Con staging + EXCHANGE
# la tabla queda identica al origen en cada corrida, sin ventana en que quede vacia.
MIN_RATIO = 0.5  # aborta si el origen trae menos de la mitad de lo ya cargado

# Orden EXACTO de SHOW CREATE TABLE dwh.mysis_mstr_pedidos_aux.
# ingested_at queda fuera a proposito: lo llena el DEFAULT now().
INSERT_COLS = [
    'posicion', 'pid', 'sku', 'qty', 'usr_in', 'dt_in', 'mda', 'pu', 'reserva',
    'picking', 'valor_2', 'descuento', 'tramo', 'especial', 'entrega', 'pmp',
    'facturado', 'dt_pmp', 'glosa', 'precio_solicitado',
]

INT_COLS_NOTNULL = {'posicion', 'picking', 'facturado'}
INT_COLS_NULLABLE = {'pid', 'qty', 'mda', 'reserva', 'entrega'}
DECIMAL_COLS_NOTNULL = {'descuento', 'pmp', 'precio_solicitado'}
DECIMAL_COLS_NULLABLE = {'pu', 'valor_2'}
STRING_COLS_NOTNULL = set()
STRING_COLS_NULLABLE = {'sku', 'usr_in', 'tramo', 'especial', 'glosa'}

# valor_2 es Decimal(18,0) en ClickHouse; el resto Decimal(18,2).
DEC_PLACES = {'pu': 2, 'valor_2': 0, 'descuento': 2, 'pmp': 2, 'precio_solicitado': 2}
INT_NN_DEFAULTS = {'picking': 0, 'facturado': 0}

DATETIME_COLS = ['dt_in', 'dt_pmp']


# ---------------------------------------------------------------------------
# PRECISION DECIMAL — leer antes de tocar el tipado
# ---------------------------------------------------------------------------
# El loader entrega las columnas de DEC_PLACES como Int64 ESCALADO por
# 10^escala (no como float64, y no como Decimal). Aca se reconvierten a
# decimal.Decimal exacto con scaleb(-escala) justo antes del insert.
#
# NUNCA devolver float() para una columna Decimal: clickhouse_connect trunca
# hacia cero el float al insertarlo en Decimal(18,2) y se pierde 1 centavo en
# todo valor cuyo float64 mas cercano cae por debajo del decimal exacto.
# Medido el 2026-08-14 sobre mstr_pedidos_aux.pmp: 30 de 82 valores usados
# como costo de devolucion por el kardex PMP quedaron 1 centavo bajos.
# ---------------------------------------------------------------------------


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
    """Tipa un trozo. Se trabaja por trozos y no sobre el frame entero: una
    copia de 3,3M filas duplicaria la memoria del worker."""
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
    # Decimales: siguen como ENTERO ESCALADO. Sin round(), sin float64.
    for c in DECIMAL_COLS_NOTNULL.intersection(df.columns):
        df[c] = df[c].astype('Int64').fillna(0)
    for c in DECIMAL_COLS_NULLABLE.intersection(df.columns):
        df[c] = df[c].astype('Int64')
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
        # Entero escalado -> Decimal exacto. NUNCA float().
        return Decimal(int(val)).scaleb(-DEC_PLACES.get(col, 2))
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
    """Full load atomico: carga en staging y hace EXCHANGE TABLES.

    La tabla final nunca queda vacia ni a medio cargar. Si algo falla, el swap
    no ocurre y los datos anteriores siguen intactos.
    """
    client = _client()
    total = len(df)

    if total == 0:
        raise Exception(
            'Origen sin filas para {}: se aborta sin tocar la tabla final.'.format(TABLE))

    # Evita reemplazar datos buenos por una carga parcial.
    actual = client.query('SELECT count() FROM {}'.format(TABLE)).result_rows[0][0]
    if actual > 0 and total < actual * MIN_RATIO:
        raise Exception(
            'Carga sospechosa en {}: origen trae {} filas contra {} ya cargadas '
            '(menos del {:.0%}). Abortado sin tocar la tabla final.'.format(
                TABLE, total, actual, MIN_RATIO)
        )

    # Suma de control de pmp en centavos, calculada sobre el entero escalado
    # del origen. Es la columna que alimenta el costo de devolucion del kardex.
    pmp_origen = int(pd.to_numeric(df['pmp'], errors='coerce').fillna(0).astype('Int64').sum())

    # Staging limpio con la misma estructura y engine que la tabla final.
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

        # El staging debe tener lo que creemos haber mandado antes de pisar la tabla buena.
        en_staging = client.query('SELECT count() FROM {}'.format(STAGING)).result_rows[0][0]
        if en_staging != rows_inserted:
            raise Exception(
                'Staging {} tiene {} filas y se enviaron {}. No se hace el swap.'.format(
                    STAGING, en_staging, rows_inserted)
            )

        # Control de precision: si algun decimal volvio a pasar por float, la
        # suma de pmp en centavos no cuadra y no se hace el swap.
        pmp_stg = int(client.query(
            'SELECT toInt64(sum(pmp) * 100) FROM {}'.format(STAGING)).result_rows[0][0])
        if pmp_stg != pmp_origen:
            raise Exception(
                'Perdida de precision en {}: SUM(pmp) origen={} centavos vs '
                'staging={} centavos (dif {}). Abortado sin tocar la tabla final.'.format(
                    TABLE, pmp_origen, pmp_stg, pmp_stg - pmp_origen)
            )

        # Swap atomico: los lectores nunca ven la tabla vacia.
        client.command('EXCHANGE TABLES {} AND {}'.format(TABLE, STAGING))
        print('EXCHANGE ok: {} reemplazada ({} -> {} filas), SUM(pmp)={} centavos'.format(
            TABLE, actual, rows_inserted, pmp_origen))
    except Exception:
        client.command('DROP TABLE IF EXISTS {}'.format(STAGING))
        raise

    client.command('DROP TABLE IF EXISTS {}'.format(STAGING))
    return {
        'table': TABLE,
        'rows_sent': total,
        'rows_inserted': rows_inserted,
        'rows_before': actual,
        'pmp_centavos': pmp_origen,
        'swapped': True,
    }
