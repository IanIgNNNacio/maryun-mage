"""Carga por LOTES de las 6 tablas maestras de MySis a ClickHouse.

Hermano gemelo de mysis_full_reload_aux_to_clickhouse, mismo diseno y mismas
guardas, pero para los espejos que quedaban con carga incremental por dt_in.

UNA CORRIDA = UN (tabla, mode, rango). El orquestador lanza las corridas de a una.
El pipeline nunca lee 1,4M de filas de un viaje ni recorre las seis tablas.

Variables de runtime (Mage las entrega por kwargs):

    tabla    'tab_bodegas' | 'almacenaje' | 'tab_sku' | 'mstr_nc' |
             'mstr_ingresos' | 'mstr_pedidos'
    mode     'init' | 'load' | 'swap' | 'status'   (default 'status', que no escribe nada)
    pk_from  inicio del rango de PK, INCLUSIVE   (solo mode='load')
    pk_to    fin del rango de PK, EXCLUSIVE      (solo mode='load')
    force    'true' para recargar un rango ya cargado (borra el rango y lo reinserta)

    read_chunk    filas por SELECT contra MySis      (default por tabla, ver SPECS)
    insert_chunk  filas por INSERT contra ClickHouse (default 25.000)
    check_sums    'false' para saltarse la comparacion de sumas contra MySis en el
                  swap (es un full scan del origen; informativo, nunca bloquea)
    tol_por_mil   override del margen de deriva del swap (default por tabla)

MODOS
    init    DROP + CREATE TABLE <staging> AS <destino>. No lee MySis.
    load    lee SOLO [pk_from, pk_to) de MySis e inserta en el staging.
            Aborta si ese rango ya tiene filas en el staging, salvo force=true.
    swap    valida y hace EXCHANGE TABLES; despues DROP del staging.
    status  no escribe: informa MySis / destino / staging y que lote toca.

POR QUE UN SOLO BLOQUE Y NO loader -> transformer -> exporter
    Mage serializa la salida de cada bloque a disco entre bloques. Un DataFrame con
    columnas Decimal round-tripeado por parquet/pickle puede volver como float64, que
    es exactamente el bug que este rediseno viene a matar. Manteniendo lectura,
    tipado e insert en un solo proceso, los decimales nunca salen de memoria como
    otra cosa que decimal.Decimal.

PRECISION DECIMAL - LO MAS IMPORTANTE DE ESTE ARCHIVO
    clickhouse_connect TRUNCA hacia cero al insertar un float en Decimal(18,2):
    Decimal('1039.05') -> float64 1039.0499999999999545... -> se almacena 1039.04.

    Aca los decimales NUNCA pasan por float:
      1. se leen de MySis como TEXTO, con CAST(col AS CHAR). Asi ni siquiera
         pd.read_sql puede tocarlos: su parametro coerce_float viene en True por
         defecto y convierte decimal.Decimal a float64 sin avisar;
      2. se reconstruyen con decimal.Decimal(texto), exacto;
      3. se insertan como decimal.Decimal.
    Y cada lote se verifica solo: la suma exacta de lo que se envio se compara contra
    SUM() en el staging. Si no coincide digito a digito, el lote falla.
    NO INTRODUCIR float() EN EL CAMINO DE LOS DATOS. Los unicos float de este archivo
    estan en los porcentajes que se imprimen por pantalla.

FECHAS FUERA DEL RANGO DE CLICKHOUSE - EL OTRO CAMPO MINADO
    Estas tablas tienen columnas DATE que las _aux no tenian, y MySis guarda ahi
    basura historica: fechaoc / fechahes / fechadocumento / fechaguia traen valores
    1899-11-30 (el cero de Delphi). Medido el 2026-08-14 contra MySis:

        mstr_pedidos.fechaoc          2 filas < 1970
        mstr_pedidos.fechahes         1 fila  < 1970
        mstr_nc.fechaoc               6 filas < 1970
        mstr_ingresos.fechadocumento 39 filas < 1970
        mstr_ingresos.fechaguia      20 filas < 1970

    El tipo Date de ClickHouse arranca en 1970-01-01. clickhouse_connect calcula
    (valor - 1970-01-01).days y lo mete en un array de enteros SIN signo: con una
    fecha de 1899 revienta con OverflowError y se cae el lote entero.

    Aca las columnas Date tambien se leen como TEXTO (CAST(col AS CHAR)) y se
    reconstruyen con datetime.date, sin que pandas infiera nada. Lo que cae fuera del
    rango de ClickHouse se convierte a NULL (todas estas columnas son Nullable en el
    destino), se CUENTA y se imprime al cierre del lote. Y si en un lote se coercen
    mas de max(100 filas, 0,1%), el lote FALLA: 68 fechas basura conocidas son un
    detalle historico, una columna entera nulificada seria un bug.
"""

