"""RunContext — the bag of resolved settings passed to every stage."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from app.config.engine_params import EngineParams, get_engine_params
from app.config.paths import ProjectPaths, get_paths
from app.config.settings import Settings, get_settings
from app.temporal.gate import TemporalGate, build_temporal_gate
from app.utils.logging import configure_logging, get_logger
from app.utils.run_id import make_run_id


@dataclass(frozen=True)
class RunContext:
    run_id: str
    process_date: date
    settings: Settings
    paths: ProjectPaths
    params: EngineParams
    gate: TemporalGate
    out_dir: Path

    @property
    def source(self) -> str:
        return self.settings.run.source


def _resolve_process_date(raw: str | None) -> date:
    if raw:
        return datetime.fromisoformat(raw).date()
    return date.today()


def build_run_context(
    *,
    settings: Settings | None = None,
    paths: ProjectPaths | None = None,
    params: EngineParams | None = None,
    run_id: str | None = None,
    process_date: str | date | None = None,
) -> RunContext:
    """Build a fully-resolved :class:`RunContext`; also configures logging."""
    s = settings or get_settings()
    p = paths or get_paths()
    prm = params or get_engine_params()
    configure_logging(s)

    if isinstance(process_date, date):
        pd_date = process_date
    else:
        pd_date = _resolve_process_date(process_date or s.run.process_date)

    rid = run_id or make_run_id()
    gate = build_temporal_gate(pd_date, prm)
    p.ensure_dirs()
    out = p.run_output_dir(rid)
    out.mkdir(parents=True, exist_ok=True)

    log = get_logger("pipeline.context")
    log.info(
        "pipeline.context.built",
        run_id=rid,
        process_date=pd_date.isoformat(),
        source=s.run.source,
        history_start=gate.history_start.isoformat(),
        last_closed_month=gate.last_closed_month.isoformat(),
    )
    return RunContext(
        run_id=rid,
        process_date=pd_date,
        settings=s,
        paths=p,
        params=prm,
        gate=gate,
        out_dir=out,
    )
