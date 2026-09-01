"""kardex_pmp_sql — el kardex PMP entero resuelto DENTRO de ClickHouse.

Reemplaza funcionalmente a la cadena dl_pmp_movimientos -> tr_pmp_kardex ->
de_pmp_detalle_to_clickhouse: es un unico INSERT ... SELECT, no mueve una sola
fila a Mage. Los tres bloques viejos quedan como referencia legible del
procedure; este es el que conviene correr.

POR QUE SQL Y NO PYTHON (medido el 2026-08-14)
----------------------------------------------
La version Python trae ~2,6M movimientos al worker y recorre el loop en
Python con decimal.Decimal. La version SQL corre el mismo recorrido con
arrayFold dentro de ClickHouse:
  * 14 pares de control (16.085 filas)          -> 4,9 s
  * universo completo: 69.946 pares, 2,64M movimientos, sum(n^2)=1,96e9
    -> del orden de 2-5 min.
El coste del fold es cuadratico DENTRO de cada par (el acumulador es un array
que crece), pero el par mas grande tiene 9.541 movimientos y el p50 es 6, asi
que sum(n^2) se mantiene chico. No hay que paralelizar por par a mano.

EXACTITUD VERIFICADA (2026-08-14, espejos recargados por full reload)
--------------------------------------------------------------------
Contra mryn_data.pmp_detalle, 14 pares elegidos para cubrir todos los caminos
del algoritmo (IN-AJ, traspasos, NC, DEVOLUCION NC, dt_cierre NULL, sku sin
precio, AJUSTE_inv, TRAZA con hid > 18). Checksum CRC32 fila a fila sobre
seq|tipo|hid|pid|nc|fecha|los 7 decimales, MAS un segundo checksum sobre
seq|proveedor_id|factura|id_externo:

  CIERRAN LOS 14 DE 14, checksum identico, 16.085 filas.

Para reproducir la comparacion hay que normalizar tres cosas o da falso
negativo:
  * MySQL FORMAT(x,4) mete separador de miles -> REPLACE(...,',','').
  * ClickHouse toString(Decimal) PODA los ceros a la derecha ('100', no
    '100.0000') -> usar toDecimalString(x,4).
  * `fecha` hay que leerla en America/Santiago (ver ZONA HORARIA).
  * factura e id_externo son NULL en MySis y '' aca; CONCAT_WS SALTEA los NULL
    (se come tambien el separador) -> IFNULL(x,'') del lado MySQL.

ZONA HORARIA (ojo, es la trampa del port)
-----------------------------------------
El server de ClickHouse corre en UTC y los espejos mysis_* guardan los datetime
YA CONVERTIDOS a UTC: MySis 2021-01-06 20:12:04 -> espejo 2021-01-06 23:12:04.
El offset varia con el horario de verano chileno (-03 en verano, -04 en
invierno), pero es una transformacion monotona de instantes, asi que NO altera
el orden del cursor ni un solo numero del kardex. `fecha` queda expresada en
UTC, consistente con el resto de la capa de espejos; para compararla contra
MySis hay que hacer toTimeZone(fecha,'America/Santiago').

El centinela de `dt_cierre IS NULL` es el UNICO timestamp que no pasa por un
espejo: es un literal en hora local dentro del procedure. Si se escribe crudo
queda 3 horas corrido respecto de MySis. Se emite convertido (ver
SENTINELA_DT_NULL). Sin esa conversion fallaba 1 par de 14 y en el universo
completo ensuciaria 107 filas.

ARITMETICA
----------
Todo entero escalado x10^4 (Int64), intermedios en Int128. Las variables del
procedure son DECIMAL(18,4) y MariaDB redondea HALF-UP (lejos del cero) en
cada asignacion.
  * qty, picking y entrega son int(11) en MySis => elqty es SIEMPRE entero =>
    saldo_valorizado = saldo_valorizado + elqty_unidades * cpp es EXACTO y no
    redondea nunca. Verificado contra los 14 pares.
  * El UNICO redondeo real es la division cpp = saldovalor / saldoqty.
    MariaDB con div_precision_increment=4 la evalua a escala 4+4=8 y recien la
    asignacion a DECIMAL(18,4) la baja a 4: son DOS redondeos HALF-UP
    encadenados, y asi se replican (bloques g1/g2/g3).
El residuo acumulado es parte del resultado correcto: en ('005153009635',2)
el saldo_valorizado final termina en .0110 tanto en MySis como aca.

kwargs
------
  scope         lista de (sucursal_id, sku) para correr acotado.
                Vacio o ausente = UNIVERSO COMPLETO.
  target_table  destino (default dwh.mysis_pmp_detalle).
  truncate      True (default) vacia el destino antes de insertar.
"""

