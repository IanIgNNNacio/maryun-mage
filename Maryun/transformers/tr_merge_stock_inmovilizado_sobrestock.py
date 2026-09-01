import re
import pandas as pd
import numpy as np

if 'transformer' not in globals():
    from mage_ai.data_preparation.decorators import transformer
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test


REQUIRED_COLUMNS = [
    'nombre',
    'sucursal',
    'dias_sin_venta',
    'dias_sin_ingreso',
    'stock_sucursal',
    'sku',
    'tipo_problema',
]


@transformer
def transform(data, data_2, *args, **kwargs):
    df_sobrestock   = data.copy()
    df_inmovilizado = data_2.copy()

    # -----------------------------
    # 1) Filtrar por tipo_problema
    # -----------------------------
    df_sobrestock   = df_sobrestock[df_sobrestock['tipo_problema'] == 'SOBRESTOCK'].copy()
    df_inmovilizado = df_inmovilizado[df_inmovilizado['tipo_problema'] == 'INMOVILIZADO'].copy()

    # -----------------------------
    # 2) Merge
    # -----------------------------
    df = pd.concat([df_sobrestock, df_inmovilizado], ignore_index=True)

    # -----------------------------
    # 3) Validar columnas minimas
    # -----------------------------
    missing_cols = [col for col in REQUIRED_COLUMNS if col not in df.columns]
    if missing_cols:
        raise ValueError(f'Faltan columnas requeridas: {missing_cols}')

    # -----------------------------
    # 4) Limpiar textos
    # -----------------------------
    text_columns = ['nombre', 'sucursal', 'sku', 'tipo_problema', 'urgencia', 'variante', 'producto_completo']

    for col in text_columns:
        if col in df.columns:
            df[col] = (
                df[col]
                .astype(str)
                .str.strip()
                .replace({'nan': None, 'None': None, '': None})
            )

    # _clean_spaces a todas las columnas de texto relevantes
    for col in ['nombre', 'sucursal', 'producto_completo', 'variante']:
        if col in df.columns:
            df[col] = df[col].apply(_clean_spaces)

    df['sku'] = (
        df['sku']
        .astype(str)
        .str.strip()
        .str.replace(r'\.0$', '', regex=True)
    )

    # -----------------------------
    # 5) Convertir numericos
    # -----------------------------
    numeric_columns = [
        'dias_sin_venta',
        'dias_sin_ingreso',
        'stock_sucursal',
        'pronostico_mes',
        'meses_cobertura',
        'cant_sucursales_con_problema',
        'total_valorizado_todas_sucursales',
        'stock_valorizado',
    ]

    for col in numeric_columns:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')

    # -----------------------------
    # 6) Eliminar filas totalmente vacias en columnas clave
    # -----------------------------
    df = df.dropna(how='all', subset=REQUIRED_COLUMNS)

    # -----------------------------
    # 7) Filtrar registros sin claves minimas
    # -----------------------------
    df = df[
        df['sku'].notna()
        & df['sucursal'].notna()
        & df['nombre'].notna()
        & df['tipo_problema'].notna()
    ].copy()

    # -----------------------------
    # 8) Normalizaciones de negocio basicas
    # -----------------------------
    for col in ['dias_sin_venta', 'dias_sin_ingreso']:
        df.loc[df[col] < 0, col] = np.nan

    for col in ['stock_sucursal', 'pronostico_mes', 'meses_cobertura',
                'total_valorizado_todas_sucursales', 'stock_valorizado']:
        if col in df.columns:
            df.loc[df[col] < 0, col] = np.nan

    # cant_sucursales_con_problema es un conteo, no puede ser negativo ni decimal
    if 'cant_sucursales_con_problema' in df.columns:
        df.loc[df['cant_sucursales_con_problema'] < 0, 'cant_sucursales_con_problema'] = np.nan

    # Cast final
    df['dias_sin_venta']               = df['dias_sin_venta'].astype('Int64')
    df['dias_sin_ingreso']             = df['dias_sin_ingreso'].astype('Int64')
    df['cant_sucursales_con_problema'] = df['cant_sucursales_con_problema'].astype('Int64')
    df['stock_sucursal']               = df['stock_sucursal'].astype(float)

    # -----------------------------
    # 9) Columnas utiles para snapshots y analisis
    # -----------------------------
    df['snapshot_date'] = pd.to_datetime(
        pd.Timestamp.now(tz='America/Santiago').date()
    ).normalize()
    df['sku_sucursal']  = df['sku'].astype(str) + '|' + df['sucursal'].astype(str)

    # -----------------------------
    # 10) Eliminar duplicados
    # -----------------------------
    df = df.drop_duplicates(
        subset=['snapshot_date', 'sku', 'sucursal', 'tipo_problema'],
        keep='last'
    ).copy()

    # -----------------------------
    # 11) Orden final
    # -----------------------------
    ordered_columns = [
        'snapshot_date',
        'sku',
        'nombre',
        'sucursal',
        'sku_sucursal',
        'tipo_problema',
        'urgencia',
        'variante',
        'producto_completo',
        'dias_sin_venta',
        'dias_sin_ingreso',
        'stock_sucursal',
        'pronostico_mes',
        'meses_cobertura',
        'cant_sucursales_con_problema',
        'total_valorizado_todas_sucursales',
        'stock_valorizado',
    ]

    existing_columns  = [col for col in ordered_columns if col in df.columns]
    remaining_columns = [col for col in df.columns if col not in existing_columns]
    df = df[existing_columns + remaining_columns]

    return df


@test
def test_output(output, *args) -> None:
    assert output is not None, 'The output is undefined'
    assert isinstance(output, pd.DataFrame), 'La salida debe ser un DataFrame'
    assert len(output) > 0, 'La salida no debe estar vacia'

    required_columns = [
        'snapshot_date',
        'sku',
        'nombre',
        'sucursal',
        'sku_sucursal',
        'tipo_problema',
        'dias_sin_venta',
        'dias_sin_ingreso',
        'stock_sucursal',
    ]

    missing_cols = [col for col in required_columns if col not in output.columns]
    assert not missing_cols, f'Faltan columnas requeridas en la salida: {missing_cols}'

    assert output['sku'].notna().all(),          'Todos los sku deben tener valor'
    assert output['sucursal'].notna().all(),      'Todas las sucursales deben tener valor'
    assert output['nombre'].notna().all(),        'Todos los nombres deben tener valor'
    assert output['tipo_problema'].notna().all(), 'Todos los tipo_problema deben tener valor'

    valid_tipos = {'INMOVILIZADO', 'SOBRESTOCK'}
    tipos_invalidos = set(output['tipo_problema'].unique()) - valid_tipos
    assert not tipos_invalidos, f'tipo_problema contiene valores inesperados: {tipos_invalidos}'

    duplicates = output.duplicated(subset=['snapshot_date', 'sku', 'sucursal', 'tipo_problema']).sum()
    assert duplicates == 0, 'No debe haber duplicados por snapshot_date + sku + sucursal + tipo_problema'


def _clean_spaces(value):
    if value is None or pd.isna(value):
        return value
    value = str(value).strip()
    value = re.sub(r'\s+', ' ', value)
    return value