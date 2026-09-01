import json
import datetime as dt
from typing import Any, Dict, Optional, List, Tuple
from urllib.parse import urlparse

import pandas as pd
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


def _mongo_client() -> MongoClient:
    cfg = ConfigFileLoader(CONFIG_PATH, PROFILE)
    mongo_uri = cfg['MONGODB_URI']

    parsed = urlparse(mongo_uri)
    if parsed.scheme not in ['mongodb', 'mongodb+srv']:
        raise ValueError("MONGODB_URI debe comenzar con 'mongodb://' o 'mongodb+srv://'")

    return MongoClient(mongo_uri)


def _json_safe_value(v: Any) -> Any:
    """
    - Dicts: se mantienen como dict para que json_normalize los aplane a columnas usando sep='.'
    - Listas: se convierten a string JSON para que sea estable (y no reviente el sample/variables de Mage)
    """
    if v is None or isinstance(v, (str, int, float, bool)):
        return v

    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()

    if ObjectId is not None and isinstance(v, ObjectId):
        return str(v)

    if Decimal128 is not None and isinstance(v, Decimal128):
        # Mongo decimal128 -> string numérica
        return str(v.to_decimal())

    if isinstance(v, dict):
        return {str(k): _json_safe_value(val) for k, val in v.items()}

    if isinstance(v, (list, tuple, set)):
        safe_list = [_json_safe_value(x) for x in list(v)]
        return json.dumps(safe_list, ensure_ascii=False)

    return str(v)


def _sanitize_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in doc.items():
        if k == "_id":
            out["_id"] = str(v)
        else:
            out[str(k)] = _json_safe_value(v)
    return out


def _read_collection_flat_df(
    client: MongoClient,
    db_name: str,
    collection_name: str,
    query: Optional[Dict[str, Any]] = None,
    projection: Optional[Dict[str, int]] = None,
    limit: Optional[int] = None,
    batch_size: int = 2000,
) -> pd.DataFrame:
    col = client[db_name][collection_name]
    cursor = col.find(query or {}, projection=projection, batch_size=batch_size)
    if limit:
        cursor = cursor.limit(int(limit))

    docs: List[Dict[str, Any]] = [_sanitize_doc(d) for d in cursor]
    if not docs:
        return pd.DataFrame()

    # Flatten: fields.xxxx, classification.costCenterSplits, etc.
    return pd.json_normalize(docs, sep='.')


@data_loader
def load_data(*args, **kwargs) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Outputs (en este orden):
      1) rcvsii_df
      2) billingNominas_df
      3) billingInvoices_df
      4) costCenter_df
      5) accounts_df
      6) billingInstallmentPlans_df
      7) supplier_df
    """
    cfg = ConfigFileLoader(CONFIG_PATH, PROFILE)
    db_name = cfg['MONGODB_DB']

    client = _mongo_client()

    rcvsii_df = _read_collection_flat_df(client, db_name, "rcvDocuments")
    # rcvsii_df = _read_collection_flat_df(client, db_name, "rcvsii")
    billingNominas_df = _read_collection_flat_df(client, db_name, "billingPayrolls")
    # billingNominas_df = _read_collection_flat_df(client, db_name, "billingNominas")
    billingInvoices_df = _read_collection_flat_df(client, db_name, "billingInvoices")
    # billingInvoices_df = _read_collection_flat_df(client, db_name, "billingInvoices")
    costCenter_df = _read_collection_flat_df(client, db_name, "costCenters")
    # costCenter_df = _read_collection_flat_df(client, db_name, "costCenters")
    accounts_df = _read_collection_flat_df(client, db_name, "accounts")
    # accounts_df = _read_collection_flat_df(client, db_name, "accounts")
    billingInstallmentPlans_df = _read_collection_flat_df(client, db_name, "billingInstallmentPlans")
    # billingInstallmentPlans_df = _read_collection_flat_df(client, db_name, "billingInstallmentPlans")
    supplier_df = _read_collection_flat_df(client, db_name, "supplier")
    # supplier_df = _read_collection_flat_df(client, db_name, "supplier")

    client.close()

    return rcvsii_df, billingNominas_df, billingInvoices_df, costCenter_df, accounts_df, billingInstallmentPlans_df, supplier_df


@test
def test_output(output, *args) -> None:
    """
    Mage pasa múltiples outputs como: test_output(output_0, output_1, output_2, ...)
    """
    outputs = (output,) + args
    assert len(outputs) >= 7, "Se esperaban 7 outputs: rcvsii, billingNominas, billingInvoices, costCenter, accounts, billingInstallmentPlans"

    for i in range(7):
        df = outputs[i]
        assert df is not None, f"Output {i} es None"
        assert isinstance(df, pd.DataFrame), f"Output {i} no es DataFrame"