from mage_ai.io.config import ConfigFileLoader
import clickhouse_connect

if 'custom' not in globals():
    from mage_ai.data_preparation.decorators import custom

CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'
TABLE_DEFAULT = 'dwh.mysis_pmp_detalle'

# `i.hid NOT IN (10,...,18)` — lista literal del procedure.
HID_EXCLUIDOS = '10,11,12,13,14,15,16,17,18'

# `IF v_dt_in IS NULL THEN SET v_dt_in = '2020-12-01 08:00:00'` — literal del
# procedure en hora de Chile. Todo lo demas llega desde los espejos, que ya
# vienen en UTC; este no, asi que hay que convertirlo explicitamente o queda
# 3 horas corrido. Da 2020-12-01 11:00:00 UTC (diciembre = verano, -03).
SENTINELA_DT_NULL = (
    "toDateTime(toUnixTimestamp(toDateTime('2020-12-01 08:00:00','America/Santiago')))"
)


def _client():
    cfg = ConfigFileLoader(CONFIG_PATH, PROFILE)
    return clickhouse_connect.get_client(
        host=cfg['CLICKHOUSE_HOST'],
        port=int(cfg['CLICKHOUSE_PORT']),
        username=cfg['CLICKHOUSE_USERNAME'],
        password=cfg['CLICKHOUSE_PASSWORD'],
        database=cfg['CLICKHOUSE_DATABASE'],
        secure=str(cfg['CLICKHOUSE_INTERFACE']).lower() == 'https',
    )


def _scope_sql(scope):
    """'' = universo completo; si no, AND (sucursal_id, sku) IN (...)."""
    if not scope:
        return ''
    pares = ', '.join(
        "({}, '{}')".format(int(s), str(k).replace("\\", "\\\\").replace("'", "\\'"))
        for s, k in scope
    )
    return 'AND (i.sucursal_id, a.sku) IN ({})'.format(pares)


