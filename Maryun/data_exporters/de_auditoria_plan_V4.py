# data_exporters/de_auditoria_plan.py
from __future__ import annotations

from datetime import datetime
import uuid
import pandas as pd
import clickhouse_connect  # type: ignore
from mage_ai.io.config import ConfigFileLoader

TABLA_DESTINO = 'logistica_v2.logistica_auditoria_plan'
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


@data_exporter
def export_auditoria_plan(prev: dict, **kwargs):
    run_id = _resolve_run_id(prev, kwargs)
    # La tabla de auditoria de plan tiene columnas a nivel de plan (origen, destino,
    # capa, ...). plan_audit puede no traer 'origen' -> caer a 'plan' que si las tiene.
    plan = prev.get('plan_audit', pd.DataFrame())
    if not isinstance(plan, pd.DataFrame) or plan.empty or 'origen' not in plan.columns:
        plan = prev.get('plan', pd.DataFrame())
    plan = plan.copy()

    if plan.empty:
        rows = 0
    else:
        now_ts = pd.Timestamp.utcnow().tz_localize(None)
        df = pd.DataFrame({
            'run_id': run_id,
            'fecha_generacion': now_ts,
            'sku_id': plan['sku_id'],
            'origen': plan['origen'],
            'destino': plan['destino'],
            'capa': plan['capa'],
            'cantidad': pd.to_numeric(plan['cantidad'], errors='coerce').fillna(0.0),
            'score': pd.to_numeric(plan.get('score', 0), errors='coerce').fillna(0.0),
            'motivo': plan.get('motivo', ''),
        })
        _client().insert_df(TABLA_DESTINO, df)
        rows = int(len(df))

    return {
        **prev,
        'export_status': {
            **prev.get('export_status', {}),
            'de_auditoria_plan': {'tabla': TABLA_DESTINO, 'rows_inserted': rows},
        }
    }


@test
def test_output(output, *args):
    assert 'export_status' in output