"""tr_pmp_kardex — port literal del LOOP de mryn_data.calculadora_pmp.

Recorre par por par (sku, sucursal_id). Cada par es INDEPENDIENTE: el costo de
un ingreso sale de tab_sku_precios y el de una devolucion del pmp persistido en
mstr_pedidos_aux, asi que ningun par lee el PMP de otro. No hay orden global.

CUERPO ORIGINAL QUE SE ESTA PORTANDO (fuente de verdad):

    IF v_qty > 0 THEN                                   -- INGRESO
        IF v_costo = 0 THEN SET v_costo = v_cpp; END IF;
        IF v_dt_in IS NULL OR v_dt_in = '0000-00-00 00:00:00' THEN
            SET v_dt_in = '2020-12-01 08:00:00'; SET v_proveedor_id = 0;
        END IF;
        IF v_id_externo = 'AJUSTE' THEN SET v_costo = v_cpp; END IF;
        SET v_cpp = v_costo;
        SET v_elqty = v_qty - v_picking + v_qnc;
        SET v_saldoqty = v_saldoqty + v_elqty;
        SET v_saldovalor = v_saldovalor + (v_elqty * v_cpp);
        SET v_cpp = v_saldovalor / v_saldoqty;
    ELSE
        IF v_qnc > 0 THEN                               -- DEVOLUCION
            SELECT IFNULL(MAX(pa.pmp), 0) INTO v_pmpnc ...;
            SET v_cpp = v_pmpnc;
            SET v_costo = v_pmpnc;
            SET v_elqty = v_qty - v_picking + v_qnc;
            SET v_saldoqty = v_saldoqty + v_elqty;
            SET v_saldovalor = v_saldovalor + (v_elqty * v_cpp);
            IF v_saldoqty <> 0 THEN SET v_cpp = v_saldovalor / v_saldoqty; END IF;
        ELSE                                            -- VENTA
            SET v_elqty = v_qty - v_picking + v_qnc;
            IF v_saldoqty < v_picking THEN
                SET v_elqty2 = v_picking - v_saldoqty;
                SET v_saldoqty = v_saldoqty + v_elqty2;
                SET v_saldovalor = v_saldovalor + (v_elqty2 * v_cpp);
                IF v_saldoqty <> 0 THEN SET v_cpp = v_saldovalor / v_saldoqty;
                                   ELSE SET v_cpp = 0; END IF;
                SET v_nuevafecha = DATE_SUB(v_dt_in, INTERVAL 1 HOUR);
                INSERT INTO tmp_pmp_detalle (...) VALUES
                    ('IN-AJ', 0, 0, 0, 0, p_sucursal_id, p_sku,
                     v_nuevafecha, v_elqty2, 0, 0, v_costo,
                     v_saldoqty, v_saldovalor, v_cpp, '0', v_id_externo);
            END IF;
            SET v_saldoqty = v_saldoqty + v_elqty;
            SET v_saldovalor = v_saldovalor + (v_elqty * v_cpp);
        END IF;
    END IF;

    INSERT INTO tmp_pmp_detalle (...) VALUES
        ('MOV', v_proveedor_id, v_hid, v_pid, v_nc, p_sucursal_id, v_sku,
         v_dt_in, v_qty, v_picking, v_qnc, v_costo,
         v_saldoqty, v_saldovalor, v_cpp, v_factura, v_id_externo);

PRECISION
---------
Todas las variables del procedure son DECIMAL(18,4) y MariaDB REDONDEA (half-up)
en cada ASIGNACION. Aca se usa decimal.Decimal y se cuantiza a 4 decimales con
ROUND_HALF_UP despues de cada asignacion a v_cpp, v_saldoqty, v_saldovalor,
v_costo, v_elqty, v_elqty2 y v_pmpnc. Nunca float. El residuo de redondeo es
parte del resultado correcto y por eso se replica el punto exacto donde ocurre.

La division merece un parrafo aparte: en MariaDB `a / b` con a DECIMAL(_,4)
produce escala 4 + div_precision_increment (default 4) = 8, y recien la
ASIGNACION a la variable DECIMAL(18,4) redondea a 4. Son DOS redondeos
encadenados, no uno. _div() los replica en ese orden.
"""

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP, getcontext, localcontext
from array import array

import numpy as np
import pandas as pd

