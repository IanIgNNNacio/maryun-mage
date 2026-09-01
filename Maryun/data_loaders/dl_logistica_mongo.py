import datetime as dt
from typing import Any, Dict, List
from urllib.parse import urlparse

from pymongo import MongoClient
from mage_ai.io.config import ConfigFileLoader

try:
    from bson import ObjectId
    from bson.decimal128 import Decimal128
except Exception:
    ObjectId = None
    Decimal128 = None

if 'data_loader' not in globals():
    from mage_ai.data_preparation.decorators import data_loader
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test

CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

# Colecciones MongoDB de maestros de logistica (nombres reales en Mongo)
COLLECTIONS = [
    'logistica_overrides_forecast',
    'logistica_overrides_clasificacion',
    'logistica_automatizacion_sku_sucursal',
    'logistica_homologacion_productos',
    'logistica_politica_proveedores',
    'logistica_politica_costos_transporte',
    'logistica_politica_distancias',
    'logistica_politica_prioridad_cd',
    'logistica_politica_reglas_sku_sucursal',
    'logistica_silencio_sugerencias',
]


def _mongo_client() -> MongoClient:
    cfg = ConfigFileLoader(CONFIG_PATH, PROFILE)
    mongo_uri = cfg['MONGODB_URI']
    parsed = urlparse(mongo_uri)
    if parsed.scheme not in ['mongodb', 'mongodb+srv']:
        raise ValueError("MONGODB_URI debe comenzar con 'mongodb://' o 'mongodb+srv://'")
    return MongoClient(mongo_uri)


def _safe(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (dt.datetime, dt.date)):
        return v
    if ObjectId is not None and isinstance(v, ObjectId):
        return str(v)
    if Decimal128 is not None and isinstance(v, Decimal128):
        return float(v.to_decimal())
    return str(v)


def _sanitize(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {str(k): (str(v) if k == '_id' else _safe(v)) for k, v in doc.items()}


@data_loader
def load_data(*args, **kwargs) -> Dict[str, List[Dict[str, Any]]]:
    """Lee las 10 colecciones de maestros de logistica desde Mongo.

    Devuelve un dict {nombre_coleccion: [docs]} hacia el data_exporter.
    """
    cfg = ConfigFileLoader(CONFIG_PATH, PROFILE)
    db_name = cfg['MONGODB_DB']

    client = _mongo_client()
    db = client[db_name]

    out: Dict[str, List[Dict[str, Any]]] = {}
    for name in COLLECTIONS:
        out[name] = [_sanitize(d) for d in db[name].find({})]

    client.close()

    for name in COLLECTIONS:
        print(f'{name}: {len(out[name])} docs')

    return out


@test
def test_output(output, *args) -> None:
    assert isinstance(output, dict), 'El data_loader debe retornar un dict'
    for name in COLLECTIONS:
        assert name in output, f'Falta la coleccion en el output: {name}'
        assert isinstance(output[name], list), f'{name} debe ser lista de docs'
