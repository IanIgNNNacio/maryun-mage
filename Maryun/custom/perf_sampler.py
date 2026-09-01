"""perf_sampler: toma una muestra del estado de MariaDB y la guarda en ClickHouse.

PARA QUE
--------
Los contadores de MariaDB (information_schema.GLOBAL_STATUS) son ACUMULADOS desde
el ultimo arranque. Sirven para saber el promedio historico y no sirven para nada
mas: si hoy el hit ratio acumulado es 94,45% sobre 26 horas de uptime, subir el
buffer pool no va a mover ese numero de forma visible en horas.

Lo que si mide una mejora es el DELTA entre dos muestras. Por eso este bloque
guarda los valores crudos cada vez que corre, y los deltas se calculan despues con
una query. Guardar el crudo y no el delta tiene una razon: si MariaDB se reinicia
los contadores vuelven a cero, y con el crudo eso se detecta (uptime baja) en vez
de producir un delta negativo silencioso.

COMO SE USA
-----------
Programarlo cada 5 minutos. Cada corrida escribe una fila por metrica en
dwh.mysis_perf_log con el valor acumulado y la fase.

    fase='antes'    los dias con el buffer pool en 128 MB
    fase='despues'  despues de subirlo

Despues se comparan las dos fases con la query de abajo (esta al final del archivo
como comentario y tambien en perf_comparar.sql).

QUE MIDE Y POR QUE CADA UNO
---------------------------
  Innodb_buffer_pool_read_requests  lecturas logicas: TODA lectura de una pagina
  Innodb_buffer_pool_reads          las que NO estaban en memoria y fueron a disco
      -> hit ratio del intervalo = 1 - reads/read_requests. ESTA es la metrica.
  Innodb_buffer_pool_wait_free      veces que un hilo tuvo que ESPERAR una pagina
      libre. En un server sano es 0. Hoy va en 58: el pool esta ahogado.
  Innodb_data_read                  bytes leidos de disco. Deberia desplomarse.
  Innodb_buffer_pool_pages_free     paginas libres. Si queda pegado en 0, el pool
      sigue chico aun despues del cambio.
  Created_tmp_disk_tables / _tables temporales que se derramaron a disco.
  Slow_queries                      consultas sobre long_query_time.
  Innodb_row_lock_waits / _time     contencion entre transacciones.
  Threads_running                   consultas activas en ese instante. Si es mucho
      mayor que la cantidad de nucleos, hay cola.
  Questions / Com_*                 volumen de trabajo, para NORMALIZAR: de nada
      sirve que bajen las lecturas si tambien bajo el trabajo.

kwargs
------
    fase   'antes' | 'despues' | lo que se quiera. Default 'antes'.
    nota   texto libre que queda en la fila (ej: 'corrida masiva de CALL').
"""

import clickhouse_connect
from mage_ai.io.config import ConfigFileLoader

if 'custom' not in globals():
    from mage_ai.data_preparation.decorators import custom

CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'
TABLA = 'dwh.mysis_perf_log'

METRICAS = [
    'UPTIME',
    'INNODB_BUFFER_POOL_READ_REQUESTS', 'INNODB_BUFFER_POOL_READS',
    'INNODB_BUFFER_POOL_WAIT_FREE', 'INNODB_BUFFER_POOL_PAGES_TOTAL',
    'INNODB_BUFFER_POOL_PAGES_FREE', 'INNODB_BUFFER_POOL_PAGES_DATA',
    'INNODB_BUFFER_POOL_PAGES_DIRTY', 'INNODB_BUFFER_POOL_WRITE_REQUESTS',
    'INNODB_DATA_READ', 'INNODB_DATA_READS', 'INNODB_DATA_WRITTEN', 'INNODB_DATA_WRITES',
    'INNODB_ROWS_READ', 'INNODB_ROWS_INSERTED', 'INNODB_ROWS_UPDATED', 'INNODB_ROWS_DELETED',
    'INNODB_ROW_LOCK_WAITS', 'INNODB_ROW_LOCK_TIME', 'INNODB_LOG_WAITS',
    'QUESTIONS', 'COM_SELECT', 'COM_INSERT', 'COM_UPDATE', 'COM_DELETE', 'COM_CALL_PROCEDURE',
    'SLOW_QUERIES', 'CREATED_TMP_TABLES', 'CREATED_TMP_DISK_TABLES',
    'THREADS_CONNECTED', 'THREADS_RUNNING', 'THREADS_CREATED',
    'TABLE_OPEN_CACHE_HITS', 'TABLE_OPEN_CACHE_MISSES',
    'ABORTED_CONNECTS', 'CONNECTIONS', 'BYTES_SENT', 'BYTES_RECEIVED',
    'OPENED_TABLES', 'HANDLER_READ_RND_NEXT', 'HANDLER_READ_NEXT',
]


