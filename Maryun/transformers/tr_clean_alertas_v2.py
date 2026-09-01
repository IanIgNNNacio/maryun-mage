import pandas as pd

if 'transformer' not in globals():
    from mage_ai.data_preparation.decorators import transformer
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test


@transformer
def transform(data, *args, **kwargs):
    """
    Limpia/normaliza el plan proveniente de dl_salida_carga_maryun_v2:
    - convierte tipos
    - normaliza strings
    - genera accion_normalizada
    - agrupa sobrestock/inmovilizado
    - genera hash_clave (incluye run_id + sucursal_origen)

    Diferencias vs my_sis_stock (v2):
    - El groupby incluye sucursal_origen: NO colapsa traspasos de origenes
      distintos (cd vs sobrestock para el mismo sku/destino).
    - Arrastra run_id (constante de la run) para que el silencio quede scoped
      por run_id en tr_fix_ordenes_stock_v2.
    """
    # limpiar nombres de columnas
    data.columns = data.columns.str.replace(r'[\[\]]', '', regex=True)
    df = pd.DataFrame(data)

    if 'SUCURSAL_ORIGEN' in df.columns:
        df = df.rename(columns={'SUCURSAL_ORIGEN': 'sucursal_origen'})

    # 1) tipos
    df["cantidad"] = round(pd.to_numeric(df["cantidad"], errors="coerce")).fillna(0)
    df["fecha_corte"] = pd.to_datetime(df["fecha_corte"], errors="coerce")

    # 2) strings base
    for col in ["sku2", "sucursal_destino", "accion"]:
        if col in df.columns:
            df[col] = df[col].astype(str).fillna("").str.strip()
        else:
            df[col] = ""

    # 3) accion_normalizada + flags
    df["accion_normalizada"] = df["accion"].apply(normalizar_accion)
    df["tiene_sobrestock"] = df["accion"].str.contains("sobrestock", case=False, na=False)
    df["tiene_inmovilizado"] = df["accion"].str.contains("inmovilizado", case=False, na=False)

    # 5) Agrupar por sku2 + sucursal_destino + sucursal_origen + accion_normalizada
    group_cols = ["sku2", "sucursal_destino", "accion_normalizada"]
    if "sucursal_origen" in df.columns:
        group_cols.insert(2, "sucursal_origen")  # mantener origenes distintos separados

    agg_dict = {
        "cantidad": "sum",
        "fecha_corte": "max",
        "tiene_sobrestock": "max",
        "tiene_inmovilizado": "max",
    }
    if "sku_original" in df.columns:
        agg_dict["sku_original"] = "first"
    if "run_id" in df.columns:
        agg_dict["run_id"] = "first"
    # rut_proveedor + costo_unitario_clp vienen de la carga V4 (para OC en mysis)
    if "rut_proveedor" in df.columns:
        agg_dict["rut_proveedor"] = "first"
    if "costo_unitario_clp" in df.columns:
        agg_dict["costo_unitario_clp"] = "first"

    grouped = df.groupby(group_cols, as_index=False).agg(agg_dict)

    # reconstruir accion combinando sufijos
    def build_accion(row):
        base = row["accion_normalizada"]
        has_sobre = bool(row["tiene_sobrestock"])
        has_inmo = bool(row["tiene_inmovilizado"])
        if has_sobre and has_inmo:
            return f"{base} (sobrestock + inmovilizado)"
        elif has_sobre:
            return f"{base} (sobrestock)"
        elif has_inmo:
            return f"{base} (inmovilizado)"
        else:
            return base

    grouped["accion"] = grouped.apply(build_accion, axis=1)

    # 4) hash_clave (incluye run_id + sucursal_origen). tr_fix lo recalcula igual.
    parts = []
    if "run_id" in grouped.columns:
        parts.append(grouped["run_id"].astype(str).str.strip())
    parts.append(grouped["sku2"].astype(str).str.lower().str.strip())
    parts.append(grouped["sucursal_destino"].astype(str).str.lower().str.strip())
    if "sucursal_origen" in grouped.columns:
        parts.append(grouped["sucursal_origen"].astype(str).str.lower().str.strip())
    parts.append(grouped["accion_normalizada"].astype(str).str.lower().str.strip())

    hk = parts[0]
    for p in parts[1:]:
        hk = hk + "|" + p
    grouped["hash_clave"] = hk

    return grouped


@test
def test_output(output, *args) -> None:
    assert output is not None, 'The output is undefined'


def normalizar_accion(accion: str) -> str:
    a = accion.lower().strip()
    a = a.replace(" (inmovilizado)", "")
    a = a.replace(" (sobrestock)", "")
    a = a.strip()
    return a
