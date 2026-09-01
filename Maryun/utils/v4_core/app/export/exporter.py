"""Write a run's outputs to disk — carga_maryun + the audit bundle."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from app.connectors.excel import ExcelWriter
from app.utils.logging import get_logger
from app.utils.reports import write_audit_bundle, write_report


@dataclass
class ExportBundle:
    carga_path: Path
    audit_workbook: Path
    audit_csv_paths: list[Path] = field(default_factory=list)


def export_run(
    out_dir: Path,
    *,
    run_id: str,
    carga_maryun: pd.DataFrame,
    audits: dict[str, pd.DataFrame],
    also_xlsx_carga: bool = True,
) -> ExportBundle:
    """Serialise all outputs. ``audits`` is a dict of name → DataFrame.

    The audit dict becomes:
        * one sheet per name inside ``run_audit.xlsx``
        * one CSV per name under ``out_dir/audit/``
    """
    log = get_logger("export.exporter")
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. carga_maryun.csv (+ xlsx)
    carga_path = out_dir / "carga_maryun.csv"
    carga_maryun.to_csv(carga_path, index=False, encoding="utf-8")
    if also_xlsx_carga:
        carga_maryun.to_excel(out_dir / "carga_maryun.xlsx", index=False)
    log.info("export.exporter.carga_written", path=str(carga_path), rows=len(carga_maryun))

    # 2. run_audit.xlsx — one sheet per audit table.
    audit_workbook = out_dir / "run_audit.xlsx"
    writer = ExcelWriter()
    # Excel sheet names are limited to 31 chars; truncate if necessary.
    sanitised = {name[:31]: df for name, df in audits.items()}
    writer.write_multi(sanitised, path=audit_workbook)
    log.info("export.exporter.audit_workbook_written", path=str(audit_workbook), sheets=len(sanitised))

    # 3. per-table CSVs under audit/
    audit_dir = out_dir / "audit"
    csv_paths = write_audit_bundle(audits, audit_dir)

    # 4. manifest
    manifest = pd.DataFrame([
        {"artifact": "carga_maryun", "path": carga_path.name, "rows": len(carga_maryun)},
        {"artifact": "run_audit", "path": audit_workbook.name, "rows": sum(len(df) for df in audits.values())},
    ])
    write_report(manifest, out_dir, f"manifest_{run_id}", also_xlsx=False)

    return ExportBundle(
        carga_path=carga_path,
        audit_workbook=audit_workbook,
        audit_csv_paths=csv_paths,
    )
