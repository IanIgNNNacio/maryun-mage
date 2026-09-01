from __future__ import annotations
from datetime import datetime
import uuid
import pandas as pd
import clickhouse_connect  # type: ignore
from mage_ai.io.config import ConfigFileLoader

TABLA_DESTINO = 'logistica_v2.logistica_ejecuciones_pipeline'
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
def export_ejecuciones_pipeline(prev: dict, **kwargs):
    run_id = _resolve_run_id(prev, kwargs)
    now_ts = pd.Timestamp.utcnow().tz_localize(None)

    plan = prev.get('plan', pd.DataFrame())
    filas_plan = int(len(plan)) if plan is not None else 0

    df = pd.DataFrame([{
        'run_id': run_id,
        'fecha_inicio': now_ts,
        'fecha_fin': now_ts,
        'estado': 'ok',
        'filas_plan': filas_plan,
        'filas_carga': filas_plan,
        'mensaje': '',
    }])

    _client().insert_df(TABLA_DESTINO, df)

    return {
        **prev,
        'export_status': {
            **prev.get('export_status', {}),
            'de_ejecuciones_pipeline': {'tabla': TABLA_DESTINO, 'rows_inserted': 1},
        }
    }


@test
def test_output(output, *args):
    assert 'export_status' in output








