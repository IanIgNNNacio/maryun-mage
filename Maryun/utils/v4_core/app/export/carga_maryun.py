"""Build ``carga_maryun`` — the final, ERP-ready plan."""
from __future__ import annotations

import pandas as pd


CARGA_COLUMNS = [
    "run_id",
    "fecha_generacion",
    "sku_id",
    "nombre",              # nombre/modelo del producto (para validacion humana)
    "variante",            # variante: color + talla (ej. "(AZUL)(M)")
    "homologado_desde_sku",
    "origen",
    "destino",
    "capa",
    "cantidad",
    "score",
    "motivo",
    "lead_time_dias",
    "proveedor",
    "costo_unitario_clp",
    "ahorro_clp",
    "clase_abc_xyz",
    "fuente_autom",
]

# Capas que evitan una compra externa — cada unidad trasladada es ahorro.
# Compra = cero ahorro por definición.
_CAPAS_AHORRO = {"inmovilizado", "sobrestock", "cd"}


def build_carga_maryun(
    plan: pd.DataFrame,
    *,
    run_id: str,
    fecha_generacion: pd.Timestamp | str,
    suppliers_df: pd.DataFrame | None = None,
    automation_source_by_pair: dict[tuple[str, str], str] | None = None,
    classification_df: pd.DataFrame | None = None,
    products: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Flatten the plan to the carga_maryun layout, joining supplier metadata."""
    if plan is None or plan.empty:
        return pd.DataFrame(columns=CARGA_COLUMNS)

    out = plan.copy()
    out["run_id"] = str(run_id)
    out["fecha_generacion"] = pd.to_datetime(fecha_generacion)

    # Nombre + variante (color/talla) desde el maestro de productos, por sku_id.
    # Facilita la validacion humana de la orden de compra.
    if products is not None and not products.empty:
        pcols = ["sku_id"]
        for c in ("nombre", "variante"):
            if c in products.columns:
                pcols.append(c)
        p = products[pcols].drop_duplicates("sku_id").copy()
        p["sku_id"] = p["sku_id"].astype(str)
        out["sku_id"] = out["sku_id"].astype(str)
        out = out.merge(p, on="sku_id", how="left")
    for c in ("nombre", "variante"):
        if c not in out.columns:
            out[c] = ""
        out[c] = out[c].fillna("").astype(str)

    # Supplier join — only meaningful for capa == "compra".
    # suppliers_df may have multiple rows per sku_id (one per ubicacion + one global).
    # We resolve: (sku_id, destino) → specific lead time first, then global fallback,
    # to avoid a cartesian-product explosion in the merge.
    if suppliers_df is not None and not suppliers_df.empty:
        s = suppliers_df[["sku_id", "ubicacion", "proveedor", "lead_time_dias", "costo_unitario_clp", "prioridad"]].copy()
        s = s.sort_values("prioridad", ascending=True)  # prioridad 1 = principal

        # Split: rows with a specific location vs. global (ubicacion is NaN/None)
        has_loc = s["ubicacion"].notna()
        s_specific = s[has_loc].copy()
        s_global = s[~has_loc].drop_duplicates("sku_id")  # one global per SKU

        # For each (sku_id, destino) in the plan, try to match a location-specific row
        # then fall back to the global row.
        if "destino" in out.columns and not s_specific.empty:
            s_specific_dedup = s_specific.rename(columns={"ubicacion": "destino"}).drop_duplicates(["sku_id", "destino"])
            out = out.merge(
                s_specific_dedup[["sku_id", "destino", "proveedor", "lead_time_dias", "costo_unitario_clp"]],
                on=["sku_id", "destino"], how="left", suffixes=("", "_spec"),
            )
            # Fill any unmatched rows from the global fallback
            missing_mask = out["proveedor"].isna() | (out["proveedor"] == "")
            if missing_mask.any():
                out = out.merge(
                    s_global[["sku_id", "proveedor", "lead_time_dias", "costo_unitario_clp"]],
                    on="sku_id", how="left", suffixes=("", "_glb"),
                )
                for col in ("proveedor", "lead_time_dias", "costo_unitario_clp"):
                    glb_col = f"{col}_glb"
                    if glb_col in out.columns:
                        out[col] = out[col].where(~missing_mask, out[glb_col])
                        out = out.drop(columns=[glb_col])
        else:
            # No location-specific rows — just use global, deduplicated by sku_id
            s_dedup = s_global[["sku_id", "proveedor", "lead_time_dias", "costo_unitario_clp"]]
            out = out.merge(s_dedup, on="sku_id", how="left", suffixes=("", "_sup"))
    for col, default in (
        ("lead_time_dias", 0),
        ("proveedor", ""),
        ("costo_unitario_clp", 0.0),
    ):
        if col not in out.columns:
            out[col] = default
        else:
            out[col] = out[col].fillna(default)

    # Automation source — per (sku, ubicacion=destino) from filter/v3 audit.
    automation_source_by_pair = automation_source_by_pair or {}
    out["fuente_autom"] = [
        automation_source_by_pair.get((s, u), "default")
        for s, u in zip(out["sku_id"], out["destino"])
    ]

    if "homologado_desde_sku" not in out.columns:
        out["homologado_desde_sku"] = None

    # Clasificación ABC/XYZ: se asigna según el destino (donde se consume).
    # Si no hay clasificación disponible para (sku, destino), queda "N/A".
    if classification_df is not None and not classification_df.empty and "clase_final" in classification_df.columns:
        cls = classification_df[["sku_id", "ubicacion", "clase_final"]].rename(
            columns={"ubicacion": "destino", "clase_final": "clase_abc_xyz"},
        ).drop_duplicates(["sku_id", "destino"])
        out = out.merge(cls, on=["sku_id", "destino"], how="left")
        out["clase_abc_xyz"] = out["clase_abc_xyz"].fillna("N/A").astype(str)
    else:
        out["clase_abc_xyz"] = "N/A"

    # Ahorro por fila: cantidad × costo_unitario_clp para todas las capas que
    # evitan una compra nueva (inmovilizado, sobrestock, cd). Las filas con
    # capa="compra" generan 0 ahorro — es la baseline contra la que comparamos.
    out["ahorro_clp"] = (
        out["cantidad"].astype(float) * out["costo_unitario_clp"].astype(float)
    ).where(out["capa"].isin(_CAPAS_AHORRO), 0.0).round(0)

    # Stable ordering; keep only canonical columns in canonical order.
    return out[CARGA_COLUMNS].sort_values(
        ["capa", "destino", "sku_id", "origen"],
        kind="mergesort",
    ).reset_index(drop=True)
