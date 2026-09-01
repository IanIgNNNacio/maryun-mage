"""Command-line interface for the replenishment pipeline.

Install via ``pyproject.toml``'s ``[project.scripts]`` and invoke as
``maryun-replenishment run --source mock``.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer

from app.config.settings import get_settings, reset_settings_cache
from app.pipeline.context import build_run_context
from app.pipeline.runner import run_pipeline
from app.utils.logging import configure_logging, get_logger


app = typer.Typer(
    help="Maryun replenishment pipeline — end-to-end forecast → plan.",
    add_completion=False,
)


@app.command("run")
def cli_run(
    source: str = typer.Option(
        None,
        "--source", "-s",
        help="Data source: mariadb or mock (defaults to MARYUN_RUN__SOURCE).",
    ),
    process_date: Optional[str] = typer.Option(
        None,
        "--process-date",
        help="ISO date (YYYY-MM-DD) — defaults to today.",
    ),
    run_id: Optional[str] = typer.Option(
        None,
        "--run-id",
        help="Override the auto-generated run id.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Run everything except the final export write.",
    ),
    json_logs: bool = typer.Option(
        False,
        "--json-logs",
        help="Emit structured JSON logs instead of the colourised console renderer.",
    ),
) -> None:
    """Execute the pipeline end-to-end and write carga_maryun.csv + run_audit.xlsx."""
    import os

    # Override settings via env before building context / configuring logging.
    if source:
        os.environ["MARYUN_RUN__SOURCE"] = source
        reset_settings_cache()
    if json_logs:
        os.environ["MARYUN_LOG__JSON"] = "true"
        reset_settings_cache()

    settings = get_settings()
    configure_logging(settings)
    log = get_logger("cli")
    if dry_run:
        # Disable the export stage by monkey-patching the runner at module level.
        from app.pipeline import runner as _runner
        _runner.PIPELINE_STAGES = [s for s in _runner.PIPELINE_STAGES if s[0] != "export"]
        log.warning("cli.dry_run", skipping="export")

    ctx = build_run_context(
        settings=settings,
        run_id=run_id,
        process_date=process_date,
    )
    try:
        result = run_pipeline(ctx)
    except Exception as exc:  # pragma: no cover — propagate after logging
        log.exception("cli.pipeline_failed")
        raise typer.Exit(code=1) from exc

    typer.echo(f"✅ run_id={result.run_id}  out={result.out_dir}")
    typer.echo(f"   plan rows:  {len(result.outputs.plan)}")
    typer.echo(f"   carga rows: {len(result.outputs.carga_maryun)}")


@app.command("describe")
def cli_describe(
    process_date: Optional[str] = typer.Option(None, "--process-date"),
) -> None:
    """Print the temporal gate descriptor without running the pipeline."""
    configure_logging()
    ctx = build_run_context(process_date=process_date)
    for k, v in ctx.gate.describe().items():
        typer.echo(f"{k:>22}: {v}")
    typer.echo(f"{'run_id':>22}: {ctx.run_id}")
    typer.echo(f"{'out_dir':>22}: {ctx.out_dir}")


@app.command("update-abc-xyz")
def cli_update_abc_xyz(
    input_path: Optional[str] = typer.Option(
        None,
        "--input", "-i",
        help="Ruta al archivo RESULTADO_ESTRATEGICO_ABC_XYZ_v3.xlsx. "
             "Si no se indica, usa la ruta por defecto en OneDrive.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Muestra resultados sin escribir archivos.",
    ),
) -> None:
    """Genera override_clasificacion.xlsx desde el archivo externo ABC/XYZ.

    Lee el Excel de clasificaciones externas, cruza con el catalogo de productos
    del pipeline y escribe data/reference/override_clasificacion.xlsx.
    Ejecuta 'python -m app.cli run' despues para aplicar las clasificaciones.
    """
    from app.classification.external_abc_xyz import generate_classification_overrides
    from pathlib import Path

    path = Path(input_path) if input_path else None
    result = generate_classification_overrides(input_path=path, dry_run=dry_run)
    if result.empty and not dry_run:
        raise typer.Exit(code=1)


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
