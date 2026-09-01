from __future__ import annotations

import sys
from pathlib import Path

MAGE_PROJECT_ROOT = Path('/home/src/Maryun')
if MAGE_PROJECT_ROOT.exists() and str(MAGE_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGE_PROJECT_ROOT))


import pandas as pd
from datetime import timedelta

from utils.v4_bridge import ensure_v4_import_path, load_v4_params, resolve_process_date

SUPPLIER_COLUMNS = [
    'sku_id', 'proveedor', 'ubicacion', 'lead_time_dias',
    'moq', 'multiplo_compra', 'incoterm', 'procedencia',
    'costo_unitario_clp', 'prioridad',
]


def _as_dataframe(value, columns: list[str]) -> pd.DataFrame:
    if isinstance(value, pd.DataFrame):
        return value.copy() if not (value.empty and len(value.columns) == 0) else pd.DataFrame(columns=columns)
    if isinstance(value, (list, tuple)):
        if not value:
            return pd.DataFrame(columns=columns)
        for item in value:
            if isinstance(item, pd.DataFrame):
                return item.copy() if not (item.empty and len(item.columns) == 0) else pd.DataFrame(columns=columns)
        if len(value) >= 2 and isinstance(value[0], (list, tuple)):
            rows = value[0]
            if not rows:
                return pd.DataFrame(columns=columns)
            first = rows[0]
            if isinstance(first, dict):
                return pd.DataFrame(rows)
            if isinstance(first, (list, tuple)):
                return pd.DataFrame(rows, columns=columns[:len(first)])
        first = value[0]
        if isinstance(first, dict):
            return pd.DataFrame(value)
        if isinstance(first, (list, tuple)):
            return pd.DataFrame(value, columns=columns[:len(first)])
    if isinstance(value, dict):
        for key in ('data', 'rows', 'records'):
            if key in value:
                return _as_dataframe(value[key], columns)
    raise TypeError(f'dl_proveedores_V4 debe ser DataFrame/list/dict; recibido {type(value).__name__}')


if 'transformer' not in globals():
    from mage_ai.data_preparation.decorators import transformer
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test


