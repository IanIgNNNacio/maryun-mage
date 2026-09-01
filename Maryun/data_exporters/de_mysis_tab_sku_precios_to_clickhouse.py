from decimal import Decimal, ROUND_HALF_UP

from mage_ai.io.config import ConfigFileLoader
import pandas as pd
import clickhouse_connect

if 'data_exporter' not in globals():
    from mage_ai.data_preparation.decorators import data_exporter

TABLE = 'dwh.mysis_tab_sku_precios'
STAGING = 'dwh.mysis_tab_sku_precios_stg'
CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

# Full load atomico. tab_sku_precios no tiene PK utilizable, asi que un
# ReplacingMergeTree no puede deduplicar ni un insert incremental propagar
# las bajas: un precio borrado o corregido en MySis quedaria vivo en
# ClickHouse para siempre. Con staging + EXCHANGE TABLES la tabla queda
# identica al origen en cada corrida (altas, ediciones y bajas), sin
# ventana en que quede vacia.
MIN_RATIO = 0.5  # aborta si el origen trae menos de la mitad de lo que ya hay

INSERT_COLS = [
    'hid', 'sku', 'precio_id', 'valor', 'usr_in', 'dt_in', 'valor_2'
]

INT_COLS_NOTNULL     = {'hid', 'precio_id', 'usr_in'}
INT_COLS_NULLABLE    = set()
DECIMAL_COLS_NOTNULL = {'valor', 'valor_2'}
DECIMAL_COLS_NULLABLE= set()
FLOAT_COLS_NOTNULL   = set()
STRING_COLS_NOTNULL  = {'sku'}
STRING_COLS_NULLABLE = set()

DATETIME_COLS = ['dt_in']
DATE_COLS     = []

# Escala de cada columna DECIMAL en ClickHouse.
DEC_PLACES = {'valor': 2, 'valor_2': 0}


# ---------------------------------------------------------------------------
# PRECISION DECIMAL — no tocar sin leer esto
# ---------------------------------------------------------------------------
# mryn_data.tab_sku_precios.valor es DECIMAL(18,2) y el conector MySQL lo
# entrega como decimal.Decimal EXACTO. La version anterior de este bloque lo
# pasaba por float64 (`pd.to_numeric(...).round(2).astype('float64')` y
# `return float(val)`), y clickhouse_connect, al insertar un float en una
# columna Decimal(18,2), TRUNCA hacia cero en vez de redondear.
#
# Resultado: todo valor cuyo float64 mas cercano cae por debajo del decimal
# exacto perdia un centavo. Ejemplo real medido:
#     Decimal('1039.05') -> float64 1039.0499999999999545... -> 1039.04
#
# Impacto verificado el 2026-08-14 sobre los 2 sku de control: 164 de 403
# combinaciones (hid, sku) usadas como costo de ingreso quedaron exactamente
# 1 centavo bajas, y ese centavo se propaga a TODO el kardex PMP posterior
# del par (el costo entra al saldo valorizado y de ahi al PMP de cada
# movimiento siguiente).
#
# Por eso los decimales viajan como decimal.Decimal de punta a punta: se
# cuantizan con ROUND_HALF_UP (mismo redondeo que MariaDB) y NUNCA se
# convierten a float. clickhouse_connect inserta objetos Decimal sin perdida.
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


