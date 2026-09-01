from mage_ai.io.config import ConfigFileLoader
import os
import pandas as pd
import clickhouse_connect

if 'data_exporter' not in globals():
    from mage_ai.data_preparation.decorators import data_exporter

TABLE = 'dwh.ventas_mysis'
CONFIG_PATH = 'io_config.yaml'
PROFILE = 'maryun'   # <-- el nombre de tu perfil en io_config.yaml

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

def _client():
    config = ConfigFileLoader(CONFIG_PATH, PROFILE)
    return clickhouse_connect.get_client(
        host=config['CLICKHOUSE_HOST'],
        port=int(config.get('CLICKHOUSE_PORT', 8123)),
        username=config['CLICKHOUSE_USERNAME'],
        password=config['CLICKHOUSE_PASSWORD'],
        interface=config.get('CLICKHOUSE_INTERFACE', 'http'),
        database=config.get('CLICKHOUSE_DATABASE', 'default'),
    )

def _prepare_for_insert(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in INSERT_COLS:
        if c not in df.columns:
            df[c] = pd.NA
    df = df[INSERT_COLS]
    df = df.dropna(subset=['pid','sku'], how='any')
    return df

def _insert_chunk_anti_join(client, chunk: pd.DataFrame):
    if chunk.empty:
        return 0

    structure = (
        "pid UInt64, padre Int64, shopify String, sucursal String, rso String, rut String, "
        "creado Nullable(DateTime), dt_picking Nullable(DateTime), facturar Nullable(DateTime), "
        "facturado Date, confirmado Nullable(Date), entregado Nullable(Date), vencimiento Nullable(Date), "
        "guia String, factura String, neto Int64, iva Int64, total Int64, deuda Int64, sku String, "
        "nombre String, descripcion String, qty Int64, picking Int64, pu Decimal(18,2), tramo String, "
        "pmp Decimal(18,2), totaliza_pmp Decimal(18,2), totaliza_vta Decimal(18,2), margen Decimal(18,2), "
        "tipo_convenio String, diferencia Decimal(18,2), totaliza_diferencia Decimal(18,2), "
        "margen_diferencia Decimal(18,2), margen_final Decimal(18,2), tipo_comision Decimal(18,2), "
        "tcomision String, observacion String, vendedor String, rut_vendedor String, remunera String, "
        "comuna String, direccion String, area String, procedencia String, marca String, familia String, tipo String"
    )

    query = f"""
    INSERT INTO {TABLE} ({', '.join(INSERT_COLS)})
    SELECT *
    FROM input('{structure}') AS s
    LEFT ANTI JOIN {TABLE} v
        ON s.pid = v.pid AND s.sku = v.sku
    """

    data_rows = chunk.astype(object).where(pd.notna(chunk), None).values.tolist()
    client.query(
        query,
        external_tables=[{'name': 'input', 'structure': structure, 'data': data_rows}],
        settings={'async_insert': 1, 'wait_for_async_insert': 1}
    )
    return len(chunk)


@data_exporter
def export_data_to_clickhouse(df: pd.DataFrame, *args, **kwargs):
    """
    Recibe un DataFrame ya limpiado (por transformer)
    y lo inserta en ClickHouse en lotes, omitiendo duplicados (pid, sku).
    """
    client = _client()
    df_prepared = _prepare_for_insert(df)

    total = len(df_prepared)
    if total == 0:
        print('No hay filas para exportar.')
        return {'inserted_chunks': 0, 'rows_sent': 0}

    chunk_size = int(os.getenv('CHUNK_SIZE', '10000'))
    inserted_chunks = 0
    rows_sent = 0

    for i in range(0, total, chunk_size):
        chunk = df_prepared.iloc[i:i+chunk_size]
        _insert_chunk_anti_join(client, chunk)
        inserted_chunks += 1
        rows_sent += len(chunk)
        print(f'Chunks insertados: {inserted_chunks} / Filas enviadas: {rows_sent}')

    return {'inserted_chunks': inserted_chunks, 'rows_sent': rows_sent}