def build_sql(target_table='dwh.mysis_pmp_detalle', scope=None):
    f = _scope_sql(scope)
    return """
INSERT INTO {tgt}
(sucursal_id, sku, seq, tipo, proveedor_id, hid, pid, nc, fecha, ingreso, venta,
 devolucion, costo, saldo_qty, saldo_valorizado, pmp, factura, id_externo, dt_calculo)
WITH
-- Subconsulta correlacionada del cursor:
--   (SELECT IFNULL(SUM(c.valor),0) FROM tab_sku_precios c
--     WHERE c.hid = i.hid AND c.sku = a.sku)
-- SIN FINAL a proposito: dwh.mysis_tab_sku_precios es ORDER BY tuple(), o sea
-- que todas las filas comparten clave y FINAL colapsaria la tabla a UNA fila.
-- Se carga por full load con EXCHANGE TABLES, no tiene duplicados.
precios AS (
    SELECT hid, sku, sum(valor) AS vs FROM dwh.mysis_tab_sku_precios GROUP BY hid, sku
),
-- Costo de la devolucion:
--   SELECT IFNULL(MAX(pa.pmp),0) FROM mstr_pedidos_aux pa
--    WHERE pa.sku = p_sku
--      AND pa.pid IN (SELECT id_externo FROM mstr_nc WHERE pid = v_nc)
-- El procedure NO filtra por sucursal aca, asi que tampoco se filtra.
pmp_padre AS (
    SELECT sku, pid, max(pmp) AS pmp_max
    FROM dwh.mysis_mstr_pedidos_aux FINAL WHERE sku IS NOT NULL GROUP BY sku, pid
),
-- UNION del cursor = DISTINCT sobre las 12 columnas. sucursal_id es constante
-- por par, asi que agregarlo no cambia la deduplicacion.
mov_raw AS (
    SELECT DISTINCT sucursal_id, sku, fms, hid, pid, nc, q, pk, qn, co, prov, fac, ext
    FROM (
        -- ---------------- INGRESOS ----------------
        SELECT toInt32(ifNull(i.sucursal_id,0)) AS sucursal_id, ifNull(a.sku,'') AS sku,
               ifNull(toInt64(toUnixTimestamp64Milli(i.dt_cierre)), toInt64(-1)) AS fms,
               toInt32(i.hid) AS hid, toInt32(0) AS pid, toInt32(0) AS nc,
               toInt64(ifNull(a.qty,0))*10000 AS q, toInt64(0) AS pk, toInt64(0) AS qn,
               toInt64(coalesce(p.vs,0)*10000) AS co, toInt32(ifNull(i.proveedor_id,0)) AS prov,
               -- `0 AS factura` del procedure: el UNION lo resuelve a varchar.
               '0' AS fac, ifNull(i.id_externo,'') AS ext
        FROM dwh.mysis_mstr_ingresos AS i FINAL
        INNER JOIN dwh.mysis_mstr_ingresos_aux AS a FINAL ON a.hid = i.hid
        LEFT JOIN precios AS p ON p.hid = i.hid AND p.sku = a.sku
        WHERE i.hid NOT IN ({hid_ex}) AND a.sku IS NOT NULL AND i.sucursal_id IS NOT NULL
          {f}
        UNION ALL
        -- ---------------- VENTAS ----------------
        -- fecha = dt_pk_out y PUEDE ser NULL: se marca con fms = -1, que ordena
        -- ANTES que cualquier fecha real, igual que los NULL en MariaDB.
        SELECT toInt32(ifNull(i.sucursal_id,0)), ifNull(a.sku,''),
               ifNull(toInt64(toUnixTimestamp64Milli(toDateTime64(i.dt_pk_out,3))), toInt64(-1)),
               toInt32(0), toInt32(i.pid), toInt32(0), toInt64(0), toInt64(a.picking)*10000,
               toInt64(0), toInt64(0), toInt32(ifNull(i.cliente_id,0)), ifNull(i.factura,''),
               -- El WHERE exige id_externo IS NULL => siempre vacio en ventas.
               ''
        FROM dwh.mysis_mstr_pedidos AS i FINAL
        INNER JOIN dwh.mysis_mstr_pedidos_aux AS a FINAL ON a.pid = i.pid
        WHERE i.dt_out IS NOT NULL AND i.id_externo IS NULL AND a.picking > 0
          AND a.sku IS NOT NULL AND i.sucursal_id IS NOT NULL
          {f}
        UNION ALL
        -- ---------------- NOTAS DE CREDITO ----------------
        -- Ojo: i.pid cae en la columna `nc`, no en `pid`.
        -- ext = mstr_nc.id_externo = pid del PEDIDO PADRE (llave del MAX(pmp)).
        SELECT toInt32(ifNull(i.sucursal_id,0)), ifNull(a.sku,''),
               ifNull(toInt64(toUnixTimestamp64Milli(i.dt_out)), toInt64(-1)),
               toInt32(0), toInt32(0), toInt32(i.pid), toInt64(0), toInt64(0),
               toInt64(ifNull(a.entrega,0))*10000, toInt64(0),
               toInt32(ifNull(i.cliente_id,0)), ifNull(i.factura,''),
               ifNull(toString(i.id_externo),'')
        FROM dwh.mysis_mstr_nc AS i FINAL
        INNER JOIN dwh.mysis_mstr_nc_aux AS a FINAL ON a.pid = i.pid
        WHERE i.dt_vencimiento IS NOT NULL AND a.sku IS NOT NULL AND i.sucursal_id IS NOT NULL
          {f}
    )
),
mov AS (
    SELECT m.sucursal_id AS sucursal_id, m.sku AS sku, m.fms AS fms, m.hid AS hid,
           m.pid AS pid, m.nc AS nc, m.q AS q, m.pk AS pk, m.qn AS qn, m.co AS co,
           m.prov AS prov, m.fac AS fac, m.ext AS ext,
           toInt64(coalesce(pp.pmp_max,0)*10000) AS pn,   -- pmpnc
           m.q - m.pk + m.qn                     AS el,   -- elqty (siempre entero)
           -- `IF v_costo = 0 THEN v_costo = v_cpp` + `IF v_id_externo = 'AJUSTE'
           -- THEN v_costo = v_cpp` (igualdad EXACTA, no LIKE).
           toUInt8((m.co = 0) OR (m.ext = 'AJUSTE')) AS uc,
           toUInt8(multiIf(m.q>0,1,m.qn>0,2,3))      AS br  -- 1=ingreso 2=devol 3=venta
    FROM mov_raw AS m
    LEFT JOIN pmp_padre AS pp
           ON pp.sku = m.sku AND pp.pid = multiIf(m.nc != 0, toInt32OrZero(m.ext), -1)
),
-- ORDER BY 6,4,3,2 del procedure = fecha, hid, pid, nc (NULLs primero).
-- Las 7 columnas siguientes son desempate SOLO por determinismo; el procedure
-- deja ese orden indefinido. arraySort ordena la tupla lexicograficamente.
grp AS (
    SELECT sucursal_id, sku,
           arraySort(groupArray((fms, hid, pid, nc, q, pk, qn, co, prov, fac, ext,
                                 pn, el, toInt64(uc), toInt64(br)))) AS arr
    FROM mov GROUP BY sucursal_id, sku
),
-- El acumulador es el propio historial: acc[-1] es el estado anterior
-- (.2=saldoqty .3=saldovalor .4=cpp). Se siembra con una tupla de ceros que
-- se descarta con arraySlice(...,2). Cada elemento emitido es
-- (costo, sq, sv, cpp, hay_inaj, inaj_qty, inaj_costo, inaj_sq, inaj_sv, inaj_cpp).
-- Los arrayMap(v -> ..., [expr])[1] son let-bindings: ClickHouse no tiene
-- variables locales dentro de un lambda.
folded AS (
    SELECT sucursal_id, sku, arr,
      arraySlice(arrayFold((acc, x) -> arrayPushBack(acc,
        arrayMap(aj ->                 -- aj = (hay_inaj, sqA, svA, cppA, elqty2)
          arrayMap(nx ->               -- nx = (costo_usado, sq1, sv1)
            (
              -- costo de la fila MOV: ingreso -> cpp previo o tab_sku_precios;
              -- devolucion -> pmpnc; venta -> el costo crudo del cursor (0).
              multiIf(x.15 = 1, if(x.14 = 1, acc[-1].4, x.8), x.15 = 2, x.12, x.8),
              nx.2, nx.3,
              -- cpp = saldovalor/saldoqty con los DOS redondeos (escala 8 y 4).
              -- En ventas el PMP NO se recalcula: queda en cppA.
              multiIf(
                x.15 = 1, if(nx.2 != 0, arrayMap(g1 -> if(g1 >= 0, toInt64(intDiv(g1 + 5000, 10000)), toInt64(0) - toInt64(intDiv(5000 - g1, 10000))), [ if((nx.3 < 0) != (nx.2 < 0), toInt128(-1), toInt128(1)) * intDiv(2 * abs(toInt128(nx.3)) * 100000000 + abs(toInt128(nx.2)), 2 * abs(toInt128(nx.2))) ])[1], toInt64(0)),
                x.15 = 2, if(nx.2 != 0, arrayMap(g2 -> if(g2 >= 0, toInt64(intDiv(g2 + 5000, 10000)), toInt64(0) - toInt64(intDiv(5000 - g2, 10000))), [ if((nx.3 < 0) != (nx.2 < 0), toInt128(-1), toInt128(1)) * intDiv(2 * abs(toInt128(nx.3)) * 100000000 + abs(toInt128(nx.2)), 2 * abs(toInt128(nx.2))) ])[1], x.12),
                aj.4),
              aj.1, aj.5, x.8, aj.2, aj.3, aj.4
            ),
            [ arrayMap(c -> (c, aj.2 + x.13,
                             aj.3 + toInt64(intDiv(toInt128(x.13), 10000) * toInt128(c))),
                [ multiIf(x.15 = 1, if(x.14 = 1, acc[-1].4, x.8), x.15 = 2, x.12, aj.4) ])[1] ])[1],
          -- Fila sintetica IN-AJ: solo en ventas y solo si saldoqty < picking.
          [ arrayMap(h ->
              arrayMap(s2 ->
                ( h, if(h = 1, x.6, acc[-1].2), s2,
                  if(h = 1, if(x.6 != 0, arrayMap(g3 -> if(g3 >= 0, toInt64(intDiv(g3 + 5000, 10000)), toInt64(0) - toInt64(intDiv(5000 - g3, 10000))), [ if((s2 < 0) != (x.6 < 0), toInt128(-1), toInt128(1)) * intDiv(2 * abs(toInt128(s2)) * 100000000 + abs(toInt128(x.6)), 2 * abs(toInt128(x.6))) ])[1], toInt64(0)), acc[-1].4),
                  if(h = 1, x.6 - acc[-1].2, toInt64(0)) ),
                [ if(h = 1, acc[-1].3 + toInt64(intDiv(toInt128(x.6) - toInt128(acc[-1].2), 10000) * toInt128(acc[-1].4)), acc[-1].3) ])[1],
              [ toInt64(if((x.15 = 3) AND (acc[-1].2 < x.6), 1, 0)) ])[1] ])[1]
      ), arr,
      [(toInt64(0),toInt64(0),toInt64(0),toInt64(0),toInt64(0),toInt64(0),toInt64(0),toInt64(0),toInt64(0),toInt64(0))]
      ), 2) AS st
    FROM grp
),
-- Explota a filas: IN-AJ (si la hay) y despues su MOV, en ese orden, que es el
-- orden de INSERT del procedure y por lo tanto el orden de `id` en pmp_detalle.
exploded AS (
    SELECT sucursal_id, sku,
      arrayJoin(arrayMap((e, i) -> (toUInt32(i), e), flat, arrayEnumerate(flat))) AS r
    FROM (
      SELECT sucursal_id, sku,
        arrayFlatten(arrayMap((x, s) ->
          if(s.5 = 1,
            [ ( 'IN-AJ', toInt32(0), toInt32(0), toInt32(0), toInt32(0),
                -- DATE_SUB(v_dt_in, INTERVAL 1 HOUR)
                if(x.1 = -1, toDateTime(0), toDateTime(intDiv(x.1, 1000) - 3600)),
                s.6, toInt64(0), toInt64(0), s.7, s.8, s.9, s.10, '0', x.11 ),
              ( 'MOV', toInt32(if((x.15 = 1) AND (x.1 = -1), 0, x.9)), toInt32(x.2), toInt32(x.3), toInt32(x.4),
                multiIf((x.15 = 1) AND (x.1 = -1), {sent}, x.1 = -1, toDateTime(0), toDateTime(intDiv(x.1, 1000))),
                x.5, x.6, x.7, s.1, s.2, s.3, s.4, x.10, x.11 ) ],
            [ ( 'MOV', toInt32(if((x.15 = 1) AND (x.1 = -1), 0, x.9)), toInt32(x.2), toInt32(x.3), toInt32(x.4),
                -- `IF v_dt_in IS NULL THEN v_dt_in='2020-12-01 08:00:00';
                --   v_proveedor_id=0` — SOLO en la rama de ingreso. El literal
                -- esta en hora de Chile y los espejos estan en UTC: hay que
                -- convertirlo (SENTINELA_DT_NULL) o queda 3 horas corrido.
                multiIf((x.15 = 1) AND (x.1 = -1), {sent}, x.1 = -1, toDateTime(0), toDateTime(intDiv(x.1, 1000))),
                x.5, x.6, x.7, s.1, s.2, s.3, s.4, x.10, x.11 ) ]),
          arr, st)) AS flat
      FROM folded
    )
)
SELECT sucursal_id, sku, r.1 AS seq, r.2.1 AS tipo, r.2.2 AS proveedor_id, r.2.3 AS hid,
       r.2.4 AS pid, r.2.5 AS nc, r.2.6 AS fecha,
       CAST(toDecimal128(r.2.7,4)/10000  AS Decimal(18,4)) AS ingreso,
       CAST(toDecimal128(r.2.8,4)/10000  AS Decimal(18,4)) AS venta,
       CAST(toDecimal128(r.2.9,4)/10000  AS Decimal(18,4)) AS devolucion,
       CAST(toDecimal128(r.2.10,4)/10000 AS Decimal(18,4)) AS costo,
       CAST(toDecimal128(r.2.11,4)/10000 AS Decimal(18,4)) AS saldo_qty,
       CAST(toDecimal128(r.2.12,4)/10000 AS Decimal(18,4)) AS saldo_valorizado,
       CAST(toDecimal128(r.2.13,4)/10000 AS Decimal(18,4)) AS pmp,
       r.2.14 AS factura, r.2.15 AS id_externo, now() AS dt_calculo
FROM exploded
""".format(tgt=target_table, hid_ex=HID_EXCLUIDOS, f=f, sent=SENTINELA_DT_NULL)


