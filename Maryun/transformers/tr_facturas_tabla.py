import json
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

if 'transformer' not in globals():
    from mage_ai.data_preparation.decorators import transformer
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test


@transformer
def transform(data, *args, **kwargs) -> pd.DataFrame:
    """
    Input esperado desde el data_loader:
      data[0] = rcvsii_df
      data[1] = billingNominas_df
      data[2] = billingInvoices_df
      data[3] = costCenter_df
      data[4] = accounts_df
      data[5] = billingInstallmentPlans_df
      data[6] = supplier_df

    Base principal: rcvsii
    Clave técnica: invoice_key = folio|rutProveedor
    """
    if data is None or len(data) < 7:
        return pd.DataFrame()

    (
        rcvsii_df,
        billingNominas_df,
        billingInvoices_df,
        costCenter_df,
        accounts_df,
        billingInstallmentPlans_df,
        supplier_df,
    ) = data

    if rcvsii_df is None or rcvsii_df.empty:
        return pd.DataFrame(columns=[
            "invoice_key",
            "folio",
            "rutProveedor",
            "razon_social",
            "periodo",
            "fechaDocto",
            "fechaRecepcion",
            "operacion",
            "tipoDoc",
            "montoTotal",
            "estadoContab",
            "status",
            "creditDays",
            "Nominas",
            "nomina_name",
            "nomina_type",
            "nomina_status",
            "nomina_created_at",
            "nomina_updated_at",
            "nominaInstallmentNumber",
            "installment_number",
            "installment_amount",
            "installment_due_date",
            "installmentCount",
            "Installments",
            "paidAmount",
            "openAmount",
            "isCreditNote",
            "costCenters",
            "codigo_cuenta_contable",
            "cuenta_contable",
            "tipo_cuenta_contable",
        ])

    rc = rcvsii_df.copy()

    for c in [
        "folio",
        "rutProveedor",
        "rutProveedorKey",
        "rutCliente",
        "periodo",
        "fechaDocto",
        "fechaRecepcion",
        "operacion",
        "tipoDoc",
        "montoTotal",
        "estadoContab",
        "status",
        "fields.razon_social",
        "razonSocial",
        "classification.accountId",
        "classification.costCenterSplits",
    ]:
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

    rc["razon_social"] = rc["fields.razon_social"].apply(_to_str)
    rs_empty = rc["razon_social"].eq("")
    if rs_empty.any():
        rc.loc[rs_empty, "razon_social"] = rc.loc[rs_empty, "razonSocial"].apply(_to_str)

    rc["periodo"] = rc["periodo"].apply(_to_str)
    rc["fechaDocto"] = rc["fechaDocto"].apply(_to_date_only)
    rc["fechaRecepcion"] = rc["fechaRecepcion"].apply(_to_date_only)
    rc["operacion"] = rc["operacion"].apply(_to_str)
    rc["tipoDoc"] = rc["tipoDoc"].apply(_to_str)
    rc["montoTotal"] = pd.to_numeric(rc["montoTotal"], errors="coerce")
    rc["estadoContab"] = rc["estadoContab"].apply(_to_str)
    rc["status"] = rc["status"].apply(_to_str)

    # ------------------------------------------------------------------
    # Suppliers
    # ------------------------------------------------------------------
    dim_suppliers = _prep_suppliers(supplier_df)
    rc = rc.merge(
        dim_suppliers,
        how="left",
        on="rutProveedor",
    )

    # ------------------------------------------------------------------
    # Accounts
    # ------------------------------------------------------------------
    dim_accounts = _prep_accounts(accounts_df)
    rc["account_id"] = rc["classification.accountId"].apply(_to_str)

    rc = rc.merge(
        dim_accounts,
        how="left",
        left_on="account_id",
        right_on="account_id",
    )

    # ------------------------------------------------------------------
    # Cost centers
    # ------------------------------------------------------------------
    dim_costcenters = _prep_costcenters(costCenter_df)
    rc["costCenters"] = rc.apply(
        lambda r: _build_cost_centers_json(
            r.get("classification.costCenterSplits"),
            dim_costcenters,
        ),
        axis=1,
    )

    # ------------------------------------------------------------------
    # Nóminas
    # ------------------------------------------------------------------
    exploded_nominas = _explode_nominas(billingNominas_df)
    nominas_agg = _aggregate_nominas(exploded_nominas)

    rc = rc.merge(
        nominas_agg,
        how="left",
        on="invoice_key",
    )

    for c in [
        "Nominas",
        "nomina_name",
        "nomina_type",
        "nomina_status",
        "nomina_created_at",
        "nomina_updated_at",
    ]:
        if c not in rc.columns:
            rc[c] = "No tiene"
        rc[c] = rc[c].fillna("No tiene")

    # ------------------------------------------------------------------
    # Billing invoices
    # ------------------------------------------------------------------
    billing_inv = _prep_billing_invoices(billingInvoices_df)

    rc = rc.merge(
        billing_inv,
        how="left",
        on="invoice_key",
    )

    if "paidAmount" not in rc.columns:
        rc["paidAmount"] = pd.NA
    if "openAmount" not in rc.columns:
        rc["openAmount"] = pd.NA
    if "isCreditNote" not in rc.columns:
        rc["isCreditNote"] = 0

    rc["paidAmount"] = pd.to_numeric(rc["paidAmount"], errors="coerce")
    rc["openAmount"] = pd.to_numeric(rc["openAmount"], errors="coerce")
    rc["isCreditNote"] = rc["isCreditNote"].fillna(0).astype(int)

    # ------------------------------------------------------------------
    # Installment plans
    # ------------------------------------------------------------------
    installment_plans = _prep_installment_plans(billingInstallmentPlans_df)
    rc = rc.merge(
        installment_plans,
        how="left",
        on="invoice_key",
    )

    if "Installments" not in rc.columns:
        rc["Installments"] = "No tiene"
    rc["Installments"] = rc["Installments"].fillna("No tiene")

    # ------------------------------------------------------------------
    # Proyección final
    # ------------------------------------------------------------------
    final_cols = [
        "invoice_key",
        "folio",
        "rutProveedor",
        "razon_social",
        "periodo",
        "fechaDocto",
        "fechaRecepcion",
        "operacion",
        "tipoDoc",
        "montoTotal",
        "estadoContab",
        "status",
        "creditDays",
        "Nominas",
        "nomina_name",
        "nomina_type",
        "nomina_status",
        "nomina_created_at",
        "nomina_updated_at",
        "nominaInstallmentNumber",
        "installment_number",
        "installment_amount",
        "installment_due_date",
        "installmentCount",
        "Installments",
        "paidAmount",
        "openAmount",
        "isCreditNote",
        "costCenters",
        "codigo_cuenta_contable",
        "cuenta_contable",
        "tipo_cuenta_contable",
    ]

    for c in final_cols:
        if c not in rc.columns:
            rc[c] = pd.NA

    out = rc[final_cols].copy()

    out["fechaDocto"] = pd.to_datetime(out["fechaDocto"], errors="coerce").dt.date
    out["fechaRecepcion"] = pd.to_datetime(out["fechaRecepcion"], errors="coerce").dt.date
    out["installment_due_date"] = pd.to_datetime(out["installment_due_date"], errors="coerce").dt.date

    out["creditDays"] = pd.to_numeric(out["creditDays"], errors="coerce")
    out["nominaInstallmentNumber"] = pd.to_numeric(out["nominaInstallmentNumber"], errors="coerce")
    out["installment_number"] = pd.to_numeric(out["installment_number"], errors="coerce")
    out["installmentCount"] = pd.to_numeric(out["installmentCount"], errors="coerce")

    out["installment_amount"] = pd.to_numeric(out["installment_amount"], errors="coerce")
    out["paidAmount"] = pd.to_numeric(out["paidAmount"], errors="coerce")
    out["openAmount"] = pd.to_numeric(out["openAmount"], errors="coerce")

    out["isCreditNote"] = out["isCreditNote"].fillna(0).astype(int)

    return out


