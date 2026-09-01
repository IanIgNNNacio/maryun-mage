"""de_pmp_detalle_to_clickhouse — carga atomica del kardex PMP.

Full load con staging + EXCHANGE TABLES, igual que el resto de los pipelines
mysis_*: el kardex se recalcula entero en cada corrida, asi que un insert
incremental sobre el ReplacingMergeTree dejaria vivas las filas de pares
(sku, sucursal) que dejaron de tener movimiento y los seq que se acortaron.
Con el swap atomico la tabla final nunca queda vacia ni a medio cargar; si algo
falla, el EXCHANGE no ocurre y los datos anteriores siguen intactos.

Este bloque es el UNICO que escribe. MySis no se toca en ninguna parte del
pipeline: todas las lecturas salen de los espejos dwh.mysis_*.

kwargs
------
  target_table  tabla destino (default dwh.mysis_pmp_detalle). Sirve para
                escribir a una tabla de prueba; debe existir previamente y
                tener la misma estructura.
  chunk_size    filas por insert (default 100.000)
  min_ratio     override del control de volumen
"""

from decimal import Decimal

import numpy as np
import pandas as pd
from mage_ai.io.config import ConfigFileLoader
import clickhouse_connect

if 'data_exporter' not in globals():
    from mage_ai.data_preparation.decorators import data_exporter


CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

TABLE_DEFAULT = 'dwh.mysis_pmp_detalle'

# Aborta si la corrida trae menos de la mitad de lo que ya hay cargado: evita
# reemplazar un kardex completo por una corrida acotada con kwargs skus/sucursales.
MIN_RATIO = 0.5

# ingested_at queda fuera a proposito: tiene DEFAULT now() y es la version del
# ReplacingMergeTree.
INSERT_COLS = [
    'sucursal_id', 'sku', 'seq', 'tipo', 'proveedor_id', 'hid', 'pid', 'nc',
    'fecha', 'ingreso', 'venta', 'devolucion', 'costo', 'saldo_qty',
    'saldo_valorizado', 'pmp', 'factura', 'id_externo', 'dt_calculo',
]

INT32_COLS = {'sucursal_id', 'proveedor_id', 'hid', 'pid', 'nc'}
UINT32_COLS = {'seq'}
STRING_COLS = {'sku', 'tipo', 'factura', 'id_externo'}
DATETIME_COLS = ['fecha', 'dt_calculo']

# Decimal(18,4). El transformer las entrega como int64 escalado por 10^4 para
# no materializar millones de objetos Decimal en memoria.
DECIMAL_COLS = [
    'ingreso', 'venta', 'devolucion', 'costo',
    'saldo_qty', 'saldo_valorizado', 'pmp',
]
ESCALA_DECIMAL = 10 ** 4
EXP_DECIMAL = Decimal('0.0001')


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
    faltan = [c for c in INSERT_COLS if c not in df.columns]
    if faltan:
        raise Exception(f'Faltan columnas en la salida del transformer: {faltan}')
    df = df[INSERT_COLS]

    for c in INT32_COLS:
        df[c] = pd.to_numeric(df[c], errors='coerce').fillna(0).astype('int32')
    for c in UINT32_COLS:
        df[c] = pd.to_numeric(df[c], errors='coerce').fillna(0).astype('uint32')
    for c in STRING_COLS:
        df[c] = df[c].where(pd.notna(df[c]), '').astype('object')
    for c in DATETIME_COLS:
        df[c] = pd.to_datetime(df[c], errors='coerce')

    # Normaliza a int64 escalado, venga como venga el bloque anterior:
    #   int64  -> ya escalado por 10^4 (camino normal)
    #   Decimal/float/str -> se escala aca
    for c in DECIMAL_COLS:
        if pd.api.types.is_integer_dtype(df[c]):
            df[c] = df[c].astype('int64')
        else:
            df[c] = (
                pd.to_numeric(df[c].astype('float64'), errors='coerce')
                .fillna(0.0)
                .mul(ESCALA_DECIMAL)
                .round(0)
                .astype('int64')
            )
    return df


def _rows_from_chunk(chunk: pd.DataFrame):
    """Convierte el chunk a tuplas nativas de Python listas para clickhouse_connect."""
    cols = {}
    for c in INSERT_COLS:
        s = chunk[c]
        if c in DATETIME_COLS:
            cols[c] = [
                (v.to_pydatetime().replace(tzinfo=None) if pd.notna(v) else None)
                for v in s
            ]
        elif c in DECIMAL_COLS:
            # int64 escalado -> Decimal exacto con 4 decimales, sin pasar por float.
            cols[c] = [Decimal(int(v)).scaleb(-4).quantize(EXP_DECIMAL) for v in s]
        elif c in STRING_COLS:
            cols[c] = [('' if v is None else str(v)) for v in s]
        elif c in UINT32_COLS:
            cols[c] = [int(v) for v in s]
        else:
            cols[c] = [int(v) for v in s]
    return list(zip(*[cols[c] for c in INSERT_COLS]))


@data_exporter
def export_data_to_clickhouse(df: pd.DataFrame, *args, **kwargs):
    table = str(kwargs.get('target_table') or TABLE_DEFAULT)
    if '.' not in table:
        table = f'dwh.{table}'
    staging = f'{table}_stg'
    min_ratio = float(kwargs.get('min_ratio') or MIN_RATIO)

    client = _client()
    dfp = _prepare_for_insert(df)
    total = len(dfp)

    if total == 0:
        print('El kardex salio vacio: se aborta sin tocar la tabla final.')
        return {'rows_sent': 0, 'rows_inserted': 0, 'swapped': False}

    # Control de volumen: una corrida acotada con kwargs skus/sucursales no
    # debe reemplazar el kardex completo.
    actual = client.query(f'SELECT count() FROM {table} FINAL').result_rows[0][0]
    if actual > 0 and total < actual * min_ratio:
        raise Exception(
            f'Carga sospechosa: el kardex trae {total} filas contra {actual} ya '
            f'cargadas (menos del {min_ratio:.0%}). Abortado sin tocar {table}. '
            f'Si es intencional (corrida acotada), usa target_table para escribir '
            f'a una tabla de prueba o baja min_ratio explicitamente.'
        )

    client.command(f'DROP TABLE IF EXISTS {staging}')
    client.command(f'CREATE TABLE {staging} AS {table}')

    rows_inserted = 0
    try:
        chunk_size = int(kwargs.get('chunk_size') or 100_000)
        for i in range(0, total, chunk_size):
            chunk = dfp.iloc[i:i + chunk_size]
            if chunk.empty:
                continue
            client.insert(staging, _rows_from_chunk(chunk), column_names=INSERT_COLS)
            rows_inserted += len(chunk)
            print(f'Staging: {rows_inserted}/{total}')

        # Swap atomico: los lectores nunca ven la tabla vacia.
        client.command(f'EXCHANGE TABLES {table} AND {staging}')
        print(f'EXCHANGE ok: {table} reemplazada ({actual} -> {rows_inserted} filas)')
    except Exception:
        client.command(f'DROP TABLE IF EXISTS {staging}')
        raise

    client.command(f'DROP TABLE IF EXISTS {staging}')
    return {
        'target_table': table,
        'rows_sent': total,
        'rows_inserted': rows_inserted,
        'rows_before': actual,
        'swapped': True,
    }
