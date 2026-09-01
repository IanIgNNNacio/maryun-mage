"""Pipeline orchestrator — turns extracted data into a written run.

The orchestrator is intentionally split into tiny ``Stage`` callables so
Mage can reuse the same logic from its own blocks. When run standalone,
``app.pipeline.runner.run_pipeline`` wires them in sequence and emits a
``PipelineResult``.
"""
from app.pipeline.context import RunContext, build_run_context
from app.pipeline.runner import PipelineResult, run_pipeline

__all__ = ["RunContext", "build_run_context", "PipelineResult", "run_pipeline"]