@test
def test_output(output, *args) -> None:
    assert output is not None, "The output is undefined"
    assert isinstance(output, pd.DataFrame), "El transformer debe retornar un DataFrame"

    required_cols = [
        "invoice_key",
        "folio",
        "rutProveedor",
        "razon_social",
        "periodo",
        "fechaDocto",
        "fechaRecepcion",
        "operacion",
        "tipoDoc",
        "montoTotal",
        "estadoContab",
        "status",
        "creditDays",
        "Nominas",
        "Installments",
        "costCenters",
        "codigo_cuenta_contable",
        "cuenta_contable",
    ]
    for col in required_cols:
        assert col in output.columns, f"Falta columna requerida: {col}"


def _to_str(x: Any) -> str:
    if x is None:
        return ""
    if isinstance(x, float) and pd.isna(x):
        return ""
    s = str(x).strip()
    return "" if s.lower() in ("nan", "none", "null") else s


def _safe_json_loads(v: Any) -> list:
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


def _to_date_only(v: Any) -> Optional[str]:
    s = _to_str(v)
    if not s:
        return None
    try:
        return pd.to_datetime(s, errors="coerce").date().isoformat()
    except Exception:
        return None


def _to_datetime_iso(v: Any) -> Optional[str]:
    s = _to_str(v)
    if not s:
        return None
    try:
        ts = pd.to_datetime(s, errors="coerce")
        if pd.isna(ts):
            return None
        return ts.isoformat()
    except Exception:
        return None


