"""dl_pmp_movimientos — cursor de mryn_data.calculadora_pmp portado a ClickHouse.

Port LITERAL del cursor del procedure (fuente de verdad: cuerpo de
mryn_data.calculadora_pmp recuperado de /var/lib/mysql/mysql/proc.MAD):

    SELECT i.proveedor_id, 0 AS nc, 0 AS pid, i.hid, a.sku,
           i.dt_cierre AS dt_in, a.qty, 0 AS picking, 0 AS qnc,
           (SELECT IFNULL(SUM(c.valor),0) FROM tab_sku_precios c
             WHERE c.hid = i.hid AND c.sku = a.sku) AS costo,
           0 AS factura, i.id_externo
      FROM mstr_ingresos i, mstr_ingresos_aux a
     WHERE i.hid = a.hid AND a.sku = p_sku AND i.sucursal_id = p_sucursal_id
       AND i.hid NOT IN (10,11,12,13,14,15,16,17,18)
    UNION
    SELECT i.cliente_id AS proveedor_id, 0, i.pid, 0, a.sku,
           i.dt_pk_out, 0, a.picking, 0, 0, i.factura, i.id_externo
      FROM mstr_pedidos i, mstr_pedidos_aux a
     WHERE i.pid = a.pid AND i.dt_out IS NOT NULL AND i.id_externo IS NULL
       AND a.sku = p_sku AND i.sucursal_id = p_sucursal_id AND a.picking > 0
    UNION
    SELECT i.cliente_id AS proveedor_id, i.pid, 0, 0, a.sku,
           i.dt_out, 0, 0, a.entrega, 0, i.factura, i.id_externo
      FROM mstr_nc i, mstr_nc_aux a
     WHERE i.pid = a.pid AND i.dt_vencimiento IS NOT NULL
       AND a.sku = p_sku AND i.sucursal_id = p_sucursal_id
    ORDER BY 6, 4, 3, 2;

Diferencia de alcance (unica): el procedure recibe (p_sku, p_sucursal_id) y
corre un par a la vez. Aca traemos TODOS los pares de una sola pasada y
agregamos sucursal_id como columna. Cada par (sku, sucursal_id) sigue siendo
independiente (el costo no depende del PMP de otra sucursal), asi que el
resultado por par es identico.

DECISIONES DE PORTE (cada una anotada contra el codigo real)
------------------------------------------------------------
* UNION (no UNION ALL) => es DISTINCT. Se replica con SELECT DISTINCT sobre
  las 13 columnas (las 12 del cursor + sucursal_id, que es constante por par
  y por lo tanto no altera la deduplicacion). Esto ademas amortigua los
  duplicados residuales de los espejos ReplacingMergeTree.
* ORDER BY 6,4,3,2 => fecha, hid, pid, nc. En MariaDB los NULL van PRIMERO en
  ASC, asi que las ventas con dt_pk_out NULL se procesan al INICIO del kardex.
  Se replica con `fecha ASC NULLS FIRST`.
  Las columnas restantes se agregan como desempate SOLO para que la corrida
  sea determinista; el procedure deja ese orden indefinido.
* Filtro de ingresos: lista fija `hid NOT IN (10..18)`. No hay ningun filtro
  por observacion ni por LIKE 'TRAZA%'.
* Filtro de ventas: dt_out IS NOT NULL, id_externo IS NULL, picking > 0.
  Nada sobre dt_pk_out ni sobre pa.pmp.
* `0 AS factura` en la rama de ingresos: en MariaDB el UNION resuelve el tipo
  de la columna a varchar (las otras ramas traen varchar), asi que el literal
  entero 0 se materializa como la cadena '0'. Se emite '0'.
* id_externo de la rama de ventas: el WHERE exige `i.id_externo IS NULL`, por
  lo que esa columna es SIEMPRE NULL para ventas. Se emite '' y se documenta
  para que nadie busque ahi un 'AJUSTE'.
* id_externo de la rama de NC: es mstr_nc.id_externo, o sea el pid del PEDIDO
  PADRE. Es la llave que el transformer necesita para resolver el costo de la
  devolucion (ver mas abajo). En MySis la columna es VARCHAR; en el espejo es
  Nullable(Int32), por eso se emite con toString().

COSTO DEL INGRESO
-----------------
Subconsulta correlacionada `IFNULL(SUM(c.valor),0)` sobre tab_sku_precios por
(hid, sku). Se traduce a un LEFT JOIN contra la agregacion previa y coalesce 0.

  ATENCION — sin FINAL a proposito: dwh.mysis_tab_sku_precios esta creada con
  `ORDER BY tuple()`. En un ReplacingMergeTree con clave de orden vacia TODAS
  las filas comparten la misma clave, asi que FINAL colapsaria la tabla entera
  a UNA sola fila y el costo de todos los ingresos saldria mal. La tabla se
  carga por full load con EXCHANGE TABLES (pipeline
  mysis_tabla_tab_sku_precios_to_clickhouse), o sea que no tiene duplicados
  por construccion y no necesita FINAL. En el resto de los espejos, cuya clave
  de orden es la PK real, FINAL si se usa.

COSTO DE LA DEVOLUCION — por que NO se resuelve aca
---------------------------------------------------
El procedure lo resuelve DENTRO del loop, y depende del pid del pedido padre:

    SELECT IFNULL(MAX(pa.pmp), 0) INTO v_pmpnc
      FROM mstr_pedidos_aux pa
     WHERE pa.sku = p_sku
       AND pa.pid IN (SELECT id_externo FROM mstr_nc WHERE pid = v_nc);

Es decir: para la NC cuyo pid es v_nc, se toma su id_externo (= pid del pedido
padre) y se busca el MAX(pmp) de las lineas de ESE pedido con el MISMO sku.
Ese `pmp` es un valor PERSISTIDO por el PHP legacy en mstr_pedidos_aux; no lo
calcula este procedure, se lee tal cual.

Este loader entrega las dos piezas y el transformer arma la busqueda:
  1. la columna `id_externo` de cada fila de NC ya trae el pid del padre;
  2. el DataFrame `pmp_nc` es el diccionario (sku, pid) -> MAX(pmp), acotado a
     los pid efectivamente referenciados por alguna NC en alcance.
El transformer hace pmp_nc[(sku, int(id_externo))] con default 0, que es
exactamente el IFNULL(...,0) de arriba (si el id_externo es NULL o no parsea,
el IN(...) queda vacio y MAX devuelve NULL -> 0).

REPRESENTACION NUMERICA — leer antes de tocar nada
--------------------------------------------------
Las columnas qty, picking, qnc y costo (y pmp_max en pmp_nc) NO salen como
float ni como Decimal: salen como **int64 escalado por ESCALA_DECIMAL = 10^4**.
Motivo: son ~3,85M filas y todas las variables del procedure son DECIMAL(18,4).
Un float perderia precision y 16M objetos Decimal no caben en memoria; un
int64 escalado por 10^4 es exacto, ocupa 8 bytes y sobrevive intacto a la
serializacion que Mage hace entre bloques. El transformer los reconvierte con
Decimal(v).scaleb(-4), que es exacto.

kwargs
------
  skus        lista de sku para correr acotado (opcional)
  sucursales  lista de sucursal_id para correr acotado (opcional)
"""

