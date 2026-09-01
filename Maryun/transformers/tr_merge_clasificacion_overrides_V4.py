from __future__ import annotations

import sys
from pathlib import Path

MAGE_PROJECT_ROOT = Path('/home/src/Maryun')
if MAGE_PROJECT_ROOT.exists() and str(MAGE_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGE_PROJECT_ROOT))


import pandas as pd

from utils.v4_bridge import ensure_v4_import_path, load_v4_params, resolve_process_date

if 'transformer' not in globals():
    from mage_ai.data_preparation.decorators import transformer
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test


def _as_dataframe(value, columns: list[str]) -> pd.DataFrame:
    if isinstance(value, pd.DataFrame):
        if value.empty and len(value.columns) == 0:
            return pd.DataFrame(columns=columns)
        return value.copy()
    if isinstance(value, (list, tuple)):
        if not value:
            return pd.DataFrame(columns=columns)
        for item in value:
            if isinstance(item, pd.DataFrame):
                if item.empty and len(item.columns) == 0:
                    return pd.DataFrame(columns=columns)
                return item.copy()
        first = value[0]
        if isinstance(first, dict):
            return pd.DataFrame(value)
        if isinstance(first, (list, tuple)):
            return pd.DataFrame(value, columns=columns[: len(first)])
    return pd.DataFrame(columns=columns)


def _expand_classification_if_needed(raw_cls: pd.DataFrame, products: pd.DataFrame) -> pd.DataFrame:
    raw = raw_cls.copy()
    raw.columns = [str(c).strip().lower() for c in raw.columns]

    if 'sku_id' not in raw.columns:
        raw['sku_id'] = pd.NA
    raw['sku_id'] = raw['sku_id'].fillna('').astype(str).str.strip()

    product_skus = set(products.get('sku_id', pd.Series(dtype=str)).astype(str).str.strip())
    direct_overlap = int(raw['sku_id'].isin(product_skus).sum()) if product_skus else 0
    if direct_overlap > 0:
        print(f'classification V4 usa sku_id directo overlap_products={direct_overlap}')
        return raw

    if 'sku_3_0' not in raw.columns or 'ubicacion' not in raw.columns:
        return raw
    if products is None or products.empty or 'nombre' not in products.columns:
        return raw

    # Proceso V4: SKU 3.0 contiene nombre_producto + sucursal; se extrae el
    # nombre base y se expande a variantes/SKU reales cruzando products.nombre.
    work = raw.copy()
    work['sku_3_0'] = work['sku_3_0'].fillna('').astype(str).str.strip()
    work['ubicacion_raw'] = work['ubicacion'].fillna('').astype(str).str.strip()

    def _nombre_base(row: pd.Series) -> str | None:
        sku3 = row['sku_3_0']
        suc = row['ubicacion_raw']
        if sku3 and suc and sku3.endswith(suc):
            base = sku3[: -len(suc)].strip()
            return base if base else None
        return None

    work['nombre_base'] = work.apply(_nombre_base, axis=1)
    work = work[work['nombre_base'].notna()].copy()
    if work.empty:
        return raw

    work['_nombre_upper'] = work['nombre_base'].astype(str).str.strip().str.upper()
    # Dedup por MEJOR ABC ante (nombre_base, sucursal) duplicados, igual que
    # app/classification/v3_loader.load_v3_classification (sort por _abc_rank +
    # drop_duplicates keep='first'). Sin esto el resultado ante duplicados es
    # arbitrario y puede diferir del proceso local.
    work['_ubic_upper'] = work['ubicacion_raw'].astype(str).str.strip().str.upper()
    if 'abc_modelo' in work.columns:
        _abc_rank = {'A': 0, 'B': 1, 'C': 2}
        work['_abc_rank'] = (
            work['abc_modelo'].astype(str).str.strip().str.upper().map(_abc_rank).fillna(9)
        )
        work = (
            work.sort_values('_abc_rank')
                .drop_duplicates(['_nombre_upper', '_ubic_upper'], keep='first')
                .drop(columns=['_abc_rank'])
        )
    prod = products[['sku_id', 'nombre']].copy()
    prod['_nombre_upper'] = prod['nombre'].astype(str).str.strip().str.upper()
    prod = prod.dropna(subset=['sku_id']).drop_duplicates(['sku_id', '_nombre_upper'])

    expanded = prod.merge(work.drop(columns=['sku_id'], errors='ignore'), on='_nombre_upper', how='inner')
    if expanded.empty:
        return raw
    expanded = expanded.drop(columns=['_nombre_upper', 'nombre'], errors='ignore')
    print(f'classification V4 expandida desde sku_3_0 rows_raw={len(raw)} rows_expanded={len(expanded)}')
    return expanded


