"""full_reload_oc: recarga completa de dwh.mysis_mstr_oc y mysis_mstr_oc_aux con swap atomico.

POR QUE EXISTE
--------------
Los pipelines incrementales mysis_tabla_mstr_oc_to_clickhouse y
mysis_tabla_mstr_oc_aux_to_clickhouse leen por ventana de dt_in. Una orden de
compra nace con dt_in = now() y entra por la ventana, pero se CIERRA despues:
el UPDATE que pone oc_fin o dt_cierre no toca dt_in, asi que esa fila nunca
vuelve a leerse y el espejo la deja abierta para siempre. Y las lineas borradas
en MySis se quedan en el espejo, porque un incremental no propaga bajas.

Medido el 2026-08-21:
                              MySis      espejo    diferencia
    mstr_oc filas             61.386     62.372     +986
    mstr_oc abiertas           1.587      2.463     +876
    mstr_oc abiertas de 2026   1.435      2.311     +876
    mstr_oc_aux filas        206.355    216.033   +9.678

Es el MISMO defecto que tenia tab_cmp, y pega directo en la migracion al ERP
nuevo: replenishment-service.ts cuenta como mercaderia en camino toda linea de
una OC en DRAFT/AUTHORIZED/SENT/PARTIAL, asi que 876 OC fantasma harian que el
ERP deje de proponer comprar lo que nunca va a llegar.

QUE HACE
--------
Staging + EXCHANGE TABLES, el patron de los otros full reload:
    1. DROP + CREATE del staging con la estructura exacta del destino
    2. lee la tabla entera de MySis por rangos de PK, en trozos
    3. valida el staging contra MySis: conteo exacto y suma de control
    4. EXCHANGE TABLES  (atomico: nadie ve la tabla vacia ni a medias)
    5. DROP del staging
Si algo falla antes del paso 4, el destino queda intacto.

Sobre MySis: SOLO SELECT.

PRECISION DECIMAL - NO METER float() EN EL CAMINO DE LOS DATOS
-------------------------------------------------------------
clickhouse_connect TRUNCA hacia cero al insertar un float en un Decimal(18,2):
Decimal('1039.05') -> 1039.04999... -> se guarda 1039.04. Y pd.read_sql
convierte Decimal a float64 solo por tener coerce_float=True por defecto. Por
eso los decimales se leen como TEXTO con CAST(x AS CHAR) y se reconstruyen con
decimal.Decimal, y las fechas igual, con strptime, sin que pandas infiera nada.

kwargs
------
    read_chunk    filas por SELECT contra MySis. Default 50.000.
    tablas        'oc', 'oc_aux' o las dos separadas por coma. Default las dos.
    min_ratio     guarda: si el staging queda con menos de esta fraccion de las
                  filas de MySis, NO se hace el swap. Default 0.90.
"""

import datetime
import decimal
import time

import clickhouse_connect
from mage_ai.io.config import ConfigFileLoader

if 'custom' not in globals():
    from mage_ai.data_preparation.decorators import custom

CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

# columna -> como leerla de MySis y como reconstruirla en Python.
#   'int'  'txt'  'dec'  'dt'  'date'
TABLAS = {
    'oc': {
        'destino': 'dwh.mysis_mstr_oc',
        'origen': 'mstr_oc',
        'pk': 'oc_id',
        'suma': 'total',
        'cols': [
            ('oc_id', 'int'), ('proveedor_id', 'int'), ('dt_in', 'dt'),
            ('id_externo', 'txt'), ('dt_registro', 'dt'), ('dt_llega', 'dt'),
            ('dt_cierre', 'dt'), ('usr_in', 'int'), ('observacion', 'txt'),
            ('usr_solicita', 'int'), ('usr_valida', 'int'), ('dt_valida', 'dt'),
            ('fpago', 'txt'), ('oc_fin', 'dt'), ('oc_fin_usr', 'int'),
            ('oa', 'int'), ('neto', 'dec'), ('iva', 'dec'), ('total', 'dec'),
            ('destino_id', 'txt'), ('archivo', 'txt'), ('dt_lectura', 'dt'),
            ('dt_envio', 'dt'), ('rso_importacion', 'txt'),
            ('fecha_original', 'date'), ('importacion', 'int'), ('versionoc', 'int'),
        ],
        'no_nulas': {'oc_id', 'oa', 'importacion', 'versionoc'},
    },
    'oc_aux': {
        'destino': 'dwh.mysis_mstr_oc_aux',
        'origen': 'mstr_oc_aux',
        'pk': 'posicion',
        'suma': 'qty',
        'cols': [
            ('posicion', 'int'), ('oc_id', 'int'), ('sku', 'txt'), ('qty', 'int'),
            ('mda', 'int'), ('pu', 'dec'), ('usr_in', 'int'), ('dt_in', 'dt'),
            ('resto', 'int'), ('descuento', 'dec'), ('ppv', 'dec'),
        ],
        'no_nulas': {'posicion', 'ppv'},
    },
}

