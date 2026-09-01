# de_alertas_clickhouse_v2.py
# Registra el "silencio de ejecucion" del pipeline maryun_abastecimiento_mysis.
# Tabla propia (logistica_v2.mysis_v2_alertas_silencio) con columna run_id.
# El scope por run_id ya viene embebido en hash_clave (run_id|sku2|destino|origen|accion),
# por lo que el anti-join por hash_clave NUNCA bloquea filas de una run_id distinta.
import clickhouse_connect
import pandas as pd
from typing import Iterable, Set
from datetime import datetime, timedelta

if 'data_exporter' not in globals():
    from mage_ai.data_preparation.decorators import data_exporter
from mage_ai.io.config import ConfigFileLoader


ALERTAS_TABLE = 'logistica_v2.mysis_v2_alertas_silencio'
CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

# Duracion del silencio en dias (solo aplica a re-ejecucion de la MISMA run_id).
SILENCIO_DIAS = 7

ALERTAS_INSERT_COLS = [
    'run_id',
    'sku2',
    'sku_original',
    'sucursal_destino',
    'sucursal_origen',
    'accion',
    'accion_original',
    'cantidad',
    'fecha_corte',
    'hash_clave',
    'fecha_alerta',
    'valida_hasta',
    'api_source',
    'estado',
]

DECIMAL_COLS = {'cantidad'}
STRING_COLS = {
    'run_id',
    'sku2',
    'sku_original',
    'sucursal_destino',
    'sucursal_origen',
    'accion',
    'accion_original',
    'hash_clave',
    'api_source',
    'estado',
}
DATE_COLS_DT = ['fecha_corte', 'fecha_alerta', 'valida_hasta']


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


def _prepare_for_insert(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in ALERTAS_INSERT_COLS:
        if c not in df.columns:
            df[c] = pd.NA
    df = df[ALERTAS_INSERT_COLS]

    for c in DECIMAL_COLS.intersection(df.columns):
        s = pd.to_numeric(df[c], errors='coerce')
        if s.isna().any():
            idx = df[s.isna()].index[:5].tolist()
            raise ValueError(f'NaN en decimal "{c}" y la columna no es Nullable (ej: filas {idx})')
        df[c] = s.astype('float64')

    for c in STRING_COLS.intersection(df.columns):
        df[c] = df[c].astype('string').fillna('')

    for c in DATE_COLS_DT:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors='coerce')

    return df


def _coerce_dates_for_clickhouse(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in DATE_COLS_DT:
        if c in df.columns:
            s = pd.to_datetime(df[c], errors='coerce')
            df[c] = s.apply(
                lambda x: x.to_pydatetime().replace(tzinfo=None) if pd.notna(x) else None
            ).astype('object')
    return df


def _hashes_from_chunk(chunk: pd.DataFrame) -> Iterable[str]:
    return list(chunk['hash_clave'].astype(str).unique())


def _fetch_existing_hashes(client, hashes: Iterable[str]) -> Set[str]:
    hashes = list(hashes)
    if not hashes:
        return set()
    existing: Set[str] = set()
    step = 1000
    now_str = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    for i in range(0, len(hashes), step):
        sub = hashes[i:i+step]
        hashes_txt = ','.join("'{}'".format(str(h).replace("'", "\\'")) for h in sub)
        q = f"""
            SELECT hash_clave
            FROM {ALERTAS_TABLE}
            WHERE hash_clave IN ({hashes_txt})
              AND lower(estado) = 'activa'
              AND valida_hasta >= toDateTime('{now_str}')
        """
        res = client.query(q)
        for row in res.result_rows:
            existing.add(str(row[0]))
    return existing


def _insert_rows(client, chunk: pd.DataFrame):
    chunk = _coerce_dates_for_clickhouse(chunk)
    chunk_py = chunk.where(pd.notna(chunk), None)
    rows = [tuple(chunk_py.loc[i, ALERTAS_INSERT_COLS]) for i in chunk_py.index]
    client.insert(ALERTAS_TABLE, rows, column_names=ALERTAS_INSERT_COLS)


@data_exporter
def export_alertas_silencio(df: pd.DataFrame, *args, **kwargs):
    if df is None or df.empty:
        print('[mysis_v2_alertas_silencio] No hay filas para procesar.')
        return {'rows_total': 0, 'rows_inserted': 0, 'rows_existing_valid': 0}

    ahora = datetime.utcnow()
    valida = ahora + timedelta(days=SILENCIO_DIAS)

    df = df.copy()
    df['fecha_alerta'] = ahora
    df['valida_hasta'] = valida
    df['estado'] = 'Activa'

    # api_source segun accion
    acc = df['accion'].astype('string').fillna('')
    df['api_source'] = ''
    df.loc[acc.str.contains('generar', case=False, na=False), 'api_source'] = 'oc'
    df.loc[acc.str.contains('despachar', case=False, na=False), 'api_source'] = 'traspaso'
    df.loc[
        (df['api_source'] == '') & acc.str.contains('transferir', case=False, na=False),
        'api_source'
    ] = 'traspaso'

    dfp = _prepare_for_insert(df)
    total = len(dfp)

    client = _client()
    chunk_size = int(kwargs.get('chunk_size') or 10000)
    inserted_chunks = 0
    rows_sent = 0
    rows_inserted = 0
    rows_existing_valid = 0

    for i in range(0, total, chunk_size):
        chunk = dfp.iloc[i:i+chunk_size].copy()
        hashes = _hashes_from_chunk(chunk)
        existing_hashes = _fetch_existing_hashes(client, hashes)

        if existing_hashes:
            hashes_chunk = chunk['hash_clave'].astype(str).values
            keep_mask = [h not in existing_hashes for h in hashes_chunk]
            rows_existing_valid += len(chunk) - sum(keep_mask)
            chunk = chunk.loc[keep_mask]

        if not chunk.empty:
            _insert_rows(client, chunk)
            rows_inserted += len(chunk)

        inserted_chunks += 1
        rows_sent += len(dfp.iloc[i:i+chunk_size])
        print(f'[mysis_v2_alertas_silencio] Chunk {inserted_chunks}: '
              f'enviados {rows_sent}, insertados {rows_inserted}, '
              f'ya existentes y vigentes {rows_existing_valid}')

    print(f'[mysis_v2_alertas_silencio] Resumen -> total: {total}, '
          f'nuevas insertadas: {rows_inserted}, ya existentes vigentes: {rows_existing_valid}')

    return {
        'rows_total': total,
        'rows_inserted': rows_inserted,
        'rows_existing_valid': rows_existing_valid,
    }
