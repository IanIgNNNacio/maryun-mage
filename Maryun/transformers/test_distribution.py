import pandas as pd
import json
import math
from datetime import datetime

if 'transformer' not in globals():
    from mage_ai.data_preparation.decorators import transformer

# RANGOS (11..15 se ignoran)
BASE_RANGE   = range(1, 11)   # 1..10
REDIST_RANGE = range(16, 21)  # 16..20

def _parse_json_maybe(x):
    if x is None or (isinstance(x, float) and math.isnan(x)): 
        return None
    if isinstance(x, dict):
        return x
    if isinstance(x, str) and x.strip():
        for attempt in (lambda s: json.loads(s),
                        lambda s: json.loads(s.replace("'", '"'))):
            try:
                return attempt(x)
            except Exception:
                continue
    return None

def _is_int_str(s): 
    return isinstance(s, str) and s.isdigit()

def _key_in_range(k, rng):
    return _is_int_str(k) and int(k) in rng

def _to_dt_dayfirst(x):
    if pd.isna(x): 
        return None
    if isinstance(x, (pd.Timestamp, datetime)):
        return pd.to_datetime(x)
    return pd.to_datetime(x, dayfirst=True, errors='coerce')

def _norm(v):
    """Para comparar IDs: 2.0 => 2"""
    if pd.isna(v):
        return v
    try:
        f = float(v)
        if f.is_integer():
            return int(f)
    except Exception:
        return v
    return v

def _match_product_partner(d2_row, prod, part):
    d2_prod, d2_part = d2_row.get('product_id'), d2_row.get('partner_id')
    d2_prod_n, d2_part_n, prod_n, part_n = map(_norm, [d2_prod, d2_part, prod, part])
    if pd.notna(d2_prod_n) and d2_prod_n != prod_n:
        return False
    if pd.notna(d2_part_n) and d2_part_n != part_n:
        return False
    return True

