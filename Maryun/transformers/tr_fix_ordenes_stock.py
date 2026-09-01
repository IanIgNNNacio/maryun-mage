import pandas as pd

if 'transformer' not in globals():
    from mage_ai.data_preparation.decorators import transformer
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test


@transformer
def transform(data_2, data, *args, **kwargs):

    df = data.copy()
    stock = data_2.copy()

    # -----------------------------
    # 0) Validaciones mínimas
    # -----------------------------
    required_data_cols = {"cantidad", "accion_normalizada", "accion", "sku2", "sucursal_destino"}
    missing = required_data_cols - set(df.columns)
    if missing:
        raise ValueError(f"`data` no contiene columnas requeridas: {sorted(list(missing))}")

    # Para poder hacer la lógica pedida necesitamos sku_original y sucursal_origen.
    if "sku_original" not in df.columns or "sucursal_origen" not in df.columns:
        # Limpieza solicitada: eliminar flags si existieran
        for c in ["tiene_inmovilizado", "tiene_sobrestock"]:
            if c in df.columns:
                df = df.drop(columns=[c])
        return df

    # -----------------------------
    # 1) Normalizar tipos en data
    # -----------------------------
    df["cantidad"] = pd.to_numeric(df["cantidad"], errors="coerce").fillna(0).round().astype(int)

    # NUEVO: eliminar filas cuyo redondeo da 0
    df = df[df["cantidad"] != 0].copy()
    if df.empty:
        for c in ["tiene_inmovilizado", "tiene_sobrestock"]:
            if c in df.columns:
                df = df.drop(columns=[c])
        return df

    # Normalizar textos clave para joins/agrupaciones
    for col in ["sku_original", "sucursal_origen", "accion_normalizada", "accion", "sku2", "sucursal_destino"]:
        df[col] = df[col].astype(str).fillna("").str.strip()

