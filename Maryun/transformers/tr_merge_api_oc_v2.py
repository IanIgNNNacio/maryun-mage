import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo

if 'transformer' not in globals():
    from mage_ai.data_preparation.decorators import transformer
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test

# Maximo de items (lineas SKU) por OC. Si un (destino, proveedor) supera esto,
# se parte en varias OC de hasta 20 lineas.
MAX_ITEMS_POR_DOC = 20

# rut generico (catch-all): lineas que tras la carga quedan sin rut se asignan a
# este proveedor. = MARYUN SEGURIDAD INDUSTRIAL SPA (verificado en tab_proveedores).
RUT_GENERICO = '77084730-3'


@transformer
def transform(data, *args, **kwargs):
    """
    Arma payloads para API de Creación OC.

    rut_proveedor y precio vienen PRIMARIO desde la carga V4 (columnas
    `rut_proveedor` y `costo_unitario_clp`), resueltos en origen por proveedor_id
    -> sin name-match, sin depender de sku_proveedores.

    data_2 (maestro sku->rut/costo) es OPCIONAL y solo se usa como FALLBACK para
    filas que llegaran sin rut/precio. Si no se conecta, se ignora.

    Output: DF agrupado por (destino, rut_proveedor) con columnas:
      - cabecera (dict), detalle (list[dict]), proveedor (str)
    """
    EMPTY_OUT = pd.DataFrame(columns=['cabecera', 'detalle', 'proveedor'])

    # data_2 (maestro sku->rut/costo) es OPCIONAL: solo llega si se conecta un
    # 2do upstream. Sin el, fallback se omite (rut/precio vienen de la carga).
    data_2 = args[0] if args else None

    if data is None or data.empty:
        return EMPTY_OUT

    df = data.copy()

    # === SOLO filas "generar OC" ===
    df = df[df['accion'].astype('string').str.contains('generar', case=False, na=False)].copy()
    if df.empty:
        print("termina en accion")
        return EMPTY_OUT

    chile_now = datetime.now(ZoneInfo("America/Santiago"))
    df['comentario'] = chile_now.strftime("%Y-%m-%d %H:%M:%S")

    df['destino'] = df['sucursal_destino'].astype(str).str.strip()
    df['sku'] = df['sku_original'].astype('string').fillna('').str.strip()
    df['cantidad'] = pd.to_numeric(df['cantidad'], errors='coerce').fillna(0)

    df = df[df['cantidad'] > 0].copy()
    if df.empty:
        print("termina en cantidad")
        return EMPTY_OUT
    df = df[df['destino'].notna() & (df['destino'] != '')].copy()
    if df.empty:
        print("termina en destino")
        return EMPTY_OUT

    # === PRIMARIO: rut + precio + proveedor desde la carga V4 ===
    df['rut_proveedor'] = (
        df['rut_proveedor'].astype('string').fillna('').str.strip()
        if 'rut_proveedor' in df.columns else ''
    )
    df['precio'] = (
        pd.to_numeric(df['costo_unitario_clp'], errors='coerce')
        if 'costo_unitario_clp' in df.columns else pd.Series([pd.NA] * len(df), index=df.index)
    )
    # En compra, sucursal_origen = nombre del proveedor.
    df['proveedor'] = (
        df['sucursal_origen'].astype('string').fillna('').str.strip()
        if 'sucursal_origen' in df.columns else ''
    )

    # === FALLBACK opcional: maestro sku -> rut/costo (data_2) para faltantes ===
    if (data_2 is not None and not data_2.empty
            and {'sku', 'rut_proveedor', 'costo'}.issubset(set(data_2.columns))):
        prov = data_2.copy()
        prov['sku'] = prov['sku'].astype('string').fillna('').str.strip()
        prov['rut_fb'] = prov['rut_proveedor'].astype('string').fillna('').str.strip()
        prov['precio_fb'] = pd.to_numeric(prov['costo'], errors='coerce')
        prov['prov_fb'] = prov['proveedor'].astype('string').fillna('') if 'proveedor' in prov.columns else ''
        prov = prov[['sku', 'prov_fb', 'rut_fb', 'precio_fb']].drop_duplicates(subset=['sku'], keep='last')
        df = df.merge(prov, on='sku', how='left')
        m_rut = df['rut_proveedor'].eq('') & df['rut_fb'].notna()
        df.loc[m_rut, 'rut_proveedor'] = df.loc[m_rut, 'rut_fb'].fillna('')
        m_pre = df['precio'].isna() & df['precio_fb'].notna()
        df.loc[m_pre, 'precio'] = df.loc[m_pre, 'precio_fb']
        m_pr = df['proveedor'].eq('') & df['prov_fb'].notna()
        df.loc[m_pr, 'proveedor'] = df.loc[m_pr, 'prov_fb'].fillna('')
        df = df.drop(columns=['prov_fb', 'rut_fb', 'precio_fb'])

    # === Sin rut tras carga (+fallback) -> rut generico catch-all ===
    sin_rut = df['rut_proveedor'].eq('') | df['rut_proveedor'].isna()
    if sin_rut.any():
        n = int(sin_rut.sum())
        n_sin_precio = int((sin_rut & (df['precio'].isna() | (pd.to_numeric(df['precio'], errors='coerce').fillna(0) <= 0))).sum())
        print(f"[OC] {n} lineas sin rut -> rut generico {RUT_GENERICO} "
              f"(de esas, {n_sin_precio} con precio <= 0)")
        df.loc[sin_rut, 'rut_proveedor'] = RUT_GENERICO

    # === Filtrar solo por precio faltante (rut ya garantizado) ===
    falta = df[df['precio'].isna()]
    if not falta.empty:
        ej = falta[['sku', 'proveedor']].drop_duplicates().head(10).to_dict('records')
        print(f"[OC] {len(falta)} lineas sin precio (NO se postean). Ej: {ej}")
    df = df[df['precio'].notna()].copy()
    if df.empty:
        print("termina en precio")
        return EMPTY_OUT

    # === Consolidar SKUs repetidos dentro de destino/proveedor ===
    df = (
        df.groupby(['destino', 'rut_proveedor', 'proveedor', 'sku', 'precio', 'comentario'], as_index=False)
          .agg({'cantidad': 'sum'})
    )

    usuario = int(kwargs.get('usuario', 73))
    confirmar = bool(kwargs.get('confirmar', False))
    autorizar = bool(kwargs.get('autorizar', False))

    grouped = (
        df.groupby(['destino', 'rut_proveedor', 'proveedor', 'comentario'], as_index=False)
          .agg({'sku': list, 'cantidad': list, 'precio': list})
    )
    if grouped.empty:
        print("termina en grouped")
        return EMPTY_OUT

    # Partir en OC de hasta MAX_ITEMS_POR_DOC lineas (1 doc por chunk).
    rows_out = []
    for _, row in grouped.iterrows():
        skus = list(row['sku'])
        cants = list(row['cantidad'])
        precios = list(row['precio'])
        for i in range(0, len(skus), MAX_ITEMS_POR_DOC):
            cs, cc, cp = skus[i:i + MAX_ITEMS_POR_DOC], cants[i:i + MAX_ITEMS_POR_DOC], precios[i:i + MAX_ITEMS_POR_DOC]
            detalle = [
                {'sku': str(s), 'cantidad': float(q), 'precio': float(p)}
                for s, q, p in zip(cs, cc, cp)
            ]
            cabecera = {
                'rut_proveedor': str(row['rut_proveedor']),
                'destino': str(row['destino']),
                'comentario': str(row['comentario']),
                'usuario': usuario,
                'confirmar': confirmar,
                'autorizar': autorizar,
            }
            rows_out.append({'cabecera': cabecera, 'detalle': detalle, 'proveedor': str(row['proveedor'])})

    out = pd.DataFrame(rows_out, columns=['cabecera', 'detalle', 'proveedor'])
    if out.empty:
        print("termina en grouped")
        return EMPTY_OUT
    return out


@test
def test_output(output, *args) -> None:
    assert output is not None, 'The output is undefined'
