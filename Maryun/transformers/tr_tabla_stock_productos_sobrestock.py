import pandas as pd

if 'transformer' not in globals():
    from mage_ai.data_preparation.decorators import transformer
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test


@transformer
def transform(data, *args, **kwargs):
    df = data.copy()
    return df[df['tipo_problema'] == 'SOBRESTOCK'].copy()


@test
def test_output(output, *args) -> None:
    assert output is not None, 'The output is undefined'
    assert isinstance(output, pd.DataFrame), 'La salida debe ser un DataFrame'
    assert len(output) > 0, 'La salida no debe estar vacia'
    assert (output['tipo_problema'] == 'SOBRESTOCK').all(), 'Solo debe haber registros SOBRESTOCK'