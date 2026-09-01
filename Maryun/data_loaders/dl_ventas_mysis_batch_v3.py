"""V3 — Carga AUTO-DRIVEN de ventas a dwh.ventas_mysis_2 leyendo DIRECTO de
`reporte_ventas_completo` (la MISMA fuente que usa base_proceso_v4) -> paridad.

Diferencia vs my_sis_to_clickhouse (live mstr):
  - lee `reporte_ventas_completo` (tabla materializada que lee base), NO re-arma
    el query de 3 UNION sobre mstr_*. Garantiza que CH = input de base.
  - reporte ya viene tipado (decimales/fechas), no necesita limpieza es_CL.

- Recorre mes a mes (viejo -> nuevo) por `facturado`. Cada query pide 1 mes.
- Inserta a ventas_mysis_2 (ReplacingMergeTree(ingested_at)) -> idempotente/resumable.

Variables runtime opcionales:
  meses_por_lote (int, default 1)
  start_date ('YYYY-MM-DD', default MIN(facturado) de reporte)
  end_date   ('YYYY-MM-DD' exclusivo, default hoy + 4 dias)
"""
from __future__ import annotations

import datetime as _dt

import pandas as pd
import clickhouse_connect
from mage_ai.io.config import ConfigFileLoader
from mage_ai.io.mysql import MySQL

if 'data_loader' not in globals():
    from mage_ai.data_preparation.decorators import data_loader

CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'
TABLE = 'dwh.ventas_mysis_2'
SOURCE = 'reporte_ventas_completo'

INSERT_COLS = [
    'pid', 'padre', 'shopify', 'sucursal', 'rso', 'rut',
    'creado', 'dt_picking', 'facturar', 'facturado', 'confirmado', 'entregado', 'vencimiento',
    'guia', 'factura', 'neto', 'iva', 'total', 'deuda', 'sku', 'nombre', 'descripcion', 'qty', 'picking',
    'pu', 'tramo', 'pmp', 'totaliza_pmp', 'totaliza_vta', 'margen', 'tipo_convenio', 'diferencia',
    'totaliza_diferencia', 'margen_diferencia', 'margen_final', 'tipo_comision', 'tcomision',
    'observacion', 'vendedor', 'rut_vendedor', 'remunera', 'comuna', 'direccion', 'area', 'procedencia',
    'marca', 'familia', 'tipo',
]
INT_COLS = {'pid', 'padre', 'picking'}
DECIMAL_COLS = {
    'pu', 'pmp', 'totaliza_pmp', 'totaliza_vta', 'margen', 'diferencia', 'deuda',
    'totaliza_diferencia', 'margen_diferencia', 'margen_final', 'tipo_comision', 'qty', 'neto', 'iva', 'total',
}
STRING_COLS = set(INSERT_COLS) - INT_COLS - DECIMAL_COLS - {
    'creado', 'dt_picking', 'facturar', 'facturado', 'confirmado', 'entregado', 'vencimiento',
}
DATE_COLS_DT = ['creado', 'dt_picking', 'facturar']
DATE_COLS_D = ['facturado', 'confirmado', 'entregado', 'vencimiento']


def _ch_client():
    cfg = ConfigFileLoader(CONFIG_PATH, PROFILE)
    use_https = str(cfg['CLICKHOUSE_INTERFACE']).lower() == 'https'
    return clickhouse_connect.get_client(
        host=cfg['CLICKHOUSE_HOST'], port=int(cfg['CLICKHOUSE_PORT']),
        username=cfg['CLICKHOUSE_USERNAME'], password=cfg['CLICKHOUSE_PASSWORD'],
        database=cfg['CLICKHOUSE_DATABASE'], secure=use_https,
    )


def _mysql():
    return MySQL.with_config(ConfigFileLoader(CONFIG_PATH, PROFILE))


def _prepare_for_insert(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    for c in INSERT_COLS:
        if c not in df.columns:
            df[c] = pd.NA
    df = df[INSERT_COLS]
    df = df.dropna(subset=['pid', 'sku'], how='any')

    for c in INT_COLS.intersection(df.columns):
        df[c] = pd.to_numeric(df[c], errors='coerce')
        df = df[df[c].notna()] if c == 'pid' else df
        df[c] = (df[c].astype('UInt64') if c == 'pid' else df[c].astype('Int64'))

    for c in DECIMAL_COLS.intersection(df.columns):
        df[c] = pd.to_numeric(df[c], errors='coerce').round(2).fillna(0).astype('float64')

    for c in STRING_COLS.intersection(df.columns):
        df[c] = df[c].astype('string').fillna('')

    for c in DATE_COLS_DT + DATE_COLS_D:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors='coerce')
    return df


def _coerce_dates(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in DATE_COLS_DT:
        if c in df.columns:
            s = pd.to_datetime(df[c], errors='coerce')
            df[c] = s.apply(lambda x: x.to_pydatetime().replace(tzinfo=None) if pd.notna(x) else None).astype('object')
    for c in DATE_COLS_D:
        if c in df.columns:
            s = pd.to_datetime(df[c], errors='coerce')
            df[c] = s.apply(lambda x: x.date() if pd.notna(x) else None).astype('object')
    return df


def _insert(client, df: pd.DataFrame, chunk=10000) -> int:
    dfp = _prepare_for_insert(df)
    n = 0
    for i in range(0, len(dfp), chunk):
        ck = _coerce_dates(dfp.iloc[i:i + chunk].copy())
        ck = ck.where(pd.notna(ck), None)
        rows = [tuple(ck.loc[j, INSERT_COLS]) for j in ck.index]
        if rows:
            client.insert(TABLE, rows, column_names=INSERT_COLS)
            n += len(rows)
    return n


def _add_months(d: _dt.date, k: int) -> _dt.date:
    m = d.month - 1 + k
    return _dt.date(d.year + m // 12, m % 12 + 1, 1)


@data_loader
def load_ventas_reporte_batch_v3(*args, **kwargs):
    meses = int(kwargs.get('meses_por_lote') or 1)
    ch = _ch_client()

    total = 0
    lotes = 0
    with _mysql() as my:
        if kwargs.get('start_date'):
            gmin = _dt.date.fromisoformat(str(kwargs['start_date']))
        else:
            mdf = my.load(f"SELECT DATE(MIN(facturado)) AS m FROM {SOURCE}", verbose=False)
            gmin = pd.to_datetime(mdf.iloc[0, 0]).date()
        if kwargs.get('end_date'):
            cap = _dt.date.fromisoformat(str(kwargs['end_date']))
        else:
            cap = _dt.date.today() + _dt.timedelta(days=4)

        w_start = _dt.date(gmin.year, gmin.month, 1)
        while w_start < cap:
            w_end = min(_add_months(w_start, meses), cap)
            sql = (f"SELECT * FROM {SOURCE} "
                   f"WHERE facturado >= '{w_start.isoformat()}' AND facturado < '{w_end.isoformat()}'")
            df = my.load(sql, verbose=False)
            ins = 0
            if df is not None and not df.empty:
                ins = _insert(ch, df)
            total += ins
            lotes += 1
            print(f'[{w_start} -> {w_end})  leidas={0 if df is None else len(df)}  insertadas={ins}  (acum={total})')
            w_start = w_end

    return {'tabla': TABLE, 'fuente': SOURCE, 'lotes': lotes,
            'filas_insertadas': total, 'rango': f'{gmin} -> {cap}', 'meses_por_lote': meses}
