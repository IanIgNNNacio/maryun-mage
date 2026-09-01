import pandas as pd

if 'transformer' not in globals():
    from mage_ai.data_preparation.decorators import transformer
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test

@transformer
def transform(data, data_2, data_3, *args, **kwargs):
    """
    data   = cabecera (ajustes_cabecera.sql unificada: SALIDA + INGRESO)
    data_2 = ingresos (detalle)
    data_3 = salidas  (detalle)

    Salidas:
      - 'ajustes_cabecera'  : DataFrame de cabecera (idéntico a data).
      - 'ajustes_detalle'   : DataFrame detalle unificado, con costo_linea y tipo.
                              Si existe 'documento' en los detalles, se enriquece con metadatos de cabecera.
    """

    cabecera = data.copy()

    # Normalizar columnas esperadas en cabecera
    # (ajusta si tus nombres varían)
    cabecera_cols_lower = {c: c.lower() for c in cabecera.columns}
    cabecera.rename(columns=cabecera_cols_lower, inplace=True)

    # ---- Ingresos (data_2) ----
    ingresos = data_2.copy()
    ingresos.rename(columns={c: c.lower() for c in ingresos.columns}, inplace=True)
    ingresos['tipo'] = 'INGRESO'

    # Asegurar columnas base presentes
    for col in ['sku', 'nombre', 'descripcion', 'qty', 'pmp']:
        if col not in ingresos.columns:
            ingresos[col] = None

    # costo_linea = qty * pmp
    ingresos['costo_linea'] = pd.to_numeric(ingresos['qty'], errors='coerce').fillna(0) * \
                              pd.to_numeric(ingresos['pmp'], errors='coerce').fillna(0)

    # ---- Salidas (data_3) ----
    salidas = data_3.copy()
    salidas.rename(columns={c: c.lower() for c in salidas.columns}, inplace=True)
    salidas['tipo'] = 'SALIDA'

    for col in ['sku', 'nombre', 'descripcion', 'qty', 'pmp']:
        if col not in salidas.columns:
            salidas[col] = None

    # Forzar signo negativo en salidas si vienen positivas
    salidas['qty'] = pd.to_numeric(salidas['qty'], errors='coerce')
    salidas.loc[salidas['qty'] > 0, 'qty'] = salidas.loc[salidas['qty'] > 0, 'qty'] * -1

    salidas['pmp'] = pd.to_numeric(salidas['pmp'], errors='coerce')
    salidas['costo_linea'] = salidas['qty'].fillna(0) * salidas['pmp'].fillna(0)

    # ---- Unificar columnas y concatenar ----
    base_cols = ['tipo', 'sku', 'nombre', 'descripcion', 'qty', 'pmp', 'costo_linea']
    # Si los detalles traen 'documento', lo preservamos para posible join con cabecera
    if 'documento' in ingresos.columns or 'documento' in salidas.columns:
        if 'documento' not in ingresos.columns:
            ingresos['documento'] = None
        if 'documento' not in salidas.columns:
            salidas['documento'] = None
        base_cols = ['tipo', 'documento'] + base_cols

    detalle_unificado = pd.concat(
        [ingresos[base_cols], salidas[base_cols]],
        ignore_index=True
    )

    # Tipos numéricos limpios
    for col in ['qty', 'pmp', 'costo_linea']:
        if col in detalle_unificado.columns:
            detalle_unificado[col] = pd.to_numeric(detalle_unificado[col], errors='coerce')

    # ---- Enriquecer detalle con metadatos de cabecera (opcional) ----
    # Se hace solo si existe 'documento' en el detalle y cabecera tiene esas columnas.
    detalle_cols_meta = ['usuario', 'dt_in', 'sucursal_id', 'bodega_desc', 'usr_in', 'costo']
    have_documento = 'documento' in detalle_unificado.columns
    have_keys_in_cab = all(col in cabecera.columns for col in ['tipo', 'documento'])

    if have_documento and have_keys_in_cab:
        # Nos quedamos con columnas clave y metadatos disponibles
        meta_cols = ['tipo', 'documento'] + [c for c in detalle_cols_meta if c in cabecera.columns]
        cab_meta = cabecera[meta_cols].drop_duplicates()

        # Merge 1→N (cabecera → detalle)
        detalle_unificado = detalle_unificado.merge(
            cab_meta,
            on=['tipo', 'documento'],
            how='left'
        )

    # Reorden final sugerido
    preferred_order = [
        c for c in
        ['tipo', 'documento', 'dt_in', 'sucursal_id', 'bodega_desc', 'usr_in', 'usuario',
         'sku', 'nombre', 'descripcion', 'qty', 'pmp', 'costo_linea', 'costo']
        if c in detalle_unificado.columns
    ]
    detalle_unificado = detalle_unificado[preferred_order]

    # Devuelve DOS salidas: cabecera y detalle_unificado
    return {
        'ajustes_cabecera': cabecera,
        'ajustes_detalle': detalle_unificado,
    }
