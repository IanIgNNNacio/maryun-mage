"""Standalone pipeline runner — sequential execution of every stage."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import time

from app.pipeline.context import RunContext
from app.pipeline.stages import (
    StageOutputs,
    stage_automation_filter,
    stage_classification,
    stage_export,
    stage_export_powerbi,
    stage_export_supabase,
    stage_external_sync,
    stage_extract,
    stage_forecast,
    stage_homologation_analytical,
    stage_homologation_operational,
    stage_needs,
    stage_overrides,
    stage_replenishment,
    stage_savings_summary,
    stage_silence_filter,
    stage_update_silence,
)
from app.utils.logging import get_logger


@dataclass
class PipelineResult:
    run_id: str
    out_dir: Path
    outputs: StageOutputs
    durations: dict[str, float] = field(default_factory=dict)


# Ordered stage registry. Mage imports the same list and runs blocks in order.
PIPELINE_STAGES = [
    ("external_sync",              stage_external_sync),        # ← sync OneDrive/red
    ("extract",                    stage_extract),
    ("forecast",                   stage_forecast),
    ("classification",             stage_classification),
    ("homologation_analytical",    stage_homologation_analytical),
    ("overrides",                  stage_overrides),
    ("needs",                      stage_needs),
    ("automation_filter",          stage_automation_filter),
    ("silence_filter",             stage_silence_filter),       # ← ventana de silencio
    ("homologation_operational",   stage_homologation_operational),
    ("replenishment",              stage_replenishment),
    ("export",                     stage_export),
    ("export_powerbi",             stage_export_powerbi),       # ← vista fija para Power BI
    ("export_supabase",            stage_export_supabase),      # ← push a la nube (dashboard)
    ("update_silence",             stage_update_silence),       # ← actualiza registro
    ("savings_summary",            stage_savings_summary),      # ← KPI de ahorro
]


def run_pipeline(ctx: RunContext) -> PipelineResult:
    """Run every stage in sequence and return the aggregated result."""
    log = get_logger("pipeline.runner")
    log.info("pipeline.start", run_id=ctx.run_id, source=ctx.source,
             process_date=ctx.process_date.isoformat())
    acc = StageOutputs()
    durations: dict[str, float] = {}

    for name, fn in PIPELINE_STAGES:
        t0 = time.perf_counter()
        try:
            acc = fn(ctx, acc)
        except Exception:
            log.exception("pipeline.stage_failed", stage=name)
            raise
        durations[name] = round(time.perf_counter() - t0, 3)
        log.info("pipeline.stage_done", stage=name, seconds=durations[name])

    log.info("pipeline.done", run_id=ctx.run_id, durations=durations)
    return PipelineResult(
        run_id=ctx.run_id,
        out_dir=ctx.out_dir,
        outputs=acc,
        durations=durations,
    )
