"""Export layer — materialise the run as csv/xlsx artifacts.

Produces:
    * ``carga_maryun.{csv,xlsx}``  — the final, flattened plan ready for ERP
    * ``run_audit.xlsx``           — one workbook with every audit table
    * individual CSVs for each audit table (for Excel-less consumers)

Las re-exportaciones son LAZY (PEP 562): importar un submódulo liviano como
``app.export.supabase_sync`` NO arrastra ``exporter`` (openpyxl, etc.), lo que
permite un despliegue slim del dashboard en la nube.
"""

__all__ = ["ExportBundle", "export_run", "build_carga_maryun"]


def __getattr__(name):  # PEP 562 — carga perezosa de las re-exportaciones
    if name in ("ExportBundle", "export_run"):
        from app.export import exporter
        return getattr(exporter, name)
    if name == "build_carga_maryun":
        from app.export.carga_maryun import build_carga_maryun
        return build_carga_maryun
    raise AttributeError(f"module 'app.export' has no attribute {name!r}")