@custom
def run_kardex(*args, **kwargs):
    target = str(kwargs.get('target_table') or TABLE_DEFAULT)
    if '.' not in target:
        target = 'dwh.{}'.format(target)
    scope = kwargs.get('scope') or None

    client = _client()

    n_precios = client.query('SELECT count() FROM dwh.mysis_tab_sku_precios').result_rows[0][0]
    if n_precios == 0:
        raise Exception(
            'dwh.mysis_tab_sku_precios esta VACIA. El costo de todo ingreso saldria 0 y, '
            'por la regla "IF v_costo = 0 THEN SET v_costo = v_cpp", el kardex entero '
            'quedaria valorizado en 0. Corre antes mysis_tabla_tab_sku_precios_to_clickhouse.'
        )

    if kwargs.get('truncate', True):
        client.command('TRUNCATE TABLE {}'.format(target))

    sql = build_sql(target_table=target, scope=scope)
    client.command(sql, settings={
        'max_execution_time': int(kwargs.get('max_execution_time') or 3600),
        'join_use_nulls': 0,
    })

    filas = client.query('SELECT count() FROM {}'.format(target)).result_rows[0][0]
    pares = client.query(
        'SELECT uniqExact((sucursal_id, sku)) FROM {}'.format(target)).result_rows[0][0]
    print('{}: {} filas, {} pares (sku, sucursal)'.format(target, filas, pares))
    return {'target_table': target, 'rows': filas, 'pares': pares,
            'scope': 'universo completo' if not scope else scope}