@transformer
def debug_analytic_distribution_reanalytic_distribution(data, data_2, *args, **kwargs):
    """
    Devuelve 3 (o 4) salidas con diagnóstico:
      - keys_summary: analytic_distribution de claves vistas en data.analytic_distribution (y cuántas filas no parsearon)
      - per_row_diag: por cada fila, qué pasaría (claves base/redis/ignoradas), rows esperadas,
                      y matches por sucursal, fechas y prod/partner en data_2
      - data2_overview: resumen por account_analytic_account (conteos y ventanas de fecha)
      - bad_analytic_distribution_sample (solo si aplica): ejemplos de analytic_distribution vacía/no parseada
    """

    # --- Copias y normalizaciones mínimas ---
    df = data.copy()
    d2 = data_2.copy()

    # Normalizar account_analytic_account a numérico
    if 'account_analytic_account' in d2.columns:
        d2['account_analytic_account_num'] = pd.to_numeric(
            d2['account_analytic_account'], errors='coerce'
        )
    else:
        d2['account_analytic_account_num'] = pd.NA

    # Parseo de fechas de la ventana en data_2
    d2['__date'] = pd.to_datetime(d2.get('date'), dayfirst=True, errors='coerce')
    d2['__date_due'] = pd.to_datetime(d2.get('date_due'), dayfirst=True, errors='coerce')

    # --- 1) Resumen de claves en data.analytic_distribution ---
    key_counts = {}
    bad_rows_sample_idx = []
    for i, row in df.iterrows():
        dist = _parse_json_maybe(row.get('analytic_distribution'))
        if not dist:
            key_counts.setdefault('__empty_or_unparsed__', 0)
            key_counts['__empty_or_unparsed__'] += 1
            bad_rows_sample_idx.append(i)
            continue
        for k in map(str, dist.keys()):
            key_counts.setdefault(k, 0)
            key_counts[k] += 1
    keys_summary = pd.DataFrame(
        [{'key': k, 'count_rows': v} for k, v in key_counts.items()]
    ).sort_values('key')

    bad_analytic_distribution_sample = pd.DataFrame(columns=['factura','analytic_distribution'])
    if bad_rows_sample_idx:
        bad_analytic_distribution_sample = df.loc[bad_rows_sample_idx[:10], ['factura','analytic_distribution']]

    # --- 2) Diagnóstico por fila ---
    diag_rows = []
    for idx, row in df.iterrows():
        dist = _parse_json_maybe(row.get('analytic_distribution')) or {}
        monto = row.get('monto', 0)
        fecha_evento = _to_dt_dayfirst(row.get('fecha_asiento_contable'))

        base_keys, redist_keys, ignored_keys, invalid_keys = [], [], [], []
        total_rows_out = 0
        redist_details = []

        # Recorremos claves de 'analytic_distribution'
        for k, v in {str(k): v for k, v in dist.items()}.items():
            # % distribuido
            try:
                pct = float(v)
            except Exception:
                invalid_keys.append(k)
                continue
            if pct == 0 or math.isnan(pct):
                continue

            # 1..10 => base
            if _key_in_range(k, BASE_RANGE):
                base_keys.append(k)
                total_rows_out += 1
                continue

            # 16..20 => redistribución
            if _key_in_range(k, REDIST_RANGE):
                redist_keys.append(k)
                suc_origen = int(k)

                # A) filtro por sucursal origen en data_2
                d2_suc = d2[d2['account_analytic_account_num'] == suc_origen]

                # B) filtro por ventana de fechas usando fecha_evento
                if fecha_evento is not None:
                    d2_date = d2_suc[
                        (d2_suc['__date'].isna() | (pd.to_datetime(d2_suc['__date']) <= fecha_evento)) &
                        (d2_suc['__date_due'].isna() | (pd.to_datetime(d2_suc['__date_due']) >= fecha_evento))
                    ]
                else:
                    d2_date = pd.DataFrame(columns=d2_suc.columns)

                # C) filtro por product/partner (si en d2_date alguno es NaN, no restringe)
                if not d2_date.empty:
                    mask = d2_date.apply(
                        lambda r: _match_product_partner(r, row.get('product_id'), row.get('partner_id')),
                        axis=1
                    )
                    d2_match = d2_date[mask]
                else:
                    d2_match = d2_date

                # Conteos + si hay analytic_distribution
                analytic_nonempty = 0
                if not d2_match.empty and 'analytic_distribution' in d2_match:
                    analytic_nonempty = int(d2_match['analytic_distribution'].notna().sum())

                redist_details.append({
                    'key': k,
                    'sucursal_origen': suc_origen,
                    'pct': pct,
                    'matches_sucursal': int(len(d2_suc)),
                    'matches_date_window': int(len(d2_date)),
                    'matches_prod_partner': int(len(d2_match)),
                    'analytic_distribution_nonempty': analytic_nonempty
                })

                # Aun sin matches, el transform real haría fallback => 1 fila
                total_rows_out += 1
                continue

            # 11..15 => ignoradas
            ignored_keys.append(k)

        diag_rows.append({
            'row_index': idx,
            'factura': row.get('factura'),
            'aml_id': row.get('aml_id'),
            'fecha_asiento_contable_parsed': fecha_evento,
            'monto': monto,
            'base_keys_1_10': ",".join(base_keys) if base_keys else "",
            'redist_keys_16_20': ",".join(redist_keys) if redist_keys else "",
            'ignored_keys_11_15': ",".join(ignored_keys) if ignored_keys else "",
            'invalid_keys_non_numeric': ",".join(invalid_keys) if invalid_keys else "",
            'expected_rows_from_logic': total_rows_out,
            'redist_debug': json.dumps(redist_details, ensure_ascii=False)
        })

    per_row_diag = pd.DataFrame(diag_rows)

    # --- 3) Resumen de data_2 por sucursal ---
    data2_overview = (
        d2
        .assign(acc=d2['account_analytic_account_num'])
        .groupby('acc', dropna=False)
        .agg(
            rows=('acc','size'),
            min_date=('__date','min'),
            max_date=('__date_due','max'),
            have_analytic_distribution=('analytic_distribution', lambda s: int(s.notna().sum()))
        )
        .reset_index()
        .sort_values('acc')
    )

    # --- Impresiones útiles al log ---
    print("Diagnóstico generado.")
    print("Filas en data:", len(df), " | Filas en data_2:", len(d2))
    print("Claves de analytic_distribution por conteo (top 20):")
    print(keys_summary.sort_values('count_rows', ascending=False).head(20))

    # --- Devolver múltiples salidas (Mage las muestra como datasets separados) ---
    outputs = {
        'keys_summary': keys_summary,
        'per_row_diag': per_row_diag,
        'data2_overview': data2_overview,
    }
    if not bad_analytic_distribution_sample.empty:
        outputs['bad_analytic_distribution_sample'] = bad_analytic_distribution_sample
    return outputs
