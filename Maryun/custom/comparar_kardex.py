"""comparar_kardex: compara MySis.pmp_detalle contra dwh.mysis_pmp_detalle, par por par.

QUE HACE
--------
Despues de la corrida masiva de CALL, los dos lados tienen el kardex del universo
completo. Este bloque los compara con checksum fila a fila, sin traer 2,6 millones
de filas a Mage: MariaDB calcula un CRC32 por fila y lo suma por par, asi que del
lado MySis viajan ~70.000 filas, no millones.

    crc1 = seq | tipo | hid | pid | nc | fecha | los 7 decimales escalados x10^4
           -> las 13 columnas que DECIDE el algoritmo
    crc2 = seq | proveedor_id | factura | id_externo
           -> la metadata, que no afecta ningun calculo

Se guardan en dwh.mysis_pmp_checksums y la comparacion final es una query de
ClickHouse contra su propia tabla, calculando el MISMO checksum del otro lado.

LAS CINCO TRAMPAS QUE HAY QUE ESQUIVAR O DA FALSO NEGATIVO
---------------------------------------------------------
 1. seq: MySis no tiene columna de secuencia; el orden es el de `id`, que es
    autoincremental y refleja el orden de INSERT del procedure. Se reconstruye con
    ROW_NUMBER() OVER (PARTITION BY sucursal_id, sku ORDER BY id).
 2. Decimales: comparar el TEXTO de un decimal falla por los ceros a la derecha
    (MySQL escribe '50.0000' y ClickHouse '50'). Se comparan enteros escalados
    x10^4, que son exactos en los dos motores.
 3. Fechas: dwh.mysis_pmp_detalle esta en UTC y mryn_data.pmp_detalle en hora de
    Chile. Se compara el texto 'YYYY-MM-DD HH:MM:SS' de MySis contra
    formatDateTime(toTimeZone(fecha,'America/Santiago')) de ClickHouse.
    OJO: en ClickHouse los minutos son %i, NO %M (%M es el nombre del mes).
 4. NULL: MySis escribe NULL en fecha y en pmp en algunos casos; ClickHouse escribe
    epoch 0 y 0. Los dos lados mapean esos casos al literal 'NUL' antes del CRC,
    asi la diferencia se ve en el conteo y no se disfraza de checksum distinto.
 5. CONCAT_WS de MySQL SALTEA los NULL y se come el separador. Todo va con IFNULL.

CRC32 da el mismo valor en MariaDB 10.6 y en ClickHouse 25.11 (verificado).

kwargs
------
    sucursales   lista separada por comas para acotar. Vacio = todas.
    corrida      etiqueta, para guardar varias comparaciones. Default 'full-2026-08-19'.
"""

import re
import time

import clickhouse_connect
from mage_ai.io.config import ConfigFileLoader

if 'custom' not in globals():
    from mage_ai.data_preparation.decorators import custom

CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'
TABLA = 'dwh.mysis_pmp_checksums'


def _txt(v):
    """CAST(... AS CHAR) puede volver como bytes segun el driver."""
    if isinstance(v, (bytes, bytearray)):
        return v.decode('utf-8', 'replace')
    return '' if v is None else str(v)


def _i(v):
    return int(float(_txt(v))) if _txt(v) not in ('', 'None') else 0


def _miles(n):
    return '{:,}'.format(int(n)).replace(',', '.')


def _ch():
    cfg = ConfigFileLoader(CONFIG_PATH, PROFILE)
    return clickhouse_connect.get_client(
        host=cfg['CLICKHOUSE_HOST'], port=int(cfg['CLICKHOUSE_PORT']),
        username=cfg['CLICKHOUSE_USERNAME'], password=cfg['CLICKHOUSE_PASSWORD'],
        database=cfg['CLICKHOUSE_DATABASE'],
        secure=str(cfg['CLICKHOUSE_INTERFACE']).lower() == 'https')


def _mysql_cfg():
    cfg = ConfigFileLoader(CONFIG_PATH, PROFILE)
    return dict(host=cfg['MYSQL_HOST'], port=int(cfg.get('MYSQL_PORT') or 3306),
                user=cfg['MYSQL_USER'], password=cfg['MYSQL_PASSWORD'],
                database=cfg['MYSQL_DATABASE'])


