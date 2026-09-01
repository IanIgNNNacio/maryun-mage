"""Carga por LOTES de las tres tablas *_aux de MySis a ClickHouse.

UNA CORRIDA = UN (tabla, mode, rango). El orquestador lanza las corridas de a una.
El pipeline nunca lee los 3,3M de filas de un viaje ni recorre las tres tablas.

Variables de runtime (Mage las entrega por kwargs):

    tabla    'pedidos_aux' | 'nc_aux' | 'ingresos_aux'
    mode     'init' | 'load' | 'swap' | 'status'   (default 'status', que no escribe nada)
    pk_from  inicio del rango de posicion, INCLUSIVE   (solo mode='load')
    pk_to    fin del rango de posicion, EXCLUSIVE      (solo mode='load')
    force    'true' para recargar un rango ya cargado (borra el rango y lo reinserta)

    read_chunk    filas por SELECT contra MySis      (default 50.000)
    insert_chunk  filas por INSERT contra ClickHouse (default 50.000)
    check_sums    'false' para saltarse la comparacion de sumas contra MySis en el
                  swap (es un full scan del origen; informativo, nunca bloquea)

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
    Medido el 2026-08-14: 164 de 403 costos y 30 de 82 pmp quedaron 1 centavo bajos,
    y ese centavo se propaga a todo el kardex PMP posterior del par (sku, sucursal).

    Aca los decimales NUNCA pasan por float:
      1. se leen de MySis como TEXTO, con CAST(col AS CHAR). Asi ni siquiera
         pd.read_sql puede tocarlos: su parametro coerce_float viene en True por
         defecto y convierte decimal.Decimal a float64 sin avisar. Esa es la puerta
         trasera por la que el float se colaba ANTES de llegar al exporter, y pedir
         CHAR la cierra de raiz;
      2. se reconstruyen con decimal.Decimal(texto), exacto;
      3. se insertan como decimal.Decimal.
    Y cada lote se verifica solo: la suma exacta de lo que se envio se compara contra
    SUM() en el staging. Si no coincide digito a digito, el lote falla.
    NO INTRODUCIR float() EN EL CAMINO DE LOS DATOS. Los unicos float de este archivo
    estan en los porcentajes que se imprimen por pantalla.
"""

import re
from decimal import Decimal, ROUND_HALF_UP

import clickhouse_connect
import pandas as pd
from mage_ai.io.config import ConfigFileLoader
from mage_ai.io.mysql import MySQL

if 'data_loader' not in globals():
    from mage_ai.data_preparation.decorators import data_loader

CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

SPECS = {
    'pedidos_aux': {
        'source': 'mstr_pedidos_aux',
        'target': 'dwh.mysis_mstr_pedidos_aux',
        'staging': 'dwh.mysis_mstr_pedidos_aux_stg_lote',
        'pk': 'posicion',
    },
    'nc_aux': {
        'source': 'mstr_nc_aux',
        'target': 'dwh.mysis_mstr_nc_aux',
        'staging': 'dwh.mysis_mstr_nc_aux_stg_lote',
        'pk': 'posicion',
    },
    'ingresos_aux': {
        'source': 'mstr_ingresos_aux',
        'target': 'dwh.mysis_mstr_ingresos_aux',
        'staging': 'dwh.mysis_mstr_ingresos_aux_stg_lote',
        'pk': 'posicion',
    },
}

# Plan de lotes calculado sobre los conteos reales de posicion medidos en MySis el
# 2026-08-14 (bloques de 25.000 en pedidos, 50.000 en ingresos). La densidad NO es
# uniforme: en pedidos_aux el tramo 1..600.000 esta casi lleno (~100k filas por cada
# 100k de PK) y de 3.000.000 en adelante baja a ~12k por cada 25k. Por eso los rangos
# se van ensanchando. Solo se usa para informar en mode='status'.
BATCHES = {
    'pedidos_aux': [
        (1, 300000, 299999), (300000, 600000, 299976), (600000, 1050000, 297806),
        (1050000, 1500000, 300424), (1500000, 1975000, 295192), (1975000, 2450000, 296612),
        (2450000, 2950000, 295436), (2950000, 3550000, 304948), (3550000, 4175000, 304310),
        (4175000, 4800000, 297850), (4800000, 999999999, 295678),
    ],
    'ingresos_aux': [(1, 300000, 296545), (300000, 999999999, 172990)],
    'nc_aux': [(1, 999999999, 98148)],
}

