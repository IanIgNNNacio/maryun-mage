"""calls_masivo: corre CALL calculadora_pmp para TODO el universo (sucursal, sku).

QUE HACE Y QUE ESCRIBE
----------------------
Ejecuta en MySis, para cada par (sucursal_id, sku):

    CALL calculadora_pmp('<sku>', <sucursal_id>);

El procedure, por cada llamada:
    1. crea la temporal tmp_pmp_detalle           (por conexion, se borra sola)
    2. recorre el cursor: UNION de mstr_ingresos+aux, mstr_pedidos+aux y mstr_nc+aux
    3. DELETE FROM pmp_detalle WHERE sku=? AND sucursal_id=?     <-- ESCRIBE
    4. INSERT INTO pmp_detalle, una fila por movimiento          <-- ESCRIBE
    5. upsert de una fila en pmp_resumen                         <-- ESCRIBE

ESCRIBE UNICAMENTE en mryn_data.pmp_detalle y mryn_data.pmp_resumen. No toca ninguna
tabla operativa. Los dos tienen indice por (sku, sucursal_id), asi que el DELETE del
paso 3 y el upsert del paso 5 son puntuales, no full scans.

CRECIMIENTO ESPERADO DE pmp_detalle
    Hoy: 24.401 filas / 5 MB con 53 pares calculados.
    Al universo completo: ~2.650.000 filas -> del orden de 540 MB.
    El esquema mryn_data completo pesa hoy 10.110 MB, o sea +5%.
    VERIFICAR DISCO LIBRE ANTES DE CORRER EL MODO COMPLETO.

POR QUE NO ABORTA AL PRIMER ERROR
    El procedure tiene una division sin proteccion en la rama de ingreso
    (SET v_cpp = v_saldovalor / v_saldoqty, sin el IF v_saldoqty <> 0 que si tienen
    las otras dos ramas). Con ERROR_FOR_DIVISION_BY_ZERO en el sql_mode, dentro del
    INSERT eso aborta la sentencia con el error 1365. Se conocen 2 pares asi. En una
    corrida de 70.000 CALL, abortar en el primero perderia toda la noche: aca cada
    CALL va en su propio try, el error se registra y la corrida sigue. La lista final
    de errores es un resultado en si misma.

MODOS (kwarg `modo`)
    estado    (default) no ejecuta ni un CALL. Informa universo, avance y lo que falta.
    muestra   corre `muestra_n` pares, mide, extrapola al universo y PARA.
              Correr SIEMPRE este primero: es el unico modo de saber cuanto va a tardar.
    completo  corre todo lo que falte, respetando `limite_minutos`.

OTROS kwargs
    corrida            etiqueta de la corrida, para reanudar. Default 'full-2026-08-19'.
    workers            conexiones paralelas contra MySis. Default 4.
                       Las temporales del procedure son por conexion, asi que no chocan.
                       Cada par toca filas distintas: no hay contencion entre workers.
    muestra_n          pares del modo muestra. Default 300.
    limite_minutos     presupuesto de tiempo. Al vencer, corta limpio. Default 600.
    incluir_sin_kardex 'true' agrega los pares que existen en almacenaje y no en el
                       kardex (~3.700). El CALL les escribe un pmp_resumen con el
                       descuadre. Default 'false'.
    reintentos         reintentos por par ante error transitorio (no ante 1365). Default 1.

EL AVANCE SE GUARDA EN CLICKHOUSE, NO EN MySis
    dwh.mysis_calls_log. Asi MySis solo recibe los CALL y nada mas. Reanudar es
    volver a lanzar con la misma `corrida`: lo que ya salio ok no se repite.
"""

import time
import threading
import queue as _queue

import clickhouse_connect
from mage_ai.io.config import ConfigFileLoader

if 'custom' not in globals():
    from mage_ai.data_preparation.decorators import custom

CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'
LOG_TABLE = 'dwh.mysis_calls_log'


def _as_int(v, d):
    if v is None or str(v).strip() == '':
        return d
    return int(str(v).strip())


def _as_bool(v, d=False):
    if v is None or str(v).strip() == '':
        return d
    return str(v).strip().lower() in ('1', 'true', 't', 'yes', 'y', 'si')