import re
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP

import clickhouse_connect
import pandas as pd
from mage_ai.io.config import ConfigFileLoader
from mage_ai.io.mysql import MySQL

if 'data_loader' not in globals():
    from mage_ai.data_preparation.decorators import data_loader

CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

# read_chunk por tabla: no es capricho, es ancho de fila. mstr_pedidos tiene 67
# columnas y tab_sku carga descripcion_web varchar(3000) + descripcionlarga
# varchar(1000) + clave varchar(600), asi que sus chunks van mas cortos para que el
# DataFrame intermedio no se dispare. mstr_ingresos y almacenaje son angostos.
SPECS = {
    'tab_bodegas': {
        'source': 'tab_bodegas', 'target': 'dwh.mysis_tab_bodegas',
        'staging': 'dwh.mysis_tab_bodegas_stg_lote', 'pk': 'bodega_id',
        'read_chunk': 50000, 'tol_por_mil': 5,
    },
    'almacenaje': {
        'source': 'almacenaje', 'target': 'dwh.mysis_almacenaje',
        'staging': 'dwh.mysis_almacenaje_stg_lote', 'pk': 'wrknre',
        # almacenaje es el estado ACTUAL del stock: las filas nacen y mueren todo el
        # dia con cada picking. Un margen de 5 por mil (181 filas) es demasiado
        # apretado para una tabla que se mueve sola mientras la leemos.
        'read_chunk': 50000, 'tol_por_mil': 20,
    },
    'tab_sku': {
        'source': 'tab_sku', 'target': 'dwh.mysis_tab_sku',
        'staging': 'dwh.mysis_tab_sku_stg_lote', 'pk': 'sku_id',
        'read_chunk': 10000, 'tol_por_mil': 5,
    },
    'mstr_nc': {
        'source': 'mstr_nc', 'target': 'dwh.mysis_mstr_nc',
        'staging': 'dwh.mysis_mstr_nc_stg_lote', 'pk': 'pid',
        'read_chunk': 25000, 'tol_por_mil': 5,
    },
    'mstr_ingresos': {
        'source': 'mstr_ingresos', 'target': 'dwh.mysis_mstr_ingresos',
        'staging': 'dwh.mysis_mstr_ingresos_stg_lote', 'pk': 'hid',
        'read_chunk': 50000, 'tol_por_mil': 5,
    },
    'mstr_pedidos': {
        'source': 'mstr_pedidos', 'target': 'dwh.mysis_mstr_pedidos',
        'staging': 'dwh.mysis_mstr_pedidos_stg_lote', 'pk': 'pid',
        'read_chunk': 25000, 'tol_por_mil': 5,
    },
}

# Plan de lotes calculado sobre los conteos REALES por bloque de 100.000 de pid
# medidos en MySis el 2026-08-14. La densidad de mstr_pedidos es casi uniforme
# (~95.000 filas por cada 100.000 de pid), asi que bastan cuatro tramos parejos de
# ~300.000. El ultimo queda abierto para recoger lo que produccion inserte durante
# la ventana de carga.
# Las cinco tablas chicas caben de sobra en un solo lote: aun asi el lote se lee en
# sub-ventanas de read_chunk filas, nunca entero en memoria.
BATCHES = {
    'tab_bodegas':   [(1, 999999999, 28)],
    'almacenaje':    [(1, 999999999, 36231)],
    'tab_sku':       [(1, 999999999, 22656)],
    'mstr_nc':       [(1, 999999999, 44503)],
    'mstr_ingresos': [(1, 999999999, 143657)],
    'mstr_pedidos':  [
        (1, 300000, 295123),
        (300000, 600000, 285648),
        (600000, 900000, 283347),
        (900000, 999999999, 297144),
    ],
}

