import pandas as pd
import json
import math
from datetime import datetime

if 'transformer' not in globals():
    from mage_ai.data_preparation.decorators import transformer
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test

SUCURSAL_MAP = {
    "1": "LOS ANGELES",
    "2": "CASTRO",
    "3": "CONCEPCIÓN",
    "4": "OSORNO",
    "5": "PUERTO MONTT",
    "6": "PUERTO VARAS",
    "7": "QUELLÓN",
    "8": "SANTIAGO",
    "9": "MARKETPLACE",
    "10": "VALDIVIA",
    "16": "DISTRIBUCIÓN TOTAL",
    "17": "CD SANTIAGO",
    "18": "CD SUR",
    "19": "LOGOS SANTIAGO",
    "20": "LOGOS CARDENAL",
}

# Rangos
BASE_RANGE   = range(1, 11)   # 1..10
REDIST_RANGE = range(16, 21)  # 16..20

def _parse_json_maybe(x):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    if isinstance(x, dict):
        return x
    if isinstance(x, str) and x.strip():
        try:
            return json.loads(x)
        except Exception:
            try:
                return json.loads(x.replace("'", '"'))
            except Exception:
                return None
    return None

def _is_int_str(s):
    return isinstance(s, str) and s.isdigit()

def _is_key_in_range(k: str, rng: range) -> bool:
    return _is_int_str(k) and int(k) in rng

def _to_dt(x):
    if pd.isna(x):
        return None
    if isinstance(x, (pd.Timestamp, datetime)):
        return pd.to_datetime(x)
    try:
        return pd.to_datetime(x)
    except Exception:
        return None

def _split_data2_two_passes(row: pd.Series, data_2: pd.DataFrame, sucursal_origen: int):
    """
    Paso 1: filtra por account_analytic_account y fecha (date <= evento <= date_due).
    Paso 2 (etapa específica): de ese subconjunto, toma filas que coinciden por product_id o partner_id.
    Paso 3 (etapa general): del mismo subconjunto, descarta las específicas y toma el resto (ignorando product/partner).
    Devuelve (d2_especificas, d2_generales_restantes)
    """
    if data_2 is None or data_2.empty:
        return (pd.DataFrame(), pd.DataFrame())

    fecha_evento = _to_dt(row.get("fecha_asiento_contable"))
    if fecha_evento is None:
        return (pd.DataFrame(), pd.DataFrame())

    prod = row.get("product_id")
    part = row.get("partner_id")

    d2 = data_2.copy()
    d2["__date"] = pd.to_datetime(d2.get("date"), errors="coerce")
    d2["__date_due"] = pd.to_datetime(d2.get("date_due"), errors="coerce")

    # Filtro base: sucursal origen + fechas
    d2 = d2[d2.get("account_analytic_account") == sucursal_origen]
    if d2.empty:
        return (pd.DataFrame(), pd.DataFrame())

    d2 = d2[
        (d2["__date"].isna() | (d2["__date"] <= fecha_evento)) &
        (d2["__date_due"].isna() | (d2["__date_due"] >= fecha_evento))
    ]
    if d2.empty:
        return (pd.DataFrame(), pd.DataFrame())

    # Etapa específica: coincidencia por product_id O partner_id (al menos uno)
    mask_especifica = (
        (pd.notna(d2.get("product_id")) & (d2.get("product_id") == prod)) |
        (pd.notna(d2.get("partner_id")) & (d2.get("partner_id") == part))
    )
    d2_especifica = d2[mask_especifica].copy()

    # Etapa general: resto de filas (descartando las ya usadas)
    if not d2_especifica.empty:
        d2_general = d2[~mask_especifica].copy()
    else:
        d2_general = d2.copy()

    return (d2_especifica, d2_general)

def _combine_analytic_distributions(d2_matches: pd.DataFrame) -> dict:
    """
    Suma las analytic_distribution de las filas y normaliza a 100.
    Conserva solo claves 1..10.
    """
    if d2_matches is None or d2_matches.empty:
        return {}

    agg = {}
    for _, r in d2_matches.iterrows():
        ad = _parse_json_maybe(r.get("analytic_distribution")) or {}
        for k, v in {str(k): v for k, v in ad.items()}.items():
            if not _is_key_in_range(k, BASE_RANGE):
                continue
            if v is None or (isinstance(v, float) and math.isnan(v)) or float(v) == 0:
                continue
            agg[k] = agg.get(k, 0.0) + float(v)

    total = sum(agg.values())
    if total <= 0:
        return {}

    return {k: (v * 100.0 / total) for k, v in agg.items()}