def _prep_suppliers(supplier_df: pd.DataFrame) -> pd.DataFrame:
    if supplier_df is None or supplier_df.empty:
        return pd.DataFrame(columns=["rutProveedor", "creditDays"])

    df = supplier_df.copy()
    for c in ["RUT", "creditDays"]:
        if c not in df.columns:
            df[c] = None

    df["rutProveedor"] = df["RUT"].apply(_to_str)
    df["creditDays"] = pd.to_numeric(df["creditDays"], errors="coerce")

    df = df.sort_values(by=["rutProveedor"]).drop_duplicates(subset=["rutProveedor"], keep="first")
    return df[["rutProveedor", "creditDays"]]


def _prep_accounts(accounts_df: pd.DataFrame) -> pd.DataFrame:
    if accounts_df is None or accounts_df.empty:
        return pd.DataFrame(columns=[
            "account_id",
            "codigo_cuenta_contable",
            "cuenta_contable",
            "tipo_cuenta_contable",
        ])

    df = accounts_df.copy()
    for c in ["_id", "code", "name", "typeName", "type"]:
        if c not in df.columns:
            df[c] = None

    df["account_id"] = df["_id"].apply(_to_str)
    df["codigo_cuenta_contable"] = df["code"].apply(_to_str)
    df["cuenta_contable"] = df["name"].apply(_to_str)
    df["tipo_cuenta_contable"] = df["typeName"].apply(_to_str)

    mask_empty = df["tipo_cuenta_contable"].eq("")
    if mask_empty.any():
        df.loc[mask_empty, "tipo_cuenta_contable"] = df.loc[mask_empty, "type"].apply(_to_str)

    return df[[
        "account_id",
        "codigo_cuenta_contable",
        "cuenta_contable",
        "tipo_cuenta_contable",
    ]].drop_duplicates(subset=["account_id"])


def _prep_costcenters(costcenter_df: pd.DataFrame) -> pd.DataFrame:
    if costcenter_df is None or costcenter_df.empty:
        return pd.DataFrame(columns=["cost_center_id", "cost_center_name"])

    df = costcenter_df.copy()
    for c in ["_id", "name"]:
        if c not in df.columns:
            df[c] = None

    df["cost_center_id"] = df["_id"].apply(_to_str)
    df["cost_center_name"] = df["name"].apply(_to_str)

    return df[["cost_center_id", "cost_center_name"]].drop_duplicates(subset=["cost_center_id"])


def _build_cost_centers_json(cost_center_splits_raw: Any, dim_costcenters: pd.DataFrame) -> str:
    arr = _safe_json_loads(cost_center_splits_raw)
    if not arr:
        return "No tiene"

    cc_map = {}
    if dim_costcenters is not None and not dim_costcenters.empty:
        cc_map = dict(
            zip(
                dim_costcenters["cost_center_id"].astype(str),
                dim_costcenters["cost_center_name"].astype(str),
            )
        )

    out = []
    for item in arr:
        if not isinstance(item, dict):
            continue

        cc_id = _to_str(item.get("costCenterId"))
        pct = item.get("percent")
        try:
            pct = float(pct) if pct is not None else None
        except Exception:
            pct = None

        out.append({
            "name": cc_map.get(cc_id, ""),
            "percent": pct,
        })

    return json.dumps(out, ensure_ascii=False) if out else "No tiene"


