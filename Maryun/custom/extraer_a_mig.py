"""extraer_a_mig: aterriza los espejos de MySis en el esquema `mig` de produccion.

QUE HACE
--------
Por cada tabla de aterrizaje: ejecuta su SELECT contra ClickHouse en formato CSV,
y empuja ese flujo de bytes directo a `COPY mig.<tabla> FROM STDIN`. El dato no
se materializa en memoria en ningun punto: la interfaz HTTP de ClickHouse
devuelve un generador de bloques y `copy_expert` de psycopg lo consume como si
fuera un archivo. Es la unica forma sensata de mover 1,3 millones de lineas sin
que el contenedor de Mage se quede sin RAM.

QUE ESCRIBE
-----------
UNICAMENTE el esquema `mig` de la base de produccion, con TRUNCATE + COPY por tabla. No toca
ninguna tabla del ERP: la promocion de `mig` a `Sale`/`SaleLine`/etc la hace el
propio ERP, donde viven las reglas. Aca solo se mueven bytes.

Sobre ClickHouse: solo SELECT. Sobre MySis: nada, no se conecta.

POR QUE CSV Y NO UN DATAFRAME
-----------------------------
`query_df` de 1,3 millones de filas por 10 columnas son unos 200 MB de pandas,
y despues hay que serializarlo otra vez para el COPY. El CSV de ClickHouse ya
es compatible con el CSV de Postgres: comillas dobles, escape doblando la
comilla. Se pasa tal cual.

Dos ajustes que hay que declarar en los dos lados o se corrompe la carga:
  * `format_csv_null_representation = ''` en ClickHouse y `NULL ''` en Postgres.
    Por defecto ClickHouse escribe \\N y Postgres en modo csv espera vacio: sin
    esto una columna numerica nula llega como el texto '\\N' y el COPY revienta.
  * El espejo de ClickHouse guarda UTC, no hora de Chile. Verificado pid a
    pid: el pedido 1190706 esta a las 13:02:25 en MySis y a las 17:02:25 en
    ClickHouse, y timezone() del servidor devuelve 'UTC'. En agosto la
    diferencia es de 4 horas; en enero son 3, asi que la conversion tiene que
    ser por NOMBRE de zona y no por un offset. Toda fecha sale por
    toTimeZone(x, 'America/Santiago') y los filtros de ventana comparan el
    valor ya convertido, o el borde del rango se corre 4 horas. Sin esto una
    venta de las 21:00 de Chile aterriza al dia siguiente.

kwargs
------
    dsn        conexion DIRECTA a la base de produccion (en Neon, el host SIN
               "-pooler"). Si falta se busca en la variable
               de entorno MIG_PG_DSN. Obligatoria de una de las dos formas.
    etiqueta   etiqueta de la corrida. Default 'ensayo-<T en fecha>'.
    t          fecha de corte, hora de pared de Chile. Default '2026-08-20 00:00:00'.
    anios      profundidad de la ventana de ventas, en anios. Default 4.
    meses      la misma ventana en MESES. Manda sobre `anios` si viene.
               Es lo que hay que usar para acotar a un plan chico: `meses=6`.
    tablas     lista separada por comas para acotar. Vacio = todas, en orden.
    dry_run    'true' = solo cuenta filas en ClickHouse y no escribe nada.

REANUDAR
--------
Cada tabla es independiente y se recarga completa. Si una falla, se vuelve a
lanzar el bloque con `tablas=<la que falto>` y no hay que repetir el resto.
"""

import io
import os
import re
import time

import clickhouse_connect
import requests
from mage_ai.io.config import ConfigFileLoader

if 'custom' not in globals():
    from mage_ai.data_preparation.decorators import custom

CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

# Orden de carga. No importa para el COPY (mig no tiene FK entre sus tablas),
# pero si para leer el log: primero lo barato, y las dos grandes al final.
ORDEN = ['corte', 'bodega', 'categoria', 'vendedor', 'proveedor', 'cliente',
         'sku', 'apertura', 'nc', 'nc_linea', 'oc', 'oc_linea', 'cartera',
         'venta', 'venta_linea']

# Las de ClickHouse que este bloque lee, para dejar constancia en el log de con
# que frescura se hizo la foto.
ESPEJOS = ['mysis_mstr_pedidos', 'mysis_mstr_pedidos_aux', 'mysis_mstr_nc',
           'mysis_mstr_nc_aux', 'mysis_almacenaje', 'mysis_tab_sku',
           'mysis_tab_clientes', 'mysis_mstr_oc', 'mysis_pmp_detalle']


