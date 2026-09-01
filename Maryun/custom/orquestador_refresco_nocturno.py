"""orquestador_refresco_nocturno: las 43 corridas manuales del full reload en UN solo run.

Reemplaza la secuencia que hasta hoy se lanzaba a mano, pipeline por pipeline y
variable por variable: 9 espejos por init/load/swap, el full load de
tab_sku_precios y el kardex PMP. ~13 minutos, 43 etapas, estrictamente en orden.

    1..3    nc_aux          init + 1 load + swap
    4..7    ingresos_aux    init + 2 loads + swap
    8..20   pedidos_aux     init + 11 loads + swap
    21..23  tab_bodegas     init + 1 load + swap
    24..26  tab_sku         init + 1 load + swap
    27..29  almacenaje      init + 1 load + swap
    30..32  mstr_nc         init + 1 load + swap
    33..35  mstr_ingresos   init + 1 load + swap
    36..41  mstr_pedidos    init + 4 loads + swap
    42      tab_sku_precios full load atomico
    43      kardex PMP      universo completo + verificacion final


COMO EJECUTA LA LOGICA DE LOS OTROS PIPELINES: CAMINO (a), NO (b) NI (c)
========================================================================
Se eligio (a): IMPORTAR los bloques de los otros pipelines desde sus archivos en
/home/src/Maryun y llamar a sus funciones. Aca NO hay una sola linea copiada de
esos bloques: se leen sus .py del disco, se ejecutan con exec() inyectando los
decoradores (@data_loader/@transformer/@data_exporter/@custom) para capturar la
funcion, y se la invoca con los kwargs que Mage les habria pasado como variables
de runtime. Es exactamente el mecanismo que usa Mage internamente para correr un
bloque, asi que no depende de detalles de la libreria mas alla del propio exec.

Consecuencia directa: si manana alguien corrige mysis_aux_lote.py o
kardex_pmp_sql.py, este orquestador corre la version corregida sin tocarse. Hay
UNA sola definicion de cada cosa, y no vive aca.

Por que NO (c), disparar los otros pipelines por la API de Mage:
  * obligaria a crear triggers de tipo API sobre los cuatro pipelines de recarga,
    que estan explicitamente congelados: acaban de validarse en produccion;
  * un pipeline_run hijo lo ejecuta el scheduler, no este proceso. Si el pool de
    ejecutores esta ocupado (y lo esta: esta misma corrida ocupa un slot), los
    hijos quedan encolados detras del padre y el padre los espera. Deadlock
    silencioso hasta el poll_timeout;
  * 43 pipeline_runs son 43 arranques de proceso. Sobre ~13 minutos de trabajo
    real, el overhead por corrida es una fraccion enorme y perfectamente evitable;
  * el fallo se detecta por polling de estado en vez de por una excepcion, con lo
    que abortar limpio es mas fragil, no menos.

Por que NO (b), copiar la logica: 3.500 lineas duplicadas, dos verdades sobre la
precision decimal y garantia de que dentro de un mes divergen. No hizo falta:
(a) funciona.

EL UNICO MATIZ, y esta razonado: la etapa 42 (tab_sku_precios)
--------------------------------------------------------------
Ese pipeline son 3 bloques: un data_loader SQL (`SELECT * FROM tab_sku_precios`),
un transformer que devuelve el dato tal cual, y el data_exporter que hace TODO el
trabajo (tipado decimal, staging, EXCHANGE TABLES, guardas). El transformer y el
exporter se importan y se llaman igual que el resto. El bloque SQL no tiene
funcion Python que importar, asi que aca se emite el SELECT contra MySis, pero:

  * la lista de columnas NO esta escrita a mano: sale de INSERT_COLS del propio
    exporter, y cuales son decimales sale de sus DECIMAL_COLS_*. Si alguien agrega
    una columna alli, esta etapa la arrastra sola;
  * las columnas decimales se leen con CAST(... AS CHAR), la misma defensa que
    usan mysis_aux_lote y mysis_maestros_lote. No es un adorno: pd.read_sql trae
    coerce_float=True por defecto y convierte decimal.Decimal en float64 sin
    avisar, y clickhouse_connect TRUNCA hacia cero al insertar un float en una
    columna Decimal(18,2): Decimal('1039.05') termina almacenado como 1039.04, y
    ese centavo se propaga a todo el kardex PMP posterior del par (sku, sucursal);
  * de paso desaparece el round-trip del DataFrame por el almacen de variables de
    Mage entre bloque y bloque, que es justamente donde una columna Decimal puede
    volver como float64 (ver la nota de mysis_aux_lote). Aca el DataFrame va de la
    lectura al exporter en memoria, sin pasar por disco;
  * el preflight verifica que dl_mysis_tab_sku_precios.sql siga apuntando a
    tab_sku_precios; si alguien cambia esa consulta, esta etapa falla antes de
    tocar nada en lugar de cargar otra cosa en silencio.


ORDEN RESPECTO DE LOS 9 INCREMENTALES: IMPORTA, Y MUCHO
=======================================================
Los 9 pipelines mysis_tabla_* siguen activos y disparan a las 00:00 UTC (sus
triggers dicen "@ 01:00", "@ 02:10", "@ 02:20"... pero el intervalo real es
@daily, que en Mage es 00:00 UTC; los nombres mienten).

Este orquestador tiene que correr DESPUES de ellos. La recarga completa deja cada
espejo exacto contra MySis, y el EXCHANGE TABLES descarta cualquier cosa que los
incrementales hubieran insertado antes: no se pierde nada, el full reload es un
superconjunto. Al reves si duele: si el orquestador corriera primero, los
incrementales escribirian encima de tablas ya exactas y reintroducirian los
duplicados fisicos y las filas viejas del ReplacingMergeTree que la recarga
acababa de limpiar, y el kardex quedaria calculado sobre espejos limpios que
dejan de estarlo diez minutos despues.


VARIABLES DE RUNTIME
====================
    desde_etapa   int, default 0. Retomar desde esa etapa (1..43) sin repetir lo
                  ya hecho. 0 o 1 = correr todo.
    solo          '' (todo) | 'espejos' (etapas 1..42) | 'kardex' (etapa 43).
    forzar_etapa  int, default 0. Aplica force=true SOLO a esa etapa, y solo si es
                  un 'load'. Es la salida para un load que murio a mitad de camino
                  y dejo filas sueltas en el staging.
    check_sums    'false' para saltear la comparacion de sumas contra MySis en los
                  swaps (es un full scan del origen; informativo, nunca bloquea).
                  Default true, igual que en las corridas manuales.
    dry_run       'true' imprime el plan, corre el preflight y termina SIN tocar
                  absolutamente nada. Para validar el mecanismo en dos segundos.


IDEMPOTENCIA
============
Correrlo dos veces seguidas deja el mismo resultado. No es casualidad:
  * cada tabla arranca por 'init', que hace DROP + CREATE del staging: cualquier
    resto de una corrida anterior fallida se borra antes de empezar;
  * los 'load' son idempotentes por rango: un rango ya cargado se niega a
    duplicarse (y con forzar_etapa se borra y se recarga);
  * el 'swap' es un EXCHANGE TABLES, atomico, y el destino queda igual al origen;
  * tab_sku_precios es full load con EXCHANGE TABLES;
  * el kardex hace TRUNCATE + INSERT del universo completo.
Nada acumula. Lo unico que cambia entre dos corridas es lo que MySis haya movido
en el medio, que es precisamente lo que se quiere reflejar.


SI FALLA
========
Se aborta en el acto: no se sigue a la etapa siguiente y el kardex NO se
recalcula, de modo que dwh.mysis_pmp_detalle nunca queda mezclando espejos nuevos
con espejos a medio cargar. Los espejos ya intercambiados son validos (el swap es
atomico); los que quedaron a medias viven en su tabla _stg_lote y no los ve nadie.
El mensaje de error dice la etapa exacta y el desde_etapa con el que retomar.
Ademas, justo antes del kardex se verifica que no haya quedado ningun staging
suelto: un _stg_lote vivo significa un swap que nunca ocurrio.
"""

