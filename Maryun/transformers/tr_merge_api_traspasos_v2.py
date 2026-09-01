import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo   # correcto para Python 3.9+

if 'transformer' not in globals():
    from mage_ai.data_preparation.decorators import transformer
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test

# Maximo de items (lineas SKU) por traspaso. Si un (origen, destino) supera esto,
# se parte en varios traspasos de hasta 20 lineas.
MAX_ITEMS_POR_DOC = 20


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

    # Partir en traspasos de hasta MAX_ITEMS_POR_DOC lineas (1 doc por chunk).
    records = []
    for _, row in grouped.iterrows():
        skus = list(row['sku_original'])
        accs = list(row['accion'])
        cants = list(row['cantidad'])
        for i in range(0, len(skus), MAX_ITEMS_POR_DOC):
            cs, ca, cc = skus[i:i + MAX_ITEMS_POR_DOC], accs[i:i + MAX_ITEMS_POR_DOC], cants[i:i + MAX_ITEMS_POR_DOC]
            records.append({
                'sucursal_origen': row['sucursal_origen'],
                'sucursal_destino': row['sucursal_destino'],
                'sku_original': cs,
                'accion': ca,
                'cantidad': cc,
                'comentario': row['comentario'],
                'detalle': [
                    {'sku': s, 'accion': a, 'cantidad': c}
                    for s, a, c in zip(cs, ca, cc)
                ],
            })

    return pd.DataFrame(records, columns=[
        'sucursal_origen', 'sucursal_destino', 'sku_original',
        'accion', 'cantidad', 'comentario', 'detalle',
    ])


@test
def test_output(output, *args) -> None:
    assert output is not None, 'The output is undefined'