def _explode_nominas(nominas_df: pd.DataFrame) -> pd.DataFrame:
    if nominas_df is None or nominas_df.empty:
        return pd.DataFrame(columns=[
            "invoice_key",
            "nomina_id",
            "nomina_payment_id",
            "nomina_name",
            "nomina_type",
            "nomina_status",
            "nomina_created_at",
            "nomina_updated_at",
        ])

    df = nominas_df.copy()
    for c in ["_id", "name", "type", "status", "createdAt", "updatedAt", "payments"]:
        if c not in df.columns:
            df[c] = None

    df["payments"] = df["payments"].apply(_safe_json_loads)
    df = df.explode("payments", ignore_index=True)

    def get_field(obj: Any, key: str) -> Any:
        if isinstance(obj, dict):
            return obj.get(key)
        return None

    df["invoice_key"] = df["payments"].apply(lambda x: _to_str(get_field(x, "InvoiceId")))
    df["nomina_payment_id"] = df["payments"].apply(lambda x: _to_str(get_field(x, "_id")))

    df["nomina_id"] = df["_id"].apply(_to_str)
    df["nomina_name"] = df["name"].apply(_to_str)
    df["nomina_type"] = df["type"].apply(_to_str)
    df["nomina_status"] = df["status"].apply(_to_str)
    df["nomina_created_at"] = df["createdAt"].apply(_to_datetime_iso)
    df["nomina_updated_at"] = df["updatedAt"].apply(_to_datetime_iso)

    df = df[df["invoice_key"].ne("")].copy()

    return df[[
        "invoice_key",
        "nomina_id",
        "nomina_payment_id",
        "nomina_name",
        "nomina_type",
        "nomina_status",
        "nomina_created_at",
        "nomina_updated_at",
    ]]


def _aggregate_nominas(exploded_nominas: pd.DataFrame) -> pd.DataFrame:
    if exploded_nominas is None or exploded_nominas.empty:
        return pd.DataFrame(columns=[
            "invoice_key",
            "Nominas",
            "nomina_name",
            "nomina_type",
            "nomina_status",
            "nomina_created_at",
            "nomina_updated_at",
        ])

    def build_group(g: pd.DataFrame) -> pd.Series:
        g = g.drop_duplicates(subset=["nomina_id", "nomina_payment_id"]).copy()

        nominas_json = []
        name_arr = []
        type_arr = []
        status_arr = []
        created_arr = []
        updated_arr = []

        for _, r in g.iterrows():
            nominas_json.append({
                "nomina_id": _to_str(r["nomina_id"]),
                "nomina_payment_id": _to_str(r["nomina_payment_id"]),
                "name": _to_str(r["nomina_name"]),
                "type": _to_str(r["nomina_type"]),
                "nomina_status": _to_str(r["nomina_status"]),
                "nomina_created_at": r["nomina_created_at"],
                "nomina_updated_at": r["nomina_updated_at"],
            })
            name_arr.append(_to_str(r["nomina_name"]))
            type_arr.append(_to_str(r["nomina_type"]))
            status_arr.append(_to_str(r["nomina_status"]))
            created_arr.append(r["nomina_created_at"])
            updated_arr.append(r["nomina_updated_at"])

        return pd.Series({
            "Nominas": json.dumps(nominas_json, ensure_ascii=False) if nominas_json else "No tiene",
            "nomina_name": json.dumps(name_arr, ensure_ascii=False) if name_arr else "No tiene",
            "nomina_type": json.dumps(type_arr, ensure_ascii=False) if type_arr else "No tiene",
            "nomina_status": json.dumps(status_arr, ensure_ascii=False) if status_arr else "No tiene",
            "nomina_created_at": json.dumps(created_arr, ensure_ascii=False) if created_arr else "No tiene",
            "nomina_updated_at": json.dumps(updated_arr, ensure_ascii=False) if updated_arr else "No tiene",
        })

    return exploded_nominas.groupby("invoice_key", dropna=False).apply(build_group).reset_index()


