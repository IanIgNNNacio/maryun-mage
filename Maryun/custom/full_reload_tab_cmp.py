"""full_reload_tab_cmp: recarga completa de dwh.mysis_tab_cmp con swap atomico.

POR QUE EXISTE
--------------
El pipeline incremental mysis_tabla_tab_cmp_to_clickhouse lee

    SELECT * FROM tab_cmp WHERE dt_in >= NOW() - INTERVAL 20 DAY

y eso no puede mantener el espejo correcto. tab_cmp es una tabla VERSIONADA: los
12 escritores PHP hacen, para cada (sku, sucursal_id),

    UPDATE tab_cmp SET oa = 0 WHERE sku = ? AND sucursal_id = ?
    INSERT INTO tab_cmp (..., oa) VALUES (..., 1)

El INSERT nace con dt_in = now(), asi que la fila NUEVA entra por la ventana de 20
dias. Pero el UPDATE que da vuelta la fila VIEJA a oa = 0 no toca su dt_in (es
DEFAULT current_timestamp, solo se setea al insertar), asi que esa fila nunca
vuelve a ser leida y el espejo se queda con su oa = 1 para siempre.

Medido el 2026-08-19:
    MySis        filas oa=1 : 127.287   (127.283 pares)
    espejo FINAL filas oa=1 : 142.639   (127.241 pares)
    sobran 15.352 filas marcadas oa=1 que en MySis ya estan en 0.
Ademas 2.863 duplicados fisicos sin mergear (count() 758.290 vs 755.427 ids).

tab_cmp NO la lee el kardex: el procedure calculadora_pmp jamas la toca. Pero es
de donde sale el COSTO_PPM del reporte "Stock Valorizado" del negocio, asi que
cualquier port de ese reporte contra el espejo daria un costo superado o ambiguo.

QUE HACE
--------
Staging + EXCHANGE TABLES, el mismo patron que los otros full reload:
    1. DROP + CREATE del staging con la estructura del destino
    2. lee tab_cmp entera de MySis por rangos de id, en chunks
    3. valida el staging contra MySis (conteo con tolerancia + suma exacta de cmp)
    4. EXCHANGE TABLES  (atomico: nadie ve la tabla vacia ni a medias)
    5. DROP del staging
Si algo falla antes del paso 4, el destino queda intacto.

PRECISION DECIMAL - NO INTRODUCIR float() EN EL CAMINO DE LOS DATOS
------------------------------------------------------------------
clickhouse_connect TRUNCA hacia cero al insertar un float en Decimal(18,2):
Decimal('1039.05') -> float64 1039.04999999... -> se almacena 1039.04. Y
pd.read_sql convierte Decimal a float64 solo por tener coerce_float=True por
defecto. Por eso cmp se lee como TEXTO con CAST(cmp AS CHAR) y se reconstruye con
decimal.Decimal(texto). Idem dt_in, que se lee como texto y se arma con
datetime.strptime, sin que pandas infiera nada.

LAS DOS TRAMPAS DE TIPO DE ESTA TABLA
-------------------------------------
  * sucursal_id es varchar(10) en MySis e Int32 en ClickHouse. Se convierte en
    Python y se CUENTAN las filas que no son enteros; si hay alguna se informa.
  * dt_in en el destino es DateTime NOT NULL con DEFAULT now(). Se inserta
    explicito (el default solo aplica si se omite la columna). Lo que venga NULL o
    fuera del rango de ClickHouse (1970-01-01 .. 2106-02-07) se lleva al piso y se
    cuenta.

kwargs
------
    read_chunk    filas por SELECT contra MySis (default 50.000)
    insert_chunk  filas por INSERT contra ClickHouse (default 25.000)
    tol_por_mil   margen de deriva del conteo en el swap (default 5)
    dry_run       'true' -> lee y valida pero NO hace el EXCHANGE
"""

from datetime import datetime
from decimal import Decimal

import clickhouse_connect
import pandas as pd
from mage_ai.io.config import ConfigFileLoader
from mage_ai.io.mysql import MySQL

if 'custom' not in globals():
    from mage_ai.data_preparation.decorators import custom

CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

SOURCE = 'tab_cmp'
TARGET = 'dwh.mysis_tab_cmp'
STAGING = 'dwh.mysis_tab_cmp_stg_full'

# Orden real de las columnas del destino, sin ingested_at (DEFAULT now()).
COLS = ['id', 'sku', 'sucursal_id', 'cmp', 'oa', 'dt_in']

DT_LO = datetime(1970, 1, 1, 0, 0, 0)
DT_HI = datetime(2106, 2, 7, 6, 28, 15)

SWAP_TOL_POR_MIL_DEFAULT = 5
SWAP_TOL_MIN_FILAS = 50
SWAP_MIN_RATIO_PCT = 50


def _as_int(v, default):
    if v is None or str(v).strip() == '':
        return default
    return int(str(v).strip())


def _as_bool(v, default=False):
    if v is None or str(v).strip() == '':
        return default
    return str(v).strip().lower() in ('1', 'true', 't', 'yes', 'y', 'si')


def _miles(n):
    return '{:,}'.format(int(n)).replace(',', '.')


