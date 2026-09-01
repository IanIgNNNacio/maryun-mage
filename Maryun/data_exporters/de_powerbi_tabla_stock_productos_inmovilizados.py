import pandas as pd
import clickhouse_connect

from datetime import datetime, timezone
from typing import Iterable, List

if 'data_exporter' not in globals():
    from mage_ai.data_preparation.decorators import data_exporter
from mage_ai.io.config import ConfigFileLoader


TABLA       = 'logistica_stock_productos_inmovilizados_sobrestock'
CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE     = 'maryun'

INSERT_COLS = [
    'snapshot_date',
    'sku',
    'nombre',
    'sucursal',
    'sku_sucursal',
    'tipo_problema',
    'urgencia',
    'variante',
    'producto_completo',
    'dias_sin_venta',
    'dias_sin_ingreso',
    'stock_sucursal',
    'pronostico_mes',
    'meses_cobertura',
    'cant_sucursales_con_problema',
    'total_valorizado_todas_sucursales',
    'stock_valorizado',
    'ingested_at',
]

STRING_COLS = {
    'sku', 'nombre', 'sucursal', 'sku_sucursal',
    'tipo_problema', 'urgencia', 'variante', 'producto_completo',
}

INT_COLS_NULLABLE = {
    'dias_sin_venta',
    'dias_sin_ingreso',
    'cant_sucursales_con_problema',
}

FLOAT_COLS = {
    'stock_sucursal',
    'pronostico_mes',
    'meses_cobertura',
    'total_valorizado_todas_sucursales',
    'stock_valorizado',
}

DATE_COLS     = ['snapshot_date']
DATETIME_COLS = ['ingested_at']


def _client():
    cfg       = ConfigFileLoader(CONFIG_PATH, PROFILE)
    use_https = str(cfg['CLICKHOUSE_INTERFACE']).lower() == 'https'
    return clickhouse_connect.get_client(
        host=cfg['CLICKHOUSE_HOST'],
        port=int(cfg['CLICKHOUSE_PORT']),
        username=cfg['CLICKHOUSE_USERNAME'],
        password=cfg['CLICKHOUSE_PASSWORD'],
        database=cfg['CLICKHOUSE_DATABASE'],
        secure=use_https,
    )


def _prepare_for_insert(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    for c in INSERT_COLS:
        if c not in df.columns:
            df[c] = pd.NA

    df['ingested_at'] = datetime.now(timezone.utc).replace(tzinfo=None)
    df = df[INSERT_COLS]

    for c in STRING_COLS.intersection(df.columns):
        df[c] = df[c].astype('string').fillna('')

    for c in INT_COLS_NULLABLE.intersection(df.columns):
        df[c] = pd.to_numeric(df[c], errors='coerce').astype('Int64')

    for c in FLOAT_COLS.intersection(df.columns):
        df[c] = pd.to_numeric(df[c], errors='coerce').astype('float64')

    for c in DATE_COLS:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors='coerce').dt.normalize()

    for c in DATETIME_COLS:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors='coerce')

    # Validaciones mínimas
    if df['snapshot_date'].isna().any():
        idx = df[df['snapshot_date'].isna()].index[:5].tolist()
        raise ValueError(f'snapshot_date no puede venir nulo (ej: filas {idx})')

    if df['sku'].replace('', pd.NA).isna().any():
        idx = df[df['sku'].replace('', pd.NA).isna()].index[:5].tolist()
        raise ValueError(f'sku no puede venir vacío/nulo (ej: filas {idx})')

    if df['sucursal'].replace('', pd.NA).isna().any():
        idx = df[df['sucursal'].replace('', pd.NA).isna()].index[:5].tolist()
        raise ValueError(f'sucursal no puede venir vacía/nula (ej: filas {idx})')

    return df


def _coerce_dates_for_clickhouse(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    for c in DATE_COLS:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors='coerce').apply(
                lambda x: x.date() if pd.notna(x) else None
            ).astype('object')

    for c in DATETIME_COLS:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors='coerce').apply(
                lambda x: x.to_pydatetime().replace(tzinfo=None) if pd.notna(x) else None
            ).astype('object')

    return df


def _snapshot_dates_from_df(df: pd.DataFrame) -> List[str]:
    fechas = pd.to_datetime(df['snapshot_date'], errors='coerce').dropna().dt.strftime('%Y-%m-%d')
    return sorted(fechas.unique().tolist())


def _delete_existing_snapshot_dates(client, snapshot_dates: Iterable[str]):
    snapshot_dates = list(snapshot_dates)
    if not snapshot_dates:
        return
    fechas_txt = ','.join(f"toDate('{f}')" for f in snapshot_dates)
    client.command(f"ALTER TABLE {TABLA} DELETE WHERE snapshot_date IN ({fechas_txt})")
    print(f'[{TABLA}] DELETE ejecutado para snapshot_date(s): {snapshot_dates}')


def _insert_rows(client, chunk: pd.DataFrame):
    chunk = _coerce_dates_for_clickhouse(chunk)
    chunk = chunk.where(pd.notna(chunk), None)
    rows  = [tuple(chunk.loc[i, INSERT_COLS]) for i in chunk.index]
    client.insert(TABLA, rows, column_names=INSERT_COLS)


@data_exporter
def export_data(df: pd.DataFrame, *args, **kwargs):
    if df is None or df.empty:
        print(f'[{TABLA}] No hay filas para procesar.')
        return {'rows_inserted': 0, 'snapshot_dates_processed': []}

    dfp            = _prepare_for_insert(df)
    snapshot_dates = _snapshot_dates_from_df(dfp)
    client         = _client()

    _delete_existing_snapshot_dates(client, snapshot_dates)

    chunk_size    = int(kwargs.get('chunk_size') or 10000)
    rows_inserted = 0

    for i in range(0, len(dfp), chunk_size):
        chunk = dfp.iloc[i:i + chunk_size].copy()
        if not chunk.empty:
            _insert_rows(client, chunk)
            rows_inserted += len(chunk)
            print(f'[{TABLA}] Chunk insertado: {rows_inserted}/{len(dfp)} filas')

    print(f'[{TABLA}] Resumen -> insertadas: {rows_inserted}, snapshot_dates: {snapshot_dates}')
    return {'rows_inserted': rows_inserted, 'snapshot_dates_processed': snapshot_dates}