from __future__ import annotations

import os
import sys
from datetime import date, datetime
from pathlib import Path


def _candidate_roots() -> list[Path]:
    local_root = Path(__file__).resolve().parent / 'v4_core'
    env = os.getenv('MARYUN_V4_ROOT', '').strip()
    cands = [local_root]
    if env:
        cands.append(Path(env))
    return cands


def ensure_v4_import_path() -> Path:
    for root in _candidate_roots():
        if (root / 'app').exists():
            s = str(root)
            if s not in sys.path:
                sys.path.insert(0, s)
            return root
    raise RuntimeError(
        'No se encontró el core V4 local dentro de Mage. '
        'Debe existir utils/v4_core/app en la carpeta maryun_abastecimiento_v4.'
    )


def load_v4_params():
    """Carga EngineParams desde el engine_params.yaml VENDIDO en v4_core.

    El runner local usa get_engine_params() (lee el yaml sobre los defaults).
    Mage debe hacer lo mismo: instanciar EngineParams() directo usa SOLO los
    defaults del código (p.ej. forecast.horizon_months=3) e ignora el yaml
    (horizon_months=12), lo que cambia compute_needs -> needs -> plan -> carga.
    """
    root = ensure_v4_import_path()
    from app.config.engine_params import load_engine_params

    yaml_path = root / 'app' / 'config' / 'engine_params.yaml'
    return load_engine_params(yaml_path if yaml_path.exists() else None)


def resolve_process_date(kwargs: dict, prev: dict | None = None) -> date:
    prev = prev or {}
    raw = kwargs.get('process_date') or prev.get('process_date')
    if not raw:
        return date.today()
    if isinstance(raw, date):
        return raw
    return datetime.fromisoformat(str(raw)).date()





