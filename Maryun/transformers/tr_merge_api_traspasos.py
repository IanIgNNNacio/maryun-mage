import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo   # correcto para Python 3.9+

if 'transformer' not in globals():
    from mage_ai.data_preparation.decorators import transformer
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test


@transformer
def transform(data, *args, **kwargs):
    df = data.copy()

    df = df[~df['accion'].str.contains('generar', case=False, na=False)]

    # === Nueva columna con fecha/hora actual de Chile ===
    chile_now = datetime.now(ZoneInfo("America/Santiago"))
    df['comentario'] = chile_now.strftime("%Y-%m-%d %H:%M:%S")

    # Reemplazar 'despachar' → 'transferir'
    df['accion'] = df['accion'].str.replace(
        r'^despachar',
        'transferir',
        case=False,
        regex=True
    )

    # Asegurar orden estable
    df = df.reset_index().rename(columns={'index': '_orden'})
    df = df.sort_values(['sucursal_origen', 'sucursal_destino', '_orden'])

    # Agrupar por origen/destino
    grouped = (
        df
        .groupby(['sucursal_origen', 'sucursal_destino'], as_index=False)
        .agg({
            'sku_original': list,
            'accion': list,
            'cantidad': list,
            'comentario': 'first',   # mismo valor para todas las filas del grupo
        })
    )

    # Crear detalle estructurado
    grouped['detalle'] = grouped.apply(
        lambda row: [
            {
                'sku': s,
                'accion': a,
                'cantidad': c,
            }
            for s, a, c in zip(row['sku_original'], row['accion'], row['cantidad'])
        ],
        axis=1
    )

    return grouped


@test
def test_output(output, *args) -> None:
    assert output is not None, 'The output is undefined'