@transformer
def transform(data, data_2, *args, **kwargs):
    """
    Distribución:
      - Claves 1..10 => asignación directa (base).
      - Claves 16..20 => dos etapas dentro del mismo evento:
          (1) usar filas de data_2 que coinciden por product_id/partner_id (si existen),
          (2) descartar esas y luego sumar las restantes (solo sucursal+fechas).
        Se combinan ambas etapas y se normaliza para obtener la distribución final 1..10.
        Si no hay ninguna fila tras ambos pasos, se mantiene en la sucursal 16..20.
    """
    return distribuir_filas_con_redistribucion(data, data_2)

@test
def test_output(output, *args) -> None:
    assert output is not None, 'The output is undefined'

def distribuir_filas_con_redistribucion(data: pd.DataFrame, data_2: pd.DataFrame) -> pd.DataFrame:
    cols_repetir = [
        "partner_id", "proveedor", "id_cuenta", "cuenta_contable",
        "tipo_cuenta", "factura", "fecha_asiento_contable", "fecha_factura",
        "aml_id", "monto", "aml", "product_id",
    ]

    out_rows = []

    for _, row in data.iterrows():
        monto_val = row.get("monto", 0) or 0
        dist = _parse_json_maybe(row.get("distribucion")) or {}
        dist = {str(k): v for k, v in dist.items()}

        for k, pct in dist.items():
            if pct is None or (isinstance(pct, float) and math.isnan(pct)) or float(pct) == 0:
                continue

            pct_base = float(pct)

            # Caso A: 1..10 => directo
            if _is_key_in_range(k, BASE_RANGE):
                out = {c: row.get(c) for c in cols_repetir}
                out["monto_original"] = monto_val
                out["sucursal_id"] = int(k)
                out["sucursal"] = SUCURSAL_MAP.get(k)
                out["porcentaje_distribucion"] = pct_base
                out["porcentaje_redistribucion"] = None
                out["monto_distribuido"] = round(monto_val * (pct_base / 100.0), 2)
                out_rows.append(out)
                continue

            # Caso B: 16..20 => redistribuye con el proceso en dos etapas
            if _is_key_in_range(k, REDIST_RANGE):
                suc_origen = int(k)

                d2_especifica, d2_general = _split_data2_two_passes(row, data_2, suc_origen)

                # Combinar (específica primero, luego generales restantes)
                d2_total = pd.concat([d2_especifica, d2_general], ignore_index=True)
                combined = _combine_analytic_distributions(d2_total)

                if combined:
                    for dk, dpct in combined.items():
                        pct_analytic = float(dpct)
                        out = {c: row.get(c) for c in cols_repetir}
                        out["monto_original"] = monto_val
                        out["sucursal_id"] = int(dk)
                        out["sucursal"] = SUCURSAL_MAP.get(dk)
                        out["porcentaje_distribucion"] = pct_base
                        out["porcentaje_redistribucion"] = pct_analytic
                        out["monto_distribuido"] = round(
                            monto_val * (pct_base / 100.0) * (pct_analytic / 100.0), 2
                        )
                        out_rows.append(out)
                else:
                    # Sin ninguna fila tras ambos pasos => queda en la sucursal origen 16..20
                    out = {c: row.get(c) for c in cols_repetir}
                    out["monto_original"] = monto_val
                    out["sucursal_id"] = suc_origen
                    out["sucursal"] = SUCURSAL_MAP.get(str(suc_origen))
                    out["porcentaje_distribucion"] = pct_base
                    out["porcentaje_redistribucion"] = None
                    out["monto_distribuido"] = round(monto_val * (pct_base / 100.0), 2)
                    out_rows.append(out)

            # Claves fuera de 1..10 y 16..20 => ignorar
            else:
                continue

    cols_finales = [
        "partner_id","proveedor","id_cuenta","cuenta_contable","tipo_cuenta","factura",
        "fecha_asiento_contable","fecha_factura","monto","aml","monto_original",
        "product_id","aml_id","sucursal_id","sucursal",
        "porcentaje_distribucion","porcentaje_redistribucion","monto_distribuido"
    ]
    out_df = pd.DataFrame(out_rows) if out_rows else pd.DataFrame(columns=cols_finales)
    if not out_df.empty:
        out_df = out_df.reindex(columns=cols_finales)
    return out_df