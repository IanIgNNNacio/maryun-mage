from decimal import Decimal, ROUND_HALF_UP

from mage_ai.io.config import ConfigFileLoader
from mage_ai.io.mysql import MySQL
import pandas as pd

if 'data_loader' not in globals():
    from mage_ai.data_preparation.decorators import data_loader
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test

CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

SOURCE = 'mstr_pedidos_aux'
PK = 'posicion'
PK_WINDOW = 250_000

# Orden EXACTO de dwh.mysis_mstr_pedidos_aux (sin ingested_at: lo llena el DEFAULT now()).
COLS = [
    'posicion', 'pid', 'sku', 'qty', 'usr_in', 'dt_in', 'mda', 'pu', 'reserva',
    'picking', 'valor_2', 'descuento', 'tramo', 'especial', 'entrega', 'pmp',
    'facturado', 'dt_pmp', 'glosa', 'precio_solicitado',
]

# ---------------------------------------------------------------------------
# PRECISION DECIMAL — no volver a float64
# ---------------------------------------------------------------------------
# El driver MySQL devuelve decimal.Decimal EXACTO (~104 B por celda). Con 3,3M
# filas y 5 columnas decimales eso son GB de objetos Python sueltos, y por eso
# la version anterior los pasaba a float64 aca mismo.
#
# El problema: float64 no representa exactamente los decimales de 2 cifras, y
# clickhouse_connect TRUNCA hacia cero al insertar un float en una columna
# Decimal(18,2). Medido el 2026-08-14: Decimal('1039.05') -> float64
# 1039.0499999999999545... -> se almaceno 1039.04. En mstr_pedidos_aux.pmp,
# 30 de 82 valores usados como costo de devolucion por el kardex PMP quedaron
# exactamente 1 centavo bajos, y ese centavo se propaga a todo el kardex
# posterior del par (sku, sucursal).
#
# Solucion: las columnas decimales viajan como ENTERO ESCALADO por 10^escala
# (Int64 nullable, 8 B por celda). Es exacto, ocupa MENOS que el float64 con
# mascara y menos aun que el objeto Decimal. El exporter las reconvierte a
# decimal.Decimal con scaleb(-escala) justo antes de insertar.
# ---------------------------------------------------------------------------

# valor_2 es Decimal(18,0) en ClickHouse; el resto Decimal(18,2).
DEC_PLACES = {'pu': 2, 'valor_2': 0, 'descuento': 2, 'pmp': 2, 'precio_solicitado': 2}
DECIMAL_COLS = list(DEC_PLACES)

INT_NULLABLE_COLS = ['pid', 'qty', 'mda', 'reserva', 'entrega']
INT_NOTNULL_COLS = ['posicion', 'picking', 'facturado']


def _dec_to_scaled(v, places: int):
    """decimal.Decimal -> int escalado por 10^places, exacto y sin float."""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    d = v if isinstance(v, Decimal) else Decimal(str(v))
    return int(d.scaleb(places).to_integral_value(rounding=ROUND_HALF_UP))


def _shrink(part: pd.DataFrame) -> pd.DataFrame:
    for c in DECIMAL_COLS:
        if c in part.columns:
            p = DEC_PLACES[c]
            part[c] = part[c].map(lambda v, _p=p: _dec_to_scaled(v, _p)).astype('Int64')
    for c in INT_NULLABLE_COLS:
        if c in part.columns:
            part[c] = pd.to_numeric(part[c], errors='coerce').astype('Int32')
    for c in INT_NOTNULL_COLS:
        if c in part.columns:
            part[c] = pd.to_numeric(part[c], errors='coerce').fillna(0).astype('int32')
    return part


@data_loader
def load_data(*args, **kwargs):
    """Lee mstr_pedidos_aux COMPLETA, recorriendo el rango de posicion por ventanas.

    Un unico SELECT de 3,3M filas / 776 MB revienta la memoria del worker y deja
    un cursor gigante abierto contra produccion, asi que el rango de PK se
    recorre en ventanas de PK_WINDOW posiciones.

    hi se fija ANTES de recorrer: lo que MySis inserte durante la lectura queda
    fuera de esta corrida (snapshot coherente) y entra en la siguiente. Las
    bajas y las reinserciones con posicion nueva si se propagan, porque el
    exporter reemplaza la tabla entera en vez de insertar sobre lo que ya hay.

    SALIDA: las columnas de DEC_PLACES salen como Int64 ESCALADO por 10^escala
    (ver bloque de comentarios arriba). El exporter las reconvierte a Decimal.
    """
    window = int(kwargs.get('pk_window') or PK_WINDOW)
    col_list = ', '.join('`{}`'.format(c) for c in COLS)

    parts = []
    with MySQL.with_config(ConfigFileLoader(CONFIG_PATH, PROFILE)) as loader:
        bounds = loader.load(
            'SELECT MIN({pk}) AS lo, MAX({pk}) AS hi, COUNT(*) AS n FROM {src}'.format(
                pk=PK, src=SOURCE)
        )
        lo, hi, n_src = bounds.iloc[0]['lo'], bounds.iloc[0]['hi'], int(bounds.iloc[0]['n'])
        if pd.isna(lo):
            raise Exception('{}: el origen no devolvio filas, se aborta.'.format(SOURCE))
        lo, hi = int(lo), int(hi)
        print('{}: {} filas, posicion {}..{}, ventana {}'.format(SOURCE, n_src, lo, hi, window))

        leidas = 0
        start = lo
        while start <= hi:
            end = min(start + window - 1, hi)
            part = loader.load(
                'SELECT {cols} FROM {src} WHERE {pk} BETWEEN {a} AND {b}'.format(
                    cols=col_list, src=SOURCE, pk=PK, a=start, b=end)
            )
            if len(part):
                leidas += len(part)
                parts.append(_shrink(part))
            start = end + 1
            print('  posicion <= {}: {} filas acumuladas'.format(end, leidas))

    df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=COLS)
    print('{}: leidas {} filas contra {} declaradas en el origen'.format(SOURCE, len(df), n_src))

    # Si la lectura por ventanas se quedo corta (p.ej. un tope silencioso del
    # driver), es preferible fallar que reemplazar la tabla con datos parciales.
    if len(df) < n_src * 0.99:
        raise Exception(
            '{}: leidas {} filas contra {} en el origen. Lectura incompleta, abortado.'.format(
                SOURCE, len(df), n_src)
        )

    df.attrs['dec_places'] = dict(DEC_PLACES)
    return df


@test
def test_output(output, *args) -> None:
    assert output is not None, 'The output is undefined'
    assert len(output) > 0, 'Lectura vacia: no se debe reemplazar la tabla destino'
    # Las decimales tienen que salir como entero escalado, no como float.
    for c in ('pmp', 'descuento', 'precio_solicitado', 'pu', 'valor_2'):
        if c in output.columns:
            assert str(output[c].dtype) == 'Int64', (
                '{} debe ser Int64 escalado por 10^{}, no {}. Un float64 aca '
                'trunca un centavo al insertar en Decimal.'.format(
                    c, DEC_PLACES[c], output[c].dtype)
            )
