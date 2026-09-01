from __future__ import annotations
import sys
from pathlib import Path

MAGE_PROJECT_ROOT = Path('/home/src/Maryun')
if MAGE_PROJECT_ROOT.exists() and str(MAGE_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGE_PROJECT_ROOT))


from datetime import datetime
import uuid
import pandas as pd
import clickhouse_connect  # type: ignore
from mage_ai.io.config import ConfigFileLoader

from utils.v4_bridge import resolve_process_date

TABLA_DESTINO = 'logistica_v2.logistica_silencio_sugerencias'
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
def export_silencio_sugerencias(prev: dict, **kwargs):
    run_id = _resolve_run_id(prev, kwargs)
    process_date = resolve_process_date(kwargs, prev)
    plan = prev.get('plan', pd.DataFrame()).copy()

    if plan.empty:
        rows = 0
    else:
        alertas = plan[plan['capa'].isin(['compra', 'cd'])][['sku_id', 'destino']].copy()
        alertas = alertas.rename(columns={'destino': 'ubicacion'}).drop_duplicates(['sku_id', 'ubicacion'])
        if alertas.empty:
            rows = 0
        else:
            now_ts = pd.Timestamp.utcnow().tz_localize(None)
            alertas['last_suggested_date'] = process_date
            alertas['last_necesidad'] = 0.0
            alertas['last_run_id'] = run_id
            alertas['updated_at'] = now_ts
            _client().insert_df(TABLA_DESTINO, alertas)
            rows = int(len(alertas))

    return {
        **prev,
        'export_status': {
            **prev.get('export_status', {}),
            'de_silencio_sugerencias_V4': {'tabla': TABLA_DESTINO, 'rows_inserted': rows},
        }
    }


@test
def test_output(output, *args):
    assert 'export_status' in output









