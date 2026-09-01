"""Carga AUTO-DRIVEN de ventas (mstr_pedidos + NC + anexo) -> dwh.ventas_mysis_2.

- Recorre la historia MES A MES (viejo -> nuevo). Cada query a MariaDB pide SOLO
  un mes (liviano para la base de produccion). Configurable con `meses_por_lote`.
- Inserta cada lote a ClickHouse `ventas_mysis_2` (ReplacingMergeTree(ingested_at)),
  que deduplica por (pid,sku) quedandose con la ultima version -> capta updates
  de `entregado`/qty. Por eso es idempotente y RESUMABLE: si falla, re-ejecuta y
  vuelve a pasar los meses (sin duplicar).
- Reutiliza el SQL canonico dl_ventas_mysis.sql (parametrizado por {{ start_date }}
  / {{ end_date }}) leyendolo de disco.

Variables runtime opcionales:
  meses_por_lote  (int, default 1)
  start_date      ('YYYY-MM-DD' para forzar inicio; default = MIN historico)
  end_date        ('YYYY-MM-DD' exclusivo; default = hoy + 4 dias)
"""
from __future__ import annotations

import glob
import re
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
# Decimales que en el SQL salen como string es_CL (coma decimal)
DEC_SQL_STR = [
    'pu', 'pmp', 'totaliza_pmp', 'totaliza_vta', 'margen', 'diferencia',
    'totaliza_diferencia', 'margen_diferencia', 'margen_final', 'tipo_comision',
]


# ── conexiones ────────────────────────────────────────────────────────────────
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


def _load_sql_template() -> str:
    hits = glob.glob('/home/src/**/dl_ventas_mysis_2.sql', recursive=True)
    if not hits:
        raise FileNotFoundError('No se encontro dl_ventas_mysis.sql bajo /home/src')
    with open(hits[0], 'r', encoding='utf-8') as fh:
        return fh.read()


# ── limpieza (igual que tr_ventas_mysis) ────────────────────────────────────────
def _clean_decimal_series(s: pd.Series) -> pd.Series:
    s_str = s.astype(str)
    mask_coma = s_str.str.contains(',', na=False)
    s_eu = (s_str.where(~mask_coma, s_str.str.replace('.', '', regex=False))
                 .where(~mask_coma, lambda x: x.str.replace(',', '.', regex=False)))
    return pd.to_numeric(s_eu, errors='coerce').round(2)


def _clean_picking_series(s: pd.Series) -> pd.Series:
    s_str = s.astype(str).str.strip()
    pat = re.compile(r'^\d{1,3}(?:\.\d{3})+$')
    s_norm = s_str.map(lambda v: v.replace('.', '') if pat.match(v) else v)
    s_num = pd.to_numeric(s_norm, errors='coerce')
    mask_dec = s_num.notna() & ((s_num % 1) != 0)
    if mask_dec.any():
        raise ValueError(f'picking con decimales: {s_num.loc[mask_dec].head().tolist()}')
    return s_num.astype('Int64')


def _transform(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in DEC_SQL_STR:
        if c in df.columns:
            df[c] = _clean_decimal_series(df[c]).fillna(0)
    for c in DATE_COLS_DT:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors='coerce')
    for c in DATE_COLS_D:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors='coerce').dt.date
    if 'picking' in df.columns:
        df['picking'] = _clean_picking_series(df['picking'])
    return df


# ── cast para insert (igual que de_ventas_mysis) ────────────────────────────────
def _prepare_for_insert(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in INSERT_COLS:
        if c not in df.columns:
            df[c] = pd.NA
    df = df[INSERT_COLS]
    df = df.dropna(subset=['pid', 'sku'], how='any')

    for c in INT_COLS.intersection(df.columns):
        df[c] = pd.to_numeric(df[c], errors='coerce')
        if df[c].isna().any():
            df = df[df[c].notna()]
        df[c] = df[c].astype('Int64') if c != 'pid' else df[c].astype('UInt64')

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


# ── util meses ──────────────────────────────────────────────────────────────
def _add_months(d: _dt.date, k: int) -> _dt.date:
    m = d.month - 1 + k
    return _dt.date(d.year + m // 12, m % 12 + 1, 1)


@data_loader
def load_ventas_batch(*args, **kwargs):
    meses = int(kwargs.get('meses_por_lote') or 1)
    sql_tpl = _load_sql_template()
    ch = _ch_client()

    total = 0
    lotes = 0
    with _mysql() as my:
        # rango global
        if kwargs.get('start_date'):
            gmin = _dt.date.fromisoformat(str(kwargs['start_date']))
        else:
            mdf = my.load("""SELECT DATE(MIN(d)) AS m FROM (
                SELECT MIN(dt_out) d FROM mstr_pedidos WHERE factura IS NOT NULL
                UNION ALL SELECT MIN(dt_out) FROM mstr_nc
                UNION ALL SELECT MIN(desde) FROM mstr_anexo) t""")
            gmin = pd.to_datetime(mdf.iloc[0, 0]).date()
        if kwargs.get('end_date'):
            cap = _dt.date.fromisoformat(str(kwargs['end_date']))
        else:
            cap = _dt.date.today() + _dt.timedelta(days=4)

        w_start = _dt.date(gmin.year, gmin.month, 1)
        while w_start < cap:
            w_end = min(_add_months(w_start, meses), cap)
            sql = (sql_tpl
                   .replace('{{ start_date }}', w_start.isoformat())
                   .replace('{{ end_date }}', w_end.isoformat()))
            df = my.load(sql)
            ins = 0
            if df is not None and not df.empty:
                df = _transform(df)
                ins = _insert(ch, df)
            total += ins
            lotes += 1
            print(f'[{w_start} -> {w_end})  leidas={0 if df is None else len(df)}  insertadas={ins}  (acum={total})')
            w_start = w_end

    return {'tabla': TABLE, 'lotes': lotes, 'filas_insertadas': total,
            'rango': f'{gmin} -> {cap}', 'meses_por_lote': meses}