def _normalize_forecast_overrides(raw, canonical_sku, canonical_location) -> pd.DataFrame:
    """Mismo contrato que app.overrides.loader.load_forecast_overrides, pero
    desde ClickHouse en vez de Excel. Columnas: sku_id, ubicacion, mes,
    forecast_override, motivo, responsable."""
    cols = ['sku_id', 'ubicacion', 'mes', 'forecast_override', 'motivo', 'responsable']
    if not isinstance(raw, pd.DataFrame) or raw.empty:
        return pd.DataFrame(columns=cols)
    df = raw.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    if 'sku' in df.columns and 'sku_id' not in df.columns:
        df = df.rename(columns={'sku': 'sku_id'})
    for c in cols:
        if c not in df.columns:
            df[c] = pd.NA
    df['sku_id'] = df['sku_id'].map(canonical_sku)
    df['ubicacion'] = df['ubicacion'].map(canonical_location)
    df['mes'] = pd.to_datetime(df['mes'], errors='coerce')
    df['forecast_override'] = pd.to_numeric(df['forecast_override'], errors='coerce')
    return df[cols]


def _normalize_classification_overrides(raw, canonical_sku, canonical_location) -> pd.DataFrame:
    """Mismo contrato que app.overrides.loader.load_classification_overrides,
    pero desde ClickHouse. Columnas: sku_id, ubicacion, abc_override,
    xyz_override, motivo, responsable."""
    cols = ['sku_id', 'ubicacion', 'abc_override', 'xyz_override', 'motivo', 'responsable']
    if not isinstance(raw, pd.DataFrame) or raw.empty:
        return pd.DataFrame(columns=cols)
    df = raw.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    if 'sku' in df.columns and 'sku_id' not in df.columns:
        df = df.rename(columns={'sku': 'sku_id'})
    for c in cols:
        if c not in df.columns:
            df[c] = pd.NA
    df['sku_id'] = df['sku_id'].map(canonical_sku)
    df['ubicacion'] = df['ubicacion'].map(canonical_location)
    for c in ('abc_override', 'xyz_override'):
        df[c] = df[c].astype('string').str.strip().str.upper()
    return df[cols]