PISO = datetime.datetime(1970, 1, 1)
TECHO = datetime.datetime(2106, 2, 7)


def _miles(n):
    return '{:,}'.format(int(n)).replace(',', '.')


def _txt(v):
    if isinstance(v, (bytes, bytearray)):
        return v.decode('utf-8', 'replace')
    return None if v is None else str(v)


def _ch():
    cfg = ConfigFileLoader(CONFIG_PATH, PROFILE)
    return clickhouse_connect.get_client(
        host=cfg['CLICKHOUSE_HOST'], port=int(cfg['CLICKHOUSE_PORT']),
        username=cfg['CLICKHOUSE_USERNAME'], password=cfg['CLICKHOUSE_PASSWORD'],
        database=cfg['CLICKHOUSE_DATABASE'],
        secure=str(cfg['CLICKHOUSE_INTERFACE']).lower() == 'https')


def _conectar():
    cfg = ConfigFileLoader(CONFIG_PATH, PROFILE)
    d = dict(host=cfg['MYSQL_HOST'], port=int(cfg.get('MYSQL_PORT') or 3306),
             user=cfg['MYSQL_USER'], password=cfg['MYSQL_PASSWORD'],
             database=cfg['MYSQL_DATABASE'])
    try:
        import mysql.connector
        return mysql.connector.connect(autocommit=True, **d)
    except ImportError:
        import pymysql
        return pymysql.connect(autocommit=True, **d)


def _select(cols):
    """CAST a CHAR todo lo que no sea entero: ver la nota de precision decimal."""
    partes = []
    for nombre, tipo in cols:
        if tipo == 'int':
            partes.append('`{}`'.format(nombre))
        else:
            partes.append('CAST(`{}` AS CHAR) AS `{}`'.format(nombre, nombre))
    return ', '.join(partes)


def _valor(tipo, crudo, no_nula):
    t = _txt(crudo)
    if tipo == 'int':
        if crudo is None:
            return 0 if no_nula else None
        return int(crudo)
    if tipo == 'txt':
        return ('' if no_nula else None) if t is None else t
    if tipo == 'dec':
        if t is None or t == '':
            return decimal.Decimal('0') if no_nula else None
        return decimal.Decimal(t)
    if tipo in ('dt', 'date'):
        if t is None or t.startswith('0000-00-00'):
            return PISO if no_nula else None
        fmt = '%Y-%m-%d' if tipo == 'date' else '%Y-%m-%d %H:%M:%S'
        try:
            d = datetime.datetime.strptime(t[:10] if tipo == 'date' else t[:19], fmt)
        except ValueError:
            return PISO if no_nula else None
        # ClickHouse no admite fechas fuera de este rango: se llevan al borde y
        # se cuentan, en vez de reventar la insercion entera por una fila.
        if d < PISO:
            return PISO
        if d > TECHO:
            return TECHO
        return d
    raise ValueError('tipo desconocido: ' + tipo)


@custom
def full_reload_oc(*args, **kwargs):
    try:
        return _correr(*args, **kwargs)
    except Exception:
        import traceback
        tb = traceback.format_exc()
        print(tb)
        try:
            c = _ch()
            c.command('CREATE TABLE IF NOT EXISTS dwh.zz_errores '
                      '(ts DateTime DEFAULT now(), bloque String, error String) '
                      'ENGINE = MergeTree ORDER BY ts')
            c.insert('dwh.zz_errores', [['full_reload_oc', tb[:8000]]],
                     column_names=['bloque', 'error'])
        except Exception:
            pass
        raise