import pandas as pd
from mage_ai.io.config import ConfigFileLoader
import clickhouse_connect

if 'data_loader' not in globals():
    from mage_ai.data_preparation.decorators import data_loader
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test


CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

# Escala fija de todas las variables DECIMAL(18,4) del procedure.
ESCALA_DECIMAL = 10 ** 4

# `i.hid NOT IN (10,11,12,13,14,15,16,17,18)` — lista literal del procedure.
HID_EXCLUIDOS = (10, 11, 12, 13, 14, 15, 16, 17, 18)

COLUMNAS = [
    'proveedor_id', 'nc', 'pid', 'hid', 'sucursal_id', 'sku', 'fecha',
    'qty', 'picking', 'qnc', 'costo', 'factura', 'id_externo',
]

COLS_INT32 = ['proveedor_id', 'nc', 'pid', 'hid', 'sucursal_id']
COLS_INT64_E4 = ['qty', 'picking', 'qnc', 'costo']
COLS_STR = ['sku', 'factura', 'id_externo']


def _client():
    cfg = ConfigFileLoader(CONFIG_PATH, PROFILE)
    use_https = str(cfg['CLICKHOUSE_INTERFACE']).lower() == 'https'
    return clickhouse_connect.get_client(
        host=cfg['CLICKHOUSE_HOST'],
        port=int(cfg['CLICKHOUSE_PORT']),
        username=cfg['CLICKHOUSE_USERNAME'],
        password=cfg['CLICKHOUSE_PASSWORD'],
        database=cfg['CLICKHOUSE_DATABASE'],
        secure=use_https,
    )