import os
import time
import traceback
from datetime import datetime

from mage_ai.io.config import ConfigFileLoader
from mage_ai.io.mysql import MySQL

if 'custom' not in globals():
    from mage_ai.data_preparation.decorators import custom


REPO_PATH = '/home/src/Maryun'

# Bloques que se reutilizan: clave -> (pipeline, bloque, subdirectorio de Mage).
BLOQUES = {
    'aux': (
        'mysis_full_reload_aux_to_clickhouse', 'mysis_aux_lote', 'data_loaders'),
    'maestros': (
        'mysis_full_reload_maestros_to_clickhouse', 'mysis_maestros_lote', 'data_loaders'),
    'precios_tr': (
        'mysis_tabla_tab_sku_precios_to_clickhouse', 'tr_mysis_tab_sku_precios', 'transformers'),
    'precios_de': (
        'mysis_tabla_tab_sku_precios_to_clickhouse', 'de_mysis_tab_sku_precios_to_clickhouse',
        'data_exporters'),
    'kardex': (
        'mysis_pmp_kardex_to_clickhouse', 'kardex_pmp_sql', 'custom'),
}

# El bloque SQL de tab_sku_precios: no se ejecuta, pero se verifica que no haya
# cambiado de tabla origen a espaldas de esta etapa.
PRECIOS_SQL_FILE = os.path.join(REPO_PATH, 'data_loaders', 'dl_mysis_tab_sku_precios.sql')
PRECIOS_SOURCE = 'tab_sku_precios'