def _is_missing(v) -> bool:
    if v is None:
        return True
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def _to_decimal(v, places: int):
    """Valor -> decimal.Decimal cuantizado, SIN pasar por float.

    Devuelve None si el valor viene nulo. `str(v)` sobre un Decimal conserva
    los digitos exactos; sobre un int/str tambien. Un float aqui ya vendria
    contaminado, pero str(float) da la repr mas corta que round-trippea, que
    es lo mejor recuperable.
    """
    if _is_missing(v):
        return None
    d = v if isinstance(v, Decimal) else Decimal(str(v))
    return d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _prepare_for_insert(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in INSERT_COLS:
        if c not in df.columns:
            df[c] = pd.NA
    df = df[INSERT_COLS]
    df = df.dropna(subset=['hid'])
    _int_nn_defaults = {'hid': 0, 'precio_id': 0, 'usr_in': 0}
    for c in INT_COLS_NOTNULL.intersection(df.columns):
        df[c] = pd.to_numeric(df[c], errors='coerce').fillna(_int_nn_defaults.get(c, 0)).astype('int32')
    for c in INT_COLS_NULLABLE.intersection(df.columns):
        df[c] = pd.to_numeric(df[c], errors='coerce').astype('Int32')

    # valor es Decimal(18,2) y valor_2 es Decimal(18,0) en ClickHouse.
    # dtype object con objetos Decimal: pd.to_numeric los volveria float.
    for c in DECIMAL_COLS_NOTNULL.intersection(df.columns):
        places = DEC_PLACES.get(c, 2)
        cero = Decimal(0).quantize(Decimal(1).scaleb(-places))
        df[c] = df[c].map(
            lambda v, p=places, z=cero: z if _to_decimal(v, p) is None else _to_decimal(v, p)
        ).astype('object')
    for c in DECIMAL_COLS_NULLABLE.intersection(df.columns):
        places = DEC_PLACES.get(c, 2)
        df[c] = df[c].map(lambda v, p=places: _to_decimal(v, p)).astype('object')

    for c in STRING_COLS_NOTNULL.intersection(df.columns):
        df[c] = df[c].where(pd.notna(df[c]), '').astype('object')
    for c in STRING_COLS_NULLABLE.intersection(df.columns):
        df[c] = df[c].where(pd.notna(df[c]), None).astype('object')
    for c in DATETIME_COLS + DATE_COLS:
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
    for c in DATE_COLS:
        if c in df.columns:
            s = pd.to_datetime(df[c], errors='coerce')
            df[c] = s.apply(
                lambda x: x.date() if pd.notna(x) else None
            ).astype('object')
    return df


def _to_python_native(val, col: str):
    if _is_missing(val):
        return None
    if col in INT_COLS_NOTNULL or col in INT_COLS_NULLABLE:
        return int(val)
    if col in DECIMAL_COLS_NOTNULL or col in DECIMAL_COLS_NULLABLE:
        # Decimal exacto. NUNCA float(): clickhouse_connect trunca el float
        # al insertarlo en una columna Decimal y se pierde 1 centavo.
        return val if isinstance(val, Decimal) else _to_decimal(val, DEC_PLACES.get(col, 2))
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

        # Control de precision: la suma en origen (Decimal exacto) tiene que
        # coincidir centavo a centavo con la suma ya insertada en staging.
        # Si aparece un centavo de diferencia, algo volvio a pasar por float.
        suma_origen = sum(
            (v for v in dfp['valor'] if isinstance(v, Decimal)), Decimal('0.00')
        )
        suma_stg = client.query(f'SELECT sum(valor) FROM {STAGING}').result_rows[0][0]
        if Decimal(str(suma_stg)) != suma_origen:
            raise Exception(
                f'Perdida de precision al insertar: SUM(valor) origen={suma_origen} '
                f'vs staging={suma_stg}. Abortado sin tocar {TABLE}.'
            )

        # Swap atomico: los lectores nunca ven la tabla vacia
        client.command(f'EXCHANGE TABLES {TABLE} AND {STAGING}')
        print(f'EXCHANGE ok: {TABLE} reemplazada ({actual} -> {rows_inserted} filas), '
              f'SUM(valor)={suma_origen}')
    except Exception:
        client.command(f'DROP TABLE IF EXISTS {STAGING}')
        raise

    client.command(f'DROP TABLE IF EXISTS {STAGING}')
    return {
        'rows_sent': total,
        'rows_inserted': rows_inserted,
        'rows_before': actual,
        'sum_valor': str(suma_origen),
        'swapped': True,
    }