# Margen del swap, en por mil, comparado con aritmetica entera.
# Produccion sigue escribiendo mientras cargamos: entre el primer lote y el swap pasan
# horas y MySis inserta lineas nuevas que quedan fuera del snapshot. 5 por mil sobre
# 3,29M son ~16.400 filas de deriva tolerada: muy por encima de lo que MySis mueve en
# una jornada y muy por debajo de lo que significaria perder un lote entero (~300k, 9%).
SWAP_TOLERANCIA_POR_MIL = 5
# Y ademas nunca reemplazar el destino por menos de la mitad de lo que ya tiene.
SWAP_MIN_RATIO_PCT = 50

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
    tabla real. ingested_at queda fuera (DEFAULT now()), igual que cualquier otra
    columna que se materialice sola con now().
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
        m = _DEC_RE.match(base)
        if m:
            kind, scale = 'decimal', int(m.group(2))
        elif base.startswith(('Int', 'UInt')):
            kind, scale = 'int', 0
        elif base.startswith('Date'):
            kind, scale = 'datetime', 0
        elif base == 'String' or base.startswith('FixedString'):
            kind, scale = 'string', 0
        else:
            raise Exception(
                'Tipo no contemplado en {}.{}: {}. Agregar el mapeo antes de cargar.'.format(
                    table, name, typ))
        cols.append({'name': name, 'kind': kind, 'scale': scale, 'nullable': nullable})
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


def _conv_dt(v, _nullable):
    if _isna(v):
        return None
    if isinstance(v, pd.Timestamp):
        return v.to_pydatetime().replace(tzinfo=None)
    return v


def _row_tuple(vals, cols):
    out = []
    for v, c in zip(vals, cols):
        k = c['kind']
        if k == 'decimal':
            out.append(_conv_decimal(v, c['scale'], c['nullable']))
        elif k == 'int':
            out.append(_conv_int(v, c['nullable']))
        elif k == 'string':
            out.append(_conv_str(v, c['nullable']))
        else:
            out.append(_conv_dt(v, c['nullable']))
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
        if c['kind'] == 'decimal':
            # TEXTO a proposito: ver la nota de PRECISION DECIMAL arriba.
            sel.append('CAST(`{n}` AS CHAR) AS `{n}`'.format(n=c['name']))
        else:
            sel.append('`{n}`'.format(n=c['name']))
    return 'SELECT {cols} FROM {src} WHERE {pk} >= {a} AND {pk} < {b} ORDER BY {pk}'.format(
        cols=', '.join(sel), src=source, pk=pk, a=a, b=b)


def _ch_sums(client, table, dec_cols, where):
    """SUM() por columna decimal, leido como texto para no perder ni un digito."""
    if not dec_cols:
        return {}
    expr = ', '.join('toString(sum({}))'.format(c) for c in dec_cols)
    row = client.query('SELECT {} FROM {} WHERE {}'.format(expr, table, where)).result_rows[0]
    return {name: Decimal(str(val)) for name, val in zip(dec_cols, row)}


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
        print('      posicion realmente presente en MySis: {}..{}'.format(lo, hi))

        # Sub-ventanas de PK: el lote nunca entra entero en memoria.
        start = lo
        while start <= hi:
            end = min(start + read_chunk, hi + 1)
            part = loader.load(_select_sql(cols, src, pk, start, end))
            if len(part):
                leidas += len(part)
                for vals in _iter_rows(part, cols):
                    t = _row_tuple(vals, cols)
                    buf.append(t)
                    # Se suma lo que REALMENTE va en el INSERT, no una copia aparte.
                    for i, name in dec_idx:
                        if t[i] is not None:
                            sumas[name] += t[i]
                    if len(buf) >= insert_chunk:
                        enviadas += len(buf)
                        _flush()
                        print('      insertadas {} de {} leidas (posicion < {})'.format(
                            enviadas, leidas, end))
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

    total = client.query('SELECT count() FROM {}'.format(stg)).result_rows[0][0]
    print('      OK  leidas={} insertadas={}  |  staging acumulado={}'.format(
        leidas, enviadas, total))
    for name in sorted(sumas):
        print('      SUM({}) del lote = {} (verificado contra el staging)'.format(
            name, sumas[name]))
    return {'mode': 'load', 'tabla': tabla, 'pk_from': pk_from, 'pk_to': pk_to,
            'rows_read': leidas, 'rows_inserted': enviadas, 'rows_staging': total,
            'sums': {k: str(v) for k, v in sumas.items()}}