# -----------------------------
# 1.1) Reemplazar SOLO la palabra "despachar" -> "transferir" en accion
# -----------------------------
# Reemplazo case-insensitive, respetando palabra completa (no "despachador", etc.)
    accion_before = df["accion"].copy()

    df["accion"] = (
        df["accion"]
        .str.replace(r"\bdespachar\b", "transferir", case=False, regex=True)
    )

    # Si cambió por ese reemplazo, mantenemos consistencia en accion_normalizada
    mask_changed = accion_before.ne(df["accion"])
    df.loc[mask_changed, "accion_normalizada"] = (
        df.loc[mask_changed, "accion_normalizada"]
        .where(~df.loc[mask_changed, "accion_normalizada"].str.contains(r"\bdespachar\b", case=False, na=False),
                df.loc[mask_changed, "accion_normalizada"].str.replace(r"\bdespachar\b", "transferir", case=False, regex=True))
    )

    # -----------------------------
    # 2) Normalizar stock (data_2)
    # -----------------------------
    required_stock_cols = {"bodega_id", "sucursal", "sku", "qty"}
    missing_stock = required_stock_cols - set(stock.columns)
    if missing_stock:
        raise ValueError(f"`data_2` (stock) no contiene columnas requeridas: {sorted(list(missing_stock))}")

    stock["qty"] = pd.to_numeric(stock["qty"], errors="coerce").fillna(0).round().astype(int)
    stock["sucursal"] = stock["sucursal"].astype(str).fillna("").str.strip()
    stock["sku"] = stock["sku"].astype(str).fillna("").str.strip()

    # Creamos un lookup de stock: (sucursal, sku) -> qty
    stock_map = stock.groupby(["sucursal", "sku"], as_index=False)["qty"].sum()
    stock_map["key"] = (
        stock_map["sucursal"].str.lower().str.strip()
        + "|"
        + stock_map["sku"].str.lower().str.strip()
    )
    stock_lookup = dict(zip(stock_map["key"], stock_map["qty"]))

    def get_available_stock(sucursal_origen: str, sku_original: str) -> int:
        k = sucursal_origen.lower().strip() + "|" + sku_original.lower().strip()
        return int(stock_lookup.get(k, 0))

    # -----------------------------
    # 3) Preparar columnas auxiliares
    # -----------------------------
    df["_so_key"] = df["sucursal_origen"].str.lower().str.strip()
    df["_sku_key"] = df["sku_original"].str.lower().str.strip()
    df["_group_key"] = df["_so_key"] + "|" + df["_sku_key"]

    # -----------------------------
    # 4) Asignación de stock por grupo
    #    (sucursal_origen, sku_original)
    # -----------------------------
    out_rows = []

    for gk, gdf in df.groupby("_group_key", sort=False):
        so = gdf["sucursal_origen"].iloc[0]
        sku = gdf["sku_original"].iloc[0]
        available = get_available_stock(so, sku)

        # Priorizar mayor cantidad
        gdf_sorted = gdf.sort_values(by=["cantidad"], ascending=False)

        for _, row in gdf_sorted.iterrows():
            need = int(row["cantidad"])

            # Ya filtramos cantidad != 0; si fuese negativa, descartamos
            if need <= 0:
                continue

            # Caso 1: stock alcanza completo -> se mantiene
            if available >= need:
                available -= need
                out_rows.append(row.drop(labels=["_so_key", "_sku_key", "_group_key"]).to_dict())
                continue

            # Caso 2: stock parcial -> dividir en 2 filas
            if 0 < available < need:
                keep_qty = available
                oc_qty = need - available
                available = 0

                # (a) fila que se mantiene con lo que alcanza
                row_keep = row.copy()
                row_keep["cantidad"] = keep_qty
                out_rows.append(row_keep.drop(labels=["_so_key", "_sku_key", "_group_key"]).to_dict())

                # (b) fila que pasa a generar oc con el resto
                row_oc = row.copy()
                row_oc["cantidad"] = oc_qty
                row_oc["accion_normalizada"] = "generar oc"
                row_oc["accion"] = "generar oc"
                out_rows.append(row_oc.drop(labels=["_so_key", "_sku_key", "_group_key"]).to_dict())
                continue

            # Caso 3: no hay stock -> todo a generar oc
            row_oc = row.copy()
            row_oc["accion_normalizada"] = "generar oc"
            row_oc["accion"] = "generar oc"
            out_rows.append(row_oc.drop(labels=["_so_key", "_sku_key", "_group_key"]).to_dict())

    result = pd.DataFrame(out_rows)
    if result.empty:
        return result

    # -----------------------------
    # 5) Limpieza solicitada: quitar flags si existieran
    # -----------------------------
    for c in ["tiene_inmovilizado", "tiene_sobrestock"]:
        if c in result.columns:
            result = result.drop(columns=[c])

    # -----------------------------
    # 6) Reagrupar SOLO si acciones son iguales
    #    (evita mezclar transferir con generar oc)
    # -----------------------------
    group_cols = ["sku2", "sucursal_origen", "sucursal_destino", "accion_normalizada"]

    agg_dict = {
        "cantidad": "sum",
    }
    if "fecha_corte" in result.columns:
        agg_dict["fecha_corte"] = "max"
    if "sku_original" in result.columns:
        agg_dict["sku_original"] = "first"
    if "accion" in result.columns:
        # Si accion_normalizada es igual en el grupo, accion debería ser consistente;
        # tomamos la primera.
        agg_dict["accion"] = "first"

    result = result.groupby(group_cols, as_index=False).agg(agg_dict)

    # Eliminar por seguridad cantidades 0 (por si llegaran a aparecer)
    result = result[result["cantidad"] != 0].copy()

    # Asegurar coherencia final para OC
    if "accion" in result.columns:
        result.loc[result["accion_normalizada"].str.lower().str.strip().eq("generar oc"), "accion"] = "generar oc"

    # -----------------------------
    # 7) Recalcular hash_clave
    # -----------------------------
    result["hash_clave"] = (
        result["sku2"].astype(str).str.lower().str.strip()
        + "|"
        + result["sucursal_destino"].astype(str).str.lower().str.strip()
        + "|"
        + result["accion_normalizada"].astype(str).str.lower().str.strip()
    )

    return result


@test
def test_output(output, *args) -> None:
    assert output is not None, "The output is undefined"