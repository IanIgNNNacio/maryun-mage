from __future__ import annotations
from datetime import datetime
import uuid
import pandas as pd
import clickhouse_connect  # type: ignore
from mage_ai.io.config import ConfigFileLoader

TABLA_DESTINO = 'logistica_v2.logistica_auditoria_overrides'
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
def export_auditoria_overrides(prev: dict, **kwargs):
    run_id = _resolve_run_id(prev, kwargs)
    ov = prev.get('overrides_audit', pd.DataFrame()).copy()

    if ov.empty:
        rows = 0
    else:
        now_ts = pd.Timestamp.utcnow().tz_localize(None)
        if 'tipo_override' not in ov.columns:
            ov['tipo_override'] = 'desconocido'
        if 'mes' not in ov.columns:
            ov['mes'] = pd.NaT
        if 'valor_original' not in ov.columns:
            ov['valor_original'] = None
        if 'valor_aplicado' not in ov.columns:
            ov['valor_aplicado'] = None
        if 'motivo' not in ov.columns:
            ov['motivo'] = None
        if 'responsable' not in ov.columns:
            ov['responsable'] = None

        df = pd.DataFrame({
            'run_id': run_id,
            'fecha_generacion': now_ts,
            'sku_id': ov['sku_id'],
            'ubicacion': ov['ubicacion'],
            'mes': pd.to_datetime(ov['mes'], errors='coerce').dt.date,
            'tipo_override': ov['tipo_override'],
            'valor_original': ov['valor_original'].astype(str),
            'valor_aplicado': ov['valor_aplicado'].astype(str),
            'motivo': ov['motivo'],
            'responsable': ov['responsable'],
        })
        _client().insert_df(TABLA_DESTINO, df)
        rows = int(len(df))

    return {
        **prev,
        'export_status': {
            **prev.get('export_status', {}),
            'de_auditoria_overrides': {'tabla': TABLA_DESTINO, 'rows_inserted': rows},
        }
    }


@test
def test_output(output, *args):
    assert 'export_status' in output