def _miles(n):
    return '{:,}'.format(int(n)).replace(',', '.')


def _dur(seg):
    seg = int(seg)
    if seg < 60:
        return '{}s'.format(seg)
    return '{}m {}s'.format(seg // 60, seg % 60)


def _bool(v, d=False):
    if v is None or str(v).strip() == '':
        return d
    return str(v).strip().lower() in ('1', 'true', 't', 'yes', 'y', 'si')


def _cfg():
    return ConfigFileLoader(CONFIG_PATH, PROFILE)


def _ch():
    cfg = _cfg()
    return clickhouse_connect.get_client(
        host=cfg['CLICKHOUSE_HOST'], port=int(cfg['CLICKHOUSE_PORT']),
        username=cfg['CLICKHOUSE_USERNAME'], password=cfg['CLICKHOUSE_PASSWORD'],
        database=cfg['CLICKHOUSE_DATABASE'],
        secure=str(cfg['CLICKHOUSE_INTERFACE']).lower() == 'https')


def _revisar_dsn(dsn):
    """Dos avisos propios de un Postgres gestionado, antes de mover 90 MB.

    1. El endpoint AGRUPADO (el que lleva '-pooler' en el host) es un PgBouncer
       en modo transaccion. Un COPY FROM STDIN de 90 MB por ahi es pedirle
       problemas: hay que usar el DIRECTO. En Neon los dos hosts son identicos
       salvo ese sufijo, asi que confundirlos es facil, y el sintoma seria una
       carga que muere a la mitad sin explicar por que.
    2. sslmode. Neon lo exige; si falta, el connect falla con un mensaje que
       habla de SSL y no de la cadena, y se pierde el tiempo mirando el lado
       equivocado.
    """
    if '-pooler.' in dsn:
        print('  AVISO: la cadena apunta al endpoint AGRUPADO (-pooler). Para el '
              'COPY hay que usar el DIRECTO: el mismo host sin "-pooler".')
    if 'sslmode=' not in dsn and '.neon.tech' in dsn:
        print('  AVISO: falta sslmode=require, que Neon exige.')


def _pg(dsn):
    """psycopg2 o psycopg3, el que haya. Los dos tienen copy, con otra forma.

    `connect_timeout` generoso a proposito: un Postgres gestionado suspende la
    base tras unos minutos sin uso y la despierta con la primera conexion. El
    primer intento de la noche puede tardar varios segundos, y eso no es un
    error: es la base arrancando.
    """
    _revisar_dsn(dsn)
    try:
        import psycopg2
        return 'psycopg2', psycopg2.connect(dsn, connect_timeout=30)
    except ImportError:
        pass
    try:
        import psycopg
        return 'psycopg3', psycopg.connect(dsn, connect_timeout=30)
    except ImportError:
        pass
    raise RuntimeError(
        'No hay psycopg2 ni psycopg en este Mage. Instalar uno de los dos, '
        'o cambiar el transporte a HTTP contra /api/migracion/aterrizar.')


class _Contador(object):
    """Envuelve el flujo HTTP para contar bytes sin romper la interfaz read(n)."""

    def __init__(self, raw):
        self._raw = raw
        self.bytes_leidos = 0

    def read(self, n=-1):
        d = self._raw.read() if (n is None or n < 0) else self._raw.read(n)
        d = d or b''
        self.bytes_leidos += len(d)
        return d

    def readline(self, n=-1):
        d = self._raw.readline(n) if hasattr(self._raw, 'readline') else b''
        self.bytes_leidos += len(d or b'')
        return d or b''


def _flujo_csv(cfg, sql, settings):
    """Abre el SELECT contra la interfaz HTTP de ClickHouse en formato CSV.

    Devuelve un objeto tipo archivo que el COPY consume a medida que llega, sin
    materializar el CSV completo. `Accept-Encoding: identity` es obligatorio: si
    el servidor comprime, urllib3 entrega gzip crudo y el COPY lee basura.
    """
    esquema = 'https' if str(cfg['CLICKHOUSE_INTERFACE']).lower() == 'https' else 'http'
    url = '{}://{}:{}/'.format(esquema, cfg['CLICKHOUSE_HOST'], int(cfg['CLICKHOUSE_PORT']))
    params = {'database': cfg['CLICKHOUSE_DATABASE'], 'default_format': 'CSV'}
    params.update({k: str(v) for k, v in (settings or {}).items()})
    r = requests.post(url, params=params, data=sql.encode('utf-8'), stream=True,
                      timeout=(30, 3600),
                      auth=(cfg['CLICKHOUSE_USERNAME'], cfg['CLICKHOUSE_PASSWORD']),
                      headers={'Accept-Encoding': 'identity'})
    if r.status_code != 200:
        raise RuntimeError('ClickHouse HTTP {}: {}'.format(
            r.status_code, r.content[:2000].decode('utf-8', 'replace')))
    r.raw.decode_content = True
    return _Contador(r.raw)


# ---------------------------------------------------------------------- SQL
# El SELECT de cada tabla de aterrizaje. Es una copia de
# maryun-erp-vercel/scripts/migracion/10-extraer-clickhouse.sql; los comentarios
# largos que explican cada decision viven alli, no aca.
#
# Las columnas van en el MISMO orden que en mig (01-esquema-mig.sql).

SQL = {}

SQL['corte'] = """
SELECT '{ETIQUETA}' AS etiqueta, '{T}' AS t, '{DESDE}' AS ventana_desde,
       concat('dwh.mysis_* @ ', formatDateTime(toTimeZone((SELECT max(ingested_at) FROM dwh.mysis_mstr_pedidos), 'America/Santiago'), '%Y-%m-%d %H:%i:%S')) AS origen,
       formatDateTime(toTimeZone(now(), 'America/Santiago'), '%Y-%m-%d %H:%i:%S') AS tomado_en
"""

SQL['cliente'] = """
SELECT c.cliente_id, trim(ifNull(c.rut,'')), trim(ifNull(c.rso,'')), '', trim(ifNull(c.giro,'')),
       trim(ifNull(c.direccion,'')), trim(ifNull(c.comuna,'')), trim(ifNull(c.ciudad,'')),
       trim(ifNull(c.telefono,'')), trim(ifNull(c.correo,'')), ifNull(c.vendedor_id,0),
       toFloat64(ifNull(c.limite,0)), ifNull(c.adias,0), ifNull(c.oa,0),
       formatDateTime(toTimeZone(ifNull(c.dt_in, toDateTime64('1970-01-01 00:00:00',3)), 'America/Santiago'), '%Y-%m-%d %H:%i:%S')
FROM dwh.mysis_tab_clientes AS c FINAL
"""

SQL['proveedor'] = """
SELECT p.proveedor_id, trim(ifNull(p.rut,'')), trim(ifNull(p.rso,'')), '',
       trim(ifNull(p.direccion,'')), '', trim(ifNull(p.ciudad,'')),
       trim(ifNull(p.fono,'')), trim(ifNull(p.mail,'')), ifNull(p.oa,0)
FROM dwh.mysis_tab_proveedores AS p FINAL
"""

SQL['vendedor'] = """
WITH v AS (
  SELECT toInt32OrZero(ifNull(usr_in,'')) AS uid, count() AS n FROM dwh.mysis_mstr_pedidos FINAL
  WHERE dt_out IS NOT NULL AND toTimeZone(dt_out, 'America/Santiago') >= '{DESDE}' AND toTimeZone(dt_out, 'America/Santiago') < '{T}'
    AND cliente_id NOT IN (0,1) AND ifNull(usr_in,'') != '' GROUP BY uid
)
SELECT u.user_id, trim(concat(ifNull(u.user_name,''),' ',ifNull(u.user_apellido,''))),
       lower(trim(ifNull(u.correo,''))), trim(ifNull(u.user_rut,'')),
       ifNull(u.sucursal_id,0), ifNull(u.objeto_activo,0), ifNull(v.n,0)
FROM dwh.mysis_tab_users AS u FINAL
LEFT JOIN v ON v.uid = u.user_id
SETTINGS join_use_nulls = 1
"""

SQL['bodega'] = """
SELECT b.bodega_id,
       ifNull(nullIf(trim(b.siglas),''), concat('B', toString(b.bodega_id))),
       trim(b.bodega_desc), trim(ifNull(b.siglas,'')), trim(ifNull(b.direccion,'')),
       trim(ifNull(b.comuna,'')), b.vta_directa, b.oa,
       multiIf(b.bodega_id IN (20,21,27), 'LOCATION', b.bodega_id = 11, 'DESCARTAR', 'BODEGA'),
       multiIf(b.bodega_id = 27, 3, b.bodega_id = 21, 1, b.bodega_id = 20, 8, 0)
FROM dwh.mysis_tab_bodegas AS b FINAL
"""

SQL['categoria'] = """
SELECT 'FAMILIA', familia_id, trim(familia_descripcion) FROM dwh.mysis_tab_familias FINAL
UNION ALL SELECT 'MARCA', marca_id, trim(marca_descripcion) FROM dwh.mysis_tab_marcas FINAL
UNION ALL SELECT 'TIPO', tipo_id, trim(tipo_descripcion) FROM dwh.mysis_tab_tipos FINAL
"""

SQL['sku'] = """
SELECT trim(ifNull(s.sku,'')), s.sku_id, ifNull(s.producto_id,0), trim(ifNull(s.nombre,'')),
       trim(ifNull(s.descripcion,'')), ifNull(s.familia_id,0), ifNull(s.marca_id,0),
       ifNull(s.tipo_id,0), trim(ifNull(s.color,'')), trim(ifNull(s.talla,'')),
       trim(ifNull(s.barcode,'')), toFloat64(s.costo), ifNull(s.oa,0),
       trim(ifNull(s.procedencia,'')),
       if((ifNull(s.familia_id,0) = 165 AND ifNull(s.tipo_id,0) = 231) OR ifNull(s.familia_id,0) = 479, 1, 0),
       formatDateTime(toTimeZone(ifNull(s.dt_in, toDateTime64('1970-01-01 00:00:00',3)), 'America/Santiago'), '%Y-%m-%d %H:%i:%S')
FROM dwh.mysis_tab_sku AS s FINAL
WHERE trim(ifNull(s.sku,'')) != ''
"""

SQL['apertura'] = """
WITH
mapa AS (SELECT arrayJoin([(27,3),(21,1),(20,8)]) AS p),
servicio AS (
  SELECT sku, max(if((ifNull(familia_id,0) = 165 AND ifNull(tipo_id,0) = 231) OR ifNull(familia_id,0) = 479, 1, 0)) AS es_servicio
  FROM dwh.mysis_tab_sku FINAL WHERE ifNull(sku,'') != '' GROUP BY sku
),
stock AS (
  SELECT bodega_id, sku, sum(qty) AS uds, sum(if(pk != 0, qty, 0)) AS uds_ap
  FROM dwh.mysis_almacenaje FINAL WHERE sku != '' GROUP BY bodega_id, sku HAVING sum(qty) > 0
),
pmp_res AS (
  SELECT sucursal_id, sku, argMax(pmp,(fecha,seq)) AS pmp_final, argMax(saldo_qty,(fecha,seq)) AS sqty
  FROM dwh.mysis_pmp_detalle GROUP BY sucursal_id, sku
),
pmp_nac AS (
  SELECT sku, toFloat64(sum(sqty * pmp_final) / sum(sqty)) AS pnac
  FROM pmp_res WHERE pmp_final > 0 AND sqty > 0 GROUP BY sku
),
ult_compra AS (
  SELECT sku, toFloat64(argMax(pu,(hid,posicion))) AS pcom FROM dwh.mysis_mstr_ingresos_aux FINAL
  WHERE qty > 0 AND pu > 0 AND mda = 1 AND ifNull(sku,'') != '' GROUP BY sku
),
costo_mae AS (
  SELECT sku, toFloat64(max(costo)) AS cmae FROM dwh.mysis_tab_sku FINAL WHERE ifNull(sku,'') != '' GROUP BY sku
),
costeada AS (
  SELECT if(m.p.1 > 0, m.p.2, s.bodega_id) AS bod_dst, s.bodega_id AS bod_org, s.sku AS ksku,
         s.uds AS u, s.uds_ap AS ua, ifNull(sv.es_servicio,0) AS svc,
         toFloat64(ifNull(pr.pmp_final, toDecimal64(0,4))) AS pp,
         toFloat64(ifNull(pm.pmp_final, toDecimal64(0,4))) AS pmad,
         ifNull(pn.pnac,0) AS pnc, ifNull(uc.pcom,0) AS pcm, ifNull(cm.cmae,0) AS cma,
         (pp > 0 AND NOT (pnc > 0 AND (pp < pnc/10 OR pp > pnc*10))) AS n1,
         multiIf(n1, 1, pmad > 0, 2, pnc > 0, 3, pcm > 0, 4, cma > 0, 5, 99) AS nivel,
         round(multiIf(n1, pp, pmad > 0, pmad, pnc > 0, pnc, pcm > 0, pcm, cma > 0, cma, 0), 2) AS ucost
  FROM stock s
  LEFT JOIN servicio   sv ON sv.sku = s.sku
  LEFT JOIN mapa       m  ON m.p.1 = s.bodega_id
  LEFT JOIN pmp_res    pr ON pr.sku = s.sku AND pr.sucursal_id = s.bodega_id
  LEFT JOIN pmp_res    pm ON pm.sku = s.sku AND pm.sucursal_id = if(m.p.1 > 0, m.p.2, -1)
  LEFT JOIN pmp_nac    pn ON pn.sku = s.sku
  LEFT JOIN ult_compra uc ON uc.sku = s.sku
  LEFT JOIN costo_mae  cm ON cm.sku = s.sku
  SETTINGS join_use_nulls = 1
)
SELECT ifNull(nullIf(trim(b.siglas),''), concat('B', toString(b.bodega_id))) AS bodega_codigo,
       c.bod_dst, c.ksku, round(sum(c.u)),
       round(sum(c.u * c.ucost) / sum(c.u), 2), round(sum(c.u * c.ucost)),
       max(c.nivel),
       arrayStringConcat(arraySort(groupUniqArray(toString(c.bod_org))), ',')
FROM costeada c
JOIN dwh.mysis_tab_bodegas AS b FINAL ON b.bodega_id = c.bod_dst
WHERE c.svc = 0 AND c.nivel != 99
GROUP BY c.bod_dst, b.siglas, b.bodega_id, c.ksku
ORDER BY b.bodega_id, c.ksku
"""

SQL['venta'] = """
SELECT p.pid, ifNull(p.cliente_id,0), trim(ifNull(c.rut,'')), ifNull(p.sucursal_id,0),
       toInt32OrZero(ifNull(p.usr_in,'')),
       formatDateTime(toTimeZone(p.dt_out, 'America/Santiago'), '%Y-%m-%d %H:%i:%S'),
       formatDateTime(toTimeZone(ifNull(p.dt_in, p.dt_out), 'America/Santiago'), '%Y-%m-%d %H:%i:%S'),
       trim(ifNull(p.factura,'')), trim(ifNull(p.guia,'')), trim(ifNull(p.documento,'')),
       toFloat64(ifNull(p.neto,0)), toFloat64(ifNull(p.iva,0)), toFloat64(ifNull(p.total,0)),
       toFloat64(p.deuda), toFloat64(p.pagado), ifNull(p.fpago,0), p.padre,
       trim(ifNull(p.observacion,'')), trim(ifNull(p.refoc,'')), trim(ifNull(p.rehes,'')),
       trim(ifNull(p.estado,'')), trim(ifNull(p.refactura,'')),
       formatDateTime(toTimeZone(ifNull(p.dt_vencimiento, p.dt_out), 'America/Santiago'), '%Y-%m-%d %H:%i:%S')
FROM dwh.mysis_mstr_pedidos AS p FINAL
LEFT JOIN dwh.mysis_tab_clientes AS c FINAL ON c.cliente_id = p.cliente_id
WHERE p.dt_out IS NOT NULL AND toTimeZone(p.dt_out, 'America/Santiago') >= '{DESDE}' AND toTimeZone(p.dt_out, 'America/Santiago') < '{T}'
  AND ifNull(p.cliente_id,0) NOT IN (0,1) AND ifNull(p.estado,'') != 'R'
SETTINGS join_use_nulls = 1
"""

SQL['venta_linea'] = """
SELECT a.posicion, a.pid, trim(ifNull(a.sku,'')), toFloat64(a.picking),
       toFloat64(ifNull(a.qty,0)), toFloat64(ifNull(a.pu,0)), toFloat64(a.descuento),
       toFloat64(a.pmp),
       formatDateTime(toTimeZone(ifNull(a.dt_pmp, toDateTime('1970-01-01 00:00:00')), 'America/Santiago'), '%Y-%m-%d %H:%i:%S'),
       ifNull(a.mda,0)
FROM dwh.mysis_mstr_pedidos_aux AS a FINAL
WHERE a.picking > 0 AND trim(ifNull(a.sku,'')) != ''
  AND a.pid IN (SELECT pid FROM dwh.mysis_mstr_pedidos FINAL
                WHERE dt_out IS NOT NULL AND toTimeZone(dt_out, 'America/Santiago') >= '{DESDE}' AND toTimeZone(dt_out, 'America/Santiago') < '{T}'
                  AND ifNull(cliente_id,0) NOT IN (0,1) AND ifNull(estado,'') != 'R')
"""

SQL['nc'] = """
SELECT n.pid, ifNull(n.id_externo,0), ifNull(n.cliente_id,0),
       ifNull(n.sucursal_id,0), upper(trim(ifNull(n.tipo,''))),
       formatDateTime(toTimeZone(n.dt_out, 'America/Santiago'), '%Y-%m-%d %H:%i:%S'), trim(ifNull(n.factura,'')),
       toFloat64(ifNull(n.neto,0)), toFloat64(ifNull(n.iva,0)), toFloat64(ifNull(n.total,0)),
       trim(ifNull(n.observacion,''))
FROM dwh.mysis_mstr_nc AS n FINAL
WHERE n.dt_out IS NOT NULL AND toTimeZone(n.dt_out, 'America/Santiago') >= '{DESDE}' AND toTimeZone(n.dt_out, 'America/Santiago') < '{T}'
"""

SQL['nc_linea'] = """
SELECT x.posicion, x.pid, trim(ifNull(x.sku,'')), toFloat64(ifNull(x.entrega,0)),
       toFloat64(ifNull(x.pu,0)),
       round(toFloat64(ifNull(x.entrega,0)) * toFloat64(ifNull(x.pu,0)), 2)
FROM dwh.mysis_mstr_nc_aux AS x FINAL
WHERE trim(ifNull(x.sku,'')) != ''
  AND x.pid IN (SELECT pid FROM dwh.mysis_mstr_nc FINAL
                WHERE dt_out IS NOT NULL AND toTimeZone(dt_out, 'America/Santiago') >= '{DESDE}' AND toTimeZone(dt_out, 'America/Santiago') < '{T}')
"""

SQL['oc'] = """
SELECT o.oc_id, ifNull(o.proveedor_id,0), toInt32OrZero(ifNull(o.destino_id,'')), 0,
       formatDateTime(toTimeZone(ifNull(o.dt_in, toDateTime64('1970-01-01 00:00:00',3)), 'America/Santiago'), '%Y-%m-%d %H:%i:%S'),
       formatDateTime(toTimeZone(ifNull(o.dt_llega, ifNull(o.dt_in, toDateTime64('1970-01-01 00:00:00',3))), 'America/Santiago'), '%Y-%m-%d %H:%i:%S'),
       ifNull(o.importacion,0), 'CLP', toFloat64(ifNull(o.neto,0)), toFloat64(ifNull(o.total,0)),
       trim(ifNull(o.observacion,'')),
       toYear(toTimeZone(ifNull(o.dt_in, toDateTime64('1970-01-01 00:00:00',3)), 'America/Santiago')),
       if(toYear(toTimeZone(ifNull(o.dt_in, toDateTime64('1970-01-01 00:00:00',3)), 'America/Santiago')) = toYear(toTimeZone(toDateTime('{T}'), 'America/Santiago')), 1, 0)
FROM dwh.mysis_mstr_oc AS o FINAL
WHERE o.oa = 1 AND o.oc_fin IS NULL AND o.dt_cierre IS NULL
"""

SQL['oc_linea'] = """
SELECT l.posicion, l.oc_id, trim(ifNull(l.sku,'')), toFloat64(ifNull(l.qty,0)),
       greatest(toFloat64(ifNull(l.qty,0)) - toFloat64(ifNull(l.resto,0)), 0),
       toFloat64(ifNull(l.pu,0))
FROM dwh.mysis_mstr_oc_aux AS l FINAL
WHERE trim(ifNull(l.sku,'')) != ''
  AND l.oc_id IN (SELECT oc_id FROM dwh.mysis_mstr_oc FINAL
                  WHERE oa = 1 AND oc_fin IS NULL AND dt_cierre IS NULL)
"""

SQL['cartera'] = """
SELECT p.pid, ifNull(p.cliente_id,0), ifNull(p.sucursal_id,0),
       formatDateTime(toTimeZone(p.dt_out, 'America/Santiago'), '%Y-%m-%d %H:%i:%S'),
       formatDateTime(toTimeZone(ifNull(p.dt_vencimiento, p.dt_out), 'America/Santiago'), '%Y-%m-%d %H:%i:%S'),
       toFloat64(ifNull(p.total,0)), toFloat64(p.pagado), toFloat64(p.deuda),
       trim(ifNull(p.factura,''))
FROM dwh.mysis_mstr_pedidos AS p FINAL
WHERE p.dt_out IS NOT NULL AND toTimeZone(p.dt_out, 'America/Santiago') >= '{DESDE}' AND toTimeZone(p.dt_out, 'America/Santiago') < '{T}'
  AND ifNull(p.cliente_id,0) NOT IN (0,1) AND ifNull(p.estado,'') != 'R'
  AND p.deuda > 0
"""


def _sustituir(sql, etiqueta, t, desde):
    return (sql.replace('{ETIQUETA}', etiqueta)
               .replace('{DESDE}', desde)
               .replace('{T}', t))


@custom
def extraer_a_mig(*args, **kwargs):
    try:
        return _extraer(*args, **kwargs)
    except Exception:
        import traceback
        tb = traceback.format_exc()
        print(tb)
        try:
            c = _ch()
            c.command('CREATE TABLE IF NOT EXISTS dwh.zz_errores '
                      '(ts DateTime DEFAULT now(), bloque String, error String) '
                      'ENGINE = MergeTree ORDER BY ts')
            c.insert('dwh.zz_errores', [['extraer_a_mig', tb[:8000]]],
                     column_names=['bloque', 'error'])
        except Exception:
            pass
        raise


def _extraer(*args, **kwargs):
    dsn = str(kwargs.get('dsn') or os.environ.get('MIG_PG_DSN') or '').strip()
    t = str(kwargs.get('t') or '2026-08-20 00:00:00').strip()
    anios = float(kwargs.get('anios') or 4)
    meses = kwargs.get('meses')
    dry = _bool(kwargs.get('dry_run'))
    etiqueta = re.sub(r'[^A-Za-z0-9_.:-]', '',
                      str(kwargs.get('etiqueta') or ('ensayo-' + t[:10])))

    if not re.match(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$', t):
        raise ValueError("t tiene que venir como 'YYYY-MM-DD HH:MM:SS', llego: " + repr(t))
    # La ventana se resta en MESES, no en anios: un plan chico no aguanta cuatro
    # anios de ventas y la unica forma honesta de acotar es recortar el periodo.
    # Restar anios enteros no daba ese grado de libertad (`anios=0.5` reventaba
    # en el int()), y restar dias se equivoca con los meses de 28 y 31.
    #
    # `dia_seguro` evita el 31 de marzo menos un mes = 31 de febrero. Se lleva al
    # ultimo dia real del mes destino, que es lo que hace cualquier calendario.
    _m = int(meses) if meses not in (None, '') else int(round(anios * 12))
    if _m < 1:
        raise ValueError('la ventana tiene que ser de al menos un mes')
    _y, _mo, _resto = int(t[:4]), int(t[5:7]), t[10:]
    _dia = int(t[8:10])
    _tot = _y * 12 + (_mo - 1) - _m
    _y2, _mo2 = _tot // 12, _tot % 12 + 1
    _ultimo = [31, 29 if (_y2 % 4 == 0 and (_y2 % 100 != 0 or _y2 % 400 == 0)) else 28,
               31, 30, 31, 30, 31, 31, 30, 31, 30, 31][_mo2 - 1]
    desde = '{:04d}-{:02d}-{:02d}{}'.format(_y2, _mo2, min(_dia, _ultimo), _resto)

    filtro = str(kwargs.get('tablas') or '').strip()
    tablas = [x.strip() for x in filtro.split(',') if x.strip()] if filtro else list(ORDEN)
    desconocidas = [x for x in tablas if x not in SQL]
    if desconocidas:
        raise ValueError('tablas desconocidas: {}. Validas: {}'.format(desconocidas, ORDEN))

    if not dry and not dsn:
        raise ValueError(
            'Falta el DSN de produccion. Pasarlo como variable `dsn` del trigger '
            'o exportar MIG_PG_DSN en el contenedor de Mage. Con dry_run=true '
            'no hace falta.')

    cfg = _cfg()
    ch = _ch()

    print('=' * 74)
    print('MIGRACION MySis -> Maryun ERP  ·  aterrizaje en el esquema mig')
    print('  etiqueta : {}'.format(etiqueta))
    print('  corte T  : {}  (hora de pared de Chile)'.format(t))
    print('  ventana  : {} -> {}   ({} meses)'.format(desde, t, _m))
    print('  modo     : {}'.format('DRY RUN, no escribe nada' if dry else 'carga real'))
    print('  tablas   : {}'.format(', '.join(tablas)))
    print('-' * 74)
    print('frescura de los espejos que se leen:')
    for e in ESPEJOS:
        try:
            r = ch.query("SELECT toString(max(ingested_at)) FROM dwh.{}".format(e)).result_rows
            print('  {:<26} {}'.format(e, r[0][0] if r else '?'))
        except Exception as ex:
            print('  {:<26} NO SE PUDO LEER: {}'.format(e, ex))
    print('-' * 74)

    resumen = []
    pg = None
    sabor = None
    if not dry:
        sabor, pg = _pg(dsn)
        pg.autocommit = False
        print('postgres: {} conectado'.format(sabor))

    t0 = time.time()
    for tabla in tablas:
        sql = _sustituir(SQL[tabla], etiqueta, t, desde)
        t1 = time.time()

        n = ch.query('SELECT count() FROM ({})'.format(sql.strip())).result_rows[0][0]
        if dry:
            print('  {:<14} {:>12} filas   (dry run)'.format(tabla, _miles(n)))
            resumen.append({'tabla': tabla, 'filas_origen': int(n), 'filas_copiadas': 0})
            continue

        # NULL como cadena vacia en los dos lados. Por defecto ClickHouse
        # escribe \N y Postgres en modo csv espera vacio: sin esto una columna
        # numerica nula llega como el texto '\N' y el COPY revienta.
        flujo = _flujo_csv(cfg, sql,
                           {'format_csv_null_representation': '',
                            'output_format_csv_crlf_end_of_line': 0})

        cur = pg.cursor()
        cur.execute('TRUNCATE TABLE mig.{}'.format(tabla))
        copia = "COPY mig.{} FROM STDIN WITH (FORMAT csv, NULL '')".format(tabla)
        if sabor == 'psycopg2':
            cur.copy_expert(copia, flujo)
        else:
            with cur.copy(copia) as cp:
                while True:
                    trozo = flujo.read(1 << 20)
                    if not trozo:
                        break
                    cp.write(trozo)
        cur.execute('SELECT count(*) FROM mig.{}'.format(tabla))
        escritas = cur.fetchone()[0]
        pg.commit()
        cur.close()

        seg = time.time() - t1
        ok = '' if escritas == n else '   <-- NO CUADRA'
        print('  {:<14} {:>12} filas -> {:>12} en mig   {:>8}  {:>9}{}'.format(
            tabla, _miles(n), _miles(escritas), _dur(seg),
            '{:.1f} MB'.format(flujo.bytes_leidos / 1048576.0), ok))
        resumen.append({'tabla': tabla, 'filas_origen': int(n),
                        'filas_copiadas': int(escritas),
                        'mb': round(flujo.bytes_leidos / 1048576.0, 1),
                        'segundos': round(seg, 1)})

    if pg is not None:
        # La bitacora se escribe al final y en una transaccion propia: si algo
        # de arriba fallo, esta fila no deberia existir.
        cur = pg.cursor()
        for r in resumen:
            # Se borra el paso anterior de ESTA tabla antes de anotar el nuevo.
            # Sin esto, recargar una sola tabla deja dos filas del mismo paso y
            # sum(filas_escritas) de la bitacora deja de describir el contenido
            # de mig: paso en esta migracion, cuando mig.oc se recargo de 2.463
            # a 1.587 filas y el resumen siguio declarando las 2.463 viejas mas
            # las 1.587 nuevas. Un log que suma dos veces es peor que ninguno.
            cur.execute(
                'DELETE FROM mig.bitacora WHERE fase = %s AND etiqueta = %s AND paso = %s',
                ('F0-aterrizaje', etiqueta, r['tabla']))
            cur.execute(
                'INSERT INTO mig.bitacora (fase, etiqueta, paso, filas_leidas, '
                'filas_escritas, ok, detalle) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)',
                ('F0-aterrizaje', etiqueta, r['tabla'], r['filas_origen'],
                 r['filas_copiadas'], r['filas_origen'] == r['filas_copiadas'],
                 __import__('json').dumps(r)))
        pg.commit()
        cur.close()
        pg.close()

    print('-' * 74)
    total = sum(r['filas_copiadas'] for r in resumen)
    print('total {} filas en {}'.format(_miles(total), _dur(time.time() - t0)))
    descuadres = [r for r in resumen if r['filas_origen'] != r['filas_copiadas']]
    if descuadres and not dry:
        raise RuntimeError('tablas que no cuadran: {}'.format(
            [r['tabla'] for r in descuadres]))
    print('=' * 74)
    return {'etiqueta': etiqueta, 't': t, 'desde': desde, 'meses': _m, 'tablas': resumen}