def _conectar(c):
    try:
        import mysql.connector
        return mysql.connector.connect(
            host=c['host'], port=c['port'], user=c['user'],
            password=c['password'], database=c['database'], autocommit=True)
    except ImportError:
        import pymysql
        return pymysql.connect(
            host=c['host'], port=c['port'], user=c['user'],
            password=c['password'], database=c['database'], autocommit=True)


# Se recorre POR SUCURSAL: acota el ORDER BY del ROW_NUMBER a un subconjunto y
# evita que MariaDB arme un filesort de 2,6 millones de filas de una sola vez.
SQL_MYSIS = """
SELECT t.sucursal_id, t.sku, COUNT(*) AS n,
       CAST(SUM(CAST(CRC32(CONCAT_WS('|',
            t.seq, t.tipo, t.hid, t.pid, t.nc,
            IFNULL(DATE_FORMAT(t.fecha,'%Y-%m-%d %H:%i:%s'),'NUL'),
            CAST(t.ingreso*10000 AS SIGNED),
            CAST(t.venta*10000 AS SIGNED),
            CAST(t.devolucion*10000 AS SIGNED),
            CAST(t.costo*10000 AS SIGNED),
            CAST(t.saldo_qty*10000 AS SIGNED),
            CAST(t.saldo_valorizado*10000 AS SIGNED),
            IFNULL(CAST(t.pmp*10000 AS SIGNED),'NUL')
       )) AS UNSIGNED)) AS CHAR) AS crc1,
       CAST(SUM(CAST(CRC32(CONCAT_WS('|',
            t.seq, IFNULL(t.proveedor_id,'NUL'),
            IFNULL(t.factura,'NUL'), IFNULL(t.id_externo,'NUL')
       )) AS UNSIGNED)) AS CHAR) AS crc2,
       CAST(SUM(t.pmp IS NULL) AS SIGNED)   AS pmp_nulos,
       CAST(SUM(t.fecha IS NULL) AS SIGNED) AS fecha_nulas,
       CAST(MAX(t.dt_calculo) AS CHAR)      AS dt_calculo
FROM (SELECT p.*, ROW_NUMBER() OVER (PARTITION BY p.sucursal_id, p.sku ORDER BY p.id) AS seq
      FROM pmp_detalle p WHERE p.sucursal_id = {suc}) t
GROUP BY t.sucursal_id, t.sku
"""


@custom
def comparar_kardex(*args, **kwargs):
    try:
        return _comparar(*args, **kwargs)
    except Exception:
        import traceback
        tb = traceback.format_exc()
        print(tb)
        try:
            c = _ch()
            c.command('CREATE TABLE IF NOT EXISTS dwh.zz_errores (ts DateTime DEFAULT now(), bloque String, error String) ENGINE = MergeTree ORDER BY ts')
            c.insert('dwh.zz_errores', [['comparar_kardex', tb[:8000]]], column_names=['bloque', 'error'])
        except Exception:
            pass
        raise


