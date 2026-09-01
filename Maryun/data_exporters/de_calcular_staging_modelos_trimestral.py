from __future__ import annotations

import glob
import re
import subprocess
import sys
from pathlib import Path

import clickhouse_connect  # type: ignore
import pandas as pd
from mage_ai.io.config import ConfigFileLoader

CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

# Debe coincidir con la DB que lee abastecimiento_v4 (logistica_v2). Ver nota
# en de_publicar_snapshots_trimestral.py.
STG_FC = 'logistica_v2.logistica_stg_forecast_precomputado'
STG_CLS = 'logistica_v2.logistica_stg_clasificacion_precomputada'

if 'data_exporter' not in globals():
    from mage_ai.data_preparation.decorators import data_exporter
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test


def _client():
    cfg = ConfigFileLoader(CONFIG_PATH, PROFILE)
    use_https = str(cfg['CLICKHOUSE_INTERFACE']).lower() == 'https'
    return clickhouse_connect.get_client(
        host=cfg['CLICKHOUSE_HOST'],
        port=int(cfg['CLICKHOUSE_PORT']),
        username=cfg['CLICKHOUSE_USERNAME'],
        password=cfg['CLICKHOUSE_PASSWORD'],
        database=cfg['CLICKHOUSE_DATABASE'],
        secure=use_https,
    )


def _resolve_base_dir() -> Path:
    # En Mage el bloque corre via exec -> __file__ no existe. Ubicamos los scripts
    # (script_forecast.py) por glob y usamos su carpeta como base_dir.
    hits = glob.glob('/home/src/**/script_forecast.py', recursive=True)
    if hits:
        return Path(hits[0]).resolve().parent
    raise FileNotFoundError(
        'No se encontro script_forecast.py bajo /home/src. '
        'Sube los scripts (script_forecast.py / script_clasificacion.py) al container.'
    )


def _run_script(cmd: list[str], cwd: Path) -> None:
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"Falló script: {' '.join(cmd)}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )


def _split_id_key(id_key: str) -> tuple[str, str]:
    s = str(id_key).strip().upper()
    # extrae prefijo numérico (sku) + sufijo (ubicación)
    m = re.match(r'^(\d+)\s*(.*)$', s)
    if not m:
        return s, ''
    sku = (m.group(1).lstrip('0') or '0')
    ubic = m.group(2).strip()
    return sku, ubic


DEMANDA_MENSUAL = 'logistica_v2.logistica_demanda_estandarizada_mensual'
CLASIFICACION_BASE = 'logistica_v2.logistica_clasificacion_base_mensual'


def _build_forecast_input_from_ch(c, out_xlsx: Path) -> int:
    """Genera el insumo del forecast (wide) DESDE ClickHouse en vez de un Excel
    manual. Pivotea logistica_demanda_estandarizada_mensual a:
        SKU 2.0 | <mes1> | <mes2> | ...   (hoja 'Hoja2')
    Es el mismo formato que esperaba script_forecast.py (--sheet Hoja2,
    --id-col 'SKU 2.0'); el script queda intacto."""
    df = c.query_df(f"SELECT sku_2_0, mes, demanda_neta FROM {DEMANDA_MENSUAL}")
    if df is None or df.empty:
        raise ValueError(
            f"{DEMANDA_MENSUAL} vacia. Corre el pipeline demanda_mensual antes "
            "de regenerar los modelos trimestrales."
        )
    # mes como DATETIME (no string): el script lo toma por la rama isinstance(Timestamp)
    # sin ambiguedad. Strings 'YYYY-MM-DD' con el dayfirst=True del script de clasificacion
    # se malinterpretan (mes leido como dia) -> columnas duplicadas.
    df['mes'] = pd.to_datetime(df['mes'])
    df['demanda_neta'] = pd.to_numeric(df['demanda_neta'], errors='coerce').fillna(0.0)
    wide = df.pivot_table(
        index='sku_2_0', columns='mes', values='demanda_neta',
        aggfunc='sum', fill_value=0.0,
    )
    wide = wide.reindex(sorted(wide.columns), axis=1)  # meses en orden
    wide = wide.reset_index().rename(columns={'sku_2_0': 'SKU 2.0'})
    wide.to_excel(out_xlsx, sheet_name='Hoja2', index=False)
    return int(len(wide))


