from mage_ai.io.config import ConfigFileLoader
import pandas as pd
import clickhouse_connect
from typing import Iterable, Set, Tuple
import datetime as _dt

if 'data_exporter' not in globals():
    from mage_ai.data_preparation.decorators import data_exporter

TABLE = 'dwh.ventas_mysis'
CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

# Columnas a insertar (todas menos id, id_2 e ingested_at)
INSERT_COLS = [
    'pid','padre','shopify','sucursal','rso','rut',
    'creado','dt_picking','facturar','facturado','confirmado','entregado','vencimiento',
    'guia','factura','neto','iva','total','deuda','sku','nombre','descripcion','qty','picking',
    'pu','tramo','pmp','totaliza_pmp','totaliza_vta','margen','tipo_convenio','diferencia',
    'totaliza_diferencia','margen_diferencia','margen_final','tipo_comision','tcomision',
    'observacion','vendedor','rut_vendedor','remunera','comuna','direccion','area','procedencia',
    'marca','familia','tipo'
]

INT_COLS = {'pid','padre','picking'}
DECIMAL_COLS = {
    'pu','pmp','totaliza_pmp','totaliza_vta','margen','diferencia', 'deuda'
    'totaliza_diferencia','margen_diferencia','margen_final','tipo_comision','qty', 'neto', 'iva', 'total'
}
# Strings (no Nullable en tu DDL)
STRING_COLS = set(INSERT_COLS) - INT_COLS - DECIMAL_COLS - {
    'creado','dt_picking','facturar','facturado','confirmado','entregado','vencimiento'
}
# Fechas
DATE_COLS_DT = ['creado', 'dt_picking', 'facturar']  # Nullable(DateTime)
DATE_COLS_D  = ['facturado', 'confirmado', 'entregado', 'vencimiento']  # Date / Nullable(Date)

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

    # Asegurar columnas requeridas
    for c in INSERT_COLS:
        if c not in df.columns:
            df[c] = pd.NA

    # Orden exacto de columnas según la tabla destino
    df = df[INSERT_COLS]

    # Claves mínimas: no insertamos filas sin pid o sku
    df = df.dropna(subset=['pid', 'sku'], how='any')

    # ----------------------------
    # Enteros (pid, padre, neto, iva, total, deuda, picking)
    # ----------------------------
    for c in INT_COLS.intersection(df.columns):
        # Intentar convertir a numérico
        df[c] = pd.to_numeric(df[c], errors='coerce')

        # Validar que no haya NaN en columnas enteras
        if df[c].isna().any():
            idx = df[df[c].isna()].index[:5].tolist()
            raise ValueError(
                f'NaN/no numérico en columna entera "{c}" '
                f'(ej: filas {idx})'
            )

        # Detectar valores no enteros (ej: 1.5)
        non_int_mask = df[c].notna() & ((df[c] % 1) != 0)
        if non_int_mask.any():
            ejemplos = df.loc[non_int_mask, c].head().tolist()
            raise ValueError(
                f'La columna entera "{c}" tiene valores no enteros: {ejemplos}. '
                'Revisa el transformer / fuente de datos.'
            )

        # Tipos finales según la definición en ClickHouse
        if c == 'pid':
            # pid es UInt64 y no debe ser negativo
            if (df[c] < 0).any():
                raise ValueError('pid negativo; incompatible con UInt64.')
            df[c] = df[c].astype('UInt64')
        else:
            # Resto de enteros como Int64 (nullable)
            df[c] = df[c].astype('Int64')

    # ----------------------------
    # Decimales (incluye qty)
    # ----------------------------
    for c in DECIMAL_COLS.intersection(df.columns):
        s = pd.to_numeric(df[c], errors='coerce').round(2)

        # No permitimos NaN porque en el DDL no son Nullable
        if s.isna().any():
            idx = df[s.isna()].index[:5].tolist()
            raise ValueError(
                f'NaN en decimal "{c}" y la columna no es Nullable '
                f'(ej: filas {idx})'
            )

        # IMPORTANTE: forzamos a float64 para evitar numpy.int64
        # que rompe con Decimal(...) en clickhouse-connect
        df[c] = s.astype('float64')

        # Si prefieres máxima precisión, alternativa:
        # df[c] = s.astype(str)

    # ----------------------------
    # Strings (no Nullable en tu DDL)
    # ----------------------------
    for c in STRING_COLS.intersection(df.columns):
        df[c] = df[c].astype('string').fillna('')

    # ----------------------------
    # Fechas: aseguramos tipos pandas correctos
    # (el coercion a objetos Python se hace en _coerce_dates_for_clickhouse)
    # ----------------------------
    for c in DATE_COLS_DT + DATE_COLS_D:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors='coerce')

    return df