def _miles(n):
    return '{:,}'.format(int(n)).replace(',', '.')


def _dur(seg):
    seg = int(seg)
    if seg < 60:
        return '{}s'.format(seg)
    if seg < 3600:
        return '{}m {}s'.format(seg // 60, seg % 60)
    return '{}h {}m'.format(seg // 3600, (seg % 3600) // 60)


def _ch():
    cfg = ConfigFileLoader(CONFIG_PATH, PROFILE)
    return clickhouse_connect.get_client(
        host=cfg['CLICKHOUSE_HOST'], port=int(cfg['CLICKHOUSE_PORT']),
        username=cfg['CLICKHOUSE_USERNAME'], password=cfg['CLICKHOUSE_PASSWORD'],
        database=cfg['CLICKHOUSE_DATABASE'],
        secure=str(cfg['CLICKHOUSE_INTERFACE']).lower() == 'https')


def _mysql_cfg():
    cfg = ConfigFileLoader(CONFIG_PATH, PROFILE)
    return dict(
        host=cfg['MYSQL_HOST'], port=int(cfg.get('MYSQL_PORT') or 3306),
        user=cfg['MYSQL_USER'], password=cfg['MYSQL_PASSWORD'],
        database=cfg['MYSQL_DATABASE'])


def _conectar(c):
    """Una conexion nueva. Cada worker abre la suya: las temporales son por conexion."""
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


# ------------------------------------------------------------------ universo

SQL_UNIVERSO_KARDEX = """
SELECT sucursal_id, sku, toUInt32(count()) AS filas
FROM dwh.mysis_pmp_detalle
WHERE sku != ''
GROUP BY sucursal_id, sku
"""

SQL_UNIVERSO_EXTRA = """
SELECT bodega_id AS sucursal_id, sku, toUInt32(0) AS filas
FROM dwh.mysis_almacenaje FINAL
WHERE sku != '' AND (bodega_id, sku) NOT IN (
    SELECT sucursal_id, sku FROM dwh.mysis_pmp_detalle GROUP BY sucursal_id, sku)
GROUP BY bodega_id, sku
"""


def _universo(client, incluir_sin_kardex):
    filas = client.query(SQL_UNIVERSO_KARDEX).result_rows
    pares = [(int(s), str(k), int(f)) for s, k, f in filas]
    if incluir_sin_kardex:
        extra = client.query(SQL_UNIVERSO_EXTRA).result_rows
        pares += [(int(s), str(k), 0) for s, k, _f in extra]
    # los mas pesados primero: con workers paralelos evita que la cola termine
    # esperando a un solo par grande.
    pares.sort(key=lambda t: (-t[2], t[0], t[1]))
    return pares


def _hechos(client, corrida):
    r = client.query(
        "SELECT sucursal_id, sku FROM {} WHERE corrida = %(c)s AND estado = 'ok'".format(LOG_TABLE),
        parameters={'c': corrida}).result_rows
    return set((int(a), str(b)) for a, b in r)


def _asegurar_log(client):
    client.command("""
        CREATE TABLE IF NOT EXISTS {} (
            corrida     String,
            sucursal_id Int32,
            sku         String,
            estado      LowCardinality(String),
            ms          UInt32,
            error       String,
            ts          DateTime DEFAULT now()
        ) ENGINE = MergeTree ORDER BY (corrida, sucursal_id, sku)
    """.format(LOG_TABLE))


# ------------------------------------------------------------------- worker

class _Estado(object):
    def __init__(self):
        self.lock = threading.Lock()
        self.ok = 0
        self.err = 0
        self.ms_total = 0
        self.parar = False
        self.buffer = []
        self.errores = []


def _worker(cola, est, mycfg, corrida, reintentos):
    cnx = None
    cur = None
    try:
        cnx = _conectar(mycfg)
        cur = cnx.cursor()
        while True:
            try:
                suc, sku, _f = cola.get_nowait()
            except _queue.Empty:
                return
            if est.parar:
                return

            t0 = time.time()
            estado, err = 'ok', ''
            intento = 0
            while True:
                try:
                    cur.execute("CALL calculadora_pmp(%s, %s)", (sku, suc))
                    # el procedure no devuelve result set, pero por las dudas
                    try:
                        while cur.nextset():
                            pass
                    except Exception:
                        pass
                    break
                except Exception as e:
                    err = '{}: {}'.format(type(e).__name__, e)[:400]
                    # 1365 = Division by 0. Es determinista: reintentar no sirve.
                    if '1365' in err or 'Division by 0' in err or intento >= reintentos:
                        estado = 'error'
                        break
                    intento += 1
                    time.sleep(0.5)
                    # una conexion rota no se recupera sola
                    try:
                        cur.close(); cnx.close()
                    except Exception:
                        pass
                    cnx = _conectar(mycfg)
                    cur = cnx.cursor()

            ms = int((time.time() - t0) * 1000)
            with est.lock:
                if estado == 'ok':
                    est.ok += 1
                else:
                    est.err += 1
                    if len(est.errores) < 200:
                        est.errores.append((suc, sku, err))
                est.ms_total += ms
                est.buffer.append([corrida, suc, sku, estado, ms, err])
    finally:
        try:
            if cur:
                cur.close()
            if cnx:
                cnx.close()
        except Exception:
            pass


def _volcar(client, est):
    with est.lock:
        buf, est.buffer = est.buffer, []
    if buf:
        client.insert(LOG_TABLE, buf,
                      column_names=['corrida', 'sucursal_id', 'sku', 'estado', 'ms', 'error'])
    return len(buf)


# -------------------------------------------------------------------- bloque

@custom
def calls_masivo(*args, **kwargs):
    modo = str(kwargs.get('modo') or 'estado').strip().lower()
    corrida = str(kwargs.get('corrida') or 'full-2026-08-19').strip()
    workers = max(1, _as_int(kwargs.get('workers'), 4))
    muestra_n = _as_int(kwargs.get('muestra_n'), 300)
    limite_min = _as_int(kwargs.get('limite_minutos'), 600)
    reintentos = _as_int(kwargs.get('reintentos'), 1)
    sin_kardex = _as_bool(kwargs.get('incluir_sin_kardex'), False)

    if modo not in ('estado', 'muestra', 'completo'):
        raise Exception("modo debe ser 'estado', 'muestra' o 'completo'; llego '{}'.".format(modo))

    client = _ch()
    _asegurar_log(client)

    universo = _universo(client, sin_kardex)
    hechos = _hechos(client, corrida)
    pendientes = [p for p in universo if (p[0], p[1]) not in hechos]
    filas_est = sum(p[2] for p in pendientes)

    print('=' * 66)
    print('CALL calculadora_pmp masivo   modo={}   corrida={}'.format(modo, corrida))
    print('=' * 66)
    print('universo            {} pares'.format(_miles(len(universo))))
    print('ya calculados       {} pares'.format(_miles(len(hechos))))
    print('pendientes          {} pares  (~{} filas de kardex)'.format(
        _miles(len(pendientes)), _miles(filas_est)))
    print('workers paralelos   {}'.format(workers))

    if modo == 'estado' or not pendientes:
        if not pendientes:
            print('\nNo queda nada pendiente para esta corrida.')
        else:
            print('\nModo estado: no se ejecuto ningun CALL.')
            print("Para medir cuanto tarda:  modo='muestra', muestra_n={}".format(muestra_n))
        return {'modo': modo, 'universo': len(universo), 'hechos': len(hechos),
                'pendientes': len(pendientes), 'ejecutados': 0}

    if modo == 'muestra':
        # muestra estratificada: se toma 1 de cada k a lo largo del universo ordenado
        # por peso, para que entren pares grandes, medianos y chicos en proporcion.
        k = max(1, len(pendientes) // muestra_n)
        lote = pendientes[::k][:muestra_n]
        print('muestra             {} pares (1 de cada {})'.format(_miles(len(lote)), k))
    else:
        lote = pendientes

    print('\nESCRIBE en mryn_data.pmp_detalle y mryn_data.pmp_resumen. Arrancando...\n')

    cola = _queue.Queue()
    for p in lote:
        cola.put(p)

    est = _Estado()
    t_ini = time.time()
    hilos = [threading.Thread(target=_worker,
                              args=(cola, est, _mysql_cfg(), corrida, reintentos),
                              daemon=True) for _ in range(workers)]
    for h in hilos:
        h.start()

    ultimo = 0
    while any(h.is_alive() for h in hilos):
        time.sleep(5)
        _volcar(client, est)
        transcurrido = time.time() - t_ini
        with est.lock:
            hechos_ahora = est.ok + est.err
        if hechos_ahora - ultimo >= 250 or transcurrido - ultimo == 0:
            if hechos_ahora != ultimo:
                vel = hechos_ahora / max(transcurrido, 1)
                falta = (len(lote) - hechos_ahora) / max(vel, 0.001)
                print('  {} / {}   {:.1f} pares/s   transcurrido {}   faltan ~{}'.format(
                    _miles(hechos_ahora), _miles(len(lote)), vel,
                    _dur(transcurrido), _dur(falta)))
                ultimo = hechos_ahora
        if transcurrido > limite_min * 60:
            print('\nLIMITE DE TIEMPO ({} min). Cortando limpio.'.format(limite_min))
            est.parar = True
            break

    for h in hilos:
        h.join(timeout=120)
    _volcar(client, est)

    transcurrido = time.time() - t_ini
    total = est.ok + est.err
    vel = total / max(transcurrido, 1)

    print('\n' + '-' * 66)
    print('ejecutados     {}  ({} ok, {} con error)'.format(
        _miles(total), _miles(est.ok), _miles(est.err)))
    print('tiempo         {}   ({:.2f} pares/s, {:.0f} ms por par en promedio)'.format(
        _dur(transcurrido), vel, (est.ms_total / max(total, 1))))

    if est.errores:
        print('\nERRORES (primeros {}):'.format(min(len(est.errores), 15)))
        for suc, sku, err in est.errores[:15]:
            print('  ({}, {}) -> {}'.format(suc, sku, err[:110]))

    extrapolacion = None
    if modo == 'muestra':
        restantes = len(pendientes) - total
        seg = restantes / max(vel, 0.001)
        extrapolacion = {'pares_restantes': restantes,
                         'segundos_estimados': int(seg),
                         'estimado_legible': _dur(seg),
                         'pares_por_segundo': round(vel, 2)}
        print('\n' + '=' * 66)
        print('EXTRAPOLACION AL UNIVERSO COMPLETO')
        print('=' * 66)
        print('  quedan {} pares a {:.2f} pares/s con {} workers'.format(
            _miles(restantes), vel, workers))
        print('  ESTIMADO: {}'.format(_dur(seg)))
        print('  proyeccion con otros workers. OJO: asume escalado lineal y NO lo es,')
        print('  porque MariaDB es una sola instancia y los workers compiten por I/O.')
        print('  Contar con 60-70% de esa mejora, no el 100%.')
        for w in (4, 6, 8, 12):
            if w != workers:
                ideal = seg * workers / float(w)
                real = seg - (seg - ideal) * 0.65
                print('    con {:>2} workers: ~{}   (optimista: {})'.format(
                    w, _dur(real), _dur(ideal)))
        print("\nSi el estimado entra en la ventana, relanzar con modo='completo'.")
        print('La muestra ya quedo registrada: el modo completo no la repite.')

    filas_mysis = None
    try:
        # cuantas filas quedaron escritas del lado de MySis (informativo)
        cnx = _conectar(_mysql_cfg())
        cur = cnx.cursor()
        cur.execute('SELECT COUNT(*) FROM pmp_detalle')
        filas_mysis = int(cur.fetchone()[0])
        cur.execute('SELECT COUNT(*) FROM pmp_resumen')
        pares_mysis = int(cur.fetchone()[0])
        cur.close(); cnx.close()
        print('\nmryn_data.pmp_detalle: {} filas   pmp_resumen: {} pares'.format(
            _miles(filas_mysis), _miles(pares_mysis)))
    except Exception as e:
        print('\nNo se pudo contar pmp_detalle: {}'.format(e))

    return {'modo': modo, 'corrida': corrida, 'universo': len(universo),
            'ejecutados': total, 'ok': est.ok, 'error': est.err,
            'segundos': int(transcurrido), 'pares_por_segundo': round(vel, 2),
            'filas_pmp_detalle_mysis': filas_mysis,
            'extrapolacion': extrapolacion}
