from __future__ import annotations
from datetime import datetime
import uuid
import pandas as pd

if 'transformer' not in globals():
    from mage_ai.data_preparation.decorators import transformer
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test


def _sku(s):
    s = '' if s is None else str(s).strip()
    return (s.lstrip('0') or '0') if s.isdigit() else s.upper()

def _loc(s):
    return '' if s is None else str(s).strip().upper()

def _run_id_from_kwargs(kwargs: dict) -> str:
    rid = (
        kwargs.get('run_id')
        or kwargs.get('pipeline_run_id')
        or kwargs.get('execution_partition')
        or kwargs.get('block_run_id')
    )
    if rid:
        return str(rid)
    return f"manual_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


@transformer
def tr_merge_extract_base_V4(
    dl_productos_sql_tab_sku_tab_familias_tab_marcas_tab_tipos: pd.DataFrame,
    dl_stock_sql_almacenaje_tab_bodegas_tab_sku: pd.DataFrame,
    dl_demanda_ventas_mysis: pd.DataFrame,
    dl_ultima_venta_V4: pd.DataFrame,
    **kwargs
) -> dict:
    products = dl_productos_sql_tab_sku_tab_familias_tab_marcas_tab_tipos.copy()
    stock = dl_stock_sql_almacenaje_tab_bodegas_tab_sku.copy()
    demand = dl_demanda_ventas_mysis.copy()
    ultima_venta = dl_ultima_venta_V4.copy()

    print('products cols:', dl_productos_sql_tab_sku_tab_familias_tab_marcas_tab_tipos.columns.tolist())
    print('stock cols:', dl_stock_sql_almacenaje_tab_bodegas_tab_sku.columns.tolist())
    print('demand cols:', dl_demanda_ventas_mysis.columns.tolist())

    products['sku_id'] = products['sku_id'].map(_sku)
    for col in ('nombre', 'variante', 'color', 'talla'):
        if col not in products.columns:
            products[col] = ''
        products[col] = products[col].fillna('').astype(str).str.strip()
    if 'procedencia' in products.columns:
        products['procedencia'] = products['procedencia'].astype(str).str.lower().str.strip()
    products = products.drop_duplicates(subset=['sku_id'])

    stock['sku_id'] = stock['sku_id'].map(_sku)
    stock['ubicacion'] = stock['ubicacion'].map(_loc)
    stock['qty'] = pd.to_numeric(stock['qty'], errors='coerce').fillna(0.0)
    stock = stock.groupby(['sku_id', 'ubicacion'], as_index=False)['qty'].sum()

    demand['sku_id'] = demand['sku_id'].map(_sku)
    demand['ubicacion'] = demand['ubicacion'].map(_loc)
    demand['mes'] = pd.to_datetime(demand['mes'])
    demand['demanda'] = pd.to_numeric(demand['demanda'], errors='coerce').fillna(0.0)
    demand = demand.groupby(['sku_id', 'ubicacion', 'mes'], as_index=False)['demanda'].sum()

    if ultima_venta.empty:
        ultima_venta = pd.DataFrame(columns=['sku_id', 'ubicacion', 'ultima_venta'])
    else:
        ultima_venta.columns = [str(c).strip().lower() for c in ultima_venta.columns]
        for c in ('sku_id', 'ubicacion', 'ultima_venta'):
            if c not in ultima_venta.columns:
                ultima_venta[c] = pd.NA
        ultima_venta['sku_id'] = ultima_venta['sku_id'].map(_sku)
        ultima_venta['ubicacion'] = ultima_venta['ubicacion'].map(_loc)
        ultima_venta['ultima_venta'] = pd.to_datetime(ultima_venta['ultima_venta'], errors='coerce')
        ultima_venta = ultima_venta.dropna(subset=['sku_id', 'ubicacion', 'ultima_venta'])
        ultima_venta = ultima_venta.sort_values('ultima_venta').drop_duplicates(['sku_id', 'ubicacion'], keep='last')

    run_id = _run_id_from_kwargs(kwargs)
    print("pipeline [merge_extract_base] finalizado")
    return {
        'products': products,
        'stock': stock,
        'demand': demand,
        'ultima_venta': ultima_venta,
        'process_date': str(kwargs.get('process_date') or ''),
        'run_id': run_id,
    }


@test
def test_output(output, *args):
    assert isinstance(output, dict)
    assert 'products' in output and 'stock' in output and 'demand' in output