@transformer
def tr_needs_V4(
    dl_automation_sku_sucursal_V4: pd.DataFrame,
    dl_silencio_V4: pd.DataFrame,
    dl_proveedores_V4: pd.DataFrame,
    prev: dict,
    **kwargs
) -> dict:
    ensure_v4_import_path()
    from app.automation.filter import apply_automation_filter
    from app.needs.calculator import build_needs_audit, compute_needs
    from app.normalize.canonical import canonical_location, canonical_sku
    from app.policies.suppliers import SupplierInfo, SupplierTable
    from app.silence.registry import apply_silence_filter

    # Monkey-patch: info() respeta prioridad sobre especificidad de ubicacion.
    # Un proveedor prioridad 1 con solo default le gana a uno prioridad 2 con
    # entrada especifica para la sucursal.  all_suppliers() ya fusiona correctamente.
    if not getattr(SupplierTable.info, '__patched_v4__', False):
        _info_orig = SupplierTable.info
        def _info_fixed(self, sku_id, ubicacion=None):
            suppliers = self.all_suppliers(sku_id, ubicacion)
            return suppliers[0] if suppliers else None
        _info_fixed.__patched_v4__ = True
        SupplierTable.info = _info_fixed

    params = load_v4_params()
    process_date = resolve_process_date(kwargs, prev)

    suppliers_src = _as_dataframe(dl_proveedores_V4, SUPPLIER_COLUMNS)
    if not suppliers_src.empty:
        suppliers_src.columns = [str(c).strip().lower() for c in suppliers_src.columns]
    for col in SUPPLIER_COLUMNS:
        if col not in suppliers_src.columns:
            suppliers_src[col] = pd.NA

    suppliers_src['sku_id'] = suppliers_src['sku_id'].map(canonical_sku)
    suppliers_src['proveedor'] = suppliers_src['proveedor'].astype('string').str.strip()
    suppliers_src['lead_time_dias'] = pd.to_numeric(
        suppliers_src['lead_time_dias'], errors='coerce'
    ).fillna(0).astype(int)
    suppliers_src['ubicacion'] = suppliers_src['ubicacion'].apply(
        lambda v: canonical_location(str(v)) if pd.notna(v) and str(v).strip() != '' else None
    )
    suppliers_src['moq'] = pd.to_numeric(suppliers_src['moq'], errors='coerce').fillna(0.0).astype(float)
    suppliers_src['multiplo_compra'] = pd.to_numeric(
        suppliers_src['multiplo_compra'], errors='coerce'
    ).fillna(1.0).astype(float)
    suppliers_src['incoterm'] = suppliers_src['incoterm'].astype('string').fillna('').str.strip()
    suppliers_src['procedencia'] = suppliers_src['procedencia'].astype('string').fillna('').str.strip()
    suppliers_src['costo_unitario_clp'] = pd.to_numeric(
        suppliers_src['costo_unitario_clp'], errors='coerce'
    ).fillna(0.0).astype(float)
    suppliers_src['prioridad'] = pd.to_numeric(
        suppliers_src['prioridad'], errors='coerce'
    ).fillna(1).astype(int)
    suppliers_src = (
        suppliers_src
        .dropna(subset=['sku_id', 'proveedor'])
        .sort_values(['sku_id', 'prioridad'])
        .reset_index(drop=True)
    )
    by_sku_loc = {}
    for r in suppliers_src.itertuples(index=False):
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
    suppliers = SupplierTable(df=suppliers_src, by_sku_loc=by_sku_loc)

    needs = compute_needs(
        forecast=prev['forecast'],
        stock=prev['stock'],
        params=params,
        classification=prev['classification'],
        demand_history=prev['demand'],
        suppliers=suppliers,
        ultima_venta=prev.get('ultima_venta'),   # <<< NUEVO — regla de recencia de venta
        process_date=process_date,                # <<< NUEVO
    )

    protected_donors = set()
    if not needs.empty and 'necesidad' in needs.columns:
        protected_donors = {
            (canonical_sku(str(r.sku_id)), canonical_location(str(r.ubicacion)))
            for r in needs.itertuples(index=False)
            if float(r.necesidad) > 0
        }

    automation = dl_automation_sku_sucursal_V4.copy()
    if not automation.empty:
        automation.columns = [str(c).strip().lower() for c in automation.columns]
        for c in ['sku_id', 'ubicacion', 'automatizar']:
            if c not in automation.columns:
                automation[c] = pd.NA
        automation = automation[['sku_id', 'ubicacion', 'automatizar']]
        automation['sku_id'] = automation['sku_id'].map(canonical_sku)
        automation['ubicacion'] = automation['ubicacion'].map(canonical_location)

    needs = apply_automation_filter(
        needs,
        automation,
        classification_audit=prev.get('classification_audit', pd.DataFrame()),
    )

    silenced: set[tuple[str, str]] = set()
    silencio = dl_silencio_V4.copy()
    if not silencio.empty:
        silencio.columns = [str(c).strip().lower() for c in silencio.columns]
        if {'sku_id', 'ubicacion', 'last_suggested_date'}.issubset(silencio.columns):
            s = silencio.copy()
            s['last_suggested_date'] = pd.to_datetime(s['last_suggested_date'], errors='coerce').dt.date
            cutoff = (pd.Timestamp(process_date) - pd.Timedelta(days=int(params.silence.window_days))).date()
            s = s[s['last_suggested_date'] >= cutoff]
            silenced = {
                (canonical_sku(r.sku_id), canonical_location(r.ubicacion))
                for r in s.itertuples(index=False)
            }

    needs, silenced_rows = apply_silence_filter(needs, silenced)

    return {
        **prev,
        'needs': needs,
        'needs_audit': build_needs_audit(needs),
        'protected_donors': [[sku, ubic] for sku, ubic in sorted(protected_donors)],
        'automation_table': automation,
        'silence_silenced_rows': silenced_rows,
        'process_date': process_date.isoformat(),
        'suppliers_needs_df': suppliers.df,
    }


@test
def test_output(output, *args):
    assert 'needs' in output