def _build_clasif_input_from_ch(c, out_xlsx: Path) -> int:
    """Genera el insumo de clasificacion (wide) DESDE ClickHouse. Pivotea
    logistica_clasificacion_base_mensual a:
        SKU 3.0 | MARGEN | <mes1> | <mes2> | ...
    MARGEN = suma de margen_total por SKU 3.0 (margen total del periodo).
    Formato que espera script_clasificacion.py (col_sku='SKU 3.0',
    col_margin='MARGEN', col_branch='' -> sucursal embebida en SKU 3.0)."""
    df = c.query_df(
        f"SELECT sku_3_0, mes, demanda_neta, margen_total FROM {CLASIFICACION_BASE}"
    )
    if df is None or df.empty:
        raise ValueError(
            f"{CLASIFICACION_BASE} vacia. Corre el pipeline demanda_mensual antes "
            "de regenerar los modelos trimestrales."
        )
    df['mes'] = pd.to_datetime(df['mes'])  # datetime, no string (ver nota en forecast)
    df['demanda_neta'] = pd.to_numeric(df['demanda_neta'], errors='coerce').fillna(0.0)
    df['margen_total'] = pd.to_numeric(df['margen_total'], errors='coerce').fillna(0.0)

    demanda_wide = df.pivot_table(
        index='sku_3_0', columns='mes', values='demanda_neta',
        aggfunc='sum', fill_value=0.0,
    )
    demanda_wide = demanda_wide.reindex(sorted(demanda_wide.columns), axis=1)
    margen = df.groupby('sku_3_0')['margen_total'].sum().rename('MARGEN')

    wide = demanda_wide.join(margen, how='left').reset_index()
    wide = wide.rename(columns={'sku_3_0': 'SKU 3.0'})
    month_cols = [col for col in wide.columns if col not in ('SKU 3.0', 'MARGEN')]
    wide = wide[['SKU 3.0', 'MARGEN'] + month_cols]
    wide.to_excel(out_xlsx, index=False)  # primera hoja -> sheet_name=0
    return int(len(wide))


def _load_forecast_staging(c, forecast_xlsx: Path) -> int:
    df = pd.read_excel(forecast_xlsx, sheet_name='Pronosticos')
    if 'Tipo' in df.columns:
        df = df[df['Tipo'].astype(str).str.upper() == 'FUTURO'].copy()

    if df.empty:
        raise ValueError("La hoja Pronosticos quedó vacía tras filtrar Tipo='FUTURO'.")

    parsed = df['ID_KEY'].apply(_split_id_key)
    out = pd.DataFrame({
        'sku_id': parsed.str[0],
        'ubicacion': parsed.str[1].str.upper().str.strip(),
        'mes': pd.to_datetime(df['Fecha'], errors='coerce').dt.date,
        'forecast_modelo': pd.to_numeric(df['Pronostico'], errors='coerce').fillna(0.0),
        'forecast_override': None,
        'forecast_final': pd.to_numeric(df['Pronostico'], errors='coerce').fillna(0.0),
        'forecast_fue_forzado': 0,
        'motivo_override': None,
        'responsable_override': None,
        'version_modelo': 'v10',
    }).dropna(subset=['mes'])

    c.command(f"""
    CREATE TABLE IF NOT EXISTS {STG_FC}
    (
      sku_id String,
      ubicacion String,
      mes Date,
      forecast_modelo Float64,
      forecast_override Nullable(Float64),
      forecast_final Float64,
      forecast_fue_forzado UInt8,
      motivo_override Nullable(String),
      responsable_override Nullable(String),
      version_modelo Nullable(String)
    )
    ENGINE = MergeTree
    PARTITION BY toYYYYMM(mes)
    ORDER BY (sku_id, ubicacion, mes)
    """)
    c.command(f"TRUNCATE TABLE {STG_FC}")
    c.insert_df(STG_FC, out)
    return int(len(out))


