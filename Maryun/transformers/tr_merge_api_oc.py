import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo

if 'transformer' not in globals():
    from mage_ai.data_preparation.decorators import transformer
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test


@transformer
def transform(data, data_2, *args, **kwargs):
    """
    Arma payloads para API de Creación OC.

    Output:
      DF agrupado por (destino, rut_proveedor) con columnas:
        - cabecera (dict)
        - detalle (list[dict])
        - proveedor (str)
    """
    EMPTY_OUT = pd.DataFrame(columns=['cabecera', 'detalle', 'proveedor'])

    if data is None or data.empty:
        return EMPTY_OUT

    if data_2 is None or data_2.empty:
        raise ValueError("data_2 (maestro de proveedores) viene vacío; no se puede armar OC.")

    df = data.copy()

    # === SOLO filas que correspondan a "generar OC" ===
    df = df[df['accion'].astype('string').str.contains('generar', case=False, na=False)].copy()
    if df.empty:
        print("termina en accion")
        return EMPTY_OUT

    # === comentario con fecha/hora Chile ===
    chile_now = datetime.now(ZoneInfo("America/Santiago"))
    comentario = chile_now.strftime("%Y-%m-%d %H:%M:%S")
    df['comentario'] = comentario

    # === Preparar llaves y normalizar tipos ===
    df['destino'] = df['sucursal_destino'].astype(str).str.strip()
    df['sku'] = df['sku_original'].astype('string').fillna('').str.strip()
    df['cantidad'] = pd.to_numeric(df['cantidad'], errors='coerce').fillna(0)

    # Filtrar cantidades inválidas (API no permite <= 0)
    df = df[df['cantidad'] > 0].copy()
    if df.empty:
        print("termina en cantidad")
        return EMPTY_OUT

    # Filtrar destinos inválidos
    df = df[df['destino'].notna()].copy()
    if df.empty:
        print("termina en destino")
        return EMPTY_OUT

    # === Maestro proveedor por SKU ===
    prov = data_2.copy()
    required_cols = {'sku', 'rut_proveedor', 'costo'}
    if not required_cols.issubset(set(prov.columns)):
        raise ValueError("data_2 debe traer columnas: sku, rut_proveedor, costo (y opcional proveedor).")

    prov['sku'] = prov['sku'].astype('string').fillna('').str.strip()
    prov['rut_proveedor'] = prov['rut_proveedor'].astype('string').fillna('').str.strip()
    prov['proveedor'] = prov['proveedor'].astype('string').fillna('') if 'proveedor' in prov.columns else ''
    prov['precio'] = pd.to_numeric(prov['costo'], errors='coerce')

    prov = (
        prov[['sku', 'proveedor', 'rut_proveedor', 'precio']]
        .drop_duplicates(subset=['sku'], keep='last')
    )

    # === Join para obtener rut_proveedor y precio ===
    df = df.merge(prov, on='sku', how='left')

    # Eliminar filas sin rut_proveedor o sin precio (API exige rut válido y precio)
    df = df[(df['rut_proveedor'].notna()) & (df['rut_proveedor'].astype('string').str.strip() != '')].copy()
    df = df[df['precio'].notna()].copy()
    if df.empty:
        print("termina en precio")
        return EMPTY_OUT

    # === Consolidar SKUs repetidos dentro del mismo destino/proveedor ===
    df = (
        df.groupby(['destino', 'rut_proveedor', 'proveedor', 'sku', 'precio', 'comentario'], as_index=False)
          .agg({'cantidad': 'sum'})
    )

    # === Parámetros cabecera (configurables por kwargs) ===
    usuario = int(kwargs.get('usuario', 73))
    confirmar = bool(kwargs.get('confirmar', False))
    autorizar = bool(kwargs.get('autorizar', False))

    # === Agrupar a nivel OC: (destino, rut_proveedor) ===
    grouped = (
        df.groupby(['destino', 'rut_proveedor', 'proveedor', 'comentario'], as_index=False)
          .agg({
              'sku': list,
              'cantidad': list,
              'precio': list,
          })
    )

    # grouped = grouped.head(1)

    if grouped.empty:
        print("termina en grouped")
        return EMPTY_OUT

    # === Construir detalle y cabecera ===
    detalles = []
    cabeceras = []

    for _, row in grouped.iterrows():
        detalles.append([
            {'sku': str(s), 'cantidad': float(q), 'precio': float(p)}
            for s, q, p in zip(row['sku'], row['cantidad'], row['precio'])
        ])

        cabeceras.append({
            'rut_proveedor': str(row['rut_proveedor']),
            'destino': str(row['destino']),
            'comentario': str(row['comentario']),
            'usuario': usuario,
            'confirmar': confirmar,
            'autorizar': autorizar,
        })

    grouped['detalle'] = detalles
    grouped['cabecera'] = cabeceras

    return grouped[['cabecera', 'detalle', 'proveedor']].copy()


@test
def test_output(output, *args) -> None:
    assert output is not None, 'The output is undefined'