def _correr(*args, **kwargs):
    chunk = int(kwargs.get('read_chunk') or 50000)
    min_ratio = float(kwargs.get('min_ratio') or 0.90)
    pedidas = str(kwargs.get('tablas') or 'oc,oc_aux')
    nombres = [x.strip() for x in pedidas.split(',') if x.strip()]
    malas = [x for x in nombres if x not in TABLAS]
    if malas:
        raise ValueError('tablas desconocidas: {}. Validas: oc, oc_aux'.format(malas))

    ch = _ch()
    cnx = _conectar()
    resumen = []

    for nombre in nombres:
        cfg = TABLAS[nombre]
        destino, origen, pk = cfg['destino'], cfg['origen'], cfg['pk']
        staging = destino + '_stg'
        cols = cfg['cols']
        no_nulas = cfg['no_nulas']
        nombres_col = [c for c, _t in cols]

        print('=' * 74)
        print('{}  ->  {}'.format(origen, destino))
        t0 = time.time()

        cur = cnx.cursor()
        cur.execute('SELECT COUNT(*), MIN(`{0}`), MAX(`{0}`), CAST(SUM(`{1}`) AS CHAR) '
                    'FROM {2}'.format(pk, cfg['suma'], origen))
        n_mysis, pk_min, pk_max, suma_mysis = cur.fetchone()
        n_mysis = int(n_mysis)
        suma_mysis = decimal.Decimal(_txt(suma_mysis) or '0')
        print('  MySis: {} filas, {} entre {} y {}, suma({})={}'.format(
            _miles(n_mysis), pk, pk_min, pk_max, cfg['suma'], suma_mysis))

        n_antes = ch.query('SELECT count() FROM {} FINAL'.format(destino)).result_rows[0][0]
        print('  espejo antes: {} filas ({:+} contra MySis)'.format(
            _miles(n_antes), int(n_antes) - n_mysis))

        ch.command('DROP TABLE IF EXISTS {}'.format(staging))
        ch.command('CREATE TABLE {} AS {}'.format(staging, destino))

        leidas = 0
        desde = int(pk_min)
        tope = int(pk_max)
        sel = _select(cols)
        while desde <= tope:
            hasta = desde + chunk - 1
            cur.execute('SELECT {} FROM {} WHERE `{}` BETWEEN {} AND {}'.format(
                sel, origen, pk, desde, hasta))
            filas = cur.fetchall()
            if filas:
                buf = []
                for f in filas:
                    buf.append([_valor(cols[i][1], f[i], cols[i][0] in no_nulas)
                                for i in range(len(cols))])
                ch.insert(staging, buf, column_names=nombres_col)
                leidas += len(buf)
            desde = hasta + 1
        cur.close()

        n_stg = ch.query('SELECT count() FROM {}'.format(staging)).result_rows[0][0]
        suma_stg = ch.query('SELECT sum({}) FROM {}'.format(cfg['suma'], staging)).result_rows[0][0]
        suma_stg = decimal.Decimal(str(suma_stg or 0))
        print('  staging: {} filas, suma({})={}'.format(_miles(n_stg), cfg['suma'], suma_stg))

        # Tres guardas antes del swap. La del ratio es la que evita el desastre
        # clasico: una lectura que fallo a medias y deja el espejo casi vacio.
        if n_stg != n_mysis:
            raise RuntimeError('conteo no cuadra: staging {} vs MySis {}'.format(n_stg, n_mysis))
        if n_mysis and n_stg < n_mysis * min_ratio:
            raise RuntimeError('staging por debajo del min_ratio {}'.format(min_ratio))
        if suma_stg != suma_mysis:
            raise RuntimeError('suma de control no cuadra: {} vs {}'.format(suma_stg, suma_mysis))

        ch.command('EXCHANGE TABLES {} AND {}'.format(staging, destino))
        ch.command('DROP TABLE IF EXISTS {}'.format(staging))

        n_final = ch.query('SELECT count() FROM {} FINAL'.format(destino)).result_rows[0][0]
        print('  swap hecho. espejo ahora: {} filas ({:.1f}s)'.format(
            _miles(n_final), time.time() - t0))
        resumen.append({'tabla': nombre, 'mysis': n_mysis, 'antes': int(n_antes),
                        'despues': int(n_final), 'sobraban': int(n_antes) - n_mysis})

    # El efecto que motiva todo esto: cuantas OC creia abiertas el espejo.
    try:
        ab = ch.query("SELECT count() FROM dwh.mysis_mstr_oc FINAL "
                      "WHERE oa = 1 AND oc_fin IS NULL AND dt_cierre IS NULL").result_rows[0][0]
        print('-' * 74)
        print('OC abiertas en el espejo tras la recarga: {}'.format(_miles(ab)))
    except Exception as e:
        print('no se pudo contar OC abiertas: {}'.format(e))

    cnx.close()
    print('=' * 74)
    return {'tablas': resumen}
