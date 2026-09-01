import pandas as pd
import re

if 'transformer' not in globals():
    from mage_ai.data_preparation.decorators import transformer
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test

# Ajusta estas listas si cambias tu DDL
DECIMAL_COLS = [
    'pu','pmp','totaliza_pmp','totaliza_vta','margen','diferencia',
    'totaliza_diferencia','margen_diferencia','margen_final','tipo_comision'
]

# ClickHouse: Nullable(DateTime)
DATE_COLS_DT = ['creado', 'dt_picking', 'facturar']

# ClickHouse: Date / Nullable(Date)
DATE_COLS_D  = ['facturado', 'confirmado', 'entregado', 'vencimiento']

@transformer
def transform(data, *args, **kwargs):
    """
    Recibe un DataFrame 'data' y devuelve el mismo DataFrame
    con decimales y fechas normalizados para ClickHouse.
    """
    if not isinstance(data, pd.DataFrame):
        data = pd.DataFrame(data)

    df = data.copy()

    # 1) Decimales
    df = _clean_decimals(df)

    # 2) Fechas
    df = _clean_dates(df)

    if 'picking' in df.columns:
        df['picking'] = _clean_picking_series(df['picking'])

    # (Opcional) Generar id_2 para debug/chequeos locales; no se usa en el insert.
    if 'pid' in df.columns and 'sku' in df.columns:
        # Evitar NaN en concatenación
        df['id_2'] = df['pid'].astype('Int64').astype(str) + df['sku'].astype(str)

    if 'picking' in df.columns:
        # Convertir a número
        df['_picking_num'] = pd.to_numeric(df['picking'], errors='coerce')

        # Filtrar valores no enteros
        mask_non_int = df['_picking_num'].notna() & ((df['_picking_num'] % 1) != 0)

        df_debug = df.loc[mask_non_int].copy()

        if not df_debug.empty:
            print("\n=== FILAS CON PICKING DECIMAL ===")
            print(df_debug[['pid', 'sku', 'picking']].head(20))
            print("=================================\n")

    # return df_debug
    return df


@test
def test_output(output, *args) -> None:
    """
    Template code for testing the output of the block.
    """
    assert output is not None, 'The output is undefined'

def _clean_decimal_series(s: pd.Series) -> pd.Series:
    """
    Limpieza robusta para columnas con coma decimal (y posibles puntos de miles).
    Regla:
    - Si hay coma (,) → la tomamos como decimal: quitamos puntos de miles y reemplazamos coma por punto.
    - Si NO hay coma → intentamos convertir directo (ya puede venir con punto decimal).
    """
    s_str = s.astype(str)

    # Caso europeo: contiene coma decimal
    mask_coma = s_str.str.contains(',', na=False)

    # Donde hay coma: quitar puntos (miles) y reemplazar coma por punto
    s_eu = (
        s_str.where(~mask_coma, s_str.str.replace('.', '', regex=False))
             .where(~mask_coma, lambda x: x.str.replace(',', '.', regex=False))
    )

    # Donde NO hay coma: dejar tal cual
    s_mix = s_eu

    out = pd.to_numeric(s_mix, errors='coerce').round(2)
    return out


def _clean_decimals(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in DECIMAL_COLS:
        if c in df.columns:
            df[c] = _clean_decimal_series(df[c])  # ya convierte y redondea a 2
            df[c] = df[c].fillna(0)              # 👈 completar NaN a 0.00
    return df


def _clean_dates(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    # DateTime
    for c in DATE_COLS_DT:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors='coerce')  # datetime64[ns] (nullable)
    # Date
    for c in DATE_COLS_D:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors='coerce').dt.date  # date
    return df

def _clean_picking_series(s: pd.Series) -> pd.Series:
    """
    Limpieza de la columna picking:
    - Si el valor es tipo '1.300' o '12.345' (punto como separador de miles, SIEMPRE 3 dígitos),
      se eliminan los puntos: '1.300' -> '1300', '12.345' -> '12345'.
    - Si no matchea ese patrón, se intenta convertir normal a número.
    - Al final, se valida que todos los valores sean enteros (sin parte decimal).
    """

    # Pasamos todo a string para trabajar patrones cómodamente
    s_str = s.astype(str).str.strip()

    # Patrón de miles: 1-3 dígitos, luego uno o más grupos ".ddd"
    # Ejemplos válidos: "1.300", "12.345", "999.999", "1.234.567"
    pattern_thousands = re.compile(r'^\d{1,3}(?:\.\d{3})+$')

    def normalize_value(val: str) -> str:
        if pattern_thousands.match(val):
            # Quitar todos los puntos -> "1.300" -> "1300"
            return val.replace('.', '')
        return val

    s_norm = s_str.map(normalize_value)

    # Convertimos a número
    s_num = pd.to_numeric(s_norm, errors='coerce')

    # Validar que sean enteros (sin decimales)
    mask_decimal = s_num.notna() & ((s_num % 1) != 0)
    if mask_decimal.any():
        ejemplos = s_num.loc[mask_decimal].head().tolist()
        raise ValueError(
            f'La columna "picking" tiene valores decimales que no parecen miles: {ejemplos}. '
            'Revisa la fuente o define una regla de negocio para ellos.'
        )

    return s_num.astype('Int64')