# Lotes medidos sobre los conteos reales de MySis del 2026-08-14. Son los mismos
# rangos que se corrieron a mano y que estan documentados en el BATCHES de cada
# bloque; aca se repiten porque el orquestador necesita el ORDEN, no la logica.
LOTES_NC_AUX = [(1, 999999999)]
LOTES_INGRESOS_AUX = [(1, 300000), (300000, 999999999)]
LOTES_PEDIDOS_AUX = [
    (1, 300000), (300000, 600000), (600000, 1050000), (1050000, 1500000),
    (1500000, 1975000), (1975000, 2450000), (2450000, 2950000), (2950000, 3550000),
    (3550000, 4175000), (4175000, 4800000), (4800000, 999999999),
]
LOTE_UNICO = [(1, 999999999)]
LOTES_MSTR_PEDIDOS = [(1, 300000), (300000, 600000), (600000, 900000), (900000, 999999999)]

# (familia, tabla, lotes). El orden es el validado en produccion: primero las tres
# _aux (las pesadas), despues las maestras, de la mas chica a la mas grande.
ESPEJOS = [
    ('aux', 'nc_aux', LOTES_NC_AUX),
    ('aux', 'ingresos_aux', LOTES_INGRESOS_AUX),
    ('aux', 'pedidos_aux', LOTES_PEDIDOS_AUX),
    ('maestros', 'tab_bodegas', LOTE_UNICO),
    ('maestros', 'tab_sku', LOTE_UNICO),
    ('maestros', 'almacenaje', LOTE_UNICO),
    ('maestros', 'mstr_nc', LOTE_UNICO),
    ('maestros', 'mstr_ingresos', LOTE_UNICO),
    ('maestros', 'mstr_pedidos', LOTES_MSTR_PEDIDOS),
]

# Verificacion final (requisito duro). Medido el 2026-08-14: 2.644.791 filas y
# 69.946 pares. Los pisos dejan ~24% y ~14% de margen: no saltan por la deriva
# normal de un dia, pero si por un espejo que se quedo corto.
MIN_FILAS_PMP = 2000000
MIN_PARES_PMP = 60000