@transformer
def tr_merge_clasificacion_overrides_V4(
    dl_override_forecast_V4: pd.DataFrame,
    dl_override_classification_V4: pd.DataFrame,
    prev: dict,
    dl_clasificacion_precomputada_V4: pd.DataFrame,
    **kwargs
) -> dict:
    v3_root = ensure_v4_import_path()
    from app.config.paths import build_paths
    from app.config.settings import Settings
    from app.normalize.canonical import canonical_location, canonical_sku
    from app.overrides.resolver import (
        apply_classification_overrides,
        apply_forecast_overrides,
        build_overrides_audit,
    )
    from app.temporal.gate import build_temporal_gate

    process_date = resolve_process_date(kwargs, prev)

    base_settings = Settings()
    settings = base_settings.model_copy(
        update={
            'run': base_settings.run.model_copy(update={
                'run_classification': True,
                'run_forecast': False,
                'source': 'mariadb',
            }),
        }
    )
    params = load_v4_params()
    paths = build_paths(settings=settings, project_root=v3_root)
    gate = build_temporal_gate(process_date, params)

    raw_cls = _as_dataframe(dl_clasificacion_precomputada_V4, [
        'sku_3_0', 'sku_id', 'ubicacion', 'abc_modelo', 'xyz_modelo', 'clase_final',
        'score_automatizacion', 'clase_automatizacion',
    ])
    if not raw_cls.empty:
        print(f'classification V4 desde ClickHouse rows={len(raw_cls)}')
    else:
        raise ValueError(
            'logistica_v2.logistica_clasificacion_precomputada no entrego filas vigentes. '
            'Mage no debe leer el Excel directamente; carga en ClickHouse la clasificacion '
            'equivalente a RESULTADO_ESTRATEGICO_ABC_XYZ_v4 antes de correr.'
        )

    if not raw_cls.empty:
        raw_cls = _expand_classification_if_needed(raw_cls, prev.get('products', pd.DataFrame()))
        raw_cls.columns = [str(c).strip().lower() for c in raw_cls.columns]
        for c in ['sku_id', 'ubicacion', 'abc_modelo', 'xyz_modelo', 'clase_final']:
            if c not in raw_cls.columns:
                raw_cls[c] = pd.NA
        raw_cls['sku_id'] = raw_cls['sku_id'].map(canonical_sku)
        raw_cls['ubicacion'] = raw_cls['ubicacion'].map(canonical_location)
        raw_cls['abc_modelo'] = raw_cls['abc_modelo'].fillna('').astype(str).str.upper().str.strip()
        raw_cls['xyz_modelo'] = raw_cls['xyz_modelo'].fillna('').astype(str).str.upper().str.strip()
        raw_cls['clase_final'] = raw_cls['clase_final'].fillna('').astype(str).str.upper().str.strip()
        raw_cls['clase_modelo'] = raw_cls.get('clase_modelo', raw_cls['clase_final']).fillna(raw_cls['clase_final']).astype(str).str.upper().str.strip()
        if 'score_automatizacion' not in raw_cls.columns:
            raw_cls['score_automatizacion'] = pd.NA
        if 'clase_automatizacion' not in raw_cls.columns:
            raw_cls['clase_automatizacion'] = pd.NA
        raw_cls['fuente_clasificacion'] = raw_cls.get('fuente_clasificacion', 'v3_estrategico')
        raw_cls['clase_fue_forzada'] = raw_cls.get('clase_fue_forzada', False)
        raw_cls['abc_override'] = raw_cls.get('abc_override', pd.NA)
        raw_cls['xyz_override'] = raw_cls.get('xyz_override', pd.NA)
        raw_cls['motivo_override'] = raw_cls.get('motivo_override', pd.NA)
        raw_cls['responsable_override'] = raw_cls.get('responsable_override', pd.NA)
        classification = raw_cls[[
            'sku_id', 'ubicacion', 'abc_modelo', 'xyz_modelo', 'clase_modelo',
            'abc_override', 'xyz_override', 'clase_final', 'clase_fue_forzada',
            'motivo_override', 'responsable_override',
            'score_automatizacion', 'clase_automatizacion',
            'fuente_clasificacion',
        ]].dropna(subset=['sku_id', 'ubicacion']).drop_duplicates(['sku_id', 'ubicacion'])
        classification_audit = classification.copy()
        # IMPORTANTE: el gate de automatizacion en base usa el ENGINE
        # (run_classification_engine sobre la demanda), NO la clasificacion v3.
        # apply_automation_filter consume classification_audit['clase_automatizacion'].
        # Reproducimos el engine aqui para que el filtrado de needs sea identico a base.
        try:
            from app.classification.engine import run_classification_engine
            _dem = prev.get('demand', pd.DataFrame())
            _prod = prev.get('products', pd.DataFrame())
            if not _dem.empty and not _prod.empty:
                _, eng_audit = run_classification_engine(_dem, _prod, gate, params)
                if eng_audit is not None and not eng_audit.empty and 'clase_automatizacion' in eng_audit.columns:
                    eng_audit = eng_audit.copy()
                    eng_audit['sku_id'] = eng_audit['sku_id'].map(canonical_sku)
                    eng_audit['ubicacion'] = eng_audit['ubicacion'].map(canonical_location)
                    classification_audit = eng_audit
                    print(f'classification_audit = ENGINE rows={len(eng_audit)} '
                          f"clase_autom={eng_audit['clase_automatizacion'].value_counts().head().to_dict()}")
        except Exception as exc:
            print('engine audit fallback (usa v3 para el gate):', exc)
        print(
            'classification V4 normalizada '
            f'rows={len(classification)} '
            f"clases={classification['clase_final'].value_counts().head(10).to_dict()}"
        )
    # Overrides desde ClickHouse (NO desde Excel). Se normalizan EXACTAMENTE
    # igual que los loaders Excel (load_forecast_overrides / load_classification_overrides)
    # para que el merge por (sku_id, ubicacion[, mes]) calce: canonicalizar llaves,
    # mes->datetime, abc/xyz en mayuscula. Sin esto el override no se aplica.
    fc_ov = _normalize_forecast_overrides(dl_override_forecast_V4, canonical_sku, canonical_location)
    cls_ov = _normalize_classification_overrides(dl_override_classification_V4, canonical_sku, canonical_location)

    forecast = apply_forecast_overrides(prev['forecast'], fc_ov, params)
    # Igual que el runner local: SIEMPRE se llama (override vacio -> clase_final = clase_modelo).
    classification = apply_classification_overrides(classification, cls_ov, params)
    overrides_audit = build_overrides_audit(forecast, classification)

    return {
        **prev,
        'forecast': forecast,
        'classification': classification,
        'classification_audit': classification_audit,
        'overrides_audit': overrides_audit,
        'process_date': process_date.isoformat(),
    }


@test
def test_output(output, *args):
    assert 'classification' in output and 'forecast' in output