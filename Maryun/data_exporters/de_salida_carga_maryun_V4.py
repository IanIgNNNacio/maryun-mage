from __future__ import annotations
import sys
from pathlib import Path

MAGE_PROJECT_ROOT = Path('/home/src/Maryun')
if MAGE_PROJECT_ROOT.exists() and str(MAGE_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGE_PROJECT_ROOT))


from datetime import datetime
import re
import uuid
import pandas as pd
import clickhouse_connect  # type: ignore
from mage_ai.io.config import ConfigFileLoader

from utils.v4_bridge import ensure_v4_import_path, resolve_process_date

# rut bien formado: digitos + '-' + (digito o K). Ej: 76363883-9 / 77084730-K
_RUT_OK = re.compile(r'^\d+-[\dkK]$')

TABLA_DESTINO = 'logistica_v2.logistica_salida_carga_maryun'
CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

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
        database=cfg['CLICKHOUSE_LOG_DATABASE'],
        secure=use_https,
    )


def _ensure_destination_schema(client) -> None:
    client.command(f'''
        CREATE TABLE IF NOT EXISTS {TABLA_DESTINO}
        (
            `run_id` String,
            `fecha_generacion` DateTime,
            `sku_id` String,
            `nombre` String,
            `variante` String,
            `homologado_desde_sku` Nullable(String),
            `origen` String,
            `destino` String,
            `capa` String,
            `cantidad` Float64,
            `score` Float64,
            `motivo` String,
            `lead_time_dias` Nullable(Int32),
            `proveedor` Nullable(String),
            `rut_proveedor` Nullable(String),
            `costo_unitario_clp` Nullable(Float64),
            `ahorro_clp` Nullable(Float64),
            `clase_abc_xyz` String,
            `fuente_autom` String
        )
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(fecha_generacion)
        ORDER BY (fecha_generacion, run_id, sku_id, destino)
    ''')
    for statement in (
        f"ALTER TABLE {TABLA_DESTINO} ADD COLUMN IF NOT EXISTS `nombre` String AFTER `sku_id`",
        f"ALTER TABLE {TABLA_DESTINO} ADD COLUMN IF NOT EXISTS `variante` String AFTER `nombre`",
        f"ALTER TABLE {TABLA_DESTINO} ADD COLUMN IF NOT EXISTS `rut_proveedor` Nullable(String) AFTER `proveedor`",
        f"ALTER TABLE {TABLA_DESTINO} ADD COLUMN IF NOT EXISTS `ahorro_clp` Nullable(Float64) AFTER `costo_unitario_clp`",
        f"ALTER TABLE {TABLA_DESTINO} ADD COLUMN IF NOT EXISTS `clase_abc_xyz` String AFTER `ahorro_clp`",
    ):
        client.command(statement)


def _load_rut_map(client) -> dict:
    """Mapa proveedor(rso) -> rut desde el espejo CH de tab_proveedores."""
    m = client.query_df(
        "SELECT rso, rut, ingested_at FROM dwh.mysis_tab_proveedores "
        "WHERE rut != '' AND rso != ''"
    )
    if m is None or m.empty:
        return {}
    m['rso'] = m['rso'].astype(str).str.strip()
    m['rut'] = m['rut'].astype(str).str.strip().str.upper()
    m['valid'] = m['rut'].str.match(_RUT_OK)
    m = m.sort_values(['rso', 'valid', 'ingested_at'], ascending=[True, False, False])
    ambig = m.duplicated('rso', keep=False)
    if ambig.any():
        ej = sorted(set(m.loc[ambig, 'rso']))[:5]
        print(f"[rut] AVISO: {m.loc[ambig, 'rso'].nunique()} proveedores con rut ambiguo "
              f"(se toma bien-formado/mas reciente). Ej: {ej}")
    return m.drop_duplicates('rso', keep='first').set_index('rso')['rut'].to_dict()