def _comparar(*args, **kwargs):
    corrida = re.sub(r'[^A-Za-z0-9_.-]', '', str(kwargs.get('corrida') or 'full-2026-08-19'))
    filtro = str(kwargs.get('sucursales') or '').strip()

    client = _ch()
    client.command("""
        CREATE TABLE IF NOT EXISTS {} (
            corrida String, sucursal_id Int32, sku String,
            n UInt32, crc1 UInt64, crc2 UInt64,
            pmp_nulos UInt32, fecha_nulas UInt32,
            dt_calculo String, ts DateTime DEFAULT now()
        ) ENGINE = MergeTree ORDER BY (corrida, sucursal_id, sku)
    """.format(TABLA))
    client.command("ALTER TABLE {} DELETE WHERE corrida = '{}'".format(TABLA, corrida))

    cnx = _conectar(_mysql_cfg())
    cur = cnx.cursor()

    if filtro:
        sucs = [int(x) for x in filtro.split(',') if x.strip()]
    else:
        cur.execute('SELECT DISTINCT sucursal_id FROM pmp_detalle ORDER BY 1')
        sucs = [_i(r[0]) for r in cur.fetchall()]

    print('sucursales a recorrer: {}'.format(sucs))
    total = 0
    t0 = time.time()
    for s in sucs:
        t1 = time.time()
        cur.execute(SQL_MYSIS.format(suc=int(s)))
        filas = cur.fetchall()
        if filas:
            buf = [[corrida, _i(r[0]), _txt(r[1]), _i(r[2]), _i(r[3]), _i(r[4]),
                    _i(r[5]), _i(r[6]), _txt(r[7])] for r in filas]
            client.insert(TABLA, buf, column_names=[
                'corrida', 'sucursal_id', 'sku', 'n', 'crc1', 'crc2',
                'pmp_nulos', 'fecha_nulas', 'dt_calculo'])
            total += len(buf)
        print('  sucursal {:>3}: {} pares en {:.1f}s'.format(
            s, _miles(len(filas)), time.time() - t1))
    cur.close(); cnx.close()
    print('\nchecksums de MySis: {} pares en {:.0f}s'.format(_miles(total), time.time() - t0))

    # -------- el MISMO checksum del lado ClickHouse, y la comparacion
    comp = client.query("""
    WITH ch AS (
      SELECT sucursal_id, sku, count() AS n,
        sum(toUInt64(CRC32(concatWithSeparator('|',
          toString(seq), toString(tipo), toString(hid), toString(pid), toString(nc),
          if(fecha = toDateTime(0), 'NUL',
             formatDateTime(toTimeZone(fecha,'America/Santiago'), '%Y-%m-%d %H:%i:%S')),
          toString(toInt64(ingreso*10000)),    toString(toInt64(venta*10000)),
          toString(toInt64(devolucion*10000)), toString(toInt64(costo*10000)),
          toString(toInt64(saldo_qty*10000)),  toString(toInt64(saldo_valorizado*10000)),
          toString(toInt64(pmp*10000)))))) AS crc1,
        sum(toUInt64(CRC32(concatWithSeparator('|',
          toString(seq), toString(proveedor_id),
          if(factura = '', 'NUL', factura), if(id_externo = '', 'NUL', id_externo))))) AS crc2
      FROM dwh.mysis_pmp_detalle GROUP BY sucursal_id, sku),
    my AS (SELECT sucursal_id, sku, n, crc1, crc2 FROM {t} WHERE corrida = '{c}'),
    u AS (SELECT sucursal_id, sku FROM ch UNION DISTINCT SELECT sucursal_id, sku FROM my)
    SELECT
      count()                                                        AS pares_universo,
      countIf(my.n IS NULL)                                          AS solo_en_clickhouse,
      countIf(ch.n IS NULL)                                          AS solo_en_mysis,
      countIf(my.n IS NOT NULL AND ch.n IS NOT NULL)                 AS en_ambos,
      countIf(my.crc1 = ch.crc1 AND my.n = ch.n)                     AS crc1_identico,
      countIf(my.crc1 != ch.crc1 OR my.n != ch.n)                    AS crc1_distinto,
      countIf(my.crc2 = ch.crc2 AND my.crc1 = ch.crc1 AND my.n = ch.n) AS identico_total,
      countIf(my.n != ch.n)                                          AS difiere_conteo,
      toInt64(sum(ifNull(ch.n,0)))                                   AS filas_clickhouse,
      toInt64(sum(ifNull(my.n,0)))                                   AS filas_mysis
    FROM u
    LEFT JOIN ch USING (sucursal_id, sku)
    LEFT JOIN my USING (sucursal_id, sku)
    SETTINGS join_use_nulls = 1
    """.format(t=TABLA, c=corrida))

    r = comp.result_rows[0]
    cols = ['pares_universo', 'solo_en_clickhouse', 'solo_en_mysis', 'en_ambos',
            'crc1_identico', 'crc1_distinto', 'identico_total', 'difiere_conteo',
            'filas_clickhouse', 'filas_mysis']
    res = dict(zip(cols, [int(x) for x in r]))

    print('\n' + '=' * 60)
    print('COMPARACION MySis vs ClickHouse')
    print('=' * 60)
    for k in cols:
        print('  {:<22} {}'.format(k, _miles(res[k])))
    if res['en_ambos']:
        print('\n  {:.3f}% de los pares comunes con checksum del ALGORITMO identico'.format(
            100.0 * res['crc1_identico'] / res['en_ambos']))
    return res