# Patron de las tablas de staging de los full reload. Si alguna sigue viva cuando
# toca el kardex, hubo un swap que no ocurrio.
STAGING_LIKE = ['mysis_%\\_stg\\_lote', 'mysis_%\\_stg']

_MODULOS = {}


# --------------------------------------------------------------------------- utils

def _as_int(v, default=0):
    if v is None or v == '':
        return default
    return int(str(v).strip())


def _as_bool(v, default=False):
    if v is None or v == '':
        return default
    return str(v).strip().lower() in ('1', 'true', 't', 'yes', 'y', 'si')


def _miles(n):
    try:
        return '{:,}'.format(int(n)).replace(',', '.')
    except (TypeError, ValueError):
        return str(n)


def _dur(seg):
    seg = int(round(seg))
    h, resto = divmod(seg, 3600)
    m, s = divmod(resto, 60)
    if h:
        return '{}h {:02d}m {:02d}s'.format(h, m, s)
    if m:
        return '{}m {:02d}s'.format(m, s)
    return '{}s'.format(s)


# ------------------------------------------------- carga de los bloques ajenos

def _cargar(clave):
    """Lee el .py del bloque y devuelve (funcion_del_bloque, namespace).

    Mismo mecanismo que usa Mage: se inyectan los decoradores como capturadores y
    se ejecuta el archivo. La funcion decorada queda en la lista sin que haga
    falta conocer su nombre (run, run_kardex, export_data_to_clickhouse...).
    @test se deja pasar sin capturar: no es la funcion del bloque.

    Ninguno de estos archivos abre conexiones al importarse: solo definen
    constantes y funciones, asi que exec() no tiene efectos secundarios.
    """
    if clave in _MODULOS:
        return _MODULOS[clave]

    pipeline_uuid, block_uuid, subdir = BLOQUES[clave]
    ruta = os.path.join(REPO_PATH, subdir, block_uuid + '.py')
    if not os.path.exists(ruta):
        raise Exception(
            'No se encontro el archivo del bloque {} (pipeline {}) en {}. El '
            'orquestador reutiliza esos archivos: si el bloque se movio o se '
            'renombro, hay que actualizar BLOQUES.'.format(block_uuid, pipeline_uuid, ruta))

    with open(ruta, 'r', encoding='utf-8') as fh:
        fuente = fh.read()

    capturadas = []

    def _captura(fn):
        capturadas.append(fn)
        return fn

    def _pasa(fn):
        return fn

    ns = {'__name__': 'orq__' + block_uuid, '__file__': ruta}
    for nombre in ('data_loader', 'transformer', 'data_exporter', 'custom', 'sensor'):
        ns[nombre] = _captura
    ns['test'] = _pasa

    exec(compile(fuente, ruta, 'exec'), ns)

    if len(capturadas) != 1:
        raise Exception(
            'Se esperaba exactamente una funcion decorada en {} y se encontraron {}. '
            'El bloque cambio de forma: revisar antes de correr.'.format(ruta, len(capturadas)))

    _MODULOS[clave] = (capturadas[0], ns)
    return _MODULOS[clave]


def _ch_client():
    """Cliente ClickHouse para los chequeos del orquestador.

    Se toma prestada la fabrica del bloque del kardex en vez de repetir la
    lectura de io_config.yaml: una sola definicion de la conexion.
    """
    _, ns = _cargar('kardex')
    return ns['_client']()


# ------------------------------------------------------------------- ejecutores

def _ejecutar_aux(**kw):
    fn, _ = _cargar('aux')
    return fn(**kw)


def _ejecutar_maestros(**kw):
    fn, _ = _cargar('maestros')
    return fn(**kw)


