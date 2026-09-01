"""Vista plana lista para Power BI — fuente fija que no cambia entre runs.

Cada vez que el pipeline corre, se sobrescribe el archivo en
``data/output/powerbi/plan_actual.xlsx`` (y un CSV gemelo) con una fila por
``(sku_id, sucursal)`` y todas las dimensiones planas que el dashboard
necesita: NOMBRE, FAMILIA, MARCA, PROVEEDOR, STOCK, CANT_REQ, CD_STGO, CD_SUR,
CLASE, COBERTURA_MESES, etc.

Power BI conecta a ``data/output/powerbi/plan_actual.xlsx`` UNA sola vez —
la ruta nunca cambia.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.utils.logging import get_logger


# Nombres canónicos de los CDs (deben coincidir con canonical_location).
CD_SANTIAGO = "CD SANTIAGO"
CD_SUR = "CD SUR"


# Columnas en el orden EXACTO en que las queremos en el Excel para que
# Power BI las reconozca de inmediato (espejo de la pantalla compartida).
POWERBI_COLUMNS = [
    "SUCURSAL",
    "NOMBRE",
    "MARCA",
    "FAMILIA",
    "TIPO",
    "DESCRIPCION",
    "SKU",
    "SKU_CORRECTO",
    "CLASE_ABC_XYZ",
    "STOCK",
    "DEMANDA_MES_ACTUAL",
    "CANT_REQ",
    "CD_STGO",
    "CD_SUR",
    "PROVEEDOR",
    "COSTO_UNITARIO_CLP",
    "VALOR_STOCK_CLP",
    "COBERTURA_MESES",
    "DESCUBIERTO",
    "RUN_ID",
    "FECHA_GENERACION",
]


def _stock_cd_pivot(stock: pd.DataFrame) -> pd.DataFrame:
    """Devuelve un DataFrame con ``sku_id, CD_STGO, CD_SUR`` (cantidades en
    los CDs)."""
    if stock is None or stock.empty:
        return pd.DataFrame(columns=["sku_id", "CD_STGO", "CD_SUR"])

    s = stock.copy()
    s["sku_id"] = s["sku_id"].astype(str)
    s["ubicacion"] = s["ubicacion"].astype(str).str.upper().str.strip()
    only_cds = s[s["ubicacion"].isin([CD_SANTIAGO, CD_SUR])]
    if only_cds.empty:
        return pd.DataFrame(columns=["sku_id", "CD_STGO", "CD_SUR"])

    pivot = (
        only_cds.groupby(["sku_id", "ubicacion"], as_index=False)["qty"].sum()
        .pivot(index="sku_id", columns="ubicacion", values="qty")
        .fillna(0.0)
        .reset_index()
    )
    # Asegurar las dos columnas aunque un CD esté vacío.
    if CD_SANTIAGO not in pivot.columns:
        pivot[CD_SANTIAGO] = 0.0
    if CD_SUR not in pivot.columns:
        pivot[CD_SUR] = 0.0
    pivot = pivot.rename(columns={CD_SANTIAGO: "CD_STGO", CD_SUR: "CD_SUR"})
    return pivot[["sku_id", "CD_STGO", "CD_SUR"]]


def _proveedor_principal(suppliers_df: pd.DataFrame) -> pd.DataFrame:
    """Devuelve un mapa SKU → proveedor con prioridad 1 (principal)."""
    if suppliers_df is None or suppliers_df.empty:
        return pd.DataFrame(columns=["sku_id", "PROVEEDOR", "COSTO_UNITARIO_CLP"])

    s = suppliers_df.copy()
    s["sku_id"] = s["sku_id"].astype(str)
    s = s.sort_values("prioridad", ascending=True)
    s = s.drop_duplicates("sku_id", keep="first")
    return s[["sku_id", "proveedor", "costo_unitario_clp"]].rename(
        columns={"proveedor": "PROVEEDOR", "costo_unitario_clp": "COSTO_UNITARIO_CLP"}
    )


def build_powerbi_view(
    *,
    needs: pd.DataFrame,
    plan: pd.DataFrame,
    stock: pd.DataFrame,
    products: pd.DataFrame,
    forecast: pd.DataFrame,
    classification: pd.DataFrame,
    suppliers_df: pd.DataFrame,
    run_id: str,
    fecha_generacion: pd.Timestamp,
) -> pd.DataFrame:
    """Arma la tabla plana lista para Power BI.

    Una fila por ``(sku_id, sucursal)`` con todas las dimensiones planas.
    """
    if needs is None or needs.empty:
        return pd.DataFrame(columns=POWERBI_COLUMNS)

    base = needs[["sku_id", "ubicacion", "stock_actual",
                  "cobertura_actual_meses"]].copy()
    base["sku_id"] = base["sku_id"].astype(str)
    base["ubicacion"] = base["ubicacion"].astype(str)

    # CANT_REQ: lo que sugiere el plan agrupado por (sku, destino).
    if plan is not None and not plan.empty:
        cant_req = (
            plan.assign(sku_id=plan["sku_id"].astype(str))
            .groupby(["sku_id", "destino"], as_index=False)["cantidad"]
            .sum()
            .rename(columns={"destino": "ubicacion", "cantidad": "CANT_REQ"})
        )
        cant_req["CANT_REQ"] = cant_req["CANT_REQ"].round(0).astype(int)
        base = base.merge(cant_req, on=["sku_id", "ubicacion"], how="left")
    else:
        base["CANT_REQ"] = 0
    base["CANT_REQ"] = base["CANT_REQ"].fillna(0).astype(int)

    # Forecast del mes actual (primer mes en forecast_final).
    if forecast is not None and not forecast.empty:
        f = forecast.copy()
        f["sku_id"] = f["sku_id"].astype(str)
        f["mes"] = pd.to_datetime(f["mes"])
        mes_min = f["mes"].min()
        f_now = (
            f[f["mes"] == mes_min][["sku_id", "ubicacion", "forecast_final"]]
            .rename(columns={"forecast_final": "DEMANDA_MES_ACTUAL"})
        )
        base = base.merge(f_now, on=["sku_id", "ubicacion"], how="left")
    else:
        base["DEMANDA_MES_ACTUAL"] = 0.0
    base["DEMANDA_MES_ACTUAL"] = base["DEMANDA_MES_ACTUAL"].fillna(0.0).round(0)

    # Stock en CDs (Santiago + Sur).
    cd_pivot = _stock_cd_pivot(stock)
    base = base.merge(cd_pivot, on="sku_id", how="left")
    base["CD_STGO"] = base["CD_STGO"].fillna(0).round(0).astype(int)
    base["CD_SUR"] = base["CD_SUR"].fillna(0).round(0).astype(int)

    # Productos: NOMBRE, MARCA, FAMILIA, TIPO.
    if products is not None and not products.empty:
        p = products[["sku_id", "nombre", "marca", "familia", "tipo"]].copy()
        p["sku_id"] = p["sku_id"].astype(str)
        base = base.merge(p, on="sku_id", how="left")
    for col in ("nombre", "marca", "familia", "tipo"):
        if col not in base.columns:
            base[col] = ""
        base[col] = base[col].fillna("").astype(str)

    # Clasificación ABC/XYZ.
    if classification is not None and not classification.empty:
        c = classification[["sku_id", "ubicacion", "clase_final"]].copy()
        c["sku_id"] = c["sku_id"].astype(str)
        base = base.merge(c, on=["sku_id", "ubicacion"], how="left")
        base["clase_final"] = base["clase_final"].fillna("N/A").astype(str)
    else:
        base["clase_final"] = "N/A"

    # Proveedor principal + costo unitario.
    prov = _proveedor_principal(suppliers_df)
    base = base.merge(prov, on="sku_id", how="left")
    base["PROVEEDOR"] = base["PROVEEDOR"].fillna("SIN_PROVEEDOR").astype(str)
    base["COSTO_UNITARIO_CLP"] = (
        pd.to_numeric(base["COSTO_UNITARIO_CLP"], errors="coerce").fillna(0.0)
    )

    # Métricas derivadas.
    base["VALOR_STOCK_CLP"] = (
        base["stock_actual"] * base["COSTO_UNITARIO_CLP"]
    ).round(0)
    base["DESCUBIERTO"] = (
        base["DEMANDA_MES_ACTUAL"] - base["stock_actual"] - base["CANT_REQ"]
    ).clip(lower=0).round(0).astype(int)

    # Renombrado a columnas finales en mayúsculas (PowerBI-friendly).
    out = pd.DataFrame({
        "SUCURSAL":            base["ubicacion"],
        "NOMBRE":              base["nombre"],
        "MARCA":               base["marca"],
        "FAMILIA":             base["familia"],
        "TIPO":                base["tipo"],
        # Descripcion = familia + tipo (concatenado), o '-' si vacío.
        "DESCRIPCION":         (base["familia"] + " — " + base["tipo"]).str.strip(" —"),
        "SKU":                 base["sku_id"],
        # SKU_CORRECTO con leading zeros a 12 caracteres (estándar Maryun).
        "SKU_CORRECTO":        base["sku_id"].astype(str).str.zfill(12),
        "CLASE_ABC_XYZ":       base["clase_final"],
        "STOCK":               base["stock_actual"].round(0).astype(int),
        "DEMANDA_MES_ACTUAL":  base["DEMANDA_MES_ACTUAL"].astype(int),
        "CANT_REQ":            base["CANT_REQ"].astype(int),
        "CD_STGO":             base["CD_STGO"].astype(int),
        "CD_SUR":              base["CD_SUR"].astype(int),
        "PROVEEDOR":           base["PROVEEDOR"],
        "COSTO_UNITARIO_CLP":  base["COSTO_UNITARIO_CLP"].round(0).astype(int),
        "VALOR_STOCK_CLP":     base["VALOR_STOCK_CLP"].astype(int),
        "COBERTURA_MESES":     base["cobertura_actual_meses"].round(2),
        "DESCUBIERTO":         base["DESCUBIERTO"],
        "RUN_ID":              str(run_id),
        "FECHA_GENERACION":    pd.to_datetime(fecha_generacion).strftime("%Y-%m-%d"),
    })

    return out[POWERBI_COLUMNS].sort_values(
        ["NOMBRE", "SUCURSAL"], kind="mergesort"
    ).reset_index(drop=True)


def write_powerbi_view(
    df: pd.DataFrame,
    output_dir: Path,
) -> dict[str, Path]:
    """Escribe la tabla plana en una ruta FIJA: ``<output_dir>/powerbi/``.

    Genera dos archivos espejo:
      * ``plan_actual.xlsx``  — formato preferido por Power BI Desktop
      * ``plan_actual.csv``   — backup / Power BI Service via OneDrive

    Devuelve los paths escritos.
    """
    log = get_logger("export.powerbi_view")
    target_dir = Path(output_dir) / "powerbi"
    target_dir.mkdir(parents=True, exist_ok=True)

    xlsx_path = target_dir / "plan_actual.xlsx"
    csv_path = target_dir / "plan_actual.csv"

    df.to_excel(xlsx_path, index=False, sheet_name="plan_actual")
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")  # BOM → Excel-friendly

    log.info(
        "powerbi.view.written",
        xlsx=str(xlsx_path),
        csv=str(csv_path),
        rows=len(df),
    )
    return {"xlsx": xlsx_path, "csv": csv_path}