def _prep_billing_invoices(billingInvoices_df: pd.DataFrame) -> pd.DataFrame:
    if billingInvoices_df is None or billingInvoices_df.empty:
        return pd.DataFrame(columns=[
            "invoice_key",
            "paidAmount",
            "openAmount",
            "isCreditNote",
        ])

    df = billingInvoices_df.copy()
    for c in ["folio", "thirdPartyRut", "paidAmount", "openAmount", "isCreditNote", "updatedAt", "createdAt"]:
        if c not in df.columns:
            df[c] = None

    df["folio"] = df["folio"].apply(_to_str)
    df["thirdPartyRut"] = df["thirdPartyRut"].apply(_to_str)
    df["invoice_key"] = df.apply(lambda r: f"{r['folio']}|{r['thirdPartyRut']}", axis=1)

    df["paidAmount"] = pd.to_numeric(df["paidAmount"], errors="coerce")
    df["openAmount"] = pd.to_numeric(df["openAmount"], errors="coerce")
    df["isCreditNote"] = df["isCreditNote"].apply(
        lambda x: 1 if str(x).strip().lower() == "true" or x is True else 0
    )

    sort_col = None
    if "updatedAt" in df.columns:
        df["_sort_dt"] = pd.to_datetime(df["updatedAt"], errors="coerce")
        sort_col = "_sort_dt"
    elif "createdAt" in df.columns:
        df["_sort_dt"] = pd.to_datetime(df["createdAt"], errors="coerce")
        sort_col = "_sort_dt"

    if sort_col:
        df = df.sort_values(by=[sort_col], ascending=False)

    df = df.drop_duplicates(subset=["invoice_key"], keep="first")

    return df[[
        "invoice_key",
        "paidAmount",
        "openAmount",
        "isCreditNote",
    ]]


def _prep_installment_plans(plans_df: pd.DataFrame) -> pd.DataFrame:
    if plans_df is None or plans_df.empty:
        return pd.DataFrame(columns=[
            "invoice_key",
            "nominaInstallmentNumber",
            "installment_number",
            "installment_amount",
            "installment_due_date",
            "installmentCount",
            "Installments",
        ])

    df = plans_df.copy()
    for c in [
        "invoiceId",
        "nominaInstallmentNumber",
        "installmentCount",
        "installments",
        "updatedAt",
        "createdAt",
    ]:
        if c not in df.columns:
            df[c] = None

    df["invoice_key"] = df["invoiceId"].apply(_to_str)
    df["nominaInstallmentNumber"] = pd.to_numeric(df["nominaInstallmentNumber"], errors="coerce")
    df["installmentCount"] = pd.to_numeric(df["installmentCount"], errors="coerce")
    df["installments"] = df["installments"].apply(_safe_json_loads)

    sort_source = "updatedAt" if "updatedAt" in df.columns else "createdAt"
    df["_sort_dt"] = pd.to_datetime(df[sort_source], errors="coerce")
    df = df.sort_values(by=["_sort_dt"], ascending=False)

    rows = []
    for invoice_key, g in df.groupby("invoice_key", dropna=False):
        row = g.iloc[0]

        installments = row["installments"] if isinstance(row["installments"], list) else []
        nomina_installment_number = row["nominaInstallmentNumber"]

        picked_number = None
        picked_amount = None
        picked_due_date = None

        installments_json = []
        for inst in installments:
            if not isinstance(inst, dict):
                continue

            number = pd.to_numeric(inst.get("Number"), errors="coerce")
            amount = pd.to_numeric(inst.get("Amount"), errors="coerce")
            due_date = _to_date_only(inst.get("DueDate"))
            paid_at = inst.get("paidAt")
            paid_at_fmt = _to_datetime_iso(paid_at) if _to_str(paid_at) else None

            installments_json.append({
                "number": int(number) if pd.notna(number) else None,
                "Amount": float(amount) if pd.notna(amount) else None,
                "DueDate": due_date,
                "paidAt": paid_at_fmt,
            })

            if pd.notna(nomina_installment_number) and pd.notna(number):
                if int(number) == int(nomina_installment_number):
                    picked_number = int(number)
                    picked_amount = float(amount) if pd.notna(amount) else None
                    picked_due_date = due_date

        rows.append({
            "invoice_key": _to_str(invoice_key),
            "nominaInstallmentNumber": int(nomina_installment_number) if pd.notna(nomina_installment_number) else None,
            "installment_number": picked_number,
            "installment_amount": picked_amount,
            "installment_due_date": picked_due_date,
            "installmentCount": int(row["installmentCount"]) if pd.notna(row["installmentCount"]) else None,
            "Installments": json.dumps(installments_json, ensure_ascii=False) if installments_json else "No tiene",
        })

    return pd.DataFrame(rows)