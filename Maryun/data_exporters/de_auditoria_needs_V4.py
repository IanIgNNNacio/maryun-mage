from __future__ import annotations
from datetime import datetime
import uuid
import pandas as pd
import clickhouse_connect  # type: ignore
from mage_ai.io.config import ConfigFileLoader

TABLA_DESTINO = 'logistica_v2.logistica_auditoria_needs'
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
def export_auditoria_needs(prev: dict, **kwargs):
    run_id = _resolve_run_id(prev, kwargs)
    needs = prev.get('needs_audit', prev.get('needs', pd.DataFrame())).copy()

    if needs.empty:
        rows = 0
    else:
        def _num_col(df_src: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
            if col in df_src.columns:
                return pd.to_numeric(df_src[col], errors='coerce').fillna(default)
            return pd.Series([default] * len(df_src), index=df_src.index, dtype='float64')

        now_ts = pd.Timestamp.utcnow().tz_localize(None)
        df = pd.DataFrame({
            'run_id': run_id,
            'fecha_generacion': now_ts,
            'sku_id': needs['sku_id'],
            'ubicacion': needs['ubicacion'],
            'demanda_esperada': _num_col(needs, 'demanda_esperada', 0.0),
            'stock_actual': _num_col(needs, 'stock_actual', 0.0),
            'safety_stock': _num_col(needs, 'safety_stock', 0.0),
            'rop': _num_col(needs, 'rop', 0.0),
            'necesidad': _num_col(needs, 'necesidad', 0.0),
            'dispara': _num_col(needs, 'dispara', 0.0).astype(int),
        })
        _client().insert_df(TABLA_DESTINO, df)
        rows = int(len(df))

    return {
        **prev,
        'export_status': {
            **prev.get('export_status', {}),
            'de_auditoria_needs': {'tabla': TABLA_DESTINO, 'rows_inserted': rows},
        }
    }


@test
def test_output(output, *args):
    assert 'export_status' in output








