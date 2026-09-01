from __future__ import annotations

from dataclasses import dataclass
import sys
from pathlib import Path

MAGE_PROJECT_ROOT = Path('/home/src/Maryun')
if MAGE_PROJECT_ROOT.exists() and str(MAGE_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGE_PROJECT_ROOT))


import pandas as pd

from utils.v4_bridge import ensure_v4_import_path, load_v4_params, resolve_process_date


@dataclass(frozen=True)
class HomologacionTable:
    df: pd.DataFrame
    pairs: list[tuple[str, str]]
    factor: dict[tuple[str, str], float]
    analitico: set[tuple[str, str]]
    operacional: set[tuple[str, str]]

    def is_empty(self) -> bool:
        return self.df.empty


def _homologacion_from_payload(payload: dict | None) -> HomologacionTable | None:
    if not payload:
        return None
    pairs = [tuple(x) for x in payload.get('pairs', [])]
    factor = {
        (r['sku_id_importado'], r['sku_id_nacional']): float(r['factor_conversion'])
        for r in payload.get('factor', [])
    }
    return HomologacionTable(
        df=pd.DataFrame(payload.get('rows', [])),
        pairs=pairs,
        factor=factor,
        analitico={tuple(x) for x in payload.get('analitico', [])},
        operacional={tuple(x) for x in payload.get('operacional', [])},
    )


def _as_dataframe(value, name: str, columns: list[str]) -> pd.DataFrame:
    """Mage SQL loaders can pass a DataFrame directly or wrap it in a list."""
    if isinstance(value, pd.DataFrame):
        if value.empty and len(value.columns) == 0:
            return pd.DataFrame(columns=columns)
        return value.copy()

    def _metadata_columns(metadata) -> list[str] | None:
        if isinstance(metadata, dict):
            raw = metadata.get('columns') or metadata.get('column_names') or metadata.get('schema')
        else:
            raw = metadata
        if not isinstance(raw, (list, tuple)):
            return None
        names = []
        for item in raw:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict):
                names.append(item.get('name') or item.get('column') or item.get('column_name'))
            elif isinstance(item, (list, tuple)) and item:
                names.append(item[0])
            else:
                names.append(None)
        if names and all(name is not None for name in names):
            return [str(name) for name in names]
        return None

    if isinstance(value, (list, tuple)):
        if not value:
            return pd.DataFrame(columns=columns)
        for item in value:
            if isinstance(item, pd.DataFrame):
                if item.empty and len(item.columns) == 0:
                    return pd.DataFrame(columns=columns)
                return item.copy()

        if len(value) >= 2 and isinstance(value[0], (list, tuple)):
            rows = value[0]
            if not rows:
                return pd.DataFrame(columns=_metadata_columns(value[1]) or columns)
            first_row = rows[0] if isinstance(rows, (list, tuple)) else None
            if isinstance(first_row, dict):
                return pd.DataFrame(rows)
            if isinstance(first_row, (list, tuple)):
                inferred = _metadata_columns(value[1]) or columns[: len(first_row)]
                return pd.DataFrame(rows, columns=inferred)

        first = value[0]
        if isinstance(first, dict):
            return pd.DataFrame(value)
        if isinstance(first, (list, tuple)):
            return pd.DataFrame(value, columns=columns[: len(first)])

    if isinstance(value, dict):
        for key in ('data', 'rows', 'records'):
            if key in value:
                return _as_dataframe(value[key], f'{name}.{key}', columns)

    raise TypeError(f'{name} debe ser DataFrame; recibido {type(value).__name__}')


def _require_columns(df: pd.DataFrame, name: str, columns: list[str]) -> None:
    missing = [col for col in columns if col not in df.columns]
    if missing:
        raise KeyError(
            f'{name} sin columnas requeridas {missing}. '
            f'Columnas recibidas: {list(df.columns)}'
        )


def _normalize_columns(df: pd.DataFrame, name: str) -> pd.DataFrame:
    df = df.copy()
    if df.empty and len(df.columns) == 0:
        return df
    df.columns = [str(col).strip().lower() for col in df.columns]
    if df.columns.duplicated().any():
        duplicates = df.columns[df.columns.duplicated()].unique().tolist()
        df = df.loc[:, ~df.columns.duplicated()].copy()
        print(f'{name}: columnas duplicadas descartadas {duplicates}')
    return df



