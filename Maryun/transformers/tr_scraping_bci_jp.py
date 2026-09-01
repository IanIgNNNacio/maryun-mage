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
    df = data[['fechaDeTransaccion', 'tipo.codigo', 'glosa.enriquecida.compuesta', 'detalleMovimiento.depositos.sucursal', 'montoDeTransaccion', 'fechaContable', 'numeroDeSerie', 'horaTerminacionProceso', 'codigoDeTransaccion']]
    # fecha = fechaDeTransaccion
    # tipoMovimiento = tipo.codigo (C: Cargo, A: Abono)
    # descripcion = glosa.enriquecida.compuesta
    # sucursal = solo si esta disponible el campo detalleMovimiento.depositos.sucursal, si no esta, "No existe"
    # banco = "BCI"
    # monto = montoDeTransaccion
    # importe = montoDeTransaccion
    # fechaContable = fechaContable
    # nroMovimiento = numeroOperacion
    # horaTransaccion = horaTerminacionProceso
    # codigoOperacion = codigoDeTransaccion
    # rutUsuario = "24.818.131-1"
    # cuenta = "60284935"
    df['horaTerminacionProceso'] = pd.to_datetime(df['horaTerminacionProceso'], format='%H:%M:%S.%f').dt.strftime('%H:%M')
    df['tipo.codigo'] = df['tipo.codigo'].map({'A': 'ABONO', 'C': 'CARGO'}).fillna(df['tipo.codigo'])
    df['detalleMovimiento.depositos.sucursal'] = df['detalleMovimiento.depositos.sucursal'].replace('', 'No existe').fillna('No existe')
    df['banco'] = 'BCI'
    df['rutUsuario'] = '24.818.131-1'
    df['cuenta'] = '60284935'
    montoDeTransaccion_2 = df['montoDeTransaccion']
    df['montoDeTransaccion_2'] = montoDeTransaccion_2


    df = df.rename(columns={
        'fechaDeTransaccion': 'fecha',
        'glosa.enriquecida.compuesta': 'descripcion',
        'detalleMovimiento.depositos.sucursal': 'sucursal',
        'tipo.codigo': 'tipoMovimiento',
        'montoDeTransaccion': 'monto',
        'montoDeTransaccion_2': 'importe',
        'numeroDeSerie': 'nroMovimiento',
        'horaTerminacionProceso': 'horaTransaccion',
        'codigoDeTransaccion': 'codigoOperacion',
    })


    return df


@test
def test_output(output, *args) -> None:
    """
    Template code for testing the output of the block.
    """
    assert output is not None, 'The output is undefined'