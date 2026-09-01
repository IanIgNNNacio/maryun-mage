from mage_ai.io.config import ConfigFileLoader
from mage_ai.io.mysql import MySQL
import pandas as pd

if 'data_loader' not in globals():
    from mage_ai.data_preparation.decorators import data_loader
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test

CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

SOURCE = 'mstr_nc_aux'
PK = 'posicion'
PK_WINDOW = 250_000

# Orden EXACTO de dwh.mysis_mstr_nc_aux (sin ingested_at: lo llena el DEFAULT now()).
# Ojo: nc_aux NO tiene facturado ni precio_solicitado, a diferencia de pedidos_aux.
COLS = [
    'posicion', 'pid', 'sku', 'qty', 'usr_in', 'dt_in', 'mda', 'pu', 'reserva',
    'picking', 'valor_2', 'descuento', 'tramo', 'especial', 'entrega', 'pmp',
    'dt_pmp', 'glosa',
]

# El driver MySQL devuelve decimal.Decimal (~104 B por celda): se pasan a
# float64 ventana por ventana, antes de acumular.
DECIMAL_COLS = ['pu', 'valor_2', 'descuento', 'pmp']
INT_NULLABLE_COLS = ['pid', 'qty', 'mda', 'reserva', 'entrega']
INT_NOTNULL_COLS = ['posicion', 'picking']


def _shrink(part: pd.DataFrame) -> pd.DataFrame:
    for c in DECIMAL_COLS:
        if c in part.columns:
            part[c] = pd.to_numeric(part[c], errors='coerce').astype('float64')
    for c in INT_NULLABLE_COLS:
        if c in part.columns:
            part[c] = pd.to_numeric(part[c], errors='coerce').astype('Int32')
    for c in INT_NOTNULL_COLS:
        if c in part.columns:
            part[c] = pd.to_numeric(part[c], errors='coerce').fillna(0).astype('int32')
    return part


@data_loader
def load_data(*args, **kwargs):
    """Lee mstr_nc_aux COMPLETA, recorriendo el rango de posicion por ventanas.

    Es la mas chica de las tres (~98k filas), pero se recorre igual por ventanas
    de PK para que las tres cargas se comporten y se depuren igual.

    hi se fija ANTES de recorrer: lo que MySis inserte durante la lectura queda
    fuera de esta corrida y entra en la siguiente.
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

    if len(df) < n_src * 0.99:
        raise Exception(
            '{}: leidas {} filas contra {} en el origen. Lectura incompleta, abortado.'.format(
                SOURCE, len(df), n_src)
        )
    return df


@test
def test_output(output, *args) -> None:
    assert output is not None, 'The output is undefined'
    assert len(output) > 0, 'Lectura vacia: no se debe reemplazar la tabla destino'