def _ch():
    cfg = ConfigFileLoader(CONFIG_PATH, PROFILE)
    return clickhouse_connect.get_client(
        host=cfg['CLICKHOUSE_HOST'],
        port=int(cfg['CLICKHOUSE_PORT']),
        username=cfg['CLICKHOUSE_USERNAME'],
        password=cfg['CLICKHOUSE_PASSWORD'],
        database=cfg['CLICKHOUSE_DATABASE'],
        secure=str(cfg['CLICKHOUSE_INTERFACE']).lower() == 'https',
    )


def _mysql():
    return MySQL.with_config(ConfigFileLoader(CONFIG_PATH, PROFILE))


# --------------------------------------------------------------- conversiones

def _to_int(v, contador, clave):
    """varchar -> int. Cuenta lo que no se puede convertir en vez de reventar."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        contador[clave] += 1
        return 0
    s = str(v).strip()
    if s == '' or s.lower() == 'nan':
        contador[clave] += 1
        return 0
    try:
        return int(float(s)) if ('.' in s or 'e' in s.lower()) else int(s)
    except (ValueError, TypeError):
        contador[clave] += 1
        return 0


def _to_dec(v):
    """texto -> decimal.Decimal exacto. Jamas pasa por float."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    if s == '' or s.lower() in ('nan', 'none'):
        return None
    return Decimal(s)