def _resolve_run_id(prev: dict, kwargs: dict) -> str:
    rid = (
        kwargs.get('run_id')
        or prev.get('run_id')
        or kwargs.get('pipeline_run_id')
        or kwargs.get('execution_partition')
        or kwargs.get('block_run_id')
    )
    if rid:
        return str(rid)
    return f"manual_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


def _validate_classification_input(classification_df) -> pd.DataFrame:
    if not isinstance(classification_df, pd.DataFrame):
        raise TypeError(
            'de_salida_carga_maryun_V4 esperaba prev["classification"] como DataFrame '
            f'y recibio {type(classification_df).__name__}. '
            'La clasificacion debe venir desde tr_merge_clasificacion_overrides_V4.'
        )
    required = {'sku_id', 'ubicacion', 'clase_final'}
    missing = sorted(required - set(classification_df.columns))
    if classification_df.empty or missing:
        raise ValueError(
            'de_salida_carga_maryun_V4 recibio classification invalida desde upstream. '
            f'rows={len(classification_df)} missing={missing} '
            f'columns={list(classification_df.columns)}. '
            'Reejecuta desde tr_merge_clasificacion_overrides_V4 y limpia variables downstream.'
        )
    return classification_df.copy()


def _classification_match_stats(plan: pd.DataFrame, classification: pd.DataFrame) -> dict:
    if plan.empty:
        return {'plan_rows': 0, 'matched_rows': 0, 'unmatched_rows': 0}
    p = plan[['sku_id', 'destino']].copy()
    c = (
        classification[['sku_id', 'ubicacion', 'clase_final']]
        .dropna(subset=['sku_id', 'ubicacion'])
        .drop_duplicates(['sku_id', 'ubicacion'])
        .rename(columns={'ubicacion': 'destino'})
    )
    merged = p.merge(c, on=['sku_id', 'destino'], how='left')
    matched = int(merged['clase_final'].notna().sum())
    return {
        'plan_rows': int(len(plan)),
        'matched_rows': matched,
        'unmatched_rows': int(len(plan) - matched),
    }


