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


def _to_date_only(v) -> str:
    s = _to_str(v)
    if not s:
        return ""
    try:
        dtv = pd.to_datetime(s, errors='coerce')
        if pd.isna(dtv):
            return ""
        return dtv.strftime('%Y-%m-%d')
    except Exception:
        return ""


def _to_datetime_str(v) -> str:
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


@transformer
def transform(data, *args, **kwargs) -> pd.DataFrame:
    """
    Input esperado desde data_loader:
      data[0] = rcvsii_df
      data[1] = billingInstallmentPlans_df

    Output:
      Una fila por cuota de factura
    """
    if data is None or len(data) < 2:
        return pd.DataFrame(columns=[
            "invoice_key",
            "folio",
            "rutProveedor",
            "nominaInstallmentNumber",
            "installmentCount",
            "installment_number",
            "installment_amount",
            "installment_due_date",
            "installment_paid_at",
            "is_nomina_installment",
            "nomina_id",
            "nomina_payment_id",
        ])

    rcvsii_df, billingInstallmentPlans_df = data

    if billingInstallmentPlans_df is None or billingInstallmentPlans_df.empty:
        return pd.DataFrame(columns=[
            "invoice_key",
            "folio",
            "rutProveedor",
            "nominaInstallmentNumber",
            "installmentCount",
            "installment_number",
            "installment_amount",
            "installment_due_date",
            "installment_paid_at",
            "is_nomina_installment",
            "nomina_id",
            "nomina_payment_id",
        ])

    # ------------------------------------------------------------
    # Base rcvsii para validar invoice_key
    # ------------------------------------------------------------
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

    # ------------------------------------------------------------
    # billingInstallmentPlans
    # ------------------------------------------------------------
    plans = billingInstallmentPlans_df.copy()

    for c in [
        "_id",
        "invoiceId",
        "nominaInstallmentNumber",
        "installmentCount",
        "installments",
        "nominaId",
        "nominaPaymentId",
    ]:
        if c not in plans.columns:
            plans[c] = None

    rows: List[Dict[str, Any]] = []

    for _, r in plans.iterrows():
        invoice_key = _to_str(r.get("invoiceId"))
        if not invoice_key:
            continue

        if "|" in invoice_key:
            folio, rut = invoice_key.split("|", 1)
            folio = _to_str(folio)
            rut = _to_str(rut)
        else:
            folio = invoice_key
            rut = ""

        if valid_invoice_keys and invoice_key not in valid_invoice_keys:
            continue

        nomina_installment_number = pd.to_numeric(
            pd.Series([r.get("nominaInstallmentNumber")]), errors="coerce"
        ).iloc[0]
        if pd.isna(nomina_installment_number):
            nomina_installment_number = None
        else:
            nomina_installment_number = int(nomina_installment_number)

        installment_count = pd.to_numeric(
            pd.Series([r.get("installmentCount")]), errors="coerce"
        ).iloc[0]
        if pd.isna(installment_count):
            installment_count = None
        else:
            installment_count = int(installment_count)

        installments = _safe_json_loads(r.get("installments"))
        if not installments:
            continue

        for inst in installments:
            if not isinstance(inst, dict):
                continue

            number = pd.to_numeric(pd.Series([inst.get("Number")]), errors="coerce").iloc[0]
            if pd.isna(number):
                number = None
            else:
                number = int(number)

            amount = pd.to_numeric(pd.Series([inst.get("Amount")]), errors="coerce").iloc[0]
            if pd.isna(amount):
                amount = None
            else:
                amount = float(amount)

            due_date = _to_date_only(inst.get("DueDate"))
            paid_at = _to_datetime_str(inst.get("paidAt"))

            is_nomina_installment = 0
            if nomina_installment_number is not None and number is not None:
                is_nomina_installment = 1 if number == nomina_installment_number else 0

            rows.append({
                "invoice_key": invoice_key,
                "folio": folio,
                "rutProveedor": rut,
                "nominaInstallmentNumber": nomina_installment_number,
                "installmentCount": installment_count,
                "installment_number": number,
                "installment_amount": amount,
                "installment_due_date": due_date,
                "installment_paid_at": paid_at,
                "is_nomina_installment": is_nomina_installment,
                "nomina_id": _to_str(r.get("nominaId")),
                "nomina_payment_id": _to_str(r.get("nominaPaymentId")),
            })

    if not rows:
        return pd.DataFrame(columns=[
            "invoice_key",
            "folio",
            "rutProveedor",
            "nominaInstallmentNumber",
            "installmentCount",
            "installment_number",
            "installment_amount",
            "installment_due_date",
            "installment_paid_at",
            "is_nomina_installment",
            "nomina_id",
            "nomina_payment_id",
        ])

    out = pd.DataFrame(rows)

    out["nominaInstallmentNumber"] = pd.to_numeric(out["nominaInstallmentNumber"], errors="coerce")
    out["installmentCount"] = pd.to_numeric(out["installmentCount"], errors="coerce")
    out["installment_number"] = pd.to_numeric(out["installment_number"], errors="coerce")
    out["installment_amount"] = pd.to_numeric(out["installment_amount"], errors="coerce")
    out["is_nomina_installment"] = pd.to_numeric(out["is_nomina_installment"], errors="coerce").fillna(0).astype(int)

    out = out.drop_duplicates(
        subset=["invoice_key", "installment_number", "nomina_id", "nomina_payment_id"],
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
        "nominaInstallmentNumber",
        "installmentCount",
        "installment_number",
        "installment_amount",
        "installment_due_date",
        "installment_paid_at",
        "is_nomina_installment",
    ]
    for col in required_cols:
        assert col in output.columns, f"Falta columna requerida: {col}"