if 'transformer' not in globals():
    from mage_ai.data_preparation.decorators import transformer
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test


# Holgura suficiente para que ninguna operacion intermedia se redondee por
# limite de contexto; el unico redondeo que existe es el explicito de q4/q8.
getcontext().prec = 60

Q4 = Decimal('0.0001')
Q8 = Decimal('0.00000001')
CERO = Decimal(0)

ESCALA_DECIMAL = 10 ** 4

# `SET v_dt_in = '2020-12-01 08:00:00'` (rama INGRESO, fecha nula).
FECHA_DEFECTO_SEC = int(np.datetime64('2020-12-01T08:00:00', 's').astype('int64'))

# Las ventas y devoluciones con fecha nula NO se corrigen en el procedure (la
# correccion vive solo en la rama de ingreso). Como dwh.mysis_pmp_detalle.fecha
# es DateTime NOT NULL, se materializan en el minimo de DateTime de ClickHouse,
# 1970-01-01 00:00:00, que ademas conserva el "ordenan primero" del cursor.
FECHA_NULA_SEC = 0

TIPO_MOV = 'MOV'
TIPO_IN_AJ = 'IN-AJ'

COLUMNAS_SALIDA = [
    'sucursal_id', 'sku', 'seq', 'tipo', 'proveedor_id', 'hid', 'pid', 'nc',
    'fecha', 'ingreso', 'venta', 'devolucion', 'costo', 'saldo_qty',
    'saldo_valorizado', 'pmp', 'factura', 'id_externo', 'dt_calculo',
]

# Columnas Decimal(18,4) que salen como int64 escalado por ESCALA_DECIMAL.
COLUMNAS_DECIMAL = [
    'ingreso', 'venta', 'devolucion', 'costo',
    'saldo_qty', 'saldo_valorizado', 'pmp',
]


def _q4(x):
    """Asignacion a DECIMAL(18,4): redondeo half-up a 4 decimales."""
    return x.quantize(Q4, rounding=ROUND_HALF_UP)


def _div(num, den):
    """`v_x = v_saldovalor / v_saldoqty` tal como lo evalua MariaDB.

    Escala del cociente = escala del dividendo (4) + div_precision_increment (4)
    = 8; la asignacion posterior a DECIMAL(18,4) redondea a 4. Dos redondeos.
    """
    return _q4((num / den).quantize(Q8, rounding=ROUND_HALF_UP))


def _dec(valor_e4):
    """int64 escalado por 10^4 -> Decimal exacto con 4 decimales."""
    return Decimal(int(valor_e4)).scaleb(-4)


def _e4(d):
    """Decimal ya cuantizado a 4 decimales -> int64 escalado por 10^4."""
    return int(d.scaleb(4))


def _preparar(mov):
    """Reafirma el orden del cursor y traduce fecha a segundos epoch."""
    # ORDER BY 6,4,3,2 del procedure, con NULLs primero como en MariaDB.
    # mergesort = estable, para no alterar el desempate que ya trajo el loader.
    mov = mov.sort_values(
        by=['sku', 'sucursal_id', 'fecha', 'hid', 'pid', 'nc',
            'qty', 'picking', 'qnc', 'costo', 'proveedor_id', 'factura', 'id_externo'],
        na_position='first',
        kind='mergesort',
    ).reset_index(drop=True)

    fecha_nula = pd.isna(mov['fecha']).to_numpy()
    seg = mov['fecha'].to_numpy(dtype='datetime64[ns]').astype('datetime64[s]').astype('int64')
    mov['_fsec'] = np.where(fecha_nula, 0, seg).astype('int64')
    mov['_fnull'] = fecha_nula
    return mov