if 'transformer' not in globals():
    from mage_ai.data_preparation.decorators import transformer
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test


@transformer
def tr_replenishment_engine_V4(
    dl_distancias_V4: pd.DataFrame,
    dl_costos_transportes_V4: pd.DataFrame,
    dl_prioridad_cd_V4: pd.DataFrame,
    dl_proveedores_V4: pd.DataFrame,
    dl_reglas_sku_sucursal_V4: pd.DataFrame,
    prev: dict,
    **kwargs,
) -> dict:
    ensure_v4_import_path()
    from app.homologation.audit import build_resumen_homologacion, build_transferencias_demanda
    from app.homologation.operational import apply_operational_substitution, compute_substitution_candidates
    from app.normalize.canonical import canonical_location, canonical_sku, is_cd
    from app.policies.costs import CostMatrix
    from app.policies.distances import DistanceMatrix
    from app.policies.priority_cd import CDPriority
    from app.policies.rules import Rule, SkuLocationRules
    from app.policies.suppliers import SupplierInfo, SupplierTable
    from app.replenishment.engine import run_replenishment

    params = load_v4_params()
    process_date = resolve_process_date(kwargs, prev)

    needs = prev['needs'].copy()
    stock = prev['stock'].copy()

    hom_table = _homologacion_from_payload(prev.get('homologacion_payload'))
    transferencias_demanda = pd.DataFrame()
    resumen_homologacion = pd.DataFrame()
    homologated_from = {}
    if hom_table is not None and not hom_table.is_empty():
        candidates = compute_substitution_candidates(needs, stock, hom_table, params)
        needs, sub_audit = apply_operational_substitution(needs, candidates)
        transferencias_demanda = build_transferencias_demanda(sub_audit)
        resumen_homologacion = build_resumen_homologacion(prev.get('homologacion_audit', pd.DataFrame()), sub_audit)
        if not sub_audit.empty:
            homologated_from = {
                (r['sku_id_nacional'], r['ubicacion']): r['sku_id_importado']
                for _, r in sub_audit.iterrows()
            }

    d = _as_dataframe(dl_distancias_V4, 'dl_distancias_V4', ['origen', 'destino', 'km'])
    d = _normalize_columns(d, 'dl_distancias_V4')
    _require_columns(d, 'dl_distancias_V4', ['origen', 'destino', 'km'])
    d['origen'] = d['origen'].map(canonical_location)
    d['destino'] = d['destino'].map(canonical_location)
    d['km'] = pd.to_numeric(d['km'], errors='coerce').astype(float)
    d = d.dropna(subset=['origen', 'destino', 'km'])
    dlookup = {(r.origen, r.destino): float(r.km) for r in d.itertuples(index=False)}
    for (a, b), v in list(dlookup.items()):
        dlookup.setdefault((b, a), v)
    distances = DistanceMatrix(df=d.reset_index(drop=True), lookup=dlookup)

    c = _as_dataframe(dl_costos_transportes_V4, 'dl_costos_transportes_V4', ['origen', 'destino', 'costo_clp_por_unidad'])
    c = _normalize_columns(c, 'dl_costos_transportes_V4')
    _require_columns(c, 'dl_costos_transportes_V4', ['origen', 'destino', 'costo_clp_por_unidad'])
    c['origen'] = c['origen'].map(canonical_location)
    c['destino'] = c['destino'].map(canonical_location)
    c['costo_clp_por_unidad'] = pd.to_numeric(c['costo_clp_por_unidad'], errors='coerce').astype(float)
    c = c.dropna(subset=['origen', 'destino', 'costo_clp_por_unidad'])
    clookup = {(r.origen, r.destino): float(r.costo_clp_por_unidad) for r in c.itertuples(index=False)}
    for (a, b), v in list(clookup.items()):
        clookup.setdefault((b, a), v)
    costs = CostMatrix(df=c.reset_index(drop=True), lookup=clookup)

    p = _as_dataframe(dl_prioridad_cd_V4, 'dl_prioridad_cd_V4', ['ubicacion', 'cd', 'prioridad'])
    p = _normalize_columns(p, 'dl_prioridad_cd_V4')
    _require_columns(p, 'dl_prioridad_cd_V4', ['ubicacion', 'cd', 'prioridad'])
    p['ubicacion'] = p['ubicacion'].map(canonical_location)
    p['cd'] = p['cd'].map(canonical_location)
    p['prioridad'] = pd.to_numeric(p['prioridad'], errors='coerce').astype('Int64')
    p = p.dropna(subset=['ubicacion', 'cd', 'prioridad'])
    cd_mask = p['cd'].map(is_cd).fillna(False).astype(bool)
    p = p.loc[cd_mask].copy()
    _require_columns(p, 'dl_prioridad_cd_V4 antes de ordenar', ['ubicacion', 'prioridad'])
    ordered = {}
    for suc, sub in p.sort_values(['ubicacion', 'prioridad']).groupby('ubicacion', sort=False):
        ordered[suc] = list(dict.fromkeys(sub['cd'].tolist()))
    priority_cd = CDPriority(df=p.reset_index(drop=True), ordered=ordered)

    s = _as_dataframe(dl_proveedores_V4, 'dl_proveedores_V4', [
        'sku_id', 'proveedor', 'ubicacion', 'lead_time_dias',
        'moq', 'multiplo_compra', 'incoterm', 'procedencia',
        'costo_unitario_clp', 'prioridad',
    ])
    s = _normalize_columns(s, 'dl_proveedores_V4')
    _require_columns(s, 'dl_proveedores_V4', ['sku_id', 'proveedor', 'lead_time_dias'])
    s['sku_id'] = s['sku_id'].map(canonical_sku)
    s['proveedor'] = s['proveedor'].astype('string').str.strip()
    s['lead_time_dias'] = pd.to_numeric(s['lead_time_dias'], errors='coerce').fillna(0).astype(int)
    s['ubicacion'] = s['ubicacion'].apply(lambda v: canonical_location(str(v)) if pd.notna(v) and str(v).strip() != '' else None)
    s['moq'] = pd.to_numeric(s.get('moq', 0.0), errors='coerce').fillna(0.0).astype(float)
    s['multiplo_compra'] = pd.to_numeric(s.get('multiplo_compra', 1.0), errors='coerce').fillna(1.0).astype(float)
    s['incoterm'] = s.get('incoterm', '').astype('string').fillna('').str.strip()
    s['procedencia'] = s.get('procedencia', '').astype('string').fillna('').str.strip()
    s['costo_unitario_clp'] = pd.to_numeric(s.get('costo_unitario_clp', 0.0), errors='coerce').fillna(0.0).astype(float)
    s['prioridad'] = pd.to_numeric(s.get('prioridad', 1), errors='coerce').fillna(1).astype(int)
    s = s.dropna(subset=['sku_id', 'proveedor']).sort_values(['sku_id', 'prioridad'])
    by_sku_loc = {}
    for r in s.itertuples(index=False):
        key = (r.sku_id, r.ubicacion)
        info = SupplierInfo(
            proveedor=str(r.proveedor),
            lead_time_dias=int(r.lead_time_dias),
            moq=float(r.moq),
            multiplo_compra=float(r.multiplo_compra),
            incoterm=str(r.incoterm or ''),
            procedencia=str(r.procedencia or ''),
            costo_unitario_clp=float(r.costo_unitario_clp),
            prioridad=int(r.prioridad),
            ubicacion=r.ubicacion,
        )
        by_sku_loc.setdefault(key, []).append(info)
    suppliers = SupplierTable(df=s.reset_index(drop=True), by_sku_loc=by_sku_loc)

    rr = _as_dataframe(dl_reglas_sku_sucursal_V4, 'dl_reglas_sku_sucursal_V4', [
        'sku_id', 'ubicacion', 'bloqueado', 'stock_minimo',
        'stock_maximo', 'solo_desde_cd', 'nota',
    ])
    rr = _normalize_columns(rr, 'dl_reglas_sku_sucursal_V4')
    _require_columns(rr, 'dl_reglas_sku_sucursal_V4', ['sku_id', 'ubicacion'])
    rr['sku_id'] = rr['sku_id'].map(canonical_sku)
    rr['ubicacion'] = rr['ubicacion'].map(canonical_location)
    for col in ['bloqueado', 'stock_minimo', 'stock_maximo', 'solo_desde_cd', 'nota']:
        if col not in rr.columns:
            rr[col] = pd.NA
    by_pair = {}
    truthy = {'true', '1', 'yes', 'si', 'sí', 'y', 't', 'x', '✓'}
    for r in rr.itertuples(index=False):
        solo_cd = canonical_location(r.solo_desde_cd) if isinstance(r.solo_desde_cd, str) and r.solo_desde_cd.strip() else None
        bloqueado = str(r.bloqueado).strip().lower() in truthy if pd.notna(r.bloqueado) else False
        def _f(v):
            try:
                x = float(v)
                return None if pd.isna(x) else x
            except Exception:
                return None
        by_pair[(r.sku_id, r.ubicacion)] = Rule(
            bloqueado=bloqueado,
            stock_minimo=_f(r.stock_minimo),
            stock_maximo=_f(r.stock_maximo),
            solo_desde_cd=solo_cd,
            nota=str(r.nota) if pd.notna(r.nota) else '',
        )
    rules = SkuLocationRules(df=rr[['ubicacion', 'bloqueado', 'stock_minimo', 'stock_maximo', 'solo_desde_cd', 'nota']].reset_index(drop=True), by_pair=by_pair)

    fc = prev.get('forecast', pd.DataFrame()).copy()
    if not fc.empty and 'mes' in fc.columns:
        fc['mes'] = pd.to_datetime(fc['mes'], errors='coerce')
        mes_actual = fc['mes'].min()
        demanda_mes_actual = (
            fc[fc['mes'] == mes_actual]
            .groupby(['sku_id', 'ubicacion'], as_index=False)['forecast_final']
            .sum()
            .rename(columns={'forecast_final': 'demanda_mes_actual'})
        )
    else:
        demanda_mes_actual = pd.DataFrame(columns=['sku_id', 'ubicacion', 'demanda_mes_actual'])

    uv = prev.get('ultima_venta', pd.DataFrame())
    if isinstance(uv, list):
        uv = pd.DataFrame(uv)
    uv = uv.copy() if isinstance(uv, pd.DataFrame) else pd.DataFrame()
    if not uv.empty and {'sku_id', 'ubicacion', 'ultima_venta'}.issubset(uv.columns):
        uv['sku_id'] = uv['sku_id'].astype(str)
        uv['ubicacion'] = uv['ubicacion'].astype(str)
        uv['ultima_venta'] = pd.to_datetime(uv['ultima_venta'], errors='coerce')
        uv = uv.dropna(subset=['ultima_venta'])
    else:
        uv = pd.DataFrame(columns=['sku_id', 'ubicacion', 'ultima_venta'])
    print(f'[ultima_venta] tipo_in={type(prev.get("ultima_venta")).__name__} '
          f'rows={len(uv)} dtype={uv["ultima_venta"].dtype if not uv.empty else "NA"}')

    result = run_replenishment(
        needs=needs,
        stock=stock,
        distances=distances,
        costs=costs,
        priority_cd=priority_cd,
        suppliers=suppliers,
        rules=rules,
        params=params,
        homologated_from=homologated_from,
        ultima_venta=uv,
        demanda_mes_actual=demanda_mes_actual,
        process_date=process_date,
        protected={tuple(x) for x in prev.get('protected_donors', [])},
    )

    return {
        **prev,
        'needs': needs,
        'plan': result.plan,
        'plan_audit': result.audit,
        'transferencias_demanda': transferencias_demanda,
        'resumen_homologacion': resumen_homologacion,
        'resumen_replenishment': result.resumen,
        'demanda_mes_actual': demanda_mes_actual,
        'suppliers_df': suppliers.df,
        'distancias': d,
        'costos': c,
        'prioridad_cd': p,
        'reglas': rr,
    }


@test
def test_output(output, *args):
    assert 'plan' in output