def _lista_sql_str(valores):
    """Literal SQL de una lista de strings, con las comillas escapadas."""
    return ', '.join("'" + str(v).replace('\\', '\\\\').replace("'", "\\'") + "'"
                     for v in valores)


def _lista_sql_int(valores):
    return ', '.join(str(int(v)) for v in valores)


def _filtros(kwargs):
    """Devuelve (filtro_sku, filtro_sucursal) como fragmentos AND ... o ''."""
    skus = kwargs.get('skus') or None
    sucursales = kwargs.get('sucursales') or None

    f_sku = ''
    if skus:
        f_sku = f'AND a.sku IN ({_lista_sql_str(skus)})'

    f_suc = ''
    if sucursales:
        f_suc = f'AND i.sucursal_id IN ({_lista_sql_int(sucursales)})'

    return f_sku, f_suc


def _sql_movimientos(f_sku, f_suc):
    return f"""
WITH precios AS (
    -- Subconsulta correlacionada del cursor:
    --   (SELECT IFNULL(SUM(c.valor),0) FROM tab_sku_precios c
    --     WHERE c.hid = i.hid AND c.sku = a.sku)
    -- SIN FINAL: la tabla es ORDER BY tuple() y FINAL la colapsaria a 1 fila.
    SELECT hid, sku, sum(valor) AS valor_sum
    FROM dwh.mysis_tab_sku_precios
    GROUP BY hid, sku
),
movs AS (
    -- UNION del procedure = DISTINCT: las filas identicas en las 12 columnas
    -- del SELECT colapsan en una sola. Se replica con SELECT DISTINCT.
    SELECT DISTINCT * FROM (

        -- ---------------- RAMA 1: INGRESOS ----------------
        SELECT
            toInt32(ifNull(i.proveedor_id, 0))                          AS proveedor_id,
            toInt32(0)                                                  AS nc,
            toInt32(0)                                                  AS pid,
            toInt32(i.hid)                                              AS hid,
            toInt32(ifNull(i.sucursal_id, 0))                           AS sucursal_id,
            ifNull(a.sku, '')                                           AS sku,
            accurateCastOrNull(i.dt_cierre, 'DateTime')                 AS fecha,
            toInt64(ifNull(a.qty, 0)) * {ESCALA_DECIMAL}                AS qty,
            toInt64(0)                                                  AS picking,
            toInt64(0)                                                  AS qnc,
            toInt64(round(toFloat64(ifNull(p.valor_sum, 0)) * {ESCALA_DECIMAL})) AS costo,
            -- `0 AS factura` del procedure: el UNION lo resuelve a varchar.
            '0'                                                         AS factura,
            ifNull(i.id_externo, '')                                    AS id_externo
        FROM dwh.mysis_mstr_ingresos AS i FINAL
        INNER JOIN dwh.mysis_mstr_ingresos_aux AS a FINAL ON a.hid = i.hid
        LEFT JOIN precios AS p ON p.hid = i.hid AND p.sku = a.sku
        WHERE i.hid NOT IN ({_lista_sql_int(HID_EXCLUIDOS)})
          AND a.sku IS NOT NULL
          AND i.sucursal_id IS NOT NULL
          {f_sku}
          {f_suc}

        UNION ALL

        -- ---------------- RAMA 2: VENTAS ----------------
        -- La fecha es dt_pk_out y PUEDE SER NULL. No se corrige: en MariaDB
        -- los NULL ordenan primero, asi que esas ventas se procesan al inicio
        -- del kardex. El ORDER BY de abajo usa NULLS FIRST para replicarlo.
        SELECT
            toInt32(ifNull(i.cliente_id, 0)),
            toInt32(0),
            toInt32(i.pid),
            toInt32(0),
            toInt32(ifNull(i.sucursal_id, 0)),
            ifNull(a.sku, ''),
            accurateCastOrNull(i.dt_pk_out, 'DateTime'),
            toInt64(0),
            toInt64(ifNull(a.picking, 0)) * {ESCALA_DECIMAL},
            toInt64(0),
            toInt64(0),
            ifNull(i.factura, ''),
            -- El WHERE exige id_externo IS NULL => siempre vacio para ventas.
            ''
        FROM dwh.mysis_mstr_pedidos AS i FINAL
        INNER JOIN dwh.mysis_mstr_pedidos_aux AS a FINAL ON a.pid = i.pid
        WHERE i.dt_out IS NOT NULL
          AND i.id_externo IS NULL
          AND a.picking > 0
          AND a.sku IS NOT NULL
          AND i.sucursal_id IS NOT NULL
          {f_sku}
          {f_suc}

        UNION ALL

        -- ---------------- RAMA 3: NOTAS DE CREDITO (DEVOLUCIONES) ----------------
        -- Ojo con las posiciones: aca i.pid cae en la columna `nc`, no en `pid`.
        SELECT
            toInt32(ifNull(i.cliente_id, 0)),
            toInt32(i.pid),
            toInt32(0),
            toInt32(0),
            toInt32(ifNull(i.sucursal_id, 0)),
            ifNull(a.sku, ''),
            accurateCastOrNull(i.dt_out, 'DateTime'),
            toInt64(0),
            toInt64(0),
            toInt64(ifNull(a.entrega, 0)) * {ESCALA_DECIMAL},
            toInt64(0),
            ifNull(i.factura, ''),
            -- pid del pedido PADRE: llave del MAX(pmp) que resuelve el costo.
            ifNull(toString(i.id_externo), '')
        FROM dwh.mysis_mstr_nc AS i FINAL
        INNER JOIN dwh.mysis_mstr_nc_aux AS a FINAL ON a.pid = i.pid
        WHERE i.dt_vencimiento IS NOT NULL
          AND a.sku IS NOT NULL
          AND i.sucursal_id IS NOT NULL
          {f_sku}
          {f_suc}
    )
)
SELECT *
FROM movs
ORDER BY
    sku ASC,
    sucursal_id ASC,
    -- ORDER BY 6,4,3,2 del procedure (NULLs primero, como MariaDB):
    fecha ASC NULLS FIRST,
    hid ASC,
    pid ASC,
    nc ASC,
    -- Desempate SOLO por determinismo; el procedure lo deja indefinido.
    qty ASC, picking ASC, qnc ASC, costo ASC,
    proveedor_id ASC, factura ASC, id_externo ASC
"""


