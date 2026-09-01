# data_loader_sku_proveedores_excel.py
import os
import pandas as pd

if 'data_loader' not in globals():
    from mage_ai.data_preparation.decorators import data_loader
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test

@data_loader
def load_data(*args, **kwargs):
    """
    Lee Excel con columnas:
      sku, proveedor, rut_proveedor, costo, divisa

    Defaults:
      fuente = "mage"
      estado = 1

    Busca el Excel en el mismo directorio de io_config.yaml.
    Puedes pasar el nombre con kwargs['excel_filename'].
    """
    excel_filename = '/home/src/Maryun/sku_proveedores.xlsx'

    df = pd.read_excel(excel_filename, dtype={'sku': 'string'})
    
    df['sku'] = df['sku'].astype('string').str.strip()

    # Normalizar nombres por si vienen con espacios/mayúsculas
    df.columns = [str(c).strip().lower() for c in df.columns]

    required = ['sku', 'proveedor', 'rut_proveedor', 'costo', 'divisa', 'categoria']
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f'Faltan columnas en Excel: {missing}. Trae: {list(df.columns)}')

    # Defaults
    df['fuente'] = 'mage'
    df['estado'] = 1

    return df


@test
def test_output(output, *args) -> None:
    assert output is not None, 'The output is undefined'