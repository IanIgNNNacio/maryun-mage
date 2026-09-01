from datetime import datetime, timezone
import pandas as pd

if 'transformer' not in globals():
    from mage_ai.data_preparation.decorators import transformer
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test


def parse_number(value):
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        if pd.isna(value):
            return 0.0
        return float(value)
    cleaned = str(value).strip()
    if not cleaned or cleaned.lower() == 'nan':
        return 0.0
    cleaned = cleaned.replace('$', '').replace('.', '').replace(',', '.')
    cleaned = ''.join(cleaned.split())
    try:
        return float(cleaned)
    except Exception:
        return 0.0


def normalize_importe(value):
    try:
        n = float(value)
    except Exception:
        n = 0.0
    return f"{n:.2f}".rstrip('0').rstrip('.')


def parse_fecha_datetime(value):
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() == 'nan':
        return None
    for fmt in (
        '%Y-%m-%dT%H:%M:%S.%f',   # ← 2026-05-25T12:46:06.700
        '%Y-%m-%dT%H:%M:%S',       # ← 2026-05-25T12:46:06
        '%d/%m/%Y',
        '%Y-%m-%d',
        '%Y%m%d',
    ):
        try:
            d = datetime.strptime(s, fmt)
            return datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=timezone.utc)
        except Exception:
            pass
    return None


def clean_string(value):
    if value is None or pd.isna(value):
        return None
    s = str(value).strip()
    if not s or s.lower() == 'nan':
        return None
    return s


@transformer
def transform(data, *args, **kwargs):
    df = pd.DataFrame(data)

    if df.empty:
        return pd.DataFrame(columns=[
            'fecha', 'tipoMovimiento', 'descripcion', 'sucursal', 'banco',
            'monto', 'importe', 'fechaContable', 'horaTransaccion',
            'nroMovimiento', 'codigoOperacion', 'rutUsuario', 'createdAtUtc',
            'cuenta', 'key',
        ])

    df['fecha']          = df['fecha'].apply(parse_fecha_datetime)
    df['descripcion']    = df['descripcion'].apply(clean_string)
    df['sucursal']       = df['sucursal'].apply(clean_string)
    df['fechaContable']  = df['fechaContable'].apply(clean_string)
    df['horaTransaccion']= df['horaTransaccion'].apply(clean_string)
    df['nroMovimiento']  = df['nroMovimiento'].apply(clean_string)
    df['codigoOperacion']= df['codigoOperacion'].apply(clean_string)

    importe_num = df['importe'].apply(parse_number)
    monto_base  = df['monto'].apply(parse_number)

    df['importe'] = importe_num.combine(monto_base, lambda i, m: abs(i) if abs(i) > 0 else abs(m)).astype(float)
    df['monto']   = df.apply(
        lambda row: -abs(monto_base[row.name]) if row['tipoMovimiento'] == 'CARGO' else abs(monto_base[row.name]),
        axis=1,
    ).astype(float)
    
    df['createdAtUtc'] = datetime.now(timezone.utc)

    df['importe_normalizado'] = df['importe'].apply(normalize_importe)
    df['key'] = df.apply(
        lambda row: (
            f"{row['importe_normalizado']}|"
            f"{'' if row['fechaContable'] is None else row['fechaContable']}|"
            f"{'' if row['nroMovimiento'] is None else row['nroMovimiento']}|"
            f"{'' if row['codigoOperacion'] is None else row['codigoOperacion']}"
        ),
        axis=1,
    )

    output = df[[
        'fecha', 'tipoMovimiento', 'descripcion', 'sucursal', 'banco',
        'monto', 'importe', 'fechaContable', 'horaTransaccion',
        'nroMovimiento', 'codigoOperacion', 'rutUsuario', 'createdAtUtc',
        'cuenta', 'key',
    ]].copy()

    output.columns = [str(c) for c in output.columns]
    return output


@test
def test_output(output, *args) -> None:
    assert output is not None, 'The output is undefined'
    assert isinstance(output, pd.DataFrame), 'El transformer debe retornar un DataFrame'
    assert len(output.columns) > 0, 'El output debe tener columnas'