# Margen del swap, en por mil, comparado con aritmetica entera. Produccion sigue
# escribiendo mientras cargamos. Ademas de la parte relativa hay un piso absoluto:
# sin el, tab_bodegas (28 filas) daria tolerancia 0 y una sola alta durante la
# ventana tumbaria el swap.
SWAP_TOLERANCIA_POR_MIL = 5
SWAP_TOLERANCIA_MIN_FILAS = 10
# Y ademas nunca reemplazar el destino por menos de la mitad de lo que ya tiene.
SWAP_MIN_RATIO_PCT = 50

# Cuantas fechas fuera de rango se toleran por lote antes de dar el lote por roto.
COERCION_MAX_PPM = 1000        # 0,1% del lote
COERCION_MIN_FILAS = 100       # piso absoluto, para que un lote chico no falle por 1

# Limites reales de cada tipo temporal de ClickHouse.
CH_DATE_LO, CH_DATE_HI = date(1970, 1, 1), date(2149, 6, 6)
CH_DATE32_LO, CH_DATE32_HI = date(1900, 1, 1), date(2299, 12, 31)
CH_DT32_LO = datetime(1970, 1, 1, 0, 0, 0)
CH_DT32_HI = datetime(2106, 2, 7, 6, 28, 15)
CH_DT64_LO = datetime(1900, 1, 1, 0, 0, 0)
CH_DT64_HI = datetime(2299, 12, 31, 23, 59, 59)

_DEC_RE = re.compile(r'^Decimal\((\d+),\s*(\d+)\)$')


# --------------------------------------------------------------------------- utils

def _as_int(v, default=None):
    if v is None or v == '':
        return default
    return int(str(v).strip())


def _as_bool(v, default=False):
    if v is None or v == '':
        return default
    return str(v).strip().lower() in ('1', 'true', 't', 'yes', 'y', 'si')


def _pct(num, den):
    """Solo para imprimir. No toca datos."""
    return '{:.3f}%'.format(100.0 * num / den) if den else 'n/d'


def _ch_client():
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


def _exists(client, table):
    db, tbl = table.split('.', 1)
    return client.query(
        'SELECT count() FROM system.tables WHERE database=%(d)s AND name=%(t)s',
        parameters={'d': db, 't': tbl},
    ).result_rows[0][0] > 0


def _columns(client, table):
    """Columnas del DESTINO leidas de system.columns EN TIEMPO DE EJECUCION.

    Nada de listas escritas a mano: el orden y los nombres del INSERT salen de la
    tabla real. Importa de verdad en dwh.mysis_almacenaje, donde el orden de columnas
    de ClickHouse (wrknre primero) NO es el de MySis (wrknre en la posicion 10).
    ingested_at queda fuera (DEFAULT now()), igual que cualquier otra columna que se
    materialice sola con now().
    """
    db, tbl = table.split('.', 1)
    rows = client.query(
        'SELECT name, type, default_expression FROM system.columns '
        'WHERE database=%(d)s AND table=%(t)s ORDER BY position',
        parameters={'d': db, 't': tbl},
    ).result_rows
    if not rows:
        raise Exception('No se encontraron columnas para {}.'.format(table))

    cols = []
    for name, typ, default_expr in rows:
        if name == 'ingested_at' or 'now()' in (default_expr or ''):
            continue
        base = typ
        nullable = base.startswith('Nullable(')
        if nullable:
            base = base[len('Nullable('):-1]

        lo = hi = None
        m = _DEC_RE.match(base)
        if m:
            kind, scale = 'decimal', int(m.group(2))
        elif base.startswith(('Int', 'UInt')):
            kind, scale = 'int', 0
        elif base.startswith('DateTime64'):
            kind, scale, lo, hi = 'datetime', 0, CH_DT64_LO, CH_DT64_HI
        elif base.startswith('DateTime'):
            kind, scale, lo, hi = 'datetime', 0, CH_DT32_LO, CH_DT32_HI
        elif base.startswith('Date32'):
            kind, scale, lo, hi = 'date', 0, CH_DATE32_LO, CH_DATE32_HI
        elif base == 'Date':
            kind, scale, lo, hi = 'date', 0, CH_DATE_LO, CH_DATE_HI
        elif base == 'String' or base.startswith('FixedString'):
            kind, scale = 'string', 0
        else:
            raise Exception(
                'Tipo no contemplado en {}.{}: {}. Agregar el mapeo antes de cargar.'.format(
                    table, name, typ))
        cols.append({'name': name, 'kind': kind, 'scale': scale,
                     'nullable': nullable, 'lo': lo, 'hi': hi})
    return cols


