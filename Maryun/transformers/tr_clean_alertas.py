import pandas as pd

if 'transformer' not in globals():
    from mage_ai.data_preparation.decorators import transformer
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test


@transformer
def transform(data, *args, **kwargs):
    """
    Limpia y normaliza los datos provenientes del loader de Power BI:
    - convierte tipos
    - normaliza strings
    - genera accion_normalizada
    - agrupa sobrestock/inmovilizado
    - genera hash_clave
    """
    # ➊ limpiar nombres de columnas
    data.columns = data.columns.str.replace(r'[\[\]]', '', regex=True)
    df = pd.DataFrame(data)

    # ➋ renombrar SUCURSAL_ORIGEN -> sucursal_origen (para usarla luego en la API)
    if 'SUCURSAL_ORIGEN' in df.columns:
        df = df.rename(columns={'SUCURSAL_ORIGEN': 'sucursal_origen'})

    #
    # 1) Convertir tipos
    #
    # cantidad → numérico
    df["cantidad"] = round(pd.to_numeric(df["cantidad"], errors="coerce")).fillna(0)

    # fecha_corte → datetime
    df["fecha_corte"] = pd.to_datetime(df["fecha_corte"], errors="coerce")

    #
    # 2) Normalizar strings base (solo para estas columnas de texto)
    #
    for col in ["sku2", "sucursal_destino", "accion"]:
        if col in df.columns:
            df[col] = (
                df[col]
                .astype(str)
                .fillna("")
                .str.strip()
            )
        else:
            df[col] = ""

    # ➌ NO toco aquí sku_original ni sucursal_origen; se usan tal cual más adelante

    # 3) accion_normalizada
    df["accion_normalizada"] = df["accion"].apply(normalizar_accion)

    # flags sobrestock / inmovilizado
    df["tiene_sobrestock"] = df["accion"].str.contains("sobrestock", case=False, na=False)
    df["tiene_inmovilizado"] = df["accion"].str.contains("inmovilizado", case=False, na=False)

    #
    # 5) Agrupar por sku2 + sucursal + accion_normalizada
    #    - sumar cantidad
    #    - tomar max(fecha_corte)
    #    - combinar flags de sobrestock/inmovilizado
    #    - mantener sucursal_origen / sku_original si existen
    #
    agg_dict = {
        "cantidad": "sum",
        "fecha_corte": "max",
        "tiene_sobrestock": "max",
        "tiene_inmovilizado": "max",
    }

    # ➍ NUEVO: mantener sucursal_origen si existe
    if "sucursal_origen" in df.columns:
        agg_dict["sucursal_origen"] = "first"

    # ➎ NUEVO: mantener sku_original si existe
    if "sku_original" in df.columns:
        agg_dict["sku_original"] = "first"

    grouped = (
        df.groupby(["sku2", "sucursal_destino", "accion_normalizada"], as_index=False)
          .agg(agg_dict)
    )

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

    #
    # 4) hash_clave
    #
    grouped["hash_clave"] = (
        grouped["sku2"].str.lower().str.strip()
        + "|"
        + grouped["sucursal_destino"].str.lower().str.strip()
        + "|"
        + grouped["accion_normalizada"].str.lower().str.strip()
    )

    return grouped


@test
def test_output(output, *args) -> None:
    """
    Template code for testing the output of the block.
    """
    assert output is not None, 'The output is undefined'


def normalizar_accion(accion: str) -> str:
    a = accion.lower().strip()
    a = a.replace(" (inmovilizado)", "")
    a = a.replace(" (sobrestock)", "")
    a = a.strip()
    return a