def _coerce_dates_for_clickhouse(df: pd.DataFrame) -> pd.DataFrame:
    """
    Convierte:
      - DateTime -> datetime.datetime o None (dtype object)
      - Date     -> datetime.date     o None (dtype object)
    y evita que queden NaT.
    """
    df = df.copy()

    # DateTime (nullable)
    for c in DATE_COLS_DT:
        if c in df.columns:
            s = pd.to_datetime(df[c], errors='coerce')
            # convertir a objetos Python (None si NaT)
            df[c] = s.apply(lambda x: x.to_pydatetime().replace(tzinfo=None) if pd.notna(x) else None).astype('object')

    # Date / Nullable(Date)
    for c in DATE_COLS_D:
        if c in df.columns:
            s = pd.to_datetime(df[c], errors='coerce')
            df[c] = s.apply(lambda x: x.date() if pd.notna(x) else None).astype('object')

    return df

def _pairs_from_chunk(chunk: pd.DataFrame) -> Iterable[Tuple[int, str]]:
    return list(zip(chunk['pid'].astype('uint64').astype(int), chunk['sku'].astype(str)))

def _fetch_existing_pairs(client, pairs: Iterable[Tuple[int, str]]) -> Set[Tuple[int, str]]:
    pairs = list(pairs)
    if not pairs:
        return set()

    existing: Set[Tuple[int, str]] = set()
    step = 1000
    for i in range(0, len(pairs), step):
        sub = pairs[i:i+step]
        tuples_txt = ','.join("(%d, '%s')" % (pid, str(sku).replace("'", "\\'")) for pid, sku in sub)
        q = f"""
            SELECT pid, sku
            FROM {TABLE}
            WHERE (pid, sku) IN ({tuples_txt})
        """
        res = client.query(q)
        for row in res.result_rows:
            existing.add((int(row[0]), str(row[1])))
    return existing

def _insert_rows(client, chunk: pd.DataFrame):
    # 1) Fechas a objetos Python y None (NO NaT)
    chunk = _coerce_dates_for_clickhouse(chunk)

    # 2) Reemplazar cualquier NA remanente por None
    chunk_py = chunk.where(pd.notna(chunk), None)

    # 3) Construir filas en el orden exacto
    rows = [tuple(chunk_py.loc[i, INSERT_COLS]) for i in chunk_py.index]

    # 4) Insert nativo
    client.insert(TABLE, rows, column_names=INSERT_COLS)

@data_exporter
def export_data_to_clickhouse(df: pd.DataFrame, *args, **kwargs):
    """
    Recibe un DataFrame ya LIMPIO por el transformer y lo inserta a ClickHouse:
    - Anti-join (pid, sku) hecho en pandas consultando a CH
    - Inserción nativa (sin CSV, sin tablas externas)
    """
    client = _client()

    dfp = _prepare_for_insert(df)
    total = len(dfp)
    if total == 0:
        print('No hay filas para exportar.')
        return {'inserted_chunks': 0, 'rows_sent': 0, 'rows_inserted': 0}

    chunk_size = int(kwargs.get('chunk_size') or 10000)
    inserted_chunks = 0
    rows_sent = 0
    rows_inserted = 0

    for i in range(0, total, chunk_size):
        chunk = dfp.iloc[i:i+chunk_size].copy()

        # Anti-join: obtener existentes en CH para este chunk
        pairs = _pairs_from_chunk(chunk)
        existing = _fetch_existing_pairs(client, pairs)

        if existing:
            # Más eficiente que apply fila a fila:
            pids = chunk['pid'].astype('uint64').astype(int).values
            skus = chunk['sku'].astype(str).values
            keep_mask = [ (int(pid), str(sku)) not in existing for pid, sku in zip(pids, skus) ]
            chunk = chunk.loc[keep_mask]

        if not chunk.empty:
            _insert_rows(client, chunk)
            rows_inserted += len(chunk)

        inserted_chunks += 1
        rows_sent += len(dfp.iloc[i:i+chunk_size])
        print(f'Chunk {inserted_chunks}: enviados {rows_sent}, insertados {rows_inserted}')

    return {'inserted_chunks': inserted_chunks, 'rows_sent': rows_sent, 'rows_inserted': rows_inserted}