def _isna(v):
    if v is None:
        return True
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


# ------------------------------------------------------- conversion (SIN float)

def _conv_decimal(v, scale, nullable):
    """texto (o Decimal) -> decimal.Decimal exacto. Jamas float."""
    if _isna(v):
        return None if nullable else Decimal(0).quantize(Decimal(1).scaleb(-scale))
    d = v if isinstance(v, Decimal) else Decimal(str(v))
    return d.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP)


def _conv_int(v, nullable):
    if _isna(v):
        return None if nullable else 0
    return int(v)


def _conv_str(v, nullable):
    if _isna(v):
        return None if nullable else ''
    return v if isinstance(v, str) else str(v)


def _conv_date(v, col, coerced):
    """texto 'YYYY-MM-DD' -> datetime.date, con el rango de ClickHouse respetado."""
    name, nullable, lo, hi = col['name'], col['nullable'], col['lo'], col['hi']
    if _isna(v):
        return None if nullable else lo

    if isinstance(v, pd.Timestamp):
        d = v.date()
    elif isinstance(v, datetime):
        d = v.date()
    elif isinstance(v, date):
        d = v
    elif isinstance(v, str):
        s = v.strip()
        try:
            d = date(int(s[0:4]), int(s[5:7]), int(s[8:10]))
        except (ValueError, IndexError):
            # '0000-00-00' y demas fauna de MySQL sin modo estricto.
            coerced[name] = coerced.get(name, 0) + 1
            return None if nullable else lo
    else:
        d = v

    if d < lo or d > hi:
        coerced[name] = coerced.get(name, 0) + 1
        return None if nullable else lo
    return d


def _conv_dt(v, col, coerced):
    name, nullable, lo, hi = col['name'], col['nullable'], col['lo'], col['hi']
    if _isna(v):
        return None if nullable else lo

    if isinstance(v, pd.Timestamp):
        d = v.to_pydatetime().replace(tzinfo=None)
    elif isinstance(v, datetime):
        d = v.replace(tzinfo=None)
    elif isinstance(v, date):
        d = datetime(v.year, v.month, v.day)
    else:
        return v

    if d < lo or d > hi:
        coerced[name] = coerced.get(name, 0) + 1
        return None if nullable else lo
    return d


def _row_tuple(vals, cols, coerced):
    out = []
    for v, c in zip(vals, cols):
        k = c['kind']
        if k == 'decimal':
            out.append(_conv_decimal(v, c['scale'], c['nullable']))
        elif k == 'int':
            out.append(_conv_int(v, c['nullable']))
        elif k == 'string':
            out.append(_conv_str(v, c['nullable']))
        elif k == 'date':
            out.append(_conv_date(v, c, coerced))
        else:
            out.append(_conv_dt(v, c, coerced))
    return tuple(out)


def _iter_rows(part, cols):
    """Itera el DataFrame columna por columna.

    No se usa .values ni to_dict('records'): ambos arman un array intermedio que
    puede promocionar enteros a float. Series.tolist() respeta el tipo de cada
    columna por separado.
    """
    series = [part[c['name']].tolist() for c in cols]
    return zip(*series)


# ------------------------------------------------------------------ lectura MySis

def _select_sql(cols, source, pk, a, b):
    sel = []
    for c in cols:
        if c['kind'] in ('decimal', 'date'):
            # TEXTO a proposito: ver PRECISION DECIMAL y FECHAS FUERA DEL RANGO.
            sel.append('CAST(`{n}` AS CHAR) AS `{n}`'.format(n=c['name']))
        else:
            sel.append('`{n}`'.format(n=c['name']))
    return 'SELECT {cols} FROM {src} WHERE {pk} >= {a} AND {pk} < {b} ORDER BY {pk}'.format(
        cols=', '.join(sel), src=source, pk=pk, a=a, b=b)