@transformer
def transform(data, *args, **kwargs):
    if not isinstance(data, dict):
        raise Exception(
            'Se esperaba el dict del loader dl_pmp_movimientos '
            "con las claves 'movimientos' y 'pmp_nc'."
        )

    mov = _preparar(data['movimientos'])
    pmp_nc_df = data['pmp_nc']

    # Diccionario de la subconsulta del procedure:
    #   SELECT IFNULL(MAX(pa.pmp),0) FROM mstr_pedidos_aux pa
    #    WHERE pa.sku = p_sku
    #      AND pa.pid IN (SELECT id_externo FROM mstr_nc WHERE pid = v_nc)
    # La llave es (sku, pid del pedido PADRE). En las filas de NC ese pid del
    # padre viaja en la columna id_externo (= mstr_nc.id_externo).
    pmp_nc = {}
    if len(pmp_nc_df):
        for _sku, _pid, _pmp in zip(pmp_nc_df['sku'].to_numpy(),
                                    pmp_nc_df['pid'].to_numpy(),
                                    pmp_nc_df['pmp_max'].to_numpy()):
            pmp_nc[(_sku, int(_pid))] = int(_pmp)

    dt_calculo = kwargs.get('dt_calculo') or datetime.now().replace(microsecond=0)
    if isinstance(dt_calculo, str):
        dt_calculo = pd.to_datetime(dt_calculo).to_pydatetime().replace(tzinfo=None)

    n = len(mov)

    o_suc = array('q'); o_seq = array('q'); o_prov = array('q')
    o_hid = array('q'); o_pid = array('q'); o_nc = array('q')
    o_fecha = array('q')
    o_ing = array('q'); o_ven = array('q'); o_dev = array('q'); o_cos = array('q')
    o_sqty = array('q'); o_sval = array('q'); o_pmp = array('q')
    o_sku = []; o_tipo = []; o_fac = []; o_ext = []

    def _emitir(sucursal_id, sku, seq, tipo, proveedor_id, hid, pid, nc, fsec,
                ingreso, venta, devolucion, costo, saldoqty, saldovalor, cpp,
                factura, id_externo):
        o_suc.append(sucursal_id); o_sku.append(sku); o_seq.append(seq)
        o_tipo.append(tipo); o_prov.append(proveedor_id)
        o_hid.append(hid); o_pid.append(pid); o_nc.append(nc)
        o_fecha.append(fsec)
        o_ing.append(_e4(ingreso)); o_ven.append(_e4(venta))
        o_dev.append(_e4(devolucion)); o_cos.append(_e4(costo))
        o_sqty.append(_e4(saldoqty)); o_sval.append(_e4(saldovalor))
        o_pmp.append(_e4(cpp))
        o_fac.append(factura); o_ext.append(id_externo)

    columnas_iter = [
        'sucursal_id', 'sku', 'proveedor_id', 'hid', 'pid', 'nc',
        '_fsec', '_fnull', 'qty', 'picking', 'qnc', 'costo',
        'factura', 'id_externo',
    ]

    par_actual = None
    saldoqty = CERO
    saldovalor = CERO
    cpp = CERO
    seq = 0

    n_in_aj = 0
    n_div_cero_ingreso = 0
    pares_div_cero = set()
    n_dev_sin_pmp = 0

    with localcontext() as ctx:
        ctx.prec = 60

        for (sucursal_id, sku, proveedor_id, hid, pid, nc,
             fsec, fnull, qty_e4, picking_e4, qnc_e4, costo_e4,
             factura, id_externo) in mov[columnas_iter].itertuples(index=False, name=None):

            sucursal_id = int(sucursal_id)
            par = (sku, sucursal_id)
            if par != par_actual:
                # Nuevo par (sku, sucursal): el procedure arranca con las
                # variables en 0 en cada invocacion.
                par_actual = par
                saldoqty = CERO
                saldovalor = CERO
                cpp = CERO
                seq = 0

            proveedor_id = int(proveedor_id)
            hid = int(hid); pid = int(pid); nc = int(nc)
            fsec = int(fsec)
            fnull = bool(fnull)

            qty = _dec(qty_e4)
            picking = _dec(picking_e4)
            qnc = _dec(qnc_e4)
            costo = _dec(costo_e4)

            if qty > 0:
                # ================= INGRESO =================
                # `IF v_costo = 0 THEN SET v_costo = v_cpp;`
                # Costo 0 (sin fila en tab_sku_precios) => se arrastra el PMP vigente.
                if costo == 0:
                    costo = cpp

                # `IF v_dt_in IS NULL OR v_dt_in = '0000-00-00 00:00:00' THEN
                #     SET v_dt_in = '2020-12-01 08:00:00'; SET v_proveedor_id = 0;`
                # Solo en la rama de ingreso; ventas y devoluciones NO se corrigen.
                # (Las fechas cero de MySis ya llegan como NULL desde el espejo.)
                if fnull:
                    fsec = FECHA_DEFECTO_SEC
                    proveedor_id = 0

                # `IF v_id_externo = 'AJUSTE' THEN SET v_costo = v_cpp;`
                # Comparacion EXACTA, no LIKE: 'AJUSTE_inv' o 'AJUSTE_1' NO entran.
                if id_externo == 'AJUSTE':
                    costo = cpp

                # `SET v_cpp = v_costo;` — el valor que entra al saldo es el
                # costo unitario de ESTE ingreso, no el promedio anterior.
                cpp = _q4(costo)

                elqty = _q4(qty - picking + qnc)
                saldoqty = _q4(saldoqty + elqty)
                saldovalor = _q4(saldovalor + (elqty * cpp))

                # `SET v_cpp = v_saldovalor / v_saldoqty;` — SIN guardia de
                # division por cero, a diferencia de las otras dos ramas.
                # UNICA DIVERGENCIA DELIBERADA DEL PORTE: en MariaDB dividir
                # por cero devuelve NULL y contamina el resto del kardex del
                # par; aca se fuerza 0 y se cuenta el caso, porque la columna
                # destino es Decimal(18,4) NOT NULL.
                if saldoqty != 0:
                    cpp = _div(saldovalor, saldoqty)
                else:
                    cpp = CERO
                    n_div_cero_ingreso += 1
                    pares_div_cero.add(par)

            elif qnc > 0:
                # ================= DEVOLUCION (NC) =================
                # `SELECT IFNULL(MAX(pa.pmp),0) INTO v_pmpnc FROM mstr_pedidos_aux pa
                #   WHERE pa.sku = p_sku
                #     AND pa.pid IN (SELECT id_externo FROM mstr_nc WHERE pid = v_nc)`
                # id_externo trae el pid del pedido padre. Si viene vacio o no
                # parsea, el IN(...) queda vacio, MAX da NULL y el IFNULL lo
                # deja en 0: exactamente el default del .get().
                padre = None
                if id_externo:
                    try:
                        padre = int(id_externo)
                    except (TypeError, ValueError):
                        padre = None
                pmpnc_e4 = pmp_nc.get((sku, padre), 0) if padre is not None else 0
                if pmpnc_e4 == 0:
                    n_dev_sin_pmp += 1
                pmpnc = _q4(_dec(pmpnc_e4))

                cpp = pmpnc
                costo = pmpnc
                elqty = _q4(qty - picking + qnc)
                saldoqty = _q4(saldoqty + elqty)
                saldovalor = _q4(saldovalor + (elqty * cpp))
                # Aca el procedure SI protege la division.
                if saldoqty != 0:
                    cpp = _div(saldovalor, saldoqty)

            else:
                # ================= VENTA =================
                # Tambien caen aca los ingresos con qty <= 0 y las lineas de NC
                # con entrega <= 0. Es el comportamiento del procedure y se
                # respeta: con picking = 0 el efecto es una fila MOV sin efecto.
                elqty = _q4(qty - picking + qnc)

                if saldoqty < picking:
                    # Stock insuficiente: el procedure inventa un ingreso de
                    # ajuste para poder descontar la venta completa.
                    elqty2 = _q4(picking - saldoqty)
                    saldoqty = _q4(saldoqty + elqty2)
                    saldovalor = _q4(saldovalor + (elqty2 * cpp))
                    if saldoqty != 0:
                        cpp = _div(saldovalor, saldoqty)
                    else:
                        cpp = CERO

                    # `SET v_nuevafecha = DATE_SUB(v_dt_in, INTERVAL 1 HOUR);`
                    if fnull:
                        fsec_aj = FECHA_NULA_SEC
                    else:
                        fsec_aj = fsec - 3600
                        if fsec_aj < FECHA_NULA_SEC:
                            # DateTime de ClickHouse arranca en 1970-01-01.
                            fsec_aj = FECHA_NULA_SEC

                    # Fila IN-AJ: proveedor/hid/pid/nc en 0 y factura '0'
                    # literales del INSERT; costo = v_costo, que en una venta
                    # llega 0 desde el cursor; id_externo heredado de la venta,
                    # que por el filtro `i.id_externo IS NULL` es SIEMPRE vacio.
                    seq += 1
                    _emitir(sucursal_id, sku, seq, TIPO_IN_AJ,
                            0, 0, 0, 0, fsec_aj,
                            elqty2, CERO, CERO, costo,
                            saldoqty, saldovalor, cpp,
                            '0', id_externo)
                    n_in_aj += 1

                saldoqty = _q4(saldoqty + elqty)
                saldovalor = _q4(saldovalor + (elqty * cpp))
                # OJO: en la rama VENTA el PMP no se recalcula fuera del bloque
                # IN-AJ. v_cpp queda como estaba. No agregar una division aca.

            # INSERT 'MOV' comun a las tres ramas, con el estado POSTERIOR.
            # Mapeo de columnas: qty -> ingreso, picking -> venta, qnc -> devolucion.
            # fsec ya vale FECHA_NULA_SEC (0) si la fecha venia nula, y
            # FECHA_DEFECTO_SEC si la rama de ingreso la corrigio.
            seq += 1
            _emitir(sucursal_id, sku, seq, TIPO_MOV,
                    proveedor_id, hid, pid, nc, fsec,
                    qty, picking, qnc, costo,
                    saldoqty, saldovalor, cpp,
                    factura, id_externo)

    out = pd.DataFrame({
        'sucursal_id': np.asarray(o_suc, dtype='int64').astype('int32'),
        'sku': o_sku,
        'seq': np.asarray(o_seq, dtype='int64').astype('uint32'),
        'tipo': o_tipo,
        'proveedor_id': np.asarray(o_prov, dtype='int64').astype('int32'),
        'hid': np.asarray(o_hid, dtype='int64').astype('int32'),
        'pid': np.asarray(o_pid, dtype='int64').astype('int32'),
        'nc': np.asarray(o_nc, dtype='int64').astype('int32'),
        'fecha': pd.to_datetime(np.asarray(o_fecha, dtype='int64'), unit='s'),
        'ingreso': np.asarray(o_ing, dtype='int64'),
        'venta': np.asarray(o_ven, dtype='int64'),
        'devolucion': np.asarray(o_dev, dtype='int64'),
        'costo': np.asarray(o_cos, dtype='int64'),
        'saldo_qty': np.asarray(o_sqty, dtype='int64'),
        'saldo_valorizado': np.asarray(o_sval, dtype='int64'),
        'pmp': np.asarray(o_pmp, dtype='int64'),
        'factura': o_fac,
        'id_externo': o_ext,
    })
    out['dt_calculo'] = dt_calculo
    out = out[COLUMNAS_SALIDA]

    # Las 7 columnas Decimal(18,4) viajan como int64 escalado por 10^4 (ver
    # docstring del loader). El exporter las reconvierte a Decimal al insertar.
    out.attrs['escala_decimal'] = ESCALA_DECIMAL
    out.attrs['columnas_decimal'] = list(COLUMNAS_DECIMAL)

    print(f'Movimientos procesados: {n}')
    print(f'Filas emitidas: {len(out)} (MOV={n} + IN-AJ={n_in_aj})')
    print(f'Pares (sku, sucursal): {out.groupby(["sucursal_id", "sku"]).ngroups}')
    print(f'Devoluciones sin MAX(pmp) en el pedido padre (costo 0): {n_dev_sin_pmp}')
    if n_div_cero_ingreso:
        print(
            f'AVISO: {n_div_cero_ingreso} ingresos dejaron saldo_qty = 0 y el '
            f'procedure dividiria por cero ahi (MariaDB -> NULL). Se forzo pmp = 0. '
            f'Pares afectados: {sorted(pares_div_cero)[:20]}'
        )
    return out


@test
def test_output(output, *args) -> None:
    assert output is not None, 'The output is undefined'
    assert list(output.columns) == COLUMNAS_SALIDA, f'Columnas: {list(output.columns)}'
    assert output['tipo'].isin([TIPO_MOV, TIPO_IN_AJ]).all(), 'tipo fuera de dominio'
    assert output['fecha'].notna().all(), 'fecha no puede quedar nula'
    # seq debe ser 1..N sin huecos dentro de cada par (sku, sucursal).
    g = output.groupby(['sucursal_id', 'sku'])['seq']
    assert (g.min() == 1).all(), 'seq no arranca en 1 en algun par'
    assert (g.max() == g.count()).all(), 'seq tiene huecos o repetidos en algun par'