def _sql_pmp_nc(f_sku, f_suc):
    """Diccionario (sku, pid) -> MAX(pmp) para resolver el costo de la devolucion.

    Replica `SELECT IFNULL(MAX(pa.pmp),0) FROM mstr_pedidos_aux pa
             WHERE pa.sku = p_sku AND pa.pid IN (
                 SELECT id_externo FROM mstr_nc WHERE pid = v_nc)`.
    El procedure NO filtra mstr_pedidos_aux por sucursal, asi que aca tampoco:
    el filtro de sucursal solo acota el conjunto de NC de las que salen los pid.
    """
    f_suc_nc = f_suc.replace('i.sucursal_id', 'sucursal_id') if f_suc else ''
    f_sku_pa = f_sku.replace('a.sku', 'pa.sku') if f_sku else ''
    return f"""
SELECT
    pa.sku                                                       AS sku,
    toInt32(pa.pid)                                              AS pid,
    toInt64(round(toFloat64(max(pa.pmp)) * {ESCALA_DECIMAL}))    AS pmp_max
FROM dwh.mysis_mstr_pedidos_aux AS pa FINAL
WHERE pa.sku IS NOT NULL
  AND pa.pid IN (
      SELECT toInt32(id_externo)
      FROM dwh.mysis_mstr_nc FINAL
      WHERE dt_vencimiento IS NOT NULL
        AND id_externo IS NOT NULL
        {f_suc_nc}
  )
  {f_sku_pa}
GROUP BY pa.sku, pa.pid
"""


