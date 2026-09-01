from mage_ai.io.config import ConfigFileLoader
import pandas as pd
import datetime as _dt

from pymongo import MongoClient, ReplaceOne

if 'data_exporter' not in globals():
    from mage_ai.data_preparation.decorators import data_exporter
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test


CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

MONGO_DB = 'MPoS'
MONGO_COLLECTION = 'bankTransactions'

DOC_COLS = [
    'fecha',
    'tipoMovimiento',
    'descripcion',
    'sucursal',
    'banco',
    'monto',
    'importe',
    'fechaContable',
    'nroMovimiento',
    'horaTransaccion',
    'codigoOperacion',
    'rutUsuario',
    'cuenta',
    'key',
    'createdAtUtc',
]


def _mongo_client():
    cfg = ConfigFileLoader(CONFIG_PATH, PROFILE)
    return MongoClient(cfg['MONGODB_URI'])


def _is_missing(v) -> bool:
    try:
        return v is None or pd.isna(v)
    except Exception:
        return v is None


def _to_python_value(v):
    """
    Convierte valores de pandas/numpy a tipos Python simples para PyMongo.
    No cambia la lógica del dato, solo lo deja serializable.
    """
    if _is_missing(v):
        return None

    # pandas Timestamp -> datetime python
    if isinstance(v, pd.Timestamp):
        return v.to_pydatetime()

    # pandas scalar -> python scalar
    if hasattr(v, 'item'):
        try:
            return v.item()
        except Exception:
            pass

    return v


def _prepare_for_mongo(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    for c in DOC_COLS:
        if c not in df.columns:
            df[c] = pd.NA

    # key obligatoria
    df['key'] = df['key'].astype('string')
    df = df[df['key'].notna() & (df['key'].astype(str).str.strip() != '')].copy()

    # si createdAtUtc no viene, completarlo
    if 'createdAtUtc' in df.columns:
        now_utc = _dt.datetime.now(_dt.timezone.utc)
        df['createdAtUtc'] = df['createdAtUtc'].where(df['createdAtUtc'].notna(), now_utc)

    return df[DOC_COLS]


def _build_docs(df: pd.DataFrame):
    docs = []
    for row in df.itertuples(index=False, name=None):
        doc = {}
        for col, val in zip(df.columns, row):
            doc[col] = _to_python_value(val)
        docs.append(doc)
    return docs


@data_exporter
def export_data_to_mongo(df: pd.DataFrame, *args, **kwargs):
    client = _mongo_client()
    db_name = kwargs.get('mongo_db') or MONGO_DB
    collection_name = kwargs.get('mongo_collection') or MONGO_COLLECTION

    collection = client[db_name][collection_name]

    dfp = _prepare_for_mongo(df)
    total = len(dfp)

    if total == 0:
        print('No hay filas para exportar a Mongo.')
        return {
            'processed_docs': 0,
            'upserted_count': 0,
            'modified_count': 0,
            'matched_count': 0,
        }

    chunk_size = int(kwargs.get('chunk_size') or 1000)

    processed_docs = 0
    upserted_count = 0
    modified_count = 0
    matched_count = 0
    processed_chunks = 0

    for i in range(0, total, chunk_size):
        chunk = dfp.iloc[i:i + chunk_size].copy()
        docs = _build_docs(chunk)

        ops = [
            ReplaceOne(
                {'key': doc['key']},
                doc,
                upsert=True,
            )
            for doc in docs
        ]

        if ops:
            result = collection.bulk_write(ops, ordered=False)
            upserted_count += len(result.upserted_ids)
            modified_count += result.modified_count
            matched_count += result.matched_count

        processed_docs += len(docs)
        processed_chunks += 1

        print(
            f'Chunk {processed_chunks}: procesados {processed_docs}, '
            f'upserted {upserted_count}, modified {modified_count}, matched {matched_count}'
        )

    return {
        'processed_docs': processed_docs,
        'upserted_count': upserted_count,
        'modified_count': modified_count,
        'matched_count': matched_count,
    }


@test
def test_output(output, *args) -> None:
    assert output is not None, 'The output is undefined'