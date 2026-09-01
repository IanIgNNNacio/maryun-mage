import pandas as pd

if 'transformer' not in globals():
    from mage_ai.data_preparation.decorators import transformer
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test


@transformer
def transform(data, *args, **kwargs):
    """
    Template code for a transformer block.

    Add more parameters to this function if this block has multiple parent blocks.
    There should be one parameter for each output variable from each parent block.

    Args:
        data: The output from the upstream parent block
        args: The output from any additional upstream blocks (if applicable)

    Returns:
        Anything (e.g. data frame, dictionary, array, int, str, etc.)
    """
    # Specify your transformation logic here

    # data['banco'] = 'SANTANDER'
    # data['rutUsuario'] = '19.150.357-0'
    # data['cuenta'] = '770847303'
    data['banco'] = 'SANTANDER'
    data['rutUsuario'] = '20.096.020-3'
    data['cuenta'] = '770847303'

    data = data.rename(columns={
        'FechaTransaccionFtm': 'fecha',
        'Descripcion': 'descripcion',
        'Sucursal': 'sucursal',
        'Monto': 'monto',
        'Importe': 'importe',
        'FechaContable': 'fechaContable',
        'NroMovimiento': 'nroMovimiento',
        'HoraTransaccion': 'horaTransaccion',
        'CodigoOperacion': 'codigoOperacion',
        'EsCargo': 'esCargo_raw',
    })

    data['tipoMovimiento'] = data['esCargo_raw'].apply(
        lambda x: 'CARGO' if parse_bool(x) else 'ABONO'
    )

    return data


@test
def test_output(output, *args) -> None:
    """
    Template code for testing the output of the block.
    """
    assert output is not None, 'The output is undefined'

def parse_bool(value):
    if value is None or pd.isna(value):
        return False
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    return s in ('true', '1', 'si', 'sí', 'yes', 'y')
