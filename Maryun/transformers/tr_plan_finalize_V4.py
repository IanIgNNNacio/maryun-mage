from __future__ import annotations
import pandas as pd

if 'transformer' not in globals():
    from mage_ai.data_preparation.decorators import transformer
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test


@transformer
def tr_plan_finalize_V4(prev: dict, **kwargs) -> dict:
    needs = prev.get('needs', pd.DataFrame()).copy()
    plan = prev.get('plan', pd.DataFrame()).copy()
    classification = prev.get('classification', pd.DataFrame())

    if 'fuente_autom' not in plan.columns:
        plan['fuente_autom'] = 'db_rule'

    if not isinstance(classification, pd.DataFrame) or classification.empty:
        raise ValueError(
            'tr_plan_finalize_V4 no recibio classification valida desde upstream. '
            f'type={type(classification).__name__} '
            f'rows={len(classification) if isinstance(classification, pd.DataFrame) else "N/A"}'
        )
    required = {'sku_id', 'ubicacion', 'clase_final'}
    missing = sorted(required - set(classification.columns))
    if missing:
        raise ValueError(
            'tr_plan_finalize_V4 recibio classification sin columnas requeridas. '
            f'missing={missing} columns={list(classification.columns)}'
        )
    print(
        'pipeline [tr_plan_finalize] classification '
        f'rows={len(classification)} '
        f"clases={classification['clase_final'].value_counts().head(10).to_dict()}"
    )
    print('pipeline [tr_plan_finalize] finalizado')
    return {
        **prev,
        'needs_audit': prev.get('needs_audit', needs.copy()),
        'plan_audit': prev.get('plan_audit', plan.copy()),
        'plan': plan,
        'classification': classification,
    }


@test
def test_output(output, *args):
    assert 'plan' in output and 'plan_audit' in output