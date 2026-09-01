import json
from typing import Any, Dict, List

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


def _to_datetime_iso(v) -> str:
    s = _to_str(v)
    if not s:
        return ""
    try:
        dtv = pd.to_datetime(s, errors='coerce')
        if pd.isna(dtv):
            return ""
        return dtv.strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return ""


def _parse_invoice_id(raw: str):
    """
    InvoiceId viene como folio|rutProveedor.
    Si no trae |, deja folio y rut vacío.
    """
    raw = _to_str(raw)
    if "|" in raw:
        folio, rut = raw.split("|", 1)
        return raw, _to_str(folio), _to_str(rut)
    return raw, raw, ""


@transformer
def transform(data, *args, **kwargs) -> pd.DataFrame:
    """
    Input esperado desde data_loader:
      data[0] = rcvsii_df
      data[1] = billingNominas_df

    Output:
      Una fila por relación factura <-> payment de nómina
    """
    if data is None or len(data) < 2:
        return pd.DataFrame(columns=[
            "invoice_key",
            "folio",
            "rutProveedor",
            "nomina_id",
            "nomina_payment_id",
            "nomina_name",
            "nomina_type",
            "nomina_status",
            "nomina_created_at",
            "nomina_updated_at",
            "nomina_amount",
        ])

    rcvsii_df, billingNominas_df = data

    if billingNominas_df is None or billingNominas_df.empty:
        return pd.DataFrame(columns=[
            "invoice_key",
            "folio",
            "rutProveedor",
            "nomina_id",
            "nomina_payment_id",
            "nomina_name",
            "nomina_type",
            "nomina_status",
            "nomina_created_at",
            "nomina_updated_at",
            "nomina_amount",
        ])

    # ------------------------------------------------------------------
    # Base rcvsii para validar / normalizar invoice_key existentes
    # ------------------------------------------------------------------
    rc = rcvsii_df.copy() if rcvsii_df is not None else pd.DataFrame()

    if rc.empty:
        valid_invoice_keys = set()
    else:
        for c in ["folio", "rutProveedor", "rutProveedorKey", "rutCliente"]:
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
        valid_invoice_keys = set(rc["invoice_key"].dropna().astype(str).tolist())

    # ------------------------------------------------------------------
    # Explode de billingNominas.payments
    # ------------------------------------------------------------------
    bn = billingNominas_df.copy()

    for c in ["_id", "name", "type", "status", "createdAt", "updatedAt", "payments"]:
        if c not in bn.columns:
            bn[c] = None

    bn["payments"] = bn["payments"].apply(_safe_json_loads)

    rows: List[Dict[str, Any]] = []

    for _, nomina in bn.iterrows():
        payments = nomina["payments"]
        if not isinstance(payments, list) or len(payments) == 0:
            continue

        for p in payments:
            if not isinstance(p, dict):
                continue

            invoice_key_raw = _to_str(p.get("InvoiceId"))
            invoice_key, folio, rut_proveedor = _parse_invoice_id(invoice_key_raw)

            if not invoice_key:
                continue

            # Si rcvsii viene poblado, filtramos solo relaciones que realmente existan ahí
            if valid_invoice_keys and invoice_key not in valid_invoice_keys:
                continue

            amount = pd.to_numeric(pd.Series([p.get("Amount")]), errors='coerce').iloc[0]
            if pd.isna(amount):
                amount = None
            else:
                amount = float(amount)

            rows.append({
                "invoice_key": invoice_key,
                "folio": folio,
                "rutProveedor": rut_proveedor,
                "nomina_id": _to_str(nomina.get("_id")),
                "nomina_payment_id": _to_str(p.get("_id")),
                "nomina_name": _to_str(nomina.get("name")),
                "nomina_type": _to_str(nomina.get("type")),
                "nomina_status": _to_str(nomina.get("status")),
                "nomina_created_at": _to_datetime_iso(nomina.get("createdAt")),
                "nomina_updated_at": _to_datetime_iso(nomina.get("updatedAt")),
                "nomina_amount": amount,
            })

    if not rows:
        return pd.DataFrame(columns=[
            "invoice_key",
            "folio",
            "rutProveedor",
            "nomina_id",
            "nomina_payment_id",
            "nomina_name",
            "nomina_type",
            "nomina_status",
            "nomina_created_at",
            "nomina_updated_at",
            "nomina_amount",
        ])

    out = pd.DataFrame(rows)

    out["nomina_amount"] = pd.to_numeric(out["nomina_amount"], errors="coerce")
    out = out.drop_duplicates(
        subset=["invoice_key", "nomina_id", "nomina_payment_id"],
        keep="first",
    ).reset_index(drop=True)

    return out


@test
def test_output(output, *args) -> None:
    assert output is not None, "The output is undefined"
    assert isinstance(output, pd.DataFrame), "El transformer debe retornar un DataFrame"

    required_cols = [
        "invoice_key",
        "folio",
        "rutProveedor",
        "nomina_id",
        "nomina_payment_id",
        "nomina_name",
        "nomina_type",
        "nomina_status",
        "nomina_created_at",
        "nomina_updated_at",
        "nomina_amount",
    ]
    for col in required_cols:
        assert col in output.columns, f"Falta columna requerida: {col}"