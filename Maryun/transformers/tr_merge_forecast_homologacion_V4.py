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

    def national_of(self, importado_sku: str) -> str | None:
        for imp, nac in self.pairs:
            if imp == importado_sku:
                return nac
        return None

    def is_empty(self) -> bool:
        return self.df.empty


def _homologacion_payload(homologacion: HomologacionTable) -> dict:
    df = homologacion.df.copy()
    rows = df.where(pd.notna(df), None).to_dict("records") if not df.empty else []
    return {
        "rows": rows,
        "pairs": [[i, n] for i, n in homologacion.pairs],
        "factor": [
            {"sku_id_importado": i, "sku_id_nacional": n, "factor_conversion": f}
            for (i, n), f in homologacion.factor.items()
        ],
        "analitico": [[i, n] for i, n in homologacion.analitico],
        "operacional": [[i, n] for i, n in homologacion.operacional],
    }


if 'transformer' not in globals():
    from mage_ai.data_preparation.decorators import transformer
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test


@transformer
def tr_merge_forecast_homologacion_V4(
    dl_control_homologacion_productos_V4: pd.DataFrame,
    prev: dict,
    dl_forecast_precomputado_V4: pd.DataFrame,
    **kwargs
) -> dict:
    ensure_v4_import_path()
    from app.homologation.analytical import reinforce_forecast_with_national
    from app.homologation.audit import build_homologacion_audit
    from app.normalize.canonical import canonical_sku

    forecast = dl_forecast_precomputado_V4.copy()
    process_date = resolve_process_date(kwargs, prev)

    if forecast.empty:
        forecast = pd.DataFrame(columns=[
            'sku_id', 'ubicacion', 'mes', 'forecast_modelo', 'forecast_override',
            'forecast_final', 'forecast_fue_forzado', 'motivo_override', 'responsable_override',
        ])
    else:
        forecast.columns = [str(c).strip().lower() for c in forecast.columns]
        req = ['sku_id', 'ubicacion', 'mes', 'forecast_final']
        miss = [c for c in req if c not in forecast.columns]
        if miss:
            raise ValueError(f"dl_forecast_precomputado_V4 sin columnas requeridas: {miss}")
        forecast['sku_id'] = forecast['sku_id'].astype(str).str.strip()
        forecast['ubicacion'] = forecast['ubicacion'].astype(str).str.strip().str.upper()
        forecast['mes'] = pd.to_datetime(forecast['mes'], errors='coerce')
        forecast['forecast_final'] = pd.to_numeric(forecast['forecast_final'], errors='coerce').fillna(0.0)
        if 'forecast_modelo' not in forecast.columns:
            forecast['forecast_modelo'] = forecast['forecast_final']
        forecast['forecast_modelo'] = pd.to_numeric(
            forecast['forecast_modelo'], errors='coerce'
        ).fillna(forecast['forecast_final']).astype(float)
        # PRE-override: el proceso local entrega el forecast del MODELO sin overrides;
        # los overrides se aplican despues SOLO desde logistica_override_forecast.
        # Se ignora cualquier columna de override que traiga la tabla precomputada
        # para no marcar forzados de mas (replica app/forecast/selector.py).
        forecast['forecast_override'] = pd.NA
        forecast['forecast_fue_forzado'] = False
        forecast['motivo_override'] = pd.NA
        forecast['responsable_override'] = pd.NA

    products = prev['products'].copy()
    homolog_raw = dl_control_homologacion_productos_V4.copy()
    if homolog_raw.empty:
        hom_table = HomologacionTable(
            df=pd.DataFrame(),
            pairs=[],
            factor={},
            analitico=set(),
            operacional=set(),
        )
    else:
        homolog_raw.columns = [str(c).strip().lower() for c in homolog_raw.columns]
        for c in ['sku_id_importado', 'sku_id_nacional', 'factor_conversion', 'usar_analitico', 'usar_operacional']:
            if c not in homolog_raw.columns:
                homolog_raw[c] = pd.NA
        homolog_raw['sku_id_importado'] = homolog_raw['sku_id_importado'].map(canonical_sku)
        homolog_raw['sku_id_nacional'] = homolog_raw['sku_id_nacional'].map(canonical_sku)
        homolog_raw['factor_conversion'] = pd.to_numeric(homolog_raw['factor_conversion'], errors='coerce')
        truthy = {'true', '1', 'yes', 'si', 'sí', 'y', 't', 'x', '✓'}
        for c in ['usar_analitico', 'usar_operacional']:
            homolog_raw[c] = homolog_raw[c].astype(str).str.strip().str.lower().isin(truthy)
        homolog_raw = homolog_raw.dropna(subset=['sku_id_importado', 'sku_id_nacional', 'factor_conversion'])
        homolog_raw = homolog_raw[homolog_raw['factor_conversion'] > 0].reset_index(drop=True)
        pairs = list(zip(homolog_raw['sku_id_importado'], homolog_raw['sku_id_nacional']))
        hom_table = HomologacionTable(
            df=homolog_raw,
            pairs=pairs,
            factor={(i, n): float(f) for i, n, f in zip(homolog_raw['sku_id_importado'], homolog_raw['sku_id_nacional'], homolog_raw['factor_conversion'])},
            analitico={(i, n) for (i, n), flag in zip(pairs, homolog_raw['usar_analitico']) if bool(flag)},
            operacional={(i, n) for (i, n), flag in zip(pairs, homolog_raw['usar_operacional']) if bool(flag)},
        )

    params = load_v4_params()
    homolog_audit = pd.DataFrame()
    if params.homologation.enabled and not hom_table.is_empty() and not forecast.empty:
        forecast = reinforce_forecast_with_national(forecast, hom_table, params)
        homolog_audit = build_homologacion_audit(forecast)

    return {
        **prev,
        'forecast': forecast,
        'homologacion_payload': _homologacion_payload(hom_table),
        'homologacion_audit': homolog_audit,
        'forecast_engine_audit': {},
        'process_date': process_date.isoformat(),
    }


@test
def test_output(output, *args):
    assert 'forecast' in output