@data_exporter
def export_salida_carga_maryun(prev: dict, **kwargs):
    ensure_v4_import_path()
    from app.export.carga_maryun import build_carga_maryun

    run_id = _resolve_run_id(prev, kwargs)
    process_date = resolve_process_date(kwargs, prev)

    plan = prev.get('plan', pd.DataFrame()).copy()
    if plan.empty:
        rows = 0
    else:
        client = _client()
        autom_src: dict[tuple[str, str], str] = {}
        at = prev.get('automation_table', pd.DataFrame())
        if at is not None and not at.empty:
            for _, r in at.iterrows():
                autom_src[(r['sku_id'], r['ubicacion'])] = 'excel'
        ca = prev.get('classification_audit', pd.DataFrame())
        if ca is not None and not ca.empty and 'clase_automatizacion' in ca.columns:
            for _, r in ca.iterrows():
                k = (r['sku_id'], r['ubicacion'])
                autom_src.setdefault(k, 'classification_v4')

        classification_df = _validate_classification_input(prev.get('classification', pd.DataFrame()))
        match_stats = _classification_match_stats(plan, classification_df)
        print(f'carga_maryun classification upstream rows={len(classification_df)} match_stats={match_stats}')

        carga = build_carga_maryun(
            plan,
            run_id=run_id,
            fecha_generacion=pd.Timestamp(process_date),
            suppliers_df=prev.get('suppliers_df', pd.DataFrame()),
            automation_source_by_pair=autom_src,
            classification_df=classification_df,
            products=prev.get('products', pd.DataFrame()),
        )
        if not carga.empty and carga['clase_abc_xyz'].fillna('N/A').astype(str).eq('N/A').all():
            raise ValueError(
                'carga_maryun quedo con clase_abc_xyz todo N/A aunque classification llego al exporter. '
                f'match_stats={match_stats}. '
                'Esto indica diferencia de llaves entre plan.sku_id/destino y classification.sku_id/ubicacion.'
            )

        # ── Fix: corregir proveedor/lead_time/costo para capa compra ──────────
        # El plan ya trae el proveedor correcto en "origen" (elegido por
        # allocate_compra con MOQ-aware fallback).  build_carga_maryun hace un
        # join independiente que puede asignar metadata de un proveedor distinto.
        # Corregimos: proveedor ← origen (compra), lead_time/costo desde
        # suppliers_df casando por (sku_id, origen, destino) → (sku_id, origen).
        suppliers_df = prev.get('suppliers_df', pd.DataFrame())
        if not suppliers_df.empty and not carga.empty:
            s = suppliers_df[['sku_id', 'ubicacion', 'proveedor', 'lead_time_dias', 'costo_unitario_clp']].copy()
            compra_mask = carga['capa'].eq('compra')
            # proveedor = origen para compra (el plan ya decidio)
            carga.loc[compra_mask, 'proveedor'] = carga.loc[compra_mask, 'origen'].fillna('')
            # Re-resolver lead_time y costo casando por (sku_id, origen, destino) -> (sku_id, origen)
            has_loc = s['ubicacion'].notna()
            s_spec = s[has_loc].rename(columns={'ubicacion': 'destino', 'proveedor': 'origen'}).drop_duplicates(['sku_id', 'origen', 'destino'])
            carga = carga.merge(
                s_spec[['sku_id', 'origen', 'destino', 'lead_time_dias', 'costo_unitario_clp']],
                on=['sku_id', 'origen', 'destino'], how='left', suffixes=('', '_fix'),
            )
            missing = carga['lead_time_dias'].isna()
            if missing.any():
                s_glob = s[~has_loc].rename(columns={'proveedor': 'origen'}).drop_duplicates(['sku_id', 'origen'])
                carga = carga.merge(
                    s_glob[['sku_id', 'origen', 'lead_time_dias', 'costo_unitario_clp']],
                    on=['sku_id', 'origen'], how='left', suffixes=('', '_glb'),
                )
                for col in ('lead_time_dias', 'costo_unitario_clp'):
                    glb_col = f'{col}_glb'
                    if glb_col in carga.columns:
                        carga[col] = carga[col].fillna(carga[glb_col])
                        carga = carga.drop(columns=[glb_col])
            # Limpiar columnas '_fix' que quedaron
            for c in list(carga.columns):
                if c.endswith('_fix'):
                    carga = carga.drop(columns=[c])
            for col, dflt in (('lead_time_dias', 0), ('proveedor', ''), ('costo_unitario_clp', 0.0)):
                if col in carga.columns:
                    carga[col] = carga[col].fillna(dflt)

        cls_rows = len(classification_df) if isinstance(classification_df, pd.DataFrame) else 0
        clase_counts = carga['clase_abc_xyz'].fillna('N/A').astype(str).value_counts().head(10).to_dict()
        print(f'carga_maryun classification rows={cls_rows} clase_abc_xyz_top={clase_counts}')

        # rut_proveedor desde el espejo CH de tab_proveedores (por rso, no name-typing).
        rut_map = _load_rut_map(client)
        carga['rut_proveedor'] = (
            carga['proveedor'].astype('string').str.strip().map(rut_map)
        )
        n_con_rut = int(carga['rut_proveedor'].notna().sum())
        print(f'carga_maryun rut_proveedor: {n_con_rut}/{len(carga)} filas con rut')

        cols = [
            'run_id', 'fecha_generacion', 'sku_id', 'nombre', 'variante',
            'homologado_desde_sku', 'origen', 'destino', 'capa', 'cantidad',
            'score', 'motivo', 'lead_time_dias', 'proveedor', 'rut_proveedor',
            'costo_unitario_clp', 'ahorro_clp', 'clase_abc_xyz', 'fuente_autom',
        ]
        df = carga[cols].copy()
        _ensure_destination_schema(client)
        client.insert_df(TABLA_DESTINO, df)
        rows = int(len(df))

    return {
        **prev,
        'export_status': {
            **prev.get('export_status', {}),
            'de_salida_carga_maryun_V4': {'tabla': TABLA_DESTINO, 'rows_inserted': rows},
        }
    }


@test
def test_output(output, *args):
    assert 'export_status' in output