def _mode_swap(client, spec, tabla, check_sums):
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
    print('      staging {} ({} posicion distintas)'.format(n_stg, n_uniq))
    print('      destino {} (incluye los duplicados fisicos sin fusionar del '
          'ReplacingMergeTree)'.format(n_dst))

    if n_stg == 0:
        raise Exception('El staging esta vacio. No se toca el destino.')
    if n_uniq != n_stg:
        raise Exception(
            'El staging tiene {} filas pero solo {} posicion distintas: algun lote se '
            'cargo dos veces. Rehacer desde init.'.format(n_stg, n_uniq))

    dif = abs(n_stg - n_src)
    if dif * 1000 > n_src * SWAP_TOLERANCIA_POR_MIL:
        raise Exception(
            'Diferencia de {} filas ({}) entre staging ({}) y MySis ({}), sobre el margen '
            'de {} por mil. Falta algun lote o produccion se movio demasiado. NO se hace '
            'el swap.'.format(dif, _pct(dif, n_src), n_stg, n_src, SWAP_TOLERANCIA_POR_MIL))
    print('      deriva contra MySis: {} filas ({}), margen {} por mil -> OK'.format(
        dif, _pct(dif, n_src), SWAP_TOLERANCIA_POR_MIL))

    if n_dst > 0 and n_stg * 100 < n_dst * SWAP_MIN_RATIO_PCT:
        raise Exception(
            'El staging ({}) tiene menos del {}% del destino ({}). NO se hace el '
            'swap.'.format(n_stg, SWAP_MIN_RATIO_PCT, n_dst))

    # Informativo: sumas decimales contra MySis sobre el MISMO dominio de PK (hasta la
    # ultima posicion cargada). NO es fatal y nunca bloquea el swap: produccion sigue
    # escribiendo y una baja durante la ventana mueve la suma. El control duro de
    # precision es el de cada lote, que compara enviado contra almacenado y es exacto.
    if check_sums and dec_cols:
        try:
            max_pos = client.query('SELECT max({}) FROM {}'.format(pk, stg)).result_rows[0][0]
            stg_sums = _ch_sums(client, stg, dec_cols, '{} <= {}'.format(pk, max_pos))
            with _mysql() as loader:
                expr = ', '.join('CAST(SUM(`{c}`) AS CHAR) `{c}`'.format(c=c) for c in dec_cols)
                r = loader.load('SELECT {} FROM {} WHERE {} <= {}'.format(
                    expr, src, pk, max_pos))
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


def _mode_status(client, spec, tabla):
    src, stg, dst, pk = spec['source'], spec['staging'], spec['target'], spec['pk']
    with _mysql() as loader:
        b = loader.load('SELECT COUNT(*) n, MIN({pk}) lo, MAX({pk}) hi FROM {src}'.format(
            pk=pk, src=src))
    n_src, lo, hi = int(b.iloc[0]['n']), int(b.iloc[0]['lo']), int(b.iloc[0]['hi'])
    n_dst = client.query('SELECT count() FROM {}'.format(dst)).result_rows[0][0]
    hay_stg = _exists(client, stg)
    n_stg = client.query('SELECT count() FROM {}'.format(stg)).result_rows[0][0] if hay_stg else 0

    print('STATUS {}'.format(tabla))
    print('  MySis   {:<12} posicion {}..{}'.format(n_src, lo, hi))
    print('  destino {:<12} {}'.format(n_dst, dst))
    print('  staging {:<12} {}'.format(n_stg if hay_stg else 'NO EXISTE', stg))

    pendientes = []
    if not hay_stg:
        print("  siguiente corrida: {{tabla: '{}', mode: 'init'}}".format(tabla))
        return {'mode': 'status', 'tabla': tabla, 'rows_mysis': n_src, 'rows_target': n_dst,
                'staging_exists': False, 'rows_staging': 0, 'pendientes': None}

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
              'por mil.'.format(dif, _pct(dif, n_src), SWAP_TOLERANCIA_POR_MIL))
        print("  siguiente corrida: {{tabla: '{}', mode: 'swap'}}".format(tabla))

    return {'mode': 'status', 'tabla': tabla, 'rows_mysis': n_src, 'rows_target': n_dst,
            'staging_exists': True, 'rows_staging': n_stg, 'pendientes': pendientes}


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
            _as_int(kwargs.get('read_chunk'), 50000),
            _as_int(kwargs.get('insert_chunk'), 50000),
        )
    elif mode == 'swap':
        res = _mode_swap(client, spec, tabla, _as_bool(kwargs.get('check_sums'), True))
    else:
        res = _mode_status(client, spec, tabla)

    print('=' * 78)
    return res