def _ch_sums(client, table, dec_cols, where):
    """SUM() por columna decimal, leido como texto para no perder ni un digito.

    El None -> Decimal(0) NO es paranoia: sum() sobre una columna Nullable que no
    tiene un solo valor no nulo devuelve NULL, no 0, y toString(NULL) llega a Python
    como None. Medido el 2026-08-14 en MySis, mstr_nc.total_pedido (0 de 44.505) y
    almacenaje.caja_id (0 de 36.220) estan enteramente en NULL: sin esta guarda,
    Decimal('None') tiraba InvalidOperation y tumbaba dos lotes perfectamente sanos.
    El acumulador `sumas` tambien vale Decimal(0) en ese caso, asi que la comparacion
    sigue siendo exacta.
    """
    if not dec_cols:
        return {}
    expr = ', '.join('toString(sum({}))'.format(c) for c in dec_cols)
    row = client.query('SELECT {} FROM {} WHERE {}'.format(expr, table, where)).result_rows[0]
    return {name: (Decimal(str(val)) if val is not None else Decimal(0))
            for name, val in zip(dec_cols, row)}


def _tolerancia(n_src, por_mil):
    return max(n_src * por_mil // 1000, SWAP_TOLERANCIA_MIN_FILAS)


# ------------------------------------------------------------------------- modos

def _mode_init(client, spec):
    stg, dst = spec['staging'], spec['target']
    if not _exists(client, dst):
        raise Exception('No existe el destino {}. Se aborta.'.format(dst))
    antes = client.query('SELECT count() FROM {}'.format(dst)).result_rows[0][0]
    client.command('DROP TABLE IF EXISTS {}'.format(stg))
    client.command('CREATE TABLE {} AS {}'.format(stg, dst))
    print('INIT  staging {} recreado vacio (misma estructura y engine que {})'.format(stg, dst))
    print('      el destino tiene hoy {} filas y queda intacto hasta el swap'.format(antes))
    return {'mode': 'init', 'staging': stg, 'rows_staging': 0, 'rows_target': antes}


def _mode_load(client, spec, tabla, pk_from, pk_to, force, read_chunk, insert_chunk):
    src, stg, pk = spec['source'], spec['staging'], spec['pk']

    if pk_from is None or pk_to is None:
        raise Exception("mode='load' necesita pk_from y pk_to.")
    if pk_to <= pk_from:
        raise Exception('Rango invalido: pk_from={} pk_to={}.'.format(pk_from, pk_to))
    if not _exists(client, stg):
        raise Exception(
            "No existe el staging {}. Corre primero {{tabla: '{}', mode: 'init'}}.".format(
                stg, tabla))

    cols = _columns(client, spec['target'])
    names = [c['name'] for c in cols]
    dec_idx = [(i, c['name']) for i, c in enumerate(cols) if c['kind'] == 'decimal']
    rango = '{pk} >= {a} AND {pk} < {b}'.format(pk=pk, a=pk_from, b=pk_to)

    print('LOAD  {} [{}, {}) -> {}'.format(tabla, pk_from, pk_to, stg))
    print('      {} columnas; decimales como texto: {}'.format(
        len(cols), [n for _, n in dec_idx] or 'ninguna'))

    # Idempotencia: un rango ya cargado no se duplica en silencio.
    ya = client.query('SELECT count() FROM {} WHERE {}'.format(stg, rango)).result_rows[0][0]
    print('      filas de ese rango ya presentes en el staging: {}'.format(ya))
    if ya > 0:
        if not force:
            raise Exception(
                'El rango [{}, {}) ya tiene {} filas en {}: ESTE LOTE YA SE CORRIO. '
                'Si de verdad hay que rehacerlo, relanzalo con force=true, que borra el '
                'rango y lo vuelve a cargar.'.format(pk_from, pk_to, ya, stg))
        print('      force=true: se borra el rango antes de recargarlo')
        client.command('ALTER TABLE {} DELETE WHERE {}'.format(stg, rango),
                       settings={'mutations_sync': 2})

    leidas = 0
    enviadas = 0
    sumas = {name: Decimal(0) for _, name in dec_idx}
    coerced = {}
    buf = []

    def _flush():
        if buf:
            client.insert(stg, buf, column_names=names)
            del buf[:]

    with _mysql() as loader:
        b = loader.load('SELECT MIN({pk}) lo, MAX({pk}) hi FROM {src} WHERE {r}'.format(
            pk=pk, src=src, r=rango))
        lo, hi = b.iloc[0]['lo'], b.iloc[0]['hi']
        if pd.isna(lo):
            total = client.query('SELECT count() FROM {}'.format(stg)).result_rows[0][0]
            print('      MySis no tiene filas en este rango: nada que cargar.')
            print('      staging acumulado={}'.format(total))
            return {'mode': 'load', 'tabla': tabla, 'pk_from': pk_from, 'pk_to': pk_to,
                    'rows_read': 0, 'rows_inserted': 0, 'rows_staging': total}
        lo, hi = int(lo), int(hi)
        print('      {} realmente presente en MySis: {}..{}'.format(pk, lo, hi))

        # Sub-ventanas de PK: el lote nunca entra entero en memoria.
        start = lo
        while start <= hi:
            end = min(start + read_chunk, hi + 1)
            part = loader.load(_select_sql(cols, src, pk, start, end))
            if len(part):
                leidas += len(part)
                for vals in _iter_rows(part, cols):
                    t = _row_tuple(vals, cols, coerced)
                    buf.append(t)
                    # Se suma lo que REALMENTE va en el INSERT, no una copia aparte.
                    for i, name in dec_idx:
                        if t[i] is not None:
                            sumas[name] += t[i]
                    if len(buf) >= insert_chunk:
                        enviadas += len(buf)
                        _flush()
                        print('      insertadas {} de {} leidas ({} < {})'.format(
                            enviadas, leidas, pk, end))
            start = end
        enviadas += len(buf)
        _flush()

    en_stg = client.query('SELECT count() FROM {} WHERE {}'.format(stg, rango)).result_rows[0][0]
    if en_stg != enviadas:
        raise Exception(
            'El staging tiene {} filas en [{}, {}) y se enviaron {}. Lote inconsistente: '
            'revisar antes de seguir.'.format(en_stg, pk_from, pk_to, enviadas))

    # Control de precision del lote: lo enviado contra lo almacenado, digito a digito.
    # Si alguien reintroduce un float en el camino, esto revienta en el primer lote.
    stg_sums = _ch_sums(client, stg, [n for _, n in dec_idx], rango)
    for name, esperado in sumas.items():
        obtenido = stg_sums.get(name, Decimal(0))
        if obtenido != esperado:
            raise Exception(
                'PERDIDA DE PRECISION en {}.{}: enviado {} vs almacenado {} (dif {}). '
                'Casi seguro volvio a colarse un float en la conversion. El lote quedo '
                'cargado pero INVALIDO: arregla el codigo y recargalo con '
                'force=true.'.format(tabla, name, esperado, obtenido, obtenido - esperado))

    # Fechas fuera del rango de ClickHouse: se informan siempre y se acotan.
    if coerced:
        tope = max(COERCION_MIN_FILAS, leidas * COERCION_MAX_PPM // 1000000)
        for name in sorted(coerced):
            print('      AVISO fecha fuera de rango en {}: {} filas -> NULL'.format(
                name, coerced[name]))
        peor = max(coerced.values())
        if peor > tope:
            raise Exception(
                'Se convirtieron a NULL {} fechas fuera del rango de ClickHouse en un solo '
                'lote de {} filas (tope {}). Eso ya no son valores basura sueltos: revisar '
                'el mapeo de tipos antes de seguir. Detalle: {}'.format(
                    peor, leidas, tope, coerced))

    total = client.query('SELECT count() FROM {}'.format(stg)).result_rows[0][0]
    print('      OK  leidas={} insertadas={}  |  staging acumulado={}'.format(
        leidas, enviadas, total))
    for name in sorted(sumas):
        print('      SUM({}) del lote = {} (verificado contra el staging)'.format(
            name, sumas[name]))
    return {'mode': 'load', 'tabla': tabla, 'pk_from': pk_from, 'pk_to': pk_to,
            'rows_read': leidas, 'rows_inserted': enviadas, 'rows_staging': total,
            'fechas_a_null': coerced,
            'sums': {k: str(v) for k, v in sumas.items()}}


def _mode_swap(client, spec, tabla, check_sums, tol_por_mil):
    src, stg, dst, pk = spec['source'], spec['staging'], spec['target'], spec['pk']
    if not _exists(client, stg):
        raise Exception('No existe el staging {}: no hay nada que intercambiar.'.format(stg))

    cols = _columns(client, dst)
    dec_cols = [c['name'] for c in cols if c['kind'] == 'decimal']
    n_stg = client.query('SELECT count() FROM {}'.format(stg)).result_rows[0][0]
    n_uniq = client.query('SELECT uniqExact({}) FROM {}'.format(pk, stg)).result_rows[0][0]
    n_dst = client.query('SELECT count() FROM {}'.format(dst)).result_rows[0][0]
    with _mysql() as loader:
        n_src = int(loader.load('SELECT COUNT(*) n FROM {}'.format(src)).iloc[0]['n'])

    print('SWAP  {}'.format(tabla))
    print('      MySis   {}'.format(n_src))
    print('      staging {} ({} {} distintas)'.format(n_stg, n_uniq, pk))
    print('      destino {} (incluye los duplicados fisicos sin fusionar del '
          'ReplacingMergeTree)'.format(n_dst))

    if n_stg == 0:
        raise Exception('El staging esta vacio. No se toca el destino.')
    if n_uniq != n_stg:
        raise Exception(
            'El staging tiene {} filas pero solo {} {} distintas: algun lote se '
            'cargo dos veces. Rehacer desde init.'.format(n_stg, n_uniq, pk))

    dif = abs(n_stg - n_src)
    tol = _tolerancia(n_src, tol_por_mil)
    if dif > tol:
        raise Exception(
            'Diferencia de {} filas ({}) entre staging ({}) y MySis ({}), sobre el margen '
            'de {} filas ({} por mil, piso {}). Falta algun lote o produccion se movio '
            'demasiado. NO se hace el swap.'.format(
                dif, _pct(dif, n_src), n_stg, n_src, tol, tol_por_mil,
                SWAP_TOLERANCIA_MIN_FILAS))
    print('      deriva contra MySis: {} filas ({}), margen {} filas -> OK'.format(
        dif, _pct(dif, n_src), tol))

    if n_dst > 0 and n_stg * 100 < n_dst * SWAP_MIN_RATIO_PCT:
        raise Exception(
            'El staging ({}) tiene menos del {}% del destino ({}). NO se hace el '
            'swap.'.format(n_stg, SWAP_MIN_RATIO_PCT, n_dst))

    # Informativo: sumas decimales contra MySis sobre el MISMO dominio de PK (hasta la
    # ultima PK cargada). NO es fatal y nunca bloquea el swap: produccion sigue
    # escribiendo y una baja o un pago durante la ventana mueve la suma. El control
    # duro de precision es el de cada lote, que compara enviado contra almacenado.
    if check_sums and dec_cols:
        try:
            max_pk = client.query('SELECT max({}) FROM {}'.format(pk, stg)).result_rows[0][0]
            stg_sums = _ch_sums(client, stg, dec_cols, '{} <= {}'.format(pk, max_pk))
            with _mysql() as loader:
                expr = ', '.join('CAST(SUM(`{c}`) AS CHAR) `{c}`'.format(c=c) for c in dec_cols)
                r = loader.load('SELECT {} FROM {} WHERE {} <= {}'.format(
                    expr, src, pk, max_pk))
            for c in dec_cols:
                v = r.iloc[0][c]
                mysis = Decimal(str(v)) if not pd.isna(v) else Decimal(0)
                ch = stg_sums.get(c, Decimal(0))
                print('      SUM({}) MySis={} staging={} -> {}'.format(
                    c, mysis, ch, 'IGUAL' if ch == mysis else 'dif {}'.format(ch - mysis)))
        except Exception as e:  # informativo: no puede tumbar el swap
            print('      (no se pudo comparar sumas contra MySis: {})'.format(e))

    client.command('EXCHANGE TABLES {} AND {}'.format(dst, stg))
    client.command('DROP TABLE IF EXISTS {}'.format(stg))
    print('      EXCHANGE ok: {} pasa de {} a {} filas. Staging eliminado.'.format(
        dst, n_dst, n_stg))
    return {'mode': 'swap', 'tabla': tabla, 'rows_before': n_dst, 'rows_after': n_stg,
            'rows_mysis': n_src, 'diff_rows': dif, 'swapped': True}


def _mode_status(client, spec, tabla, tol_por_mil):
    src, stg, dst, pk = spec['source'], spec['staging'], spec['target'], spec['pk']
    with _mysql() as loader:
        b = loader.load('SELECT COUNT(*) n, MIN({pk}) lo, MAX({pk}) hi FROM {src}'.format(
            pk=pk, src=src))
    n_src, lo, hi = int(b.iloc[0]['n']), int(b.iloc[0]['lo']), int(b.iloc[0]['hi'])
    n_dst = client.query('SELECT count() FROM {}'.format(dst)).result_rows[0][0]
    n_uniq_dst = client.query('SELECT uniqExact({}) FROM {}'.format(pk, dst)).result_rows[0][0]
    hay_stg = _exists(client, stg)
    n_stg = client.query('SELECT count() FROM {}'.format(stg)).result_rows[0][0] if hay_stg else 0

    print('STATUS {}'.format(tabla))
    print('  MySis   {:<12} {} {}..{}'.format(n_src, pk, lo, hi))
    print('  destino {:<12} {} ({} {} distintas -> {:+d} contra MySis)'.format(
        n_dst, dst, n_uniq_dst, pk, n_uniq_dst - n_src))
    print('  staging {:<12} {}'.format(n_stg if hay_stg else 'NO EXISTE', stg))

    pendientes = []
    if not hay_stg:
        print("  siguiente corrida: {{tabla: '{}', mode: 'init'}}".format(tabla))
        return {'mode': 'status', 'tabla': tabla, 'rows_mysis': n_src, 'rows_target': n_dst,
                'uniq_target': n_uniq_dst, 'staging_exists': False, 'rows_staging': 0,
                'pendientes': None}

    print('  lotes:')
    for i, (a, z, est) in enumerate(BATCHES.get(tabla, []), 1):
        n = client.query('SELECT count() FROM {} WHERE {} >= {} AND {} < {}'.format(
            stg, pk, a, pk, z)).result_rows[0][0]
        d = abs(n - est)
        if n == 0:
            estado, pend = 'PENDIENTE', True
        elif d * 20 <= est or d <= 500:
            estado, pend = 'cargado', False
        else:
            estado, pend = 'PARCIAL/REVISAR', True
        if pend:
            pendientes.append({'pk_from': a, 'pk_to': z, 'filas_est': est})
        print('    {:>2}) [{:<9} {:<10}) esperadas ~{:<8} en staging {:<8} {}'.format(
            i, a, z, est, n, estado))

    if pendientes:
        p = pendientes[0]
        print("  siguiente corrida: {{tabla: '{}', mode: 'load', pk_from: {}, "
              "pk_to: {}}}".format(tabla, p['pk_from'], p['pk_to']))
    else:
        dif = abs(n_stg - n_src)
        print('  todos los lotes cargados. Deriva contra MySis: {} filas ({}), margen {} '
              'filas.'.format(dif, _pct(dif, n_src), _tolerancia(n_src, tol_por_mil)))
        print("  siguiente corrida: {{tabla: '{}', mode: 'swap'}}".format(tabla))

    return {'mode': 'status', 'tabla': tabla, 'rows_mysis': n_src, 'rows_target': n_dst,
            'uniq_target': n_uniq_dst, 'staging_exists': True, 'rows_staging': n_stg,
            'pendientes': pendientes}


# ------------------------------------------------------------------------ entrada

@data_loader
def run(*args, **kwargs):
    tabla = str(kwargs.get('tabla') or '').strip()
    mode = str(kwargs.get('mode') or 'status').strip().lower()

    if tabla not in SPECS:
        raise Exception("tabla debe ser una de {}, llego '{}'.".format(
            sorted(SPECS), kwargs.get('tabla')))
    if mode not in ('init', 'load', 'swap', 'status'):
        raise Exception("mode debe ser init|load|swap|status, llego '{}'.".format(mode))

    spec = SPECS[tabla]
    tol_por_mil = _as_int(kwargs.get('tol_por_mil'),
                          spec.get('tol_por_mil', SWAP_TOLERANCIA_POR_MIL))
    client = _ch_client()
    print('=' * 78)
    print('tabla={}  mode={}  destino={}  staging={}'.format(
        tabla, mode, spec['target'], spec['staging']))

    if mode == 'init':
        res = _mode_init(client, spec)
    elif mode == 'load':
        res = _mode_load(
            client, spec, tabla,
            _as_int(kwargs.get('pk_from')), _as_int(kwargs.get('pk_to')),
            _as_bool(kwargs.get('force')),
            _as_int(kwargs.get('read_chunk'), spec.get('read_chunk', 25000)),
            _as_int(kwargs.get('insert_chunk'), 25000),
        )
    elif mode == 'swap':
        res = _mode_swap(client, spec, tabla,
                         _as_bool(kwargs.get('check_sums'), True), tol_por_mil)
    else:
        res = _mode_status(client, spec, tabla, tol_por_mil)

    print('=' * 78)
    return res
