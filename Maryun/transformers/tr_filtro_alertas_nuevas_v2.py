import pandas as pd
from datetime import datetime, timezone

if 'transformer' not in globals():
    from mage_ai.data_preparation.decorators import transformer
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test


@transformer
def transform(data, data_2, *args, **kwargs):
    """
    Recibe 2 dataframes:
      data   -> alertas normalizadas (con hash_clave)
      data_2 -> alertas_silencio (hash_clave, valida_hasta, estado)

    Devuelve sólo las alertas nuevas (no silenciadas).
    """

    df_alertas = data_2.copy()
    df_silencios = data.copy()

    # --- Validaciones básicas ---
    if "hash_clave" not in df_alertas.columns:
        raise ValueError("df_alertas no contiene 'hash_clave'.")

    if df_silencios is None or df_silencios.empty:
        # No hay silencios -> todas son nuevas
        return df_alertas

    # --- Normalizar estado ---
    if "estado" in df_silencios.columns:
        df_silencios["estado"] = (
            df_silencios["estado"]
            .astype(str)
            .str.lower()
            .str.strip()
        )
    else:
        df_silencios["estado"] = "activa"

    # --- Validar fecha ---
    if "valida_hasta" in df_silencios.columns:
        df_silencios["valida_hasta"] = pd.to_datetime(
            df_silencios["valida_hasta"], errors="coerce"
        )
    else:
        df_silencios["valida_hasta"] = datetime.now(timezone.utc)

    now_utc = datetime.now(timezone.utc)

    # --- Filtrar solo silencios vigentes ---
    df_silencios_vigentes = df_silencios[
        (df_silencios["estado"] == "activa")
        & (df_silencios["valida_hasta"] >= now_utc)
    ]

    # --- Set de hashes silenciados ---
    hashes_silenciados = set(
        df_silencios_vigentes["hash_clave"]
        .dropna()
        .astype(str)
        .unique()
    )

    # --- Filtrar alertas nuevas ---
    df_alertas["hash_clave"] = df_alertas["hash_clave"].astype(str)

    mask_nueva = ~df_alertas["hash_clave"].isin(hashes_silenciados)
    df_alertas_nuevas = df_alertas[mask_nueva].copy()

    return df_alertas_nuevas

    # return data


@test
def test_output(output, *args) -> None:
    """
    Template code for testing the output of the block.
    """
    assert output is not None, 'The output is undefined'