def _normalizar(df):
    for c in COLS_INT32:
        df[c] = pd.to_numeric(df[c], errors='coerce').fillna(0).astype('int32')
    for c in COLS_INT64_E4:
        df[c] = pd.to_numeric(df[c], errors='coerce').fillna(0).astype('int64')
    for c in COLS_STR:
        df[c] = df[c].where(pd.notna(df[c]), '').astype('object')
    # NaT se conserva a proposito: marca las ventas sin dt_pk_out y los
    # ingresos sin dt_cierre, que el transformer trata segun el procedure.
    df['fecha'] = pd.to_datetime(df['fecha'], errors='coerce')
    return df[COLUMNAS]


@data_loader
def load_data(*args, **kwargs):
    client = _client()

    n_precios = client.query('SELECT count() FROM dwh.mysis_tab_sku_precios').result_rows[0][0]
    if n_precios == 0:
        print(
            'AVISO: dwh.mysis_tab_sku_precios esta VACIA. El costo de todo ingreso '
            'saldra 0 y, por la regla "IF v_costo = 0 THEN SET v_costo = v_cpp", '
            'todo el kardex quedara valorizado en 0. Corre primero el pipeline '
            'mysis_tabla_tab_sku_precios_to_clickhouse.'
        )

    f_sku, f_suc = _filtros(kwargs)
    if f_sku or f_suc:
        print(f'Corrida acotada -> skus={kwargs.get("skus")} sucursales={kwargs.get("sucursales")}')

    settings = {
        'max_execution_time': int(kwargs.get('max_execution_time') or 3600),
        'join_use_nulls': 0,
    }

    df = client.query_df(_sql_movimientos(f_sku, f_suc), settings=settings)
    df = _normalizar(df)
    print(f'Movimientos (UNION DISTINCT): {len(df)} filas, '
          f'{df.groupby(["sku", "sucursal_id"]).ngroups} pares (sku, sucursal)')

    pmp = client.query_df(_sql_pmp_nc(f_sku, f_suc), settings=settings)
    if len(pmp):
        pmp['sku'] = pmp['sku'].where(pd.notna(pmp['sku']), '').astype('object')
        pmp['pid'] = pd.to_numeric(pmp['pid'], errors='coerce').fillna(0).astype('int32')
        pmp['pmp_max'] = pd.to_numeric(pmp['pmp_max'], errors='coerce').fillna(0).astype('int64')
    else:
        pmp = pd.DataFrame({'sku': pd.Series(dtype='object'),
                            'pid': pd.Series(dtype='int32'),
                            'pmp_max': pd.Series(dtype='int64')})
    print(f'Diccionario (sku, pid) -> MAX(pmp) para devoluciones: {len(pmp)} entradas')

    return {
        'movimientos': df,
        'pmp_nc': pmp,
        'escala_decimal': ESCALA_DECIMAL,
    }


@test
def test_output(output, *args) -> None:
    assert output is not None, 'The output is undefined'
    assert isinstance(output, dict), 'El loader devuelve un dict con movimientos y pmp_nc'
    assert 'movimientos' in output and 'pmp_nc' in output, 'Faltan claves en la salida'
    mov = output['movimientos']
    assert list(mov.columns) == COLUMNAS, f'Columnas inesperadas: {list(mov.columns)}'
    for c in COLS_INT64_E4:
        assert str(mov[c].dtype) == 'int64', (
            f'{c} debe ser int64 escalado por {ESCALA_DECIMAL}, no {mov[c].dtype}'
        )
