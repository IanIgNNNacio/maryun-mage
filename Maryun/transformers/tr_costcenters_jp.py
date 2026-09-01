import json
from typing import Any, List, Dict

import pandas as pd

if 'transformer' not in globals():
    from mage_ai.data_preparation.decorators import transformer
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test


def _to_str(x) -> str:
    if x is None:
        return ""
    if isinstance(x, float) and pd.isna(x):
        return ""
    s = str(x).strip()
    return "" if s.lower() in ("nan", "none", "null") else s


def _safe_json_loads(v) -> list:
    """
    En el data_loader, arrays fueron serializados como JSON string.
    Esto los convierte a lista Python. Si ya viene lista, la retorna.
    """
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return []
        try:
            out = json.loads(s)
            return out if isinstance(out, list) else []
        except Exception:
            return []
    return []


def _prep_dim_costcenter(costcenter_df: pd.DataFrame) -> pd.DataFrame:
    if costcenter_df is None or costcenter_df.empty:
        return pd.DataFrame(columns=["cost_center_id", "cost_center_name"])

    df = costcenter_df.copy()
    for c in ["_id", "name"]:
        if c not in df.columns:
            df[c] = None

    df["cost_center_id"] = df["_id"].apply(_to_str)
    df["cost_center_name"] = df["name"].apply(_to_str)

    return df[["cost_center_id", "cost_center_name"]].drop_duplicates(subset=["cost_center_id"])


@transformer
def transform(data, *args, **kwargs) -> pd.DataFrame:
    """
    Input esperado desde el data_loader:
      data[0] = rcvsii_df
      data[1] = costCenter_df

    Output:
      Una fila por factura + centro de costo
    """
    if data is None or len(data) < 2:
        return pd.DataFrame(columns=[
            "invoice_key",
            "folio",
            "rutProveedor",
            "cost_center_id",
            "cost_center_name",
            "cost_center_percent",
        ])

    rcvsii_df, costCenter_df = data

    if rcvsii_df is None or rcvsii_df.empty:
        return pd.DataFrame(columns=[
            "invoice_key",
            "folio",
            "rutProveedor",
            "cost_center_id",
            "cost_center_name",
            "cost_center_percent",
        ])

    rc = rcvsii_df.copy()

    for c in ["folio", "rutProveedor", "rutProveedorKey", "rutCliente", "classification.costCenterSplits"]:
        if c not in rc.columns:
            rc[c] = None

    rc["folio"] = rc["folio"].apply(_to_str)

    rc["rutProveedor"] = rc["rutProveedor"].apply(_to_str)
    mask_empty = rc["rutProveedor"].eq("")
    if mask_empty.any():
        rc.loc[mask_empty, "rutProveedor"] = rc.loc[mask_empty, "rutProveedorKey"].apply(_to_str)

    mask_empty = rc["rutProveedor"].eq("")
    if mask_empty.any():
        rc.loc[mask_empty, "rutProveedor"] = rc.loc[mask_empty, "rutCliente"].apply(_to_str)

    rc["invoice_key"] = rc.apply(
        lambda r: f"{_to_str(r['folio'])}|{_to_str(r['rutProveedor'])}",
        axis=1,
    )

    dim_cc = _prep_dim_costcenter(costCenter_df)
    cc_map = dict(zip(dim_cc["cost_center_id"], dim_cc["cost_center_name"]))

    rows: List[Dict[str, Any]] = []

    for _, r in rc.iterrows():
        splits = _safe_json_loads(r.get("classification.costCenterSplits"))

        if not splits:
            continue

        for item in splits:
            if not isinstance(item, dict):
                continue

            cc_id = _to_str(item.get("costCenterId"))

            pct_raw = item.get("percent")
            try:
                pct = float(pct_raw) if pct_raw is not None and str(pct_raw).strip() != "" else None
            except Exception:
                pct = None

            rows.append({
                "invoice_key": _to_str(r.get("invoice_key")),
                "folio": _to_str(r.get("folio")),
                "rutProveedor": _to_str(r.get("rutProveedor")),
                "cost_center_id": cc_id,
                "cost_center_name": cc_map.get(cc_id, ""),
                "cost_center_percent": pct,
            })

    if not rows:
        return pd.DataFrame(columns=[
            "invoice_key",
            "folio",
            "rutProveedor",
            "cost_center_id",
            "cost_center_name",
            "cost_center_percent",
        ])

    out = pd.DataFrame(rows)

    out["cost_center_percent"] = pd.to_numeric(out["cost_center_percent"], errors="coerce")
    out = out.drop_duplicates()

    return out


@test
def test_output(output, *args) -> None:
    assert output is not None, "The output is undefined"
    assert isinstance(output, pd.DataFrame), "El transformer debe retornar un DataFrame"

    required_cols = [
        "invoice_key",
        "folio",
        "rutProveedor",
        "cost_center_id",
        "cost_center_name",
        "cost_center_percent",
    ]
    for col in required_cols:
        assert col in output.columns, f"Falta columna requerida: {col}"