def _ch():
    cfg = ConfigFileLoader(CONFIG_PATH, PROFILE)
    return clickhouse_connect.get_client(
        host=cfg['CLICKHOUSE_HOST'], port=int(cfg['CLICKHOUSE_PORT']),
        username=cfg['CLICKHOUSE_USERNAME'], password=cfg['CLICKHOUSE_PASSWORD'],
        database=cfg['CLICKHOUSE_DATABASE'],
        secure=str(cfg['CLICKHOUSE_INTERFACE']).lower() == 'https')


def _conectar():
    cfg = ConfigFileLoader(CONFIG_PATH, PROFILE)
    kw = dict(host=cfg['MYSQL_HOST'], port=int(cfg.get('MYSQL_PORT') or 3306),
              user=cfg['MYSQL_USER'], password=cfg['MYSQL_PASSWORD'],
              database=cfg['MYSQL_DATABASE'])
    try:
        import mysql.connector
        return mysql.connector.connect(autocommit=True, **kw)
    except ImportError:
        import pymysql
        return pymysql.connect(autocommit=True, **kw)


@custom
def perf_sampler(*args, **kwargs):
    fase = str(kwargs.get('fase') or 'antes').strip()
    nota = str(kwargs.get('nota') or '').strip()

    client = _ch()
    client.command("""
        CREATE TABLE IF NOT EXISTS {} (
            ts          DateTime DEFAULT now(),
            fase        LowCardinality(String),
            metrica     LowCardinality(String),
            valor       Int64,
            nota        String
        ) ENGINE = MergeTree ORDER BY (metrica, ts)
    """.format(TABLA))

    cnx = _conectar()
    cur = cnx.cursor()

    lista = "','".join(METRICAS)
    cur.execute(
        "SELECT VARIABLE_NAME, VARIABLE_VALUE FROM information_schema.GLOBAL_STATUS "
        "WHERE VARIABLE_NAME IN ('{}')".format(lista))
    filas = cur.fetchall()

    # el tamano del pool tambien se guarda: es lo que estamos cambiando
    cur.execute("SELECT VARIABLE_NAME, VARIABLE_VALUE FROM information_schema.GLOBAL_VARIABLES "
                "WHERE VARIABLE_NAME = 'INNODB_BUFFER_POOL_SIZE'")
    filas += cur.fetchall()
    cur.close(); cnx.close()

    buf = []
    for nombre, valor in filas:
        try:
            v = int(float(valor))
        except (TypeError, ValueError):
            continue
        buf.append([fase, str(nombre).upper(), v, nota])

    client.insert(TABLA, buf, column_names=['fase', 'metrica', 'valor', 'nota'])

    d = {r[1]: r[2] for r in buf}
    rr = d.get('INNODB_BUFFER_POOL_READ_REQUESTS', 0)
    rd = d.get('INNODB_BUFFER_POOL_READS', 0)
    hit = round(100.0 * (1 - rd / float(rr)), 3) if rr else None
    pool_mb = round(d.get('INNODB_BUFFER_POOL_SIZE', 0) / 1024.0 / 1024.0)

    print('muestra fase={} : {} metricas'.format(fase, len(buf)))
    print('  buffer pool          {} MB'.format(pool_mb))
    print('  hit ratio ACUMULADO  {}%   (el que importa es el del intervalo)'.format(hit))
    print('  wait_free            {}   (deberia ser 0)'.format(
        d.get('INNODB_BUFFER_POOL_WAIT_FREE')))
    print('  paginas libres       {} de {}'.format(
        d.get('INNODB_BUFFER_POOL_PAGES_FREE'), d.get('INNODB_BUFFER_POOL_PAGES_TOTAL')))
    print('  threads running      {}'.format(d.get('THREADS_RUNNING')))
    print('  uptime               {} s'.format(d.get('UPTIME')))

    return {'fase': fase, 'metricas': len(buf), 'pool_mb': pool_mb,
            'hit_acumulado': hit, 'wait_free': d.get('INNODB_BUFFER_POOL_WAIT_FREE'),
            'uptime': d.get('UPTIME')}
