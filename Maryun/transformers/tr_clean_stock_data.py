import pandas as pd

if 'transformer' not in globals():
    from mage_ai.data_preparation.decorators import transformer
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test


# Mapeo sucursal -> bodega_id (como lo enviaste)
SUCURSAL_POR_BODEGA_ID = {
    "SANTIAGO": 1,
    "PUERTO MONTT": 2,
    "CONCEPCION": 3,
    "QUELLON": 4,
    "OSORNO": 5,
    "LOS ANGELES": 6,
    "CASTRO": 7,
    "PUERTO VARAS": 8,
    "CARDONAL": 9,
    "ADMINISTRACION": 10,
    "PENDIENTES": 11,
    "CD SUR": 12,
    "CD SANTIAGO": 13,
    "MUESTRA SIN RETORNO": 14,
    "DISTRIBUCION TOTAL": 15,
    "ZONA SUR TOTAL": 16,
    "ZONA SUR AUSTRAL": 17,
    "ISLA CHILOE": 18,
    "ZONA BIO BIO": 19,
    "PROVINCIA LLANQUIHUE": 20,
    "INVENTARIO STGO": 21,
    "LOS ANGELES EXPRESS": 22,
    "CONSUMOS INTERNOS": 23,
    "BORDADOS": 24,
    "VALDIVIA": 25,
    "MARKETPLACE": 26,
}

# Invertimos para mapear bodega_id -> sucursal (nombre)
BODEGA_ID_A_SUCURSAL = {v: k for k, v in SUCURSAL_POR_BODEGA_ID.items()}


@transformer
def transform(data, *args, **kwargs):
    """
    Input columns esperadas:
      - sku
      - qty
      - bodega_id

    Output:
      - bodega_id
      - sucursal (nombre, según bodega_id)
      - sku
      - qty (sumada por sku dentro de la misma bodega_id)
    """
    df = data.copy()

    required = {"sku", "qty", "bodega_id"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Faltan columnas requeridas: {sorted(list(missing))}")

    # Normaliza tipos
    df["bodega_id"] = pd.to_numeric(df["bodega_id"], errors="coerce").astype("Int64")
    df["qty"] = pd.to_numeric(df["qty"], errors="coerce").fillna(0)

    # Sumar qty por (bodega_id, sku)
    out = (
        df.groupby(["bodega_id", "sku"], as_index=False)["qty"]
          .sum()
    )

    # Agregar sucursal (nombre) según bodega_id
    out["sucursal"] = out["bodega_id"].map(BODEGA_ID_A_SUCURSAL)

    # Si quieres marcar los no mapeados explícitamente:
    out["sucursal"] = out["sucursal"].fillna("NO_MAPEADO")

    # Reorden de columnas
    out = out[["bodega_id", "sucursal", "sku", "qty"]]

    return out


@test
def test_output(output, *args) -> None:
    assert output is not None, "El output es None"
    for col in ["bodega_id", "sucursal", "sku", "qty"]:
        assert col in output.columns, f"Falta columna {col}"