def _ejecutar_precios(**kw):
    """tab_sku_precios: lectura + transformer + exporter originales.

    El SELECT se arma con las constantes del propio exporter y con los decimales
    en texto. El porque esta en el docstring de arriba (PRECISION DECIMAL).
    """
    transformar, _ = _cargar('precios_tr')
    exportar, ns = _cargar('precios_de')

    columnas = list(ns['INSERT_COLS'])
    decimales = set(ns.get('DECIMAL_COLS_NOTNULL') or set())
    decimales |= set(ns.get('DECIMAL_COLS_NULLABLE') or set())

    proyeccion = ', '.join(
        'CAST(`{c}` AS CHAR) AS `{c}`'.format(c=c) if c in decimales else '`{c}`'.format(c=c)
        for c in columnas
    )
    sql = 'SELECT {} FROM {}'.format(proyeccion, PRECIOS_SOURCE)
    print('      lectura: {}'.format(sql))
    print('      decimales leidos como texto: {}'.format(
        sorted(c for c in columnas if c in decimales) or 'ninguno'))

    cfg = ConfigFileLoader(ns['CONFIG_PATH'], ns['PROFILE'])
    with MySQL.with_config(cfg) as loader:
        n_src = int(loader.load(
            'SELECT COUNT(*) n FROM {}'.format(PRECIOS_SOURCE)).iloc[0]['n'])
        df = loader.load(sql)
    print('      MySis: {} filas en el origen, {} traidas'.format(
        _miles(n_src), _miles(len(df))))

    # Esta es la unica lectura del orquestador que se trae una tabla entera de un
    # viaje (~470k filas). Si el conector aplicara un LIMIT por defecto, la carga
    # entraria corta y el EXCHANGE dejaria una tabla de precios incompleta: el
    # costo de todo ingreso sin precio saldria 0 y, por la regla "IF v_costo = 0
    # THEN v_costo = v_cpp", el kardex se valorizaria mal SIN dar error. Se
    # compara contra COUNT(*) con el mismo margen de deriva que usan los swaps
    # (5 por mil, piso 10 filas), que absorbe lo que produccion mueva entre las
    # dos consultas pero no una truncacion.
    faltan = n_src - len(df)
    if faltan > max(n_src * 5 // 1000, 10):
        raise Exception(
            'La lectura de {} trajo {} filas contra {} en el origen (faltan {}). '
            'Parece una truncacion, no deriva de produccion. NO se toca '
            'dwh.mysis_tab_sku_precios.'.format(
                PRECIOS_SOURCE, _miles(len(df)), _miles(n_src), _miles(faltan)))

    df = transformar(df)
    return exportar(df, **kw)


def _ejecutar_kardex(**kw):
    fn, _ = _cargar('kardex')
    return fn(**kw)


EJECUTORES = {
    'aux': _ejecutar_aux,
    'maestros': _ejecutar_maestros,
    'precios': _ejecutar_precios,
    'kardex': _ejecutar_kardex,
}


# ------------------------------------------------------------------------- plan

def _construir_plan(check_sums):
    plan = []

    def agregar(grupo, nombre, ejecutor, kwargs):
        plan.append({
            'n': len(plan) + 1,
            'grupo': grupo,
            'nombre': nombre,
            'ejecutor': ejecutor,
            'kwargs': kwargs,
        })

    for familia, tabla, lotes in ESPEJOS:
        agregar('espejos', '{} init'.format(tabla), familia,
                {'tabla': tabla, 'mode': 'init'})
        for pk_from, pk_to in lotes:
            agregar('espejos', '{} load [{}, {})'.format(tabla, pk_from, pk_to), familia,
                    {'tabla': tabla, 'mode': 'load', 'pk_from': pk_from, 'pk_to': pk_to})
        agregar('espejos', '{} swap'.format(tabla), familia,
                {'tabla': tabla, 'mode': 'swap', 'check_sums': check_sums})

    agregar('espejos', 'tab_sku_precios full load', 'precios', {})
    agregar('kardex', 'kardex PMP universo completo', 'kardex', {})
    return plan


def _filas_afectadas(res):
    if not isinstance(res, dict):
        return None
    for clave in ('rows_inserted', 'rows_after', 'rows', 'rows_staging'):
        if res.get(clave) is not None:
            return res[clave]
    return None


# -------------------------------------------------------------------- preflight

def _preflight():
    """Todo lo que puede estar mal, que este mal ANTES de escribir nada.

    Carga los cinco bloques (parsea y ejecuta sus definiciones), verifica que el
    .sql de precios siga apuntando a la misma tabla y que se llegue a ClickHouse.
    Si algo de esto falla, la corrida termina sin haber tocado un solo dato.
    """
    print('PREFLIGHT')
    for clave in ('aux', 'maestros', 'precios_tr', 'precios_de', 'kardex'):
        fn, _ = _cargar(clave)
        pipeline_uuid, block_uuid, subdir = BLOQUES[clave]
        print('  ok  {:<10} {}/{}.py  ->  {}()'.format(
            clave, subdir, block_uuid, getattr(fn, '__name__', '?')))

    if not os.path.exists(PRECIOS_SQL_FILE):
        raise Exception('No se encontro {}.'.format(PRECIOS_SQL_FILE))
    with open(PRECIOS_SQL_FILE, 'r', encoding='utf-8') as fh:
        sql_original = fh.read()
    if PRECIOS_SOURCE not in sql_original:
        raise Exception(
            'El bloque SQL {} ya no menciona {}. La etapa de tab_sku_precios '
            'estaria leyendo una tabla distinta de la que lee el pipeline '
            'original: revisar antes de seguir.'.format(PRECIOS_SQL_FILE, PRECIOS_SOURCE))
    print('  ok  data_loaders/dl_mysis_tab_sku_precios.sql sigue leyendo {}'.format(
        PRECIOS_SOURCE))

    client = _ch_client()
    n = client.query('SELECT count() FROM dwh.mysis_pmp_detalle').result_rows[0][0]
    print('  ok  ClickHouse responde. dwh.mysis_pmp_detalle tiene hoy {} filas'.format(
        _miles(n)))
    return client


def _verificar_sin_staging(client):
    """Ningun _stg vivo antes del kardex: uno vivo = un swap que no paso."""
    condicion = ' OR '.join("name LIKE '{}'".format(p) for p in STAGING_LIKE)
    filas = client.query(
        "SELECT name FROM system.tables WHERE database='dwh' AND ({})".format(condicion)
    ).result_rows
    if filas:
        nombres = ', '.join(r[0] for r in filas)
        raise Exception(
            'Quedaron tablas de staging vivas en dwh antes de calcular el kardex: {}. '
            'Eso significa que algun espejo se cargo pero NUNCA se intercambio, asi que '
            'los espejos no estan todos al dia. NO se recalcula el kardex: '
            'dwh.mysis_pmp_detalle se deja como esta, que al menos es consistente. '
            'Rehacer la tabla afectada desde su init.'.format(nombres))
    print('      sin stagings sueltos en dwh: los espejos estan todos intercambiados')


def _verificacion_final(client):
    filas, pares = client.query(
        'SELECT count(), uniqExact((sucursal_id, sku)) FROM dwh.mysis_pmp_detalle'
    ).result_rows[0]
    print('VERIFICACION FINAL  dwh.mysis_pmp_detalle: {} filas, {} pares '
          '(pisos: {} y {})'.format(_miles(filas), _miles(pares),
                                    _miles(MIN_FILAS_PMP), _miles(MIN_PARES_PMP)))
    if filas <= MIN_FILAS_PMP or pares <= MIN_PARES_PMP:
        raise Exception(
            'KARDEX SOSPECHOSO: dwh.mysis_pmp_detalle quedo con {} filas y {} pares, '
            'por debajo del piso ({} filas / {} pares). El kardex termino sin error, '
            'asi que el problema esta AGUAS ARRIBA: algun espejo quedo corto o vacio y '
            'el kardex se calculo sobre menos movimientos de los que hay. Revisar los '
            'conteos de dwh.mysis_mstr_* contra MySis y rehacer el espejo que no '
            'cierre.'.format(_miles(filas), _miles(pares),
                             _miles(MIN_FILAS_PMP), _miles(MIN_PARES_PMP)))
    return filas, pares


# ------------------------------------------------------------------------ entrada

@custom
def refresco_nocturno(*args, **kwargs):
    desde_etapa = _as_int(kwargs.get('desde_etapa'), 0)
    forzar_etapa = _as_int(kwargs.get('forzar_etapa'), 0)
    solo = str(kwargs.get('solo') or '').strip().lower()
    check_sums = _as_bool(kwargs.get('check_sums'), True)
    dry_run = _as_bool(kwargs.get('dry_run'), False)

    if solo not in ('', 'espejos', 'kardex'):
        raise Exception("solo debe ser 'espejos', 'kardex' o vacio; llego '{}'.".format(solo))

    plan = _construir_plan(check_sums)
    total = len(plan)
    if desde_etapa > total:
        raise Exception(
            'desde_etapa={} y el plan tiene {} etapas. No hay nada que correr.'.format(
                desde_etapa, total))

    inicio = datetime.utcnow()
    print('=' * 80)
    print('mysis_refresco_nocturno   inicio {} UTC'.format(inicio.strftime('%Y-%m-%d %H:%M:%S')))
    print('  etapas={}  desde_etapa={}  solo={}  forzar_etapa={}  check_sums={}  '
          'dry_run={}'.format(total, desde_etapa or 1, solo or 'todo',
                              forzar_etapa or '-', check_sums, dry_run))
    print('=' * 80)

    client = _preflight()

    pendientes = [
        e for e in plan
        if e['n'] >= max(desde_etapa, 1) and (not solo or e['grupo'] == solo)
    ]
    saltadas = total - len(pendientes)

    if dry_run:
        print('-' * 80)
        print('DRY RUN: plan que se habria ejecutado ({} etapas, {} salteadas)'.format(
            len(pendientes), saltadas))
        for e in pendientes:
            print('  [{:>2}/{}] {:<38} {}'.format(e['n'], total, e['nombre'], e['kwargs']))
        print('-' * 80)
        print('No se toco nada. Sacar dry_run para correr de verdad.')
        return {'dry_run': True, 'etapas': len(pendientes), 'saltadas': saltadas,
                'plan': [{'n': e['n'], 'nombre': e['nombre']} for e in pendientes]}

    if not pendientes:
        raise Exception(
            'La combinacion desde_etapa={} / solo={} no deja ninguna etapa por correr.'.format(
                desde_etapa, solo or 'todo'))

    t0 = time.time()
    ejecutadas = []
    total_filas = 0

    for etapa in pendientes:
        n, nombre = etapa['n'], etapa['nombre']
        kw = dict(etapa['kwargs'])
        if forzar_etapa and n == forzar_etapa and kw.get('mode') == 'load':
            kw['force'] = True
            print('>>> etapa {} con force=true: se borra el rango del staging y se '
                  'recarga'.format(n))

        # El kardex no arranca si quedo algun espejo a medio intercambiar.
        if etapa['ejecutor'] == 'kardex':
            _verificar_sin_staging(client)

        print('-' * 80)
        print('>>> [{:>2}/{}] {}'.format(n, total, nombre))
        t = time.time()
        try:
            res = EJECUTORES[etapa['ejecutor']](**kw)
        except Exception as exc:
            dur = time.time() - t
            pipeline_uuid, block_uuid, _ = (
                BLOQUES[etapa['ejecutor']] if etapa['ejecutor'] in BLOQUES
                else BLOQUES['precios_de'])
            print(traceback.format_exc())
            print('#' * 80)
            print('FALLO LA ETAPA {}/{}: {}'.format(n, total, nombre))
            print('  bloque    : {} / {}'.format(pipeline_uuid, block_uuid))
            print('  variables : {}'.format(kw))
            print('  duracion  : {}'.format(_dur(dur)))
            print('  error     : {}: {}'.format(type(exc).__name__, exc))
            print('')
            print('  SE ABORTA TODO. El kardex NO se recalculo: dwh.mysis_pmp_detalle')
            print('  sigue con los datos de la corrida anterior, consistentes entre si.')
            print('  Los espejos ya intercambiados en esta corrida son validos (el swap')
            print('  es atomico); el que quedo a medias vive en su tabla _stg_lote y no')
            print('  lo lee nadie.')
            print('')
            print('  PARA RETOMAR, una vez arreglada la causa:')
            print('      desde_etapa = {}'.format(n))
            if kw.get('mode') == 'load':
                print('  Si esta etapa alcanzo a insertar filas antes de morir, el reintento')
                print('  va a decir "ESTE LOTE YA SE CORRIO". En ese caso, ademas:')
                print('      forzar_etapa = {}'.format(n))
            print('  Quedaban por correr las etapas {}..{}.'.format(n, total))
            print('#' * 80)
            raise Exception(
                'mysis_refresco_nocturno abortado en la etapa {}/{} ({}). Retomar con '
                'desde_etapa={}{}. Causa: {}: {}'.format(
                    n, total, nombre, n,
                    ', forzar_etapa={}'.format(n) if kw.get('mode') == 'load' else '',
                    type(exc).__name__, exc))

        dur = time.time() - t
        filas = _filas_afectadas(res)
        if isinstance(filas, int):
            total_filas += filas
        print('<<< [{:>2}/{}] {:<38} filas={:<12} {}'.format(
            n, total, nombre, _miles(filas) if filas is not None else 'n/d', _dur(dur)))
        ejecutadas.append({'n': n, 'nombre': nombre, 'filas': filas,
                           'segundos': round(dur, 1)})

    duracion = time.time() - t0

    print('=' * 80)
    print('RESUMEN mysis_refresco_nocturno')
    print('  {:>3}  {:<40} {:>14}  {:>10}'.format('#', 'etapa', 'filas', 'duracion'))
    for e in ejecutadas:
        print('  {:>3}  {:<40} {:>14}  {:>10}'.format(
            e['n'], e['nombre'],
            _miles(e['filas']) if e['filas'] is not None else 'n/d',
            _dur(e['segundos'])))
    print('  ' + '-' * 74)
    print('  etapas ejecutadas : {} de {} (salteadas {})'.format(
        len(ejecutadas), total, saltadas))
    print('  duracion total    : {}'.format(_dur(duracion)))

    filas_pmp = pares_pmp = None
    if solo == 'espejos':
        filas_pmp, pares_pmp = client.query(
            'SELECT count(), uniqExact((sucursal_id, sku)) FROM dwh.mysis_pmp_detalle'
        ).result_rows[0]
        print('  AVISO: solo=espejos, el kardex NO se recalculo. '
              'dwh.mysis_pmp_detalle sigue en {} filas / {} pares, calculado sobre los '
              'espejos ANTERIORES.'.format(_miles(filas_pmp), _miles(pares_pmp)))
        print('  Correr la etapa 43 (solo=kardex) para ponerlo al dia.')
    else:
        filas_pmp, pares_pmp = _verificacion_final(client)

    print('  dwh.mysis_pmp_detalle : {} filas, {} pares'.format(
        _miles(filas_pmp), _miles(pares_pmp)))
    print('  fin {} UTC'.format(datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')))
    print('=' * 80)

    return {
        'ok': True,
        'etapas_ejecutadas': len(ejecutadas),
        'etapas_totales': total,
        'etapas_salteadas': saltadas,
        'duracion_segundos': round(duracion, 1),
        'duracion': _dur(duracion),
        'filas_escritas': total_filas,
        'pmp_detalle_filas': filas_pmp,
        'pmp_detalle_pares': pares_pmp,
        'solo': solo or 'todo',
        'desde_etapa': desde_etapa or 1,
        'etapas': ejecutadas,
    }