def _to_dt(v, contador, clave):
    """texto -> datetime dentro del rango de ClickHouse."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        contador[clave] += 1
        return DT_LO
    s = str(v).strip()
    if s == '' or s.lower() in ('nan', 'none') or s.startswith('0000-00-00'):
        contador[clave] += 1
        return DT_LO
    if '.' in s:
        s = s.split('.', 1)[0]
    try:
        d = datetime.strptime(s, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        try:
            d = datetime.strptime(s, '%Y-%m-%d')
        except ValueError:
            contador[clave] += 1
            return DT_LO
    if d < DT_LO or d > DT_HI:
        contador[clave] += 1
        return DT_LO
    return d


# --------------------------------------------------------------------- bloque

@custom
def full_reload_tab_cmp(*args, **kwargs):
    read_chunk = _as_int(kwargs.get('read_chunk'), 50000)
    insert_chunk = _as_int(kwargs.get('insert_chunk'), 25000)
    tol_por_mil = _as_int(kwargs.get('tol_por_mil'), SWAP_TOL_POR_MIL_DEFAULT)
    dry_run = _as_bool(kwargs.get('dry_run'), False)

    client = _ch()

    # ------------------------------------------------------------ 0. estado previo
    # MySQL.with_config es un CONTEXT MANAGER: fuera del `with` la conexion no
    # esta abierta y cualquier .load() falla.
    with _mysql() as loader:
        filas_origen = int(loader.load(
            'SELECT COUNT(*) AS n FROM {}'.format(SOURCE)).iloc[0]['n'])
        b = loader.load(
            'SELECT IFNULL(MIN(id),0) AS lo, IFNULL(MAX(id),0) AS hi FROM {}'.format(SOURCE))
        id_min, id_max = int(b.iloc[0]['lo']), int(b.iloc[0]['hi'])
        suma_origen = loader.load(
            'SELECT CAST(IFNULL(SUM(cmp),0) AS CHAR) AS s FROM {}'.format(SOURCE)).iloc[0]['s']
        oa1_origen = int(loader.load(
            'SELECT COUNT(*) AS n FROM {} WHERE oa = 1'.format(SOURCE)).iloc[0]['n'])

    destino_antes = int(client.query(
        'SELECT count() FROM {} FINAL'.format(TARGET)).result_rows[0][0])
    oa1_antes = int(client.query(
        'SELECT countIf(oa = 1) FROM {} FINAL'.format(TARGET)).result_rows[0][0])

    print('MySis  {}: {} filas (id {}..{}), oa=1 {}, SUM(cmp) {}'.format(
        SOURCE, _miles(filas_origen), id_min, id_max, _miles(oa1_origen), suma_origen))
    print('Espejo {} ANTES: {} filas FINAL, oa=1 {}  (sobran {})'.format(
        TARGET, _miles(destino_antes), _miles(oa1_antes), _miles(oa1_antes - oa1_origen)))

    if filas_origen == 0:
        raise Exception('tab_cmp devolvio 0 filas en MySis. Abortado sin tocar nada.')

    # ------------------------------------------------------------ 1. staging limpio
    client.command('DROP TABLE IF EXISTS {}'.format(STAGING))
    client.command('CREATE TABLE {} AS {}'.format(STAGING, TARGET))
    print('staging {} recreado'.format(STAGING))

    # ------------------------------------------------------------ 2. carga por rangos
    coerciones = {'sucursal_id': 0, 'dt_in': 0, 'id': 0, 'oa': 0}
    insertadas = 0
    suma_enviada = Decimal('0')

    with _mysql() as loader:
        desde = id_min
        while desde <= id_max:
            hasta = desde + read_chunk
            sql = (
                'SELECT id, sku, sucursal_id, CAST(cmp AS CHAR) AS cmp, oa, '
                'CAST(dt_in AS CHAR) AS dt_in '
                'FROM {src} WHERE id >= {a} AND id < {b} ORDER BY id'
            ).format(src=SOURCE, a=desde, b=hasta)
            df = loader.load(sql)
            desde = hasta
            if df is None or len(df) == 0:
                continue

            filas = []
            for id_, sku, suc, cmp_, oa, dt in zip(
                    df['id'].tolist(), df['sku'].tolist(), df['sucursal_id'].tolist(),
                    df['cmp'].tolist(), df['oa'].tolist(), df['dt_in'].tolist()):
                dec = _to_dec(cmp_)
                if dec is not None:
                    suma_enviada += dec
                filas.append((
                    _to_int(id_, coerciones, 'id'),
                    None if sku is None or (isinstance(sku, float) and pd.isna(sku)) else str(sku),
                    _to_int(suc, coerciones, 'sucursal_id'),
                    dec,
                    None if oa is None or (isinstance(oa, float) and pd.isna(oa)) else _to_int(oa, coerciones, 'oa'),
                    _to_dt(dt, coerciones, 'dt_in'),
                ))

            for i in range(0, len(filas), insert_chunk):
                client.insert(STAGING, filas[i:i + insert_chunk], column_names=COLS)
            insertadas += len(filas)
            print('  ...{} filas insertadas'.format(_miles(insertadas)))

    # ------------------------------------------------------------ 3. validacion
    filas_stg = int(client.query('SELECT count() FROM {}'.format(STAGING)).result_rows[0][0])
    unicos_stg = int(client.query(
        'SELECT uniqExact(id) FROM {}'.format(STAGING)).result_rows[0][0])
    suma_stg = client.query(
        'SELECT toDecimalString(sum(cmp), 2) FROM {}'.format(STAGING)).result_rows[0][0]
    oa1_stg = int(client.query(
        'SELECT countIf(oa = 1) FROM {}'.format(STAGING)).result_rows[0][0])

    print('staging: {} filas, {} ids unicos, oa=1 {}, SUM(cmp) {}'.format(
        _miles(filas_stg), _miles(unicos_stg), _miles(oa1_stg), suma_stg))
    print('coerciones: {}'.format(coerciones))

    if filas_stg != insertadas:
        raise Exception('El staging tiene {} filas y se enviaron {}.'.format(
            filas_stg, insertadas))
    if unicos_stg != filas_stg:
        raise Exception(
            'El staging tiene {} filas pero solo {} ids unicos: hay duplicados.'.format(
                filas_stg, unicos_stg))

    if Decimal(str(suma_stg)) != suma_enviada.quantize(Decimal('0.01')):
        raise Exception(
            'SUM(cmp) del staging ({}) no coincide digito a digito con lo enviado ({}).'
            .format(suma_stg, suma_enviada))

    tol = max(SWAP_TOL_MIN_FILAS, (filas_origen * tol_por_mil) // 1000)
    if abs(filas_stg - filas_origen) > tol:
        raise Exception(
            'Deriva fuera de tolerancia: staging {} vs MySis {} (tolerancia {}).'.format(
                filas_stg, filas_origen, tol))
    if destino_antes and filas_stg * 100 < destino_antes * SWAP_MIN_RATIO_PCT:
        raise Exception(
            'El staging ({}) es menos de la mitad del destino ({}). No se reemplaza.'.format(
                filas_stg, destino_antes))

    if dry_run:
        print('dry_run: validaciones OK, NO se hizo el EXCHANGE. El staging queda para inspeccion.')
        return {'staging': STAGING, 'filas': filas_stg, 'oa1': oa1_stg,
                'swap': False, 'coerciones': coerciones}

    # ------------------------------------------------------------ 4. swap atomico
    client.command('EXCHANGE TABLES {} AND {}'.format(TARGET, STAGING))
    client.command('DROP TABLE IF EXISTS {}'.format(STAGING))

    filas_fin = int(client.query(
        'SELECT count() FROM {} FINAL'.format(TARGET)).result_rows[0][0])
    oa1_fin = int(client.query(
        'SELECT countIf(oa = 1) FROM {} FINAL'.format(TARGET)).result_rows[0][0])
    pares_fin = int(client.query(
        'SELECT uniqExact((sku, sucursal_id)) FROM {} FINAL WHERE oa = 1'.format(
            TARGET)).result_rows[0][0])

    print('SWAP OK. {}: {} filas, oa=1 {} ({} pares).'.format(
        TARGET, _miles(filas_fin), _miles(oa1_fin), _miles(pares_fin)))
    print('oa=1 fantasma antes {} -> ahora {}'.format(
        _miles(oa1_antes - oa1_origen), _miles(oa1_fin - oa1_origen)))

    return {'target': TARGET, 'filas': filas_fin, 'oa1': oa1_fin,
            'pares_oa1': pares_fin, 'oa1_mysis': oa1_origen,
            'fantasmas_antes': oa1_antes - oa1_origen,
            'fantasmas_ahora': oa1_fin - oa1_origen,
            'swap': True, 'coerciones': coerciones}