def _load_classification_staging(c, cls_xlsx: Path) -> int:
    """Expande la clasificacion v3 (SKU 3.0 -> SKU real) usando el MISMO v3_loader
    del core que usa base (build_classification). Cruza el RESULTADO con products
    (dwh.mysis_tab_sku, sale_ok=1) por nombre -> pares (sku_id real, ubicacion) +
    clase_automatizacion. Asi la precomputada = classification_final de base."""
    import sys
    V4 = '/home/src/Maryun/utils/v4_core'
    if V4 not in sys.path:
        sys.path.insert(0, V4)
    from app.classification.v3_loader import load_v3_classification
    from app.normalize.canonical import canonical_sku

    # products = mismos que usa abastecimiento/base (catalogo activo)
    prod = c.query_df(
        "SELECT toString(sku) AS sku_id, toString(ifNull(nombre,'')) AS nombre "
        "FROM mysis_tab_sku FINAL WHERE sale_ok = 1"
    )
    prod['sku_id'] = prod['sku_id'].map(canonical_sku)

    cls = load_v3_classification(Path(cls_xlsx), prod)  # expansion exacta del core

    out = pd.DataFrame({
        'sku_3_0': cls['sku_id'].astype(str),
        'sku_id': cls['sku_id'].astype(str),
        'ubicacion': cls['ubicacion'].astype(str).str.upper().str.strip(),
        'abc_modelo': cls['abc_modelo'].astype(str).str.upper().str.strip(),
        'xyz_modelo': cls['xyz_modelo'].astype(str).str.upper().str.strip(),
        'clase_final': cls['clase_modelo'].astype(str).str.upper().str.strip(),
        'score_automatizacion': pd.to_numeric(cls.get('score_automatizacion'), errors='coerce'),
        'clase_automatizacion': cls.get('clase_automatizacion', pd.NA),
        'version_modelo': 'v3',
    }).dropna(subset=['sku_id', 'ubicacion']).drop_duplicates(['sku_id', 'ubicacion'])

    # staging con sku_id REAL (DROP+CREATE para garantizar el schema con sku_id)
    c.command(f"DROP TABLE IF EXISTS {STG_CLS}")
    c.command(f"""
    CREATE TABLE {STG_CLS}
    (
      sku_3_0 String,
      sku_id String,
      ubicacion String,
      abc_modelo String,
      xyz_modelo String,
      clase_final String,
      score_automatizacion Nullable(Float64),
      clase_automatizacion Nullable(String),
      version_modelo Nullable(String)
    )
    ENGINE = MergeTree
    ORDER BY (sku_id, ubicacion)
    """)
    c.insert_df(STG_CLS, out)
    print(f'clasificacion expandida (v3_loader): {len(out)} pares (sku_id real)')
    return int(len(out))


@data_exporter
def de_calcular_staging_modelos_trimestral(**kwargs):
    """
    Ejecuta scripts originales y carga staging:
    - script_forecast.py -> PRONOSTICOS_DEMANDA_v10.xlsx -> STG_FC
    - script_clasificacion.py -> RESULTADO_ESTRATEGICO_ABC_XYZ_v3.xlsx -> STG_CLS
    """
    base_dir = _resolve_base_dir()
    forecast_script = base_dir / 'script_forecast.py'
    clasif_script = base_dir / 'script_clasificacion.py'

    # Insumos generados DESDE ClickHouse (cloud, sin Excel manual). Se escriben
    # en base_dir para que el output de los scripts caiga junto a ellos.
    demanda_xlsx = base_dir / 'DEMANDA ESTANDARIZADA.xlsx'
    clasif_input_xlsx = base_dir / 'CLASIFICACION ABC-XYZ.xlsx'

    forecast_out = base_dir / 'PRONOSTICOS_DEMANDA_v10.xlsx'
    clasif_out = base_dir / 'RESULTADO_ESTRATEGICO_ABC_XYZ_v3.xlsx'

    if not forecast_script.exists():
        raise FileNotFoundError(f"No existe script_forecast.py en {forecast_script}")
    if not clasif_script.exists():
        raise FileNotFoundError(f"No existe script_clasificacion.py en {clasif_script}")

    c = _client()

    # Construir los insumos wide desde ClickHouse (reemplaza los Excel manuales).
    n_in_fc = _build_forecast_input_from_ch(c, demanda_xlsx)
    n_in_cls = _build_clasif_input_from_ch(c, clasif_input_xlsx)

    # Ejecutar scripts originales con rutas explícitas (scripts INTACTOS).
    _run_script(
        [
            sys.executable,
            str(forecast_script),
            '--input', str(demanda_xlsx),
            '--output', str(forecast_out),
            '--sheet', 'Hoja2',
        ],
        cwd=base_dir,
    )
    _run_script(
        [
            sys.executable,
            str(clasif_script),
            '--input', str(clasif_input_xlsx),
            '--output', str(clasif_out.name),
        ],
        cwd=base_dir,
    )

    if not forecast_out.exists():
        raise FileNotFoundError(f"No se generó forecast output: {forecast_out}")
    if not clasif_out.exists():
        raise FileNotFoundError(f"No se generó clasificación output: {clasif_out}")

    n_fc = _load_forecast_staging(c, forecast_out)
    n_cls = _load_classification_staging(c, clasif_out)

    return {
        'status': 'ok',
        'rows_input_forecast_ch': n_in_fc,
        'rows_input_clasif_ch': n_in_cls,
        'tabla_stg_forecast': STG_FC,
        'rows_stg_forecast': n_fc,
        'tabla_stg_clasificacion': STG_CLS,
        'rows_stg_clasificacion': n_cls,
        'forecast_output': str(forecast_out),
        'clasificacion_output': str(clasif_out),
    }


@test
def test_output(output, *args):
    assert output['status'] == 'ok'