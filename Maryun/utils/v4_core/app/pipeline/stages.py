"""Individual pipeline stages — thin wrappers callable from CLI **and** Mage.

Each stage is a pure function of its inputs + :class:`RunContext`. This
design lets Mage blocks import exactly the stage they need (without
pulling the full runner) while the standalone CLI composes them
sequentially in ``runner.run_pipeline``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from app.automation.filter import apply_automation_filter, load_automation_table
from app.classification.selector import build_classification
from app.config.paths import ProjectPaths
from app.export.carga_maryun import build_carga_maryun
from app.export.exporter import export_run
from app.export.powerbi_view import build_powerbi_view, write_powerbi_view
from app.extract.extractor import extract_all
from app.forecast.selector import build_forecast
from app.homologation.analytical import reinforce_forecast_with_national
from app.homologation.audit import (
    build_homologacion_audit,
    build_resumen_homologacion,
    build_transferencias_demanda,
)
from app.homologation.loader import load_homologacion
from app.homologation.operational import (
    apply_operational_substitution,
    compute_substitution_candidates,
)
from app.needs.calculator import build_needs_audit, compute_needs
from app.savings.aggregator import compute_ahorro_resumen, load_historical_carga
from app.silence.registry import (
    apply_silence_filter,
    get_silenced_pairs,
    load_registry,
    update_registry,
)
from app.overrides.loader import (
    load_classification_overrides,
    load_forecast_overrides,
)
from app.overrides.resolver import (
    apply_classification_overrides,
    apply_forecast_overrides,
    build_overrides_audit,
)
from app.policies.costs import load_costs
from app.policies.distances import load_distances
from app.policies.priority_cd import load_priority_cd
from app.policies.rules import load_sku_location_rules
from app.policies.suppliers import load_suppliers
from app.replenishment.engine import run_replenishment
from app.utils.logging import get_logger

if TYPE_CHECKING:
    from app.pipeline.context import RunContext


@dataclass
class StageOutputs:
    """Accumulator handed from stage to stage."""

    # Extract
    products: pd.DataFrame = field(default_factory=pd.DataFrame)
    stock: pd.DataFrame = field(default_factory=pd.DataFrame)
    demand: pd.DataFrame = field(default_factory=pd.DataFrame)
    ultima_venta: pd.DataFrame = field(default_factory=pd.DataFrame)

    # Forecast
    forecast: pd.DataFrame = field(default_factory=pd.DataFrame)
    forecast_engine_audit: dict[str, pd.DataFrame] = field(default_factory=dict)

    # Classification
    classification: pd.DataFrame = field(default_factory=pd.DataFrame)
    classification_audit: pd.DataFrame = field(default_factory=pd.DataFrame)

    # Homologation
    homologacion_table: object | None = None
    homologacion_audit: pd.DataFrame = field(default_factory=pd.DataFrame)
    transferencias_demanda: pd.DataFrame = field(default_factory=pd.DataFrame)
    resumen_homologacion: pd.DataFrame = field(default_factory=pd.DataFrame)

    # Overrides
    overrides_audit: pd.DataFrame = field(default_factory=pd.DataFrame)

    # Automation
    automation_table: pd.DataFrame = field(default_factory=pd.DataFrame)

    # Needs & Replenishment
    needs: pd.DataFrame = field(default_factory=pd.DataFrame)
    needs_audit: pd.DataFrame = field(default_factory=pd.DataFrame)
    plan: pd.DataFrame = field(default_factory=pd.DataFrame)
    plan_audit: pd.DataFrame = field(default_factory=pd.DataFrame)
    resumen_replenishment: dict = field(default_factory=dict)
    resumen_ahorro: dict = field(default_factory=dict)
    homologated_from: dict = field(default_factory=dict)

    # Policies (kept for audit + export join)
    suppliers_df: pd.DataFrame = field(default_factory=pd.DataFrame)

    # Silence window
    silence_registry: pd.DataFrame = field(default_factory=pd.DataFrame)
    silence_silenced_rows: pd.DataFrame = field(default_factory=pd.DataFrame)

    # Final
    carga_maryun: pd.DataFrame = field(default_factory=pd.DataFrame)

    # Trazabilidad de fuentes oficiales (validacion + reconciliacion)
    source_metadata: dict = field(default_factory=dict)

    # Tenedores protegidos: (sku_id, ubicacion) con necesidad>0 que NO pueden
    # donar ese SKU. Se captura del needs COMPLETO antes de los filtros.
    protected_donors: set = field(default_factory=set)

    # Vista Power BI (se reutiliza para el push a Supabase)
    powerbi_view: pd.DataFrame = field(default_factory=pd.DataFrame)


# ---------------------------------------------------------------------------
# Stage 0 — Validacion de fuentes OFICIALES (gate con fallo explicito)
# ---------------------------------------------------------------------------
def stage_external_sync(ctx: "RunContext", acc: StageOutputs) -> StageOutputs:
    """Valida los archivos fuente OFICIALES antes de extraer nada.

    Reglas obligatorias:
      - Pronostico SOLO desde PRONOSTICOS_DEMANDA_v10.xlsx (hoja 'Pronosticos').
      - Clasificacion SOLO desde RESULTADO_ESTRATEGICO_ABC_XYZ_v3.xlsx (hoja 'Analisis').

    Valida existencia, accesibilidad, hoja correcta, columnas/encabezados y
    registra fecha de modificacion + conteo de filas. Si algo falla, lanza
    SourceFileError y el pipeline se detiene (NO usa copias/versiones alternas).
    """
    from app.validation.source_files import (
        validate_classification_source,
        validate_forecast_source,
    )
    log = get_logger("pipeline.stages.external_sync")
    log.info("stage.start", name="external_sync")

    # La validacion de fuentes oficiales aplica SOLO en produccion (mariadb).
    # En modo mock/dev no hay acceso a OneDrive: se omite y el pipeline usa
    # engines/baselines internos (ver build_forecast / build_classification).
    if ctx.settings.run.source != "mariadb":
        log.info("external_sync.skipped_non_prod", source=ctx.settings.run.source,
                 nota="validacion de fuentes oficiales solo en modo mariadb")
        return acc

    fc_path = ctx.paths.external_forecast_xlsx
    abc_path = ctx.paths.external_abc_xyz_xlsx

    if fc_path is None:
        from app.validation.source_files import SourceFileError
        raise SourceFileError(
            "MARYUN_EXTERNAL__FORECAST_XLSX no esta configurado en el .env. "
            "Es obligatorio apuntar al archivo oficial PRONOSTICOS_DEMANDA_v10.xlsx."
        )
    if abc_path is None:
        from app.validation.source_files import SourceFileError
        raise SourceFileError(
            "MARYUN_EXTERNAL__ABC_XYZ_XLSX no esta configurado en el .env. "
            "Es obligatorio apuntar al archivo oficial RESULTADO_ESTRATEGICO_ABC_XYZ_v3.xlsx."
        )

    # Fallan de forma explicita si no cumplen (no hay fallback silencioso).
    meta_fc = validate_forecast_source(fc_path)
    meta_abc = validate_classification_source(abc_path)

    acc.source_metadata = {
        "pronostico": meta_fc,
        "clasificacion": meta_abc,
    }
    log.info(
        "external_sync.validated",
        pronostico_ruta=meta_fc["ruta"],
        pronostico_mtime=meta_fc["mtime"],
        pronostico_filas_excel=meta_fc["filas_excel"],
        clasif_ruta=meta_abc["ruta"],
        clasif_mtime=meta_abc["mtime"],
        clasif_filas_excel=meta_abc["filas_excel"],
    )
    return acc


# ---------------------------------------------------------------------------
# Stage 1 — Extract
# ---------------------------------------------------------------------------
def stage_extract(ctx: "RunContext", acc: StageOutputs) -> StageOutputs:
    log = get_logger("pipeline.stages.extract")
    log.info("stage.start", name="extract")
    res = extract_all(ctx.gate, ctx.settings)
    acc.products = res["products"]
    acc.stock = res["stock"]
    acc.demand = res["demand"]
    acc.ultima_venta = res.get("ultima_venta", pd.DataFrame())
    return acc


# ---------------------------------------------------------------------------
# Stage 2 — Forecast
# ---------------------------------------------------------------------------
def stage_forecast(ctx: "RunContext", acc: StageOutputs) -> StageOutputs:
    log = get_logger("pipeline.stages.forecast")
    log.info("stage.start", name="forecast")
    forecast_df, engine_result = build_forecast(
        demand=acc.demand,
        gate=ctx.gate,
        settings=ctx.settings,
        params=ctx.params,
        paths=ctx.paths,
    )
    acc.forecast = forecast_df
    if engine_result is not None:
        acc.forecast_engine_audit = {
            "forecast_intervalos": engine_result.intervals,
            "forecast_resumen": engine_result.resumen,
            "forecast_perfiles": engine_result.perfiles,
            "forecast_correcciones": engine_result.correcciones,
        }
    # Reconciliacion Excel vs extraido (filas oficiales vs subidas al pipeline)
    meta = acc.source_metadata.get("pronostico", {})
    filas_excel = meta.get("filas_excel")
    log.info(
        "forecast.reconciliacion",
        filas_excel=filas_excel,
        filas_extraidas=len(forecast_df),
        coincide=(filas_excel == len(forecast_df)) if filas_excel else None,
    )
    return acc


# ---------------------------------------------------------------------------
# Stage 3 — Classification
# ---------------------------------------------------------------------------
def stage_classification(ctx: "RunContext", acc: StageOutputs) -> StageOutputs:
    log = get_logger("pipeline.stages.classification")
    log.info("stage.start", name="classification")
    cls_df, audit_df = build_classification(
        demand=acc.demand,
        products=acc.products,
        settings=ctx.settings,
        params=ctx.params,
        paths=ctx.paths,
        gate=ctx.gate,
    )
    acc.classification = cls_df
    acc.classification_audit = audit_df
    # Reconciliacion: filas v3 (modelo x sucursal) -> pares (sku x sucursal) expandidos.
    # NO es 1:1: cada modelo del Excel se propaga a sus variantes/SKU (documentado).
    meta = acc.source_metadata.get("clasificacion", {})
    if "fuente_clasificacion" in cls_df.columns:
        desde_v3 = int((cls_df["fuente_clasificacion"] == "v3_estrategico").sum())
        desde_override = int((cls_df["fuente_clasificacion"] == "override_manual").sum())
    else:
        desde_v3 = desde_override = None
    log.info(
        "classification.reconciliacion",
        filas_excel_v3=meta.get("filas_excel"),
        pares_sku_sucursal=len(cls_df),
        desde_v3=desde_v3,
        desde_override=desde_override,
        nota="expansion modelo->variantes documentada (no es 1:1)",
    )
    return acc


# ---------------------------------------------------------------------------
# Stage 4 — Homologation (analytical reinforcement)
# ---------------------------------------------------------------------------
def stage_homologation_analytical(ctx: "RunContext", acc: StageOutputs) -> StageOutputs:
    log = get_logger("pipeline.stages.homologation.analytical")
    log.info("stage.start", name="homologation_analytical")
    table = load_homologacion(
        ctx.paths.homologacion_productos_xlsx,
        products=acc.products,
        process_date=ctx.process_date,
        strict=False,
    )
    acc.homologacion_table = table

    if ctx.params.homologation.enabled and not table.is_empty():
        acc.forecast = reinforce_forecast_with_national(acc.forecast, table, ctx.params)
        acc.homologacion_audit = build_homologacion_audit(acc.forecast)

    return acc


# ---------------------------------------------------------------------------
# Stage 5 — Overrides (solo forecast)
# ---------------------------------------------------------------------------
def stage_overrides(ctx: "RunContext", acc: StageOutputs) -> StageOutputs:
    """Aplica overrides de forecast.

    NOTA: Los overrides de CLASIFICACION (override_clasificacion.xlsx) ya
    se aplican dentro de stage_classification, donde v3 es la fuente primaria
    y el override es la capa secundaria que solo modifica pares ya existentes
    en v3. Esta etapa NO los re-aplica para evitar doble-aplicacion.
    """
    log = get_logger("pipeline.stages.overrides")
    log.info("stage.start", name="overrides")

    fc_ov = load_forecast_overrides(
        ctx.paths.override_forecast_xlsx, params=ctx.params, strict=False
    )

    acc.forecast = apply_forecast_overrides(acc.forecast, fc_ov, ctx.params)
    # Override de clasificacion ya aplicado en stage_classification (v3_loader)
    acc.overrides_audit = build_overrides_audit(acc.forecast, acc.classification)

    return acc


# ---------------------------------------------------------------------------
# Stage 6 — Needs
# ---------------------------------------------------------------------------
def stage_needs(ctx: "RunContext", acc: StageOutputs) -> StageOutputs:
    log = get_logger("pipeline.stages.needs")
    log.info("stage.start", name="needs")
    # Proveedores: el stock de seguridad usa el lead time del proveedor
    # (default 10 días si falta). Se carga aquí porque la necesidad se calcula
    # en esta etapa, antes de la reposición.
    _suppliers_needs = load_suppliers(ctx.paths.proveedores_xlsx, strict=False)
    acc.needs = compute_needs(
        forecast=acc.forecast,
        stock=acc.stock,
        params=ctx.params,
        classification=acc.classification,
        demand_history=acc.demand,
        suppliers=_suppliers_needs,
    )
    # Capturar tenedores protegidos desde el needs COMPLETO (antes de
    # automation_filter / silence_filter). Un (sku, sucursal) con necesidad>0
    # NO puede donar ese SKU: evita re-donacion de stock en transito (H1),
    # el solape de umbrales need/sobrestock (H2) y la cobertura fantasma (H3).
    # Se usa necesidad>0 (no dispara) para ser inmune a silencio/automatizacion.
    from app.normalize.canonical import canonical_location as _cloc
    from app.normalize.canonical import canonical_sku as _csku
    if not acc.needs.empty and "necesidad" in acc.needs.columns:
        acc.protected_donors = {
            (_csku(str(r.sku_id)), _cloc(str(r.ubicacion)))
            for r in acc.needs.itertuples(index=False)
            if float(r.necesidad) > 0
        }
    log.info("needs.protected_donors", pares=len(acc.protected_donors))
    acc.needs_audit = build_needs_audit(acc.needs)
    return acc


# ---------------------------------------------------------------------------
# Stage 7 — Automation filter
# ---------------------------------------------------------------------------
def stage_automation_filter(ctx: "RunContext", acc: StageOutputs) -> StageOutputs:
    log = get_logger("pipeline.stages.automation_filter")
    log.info("stage.start", name="automation_filter")
    acc.automation_table = load_automation_table(
        ctx.paths.automatizacion_xlsx, strict=False
    )
    acc.needs = apply_automation_filter(
        acc.needs,
        acc.automation_table,
        classification_audit=acc.classification_audit,
    )
    # Ensure the audit also mirrors the filtered frame.
    acc.needs_audit = build_needs_audit(acc.needs)
    return acc


# ---------------------------------------------------------------------------
# Stage 7b — Silence filter (ventana de silencio por par sku/sucursal)
# ---------------------------------------------------------------------------
def stage_silence_filter(ctx: "RunContext", acc: StageOutputs) -> StageOutputs:
    log = get_logger("pipeline.stages.silence")
    log.info("stage.start", name="silence_filter")

    if not ctx.params.silence.enabled:
        log.info("silence.filter.disabled")
        return acc

    registry = load_registry(ctx.paths.silence_registry_csv)
    silenced = get_silenced_pairs(
        registry,
        process_date=ctx.process_date,
        window_days=ctx.params.silence.window_days,
    )

    acc.needs, silenced_rows = apply_silence_filter(acc.needs, silenced)
    acc.needs_audit = build_needs_audit(acc.needs)
    acc.silence_registry = registry
    acc.silence_silenced_rows = silenced_rows

    log.info(
        "silence.filter.applied",
        window_days=ctx.params.silence.window_days,
        pares_silenciados=len(silenced),
        filas_suprimidas=int(silenced_rows["dispara"].sum()) if not silenced_rows.empty else 0,
    )
    return acc


# ---------------------------------------------------------------------------
# Stage 8 — Homologation (operational substitution)
# ---------------------------------------------------------------------------
def stage_homologation_operational(ctx: "RunContext", acc: StageOutputs) -> StageOutputs:
    log = get_logger("pipeline.stages.homologation.operational")
    log.info("stage.start", name="homologation_operational")

    if acc.homologacion_table is None or acc.homologacion_table.is_empty():
        return acc

    candidates = compute_substitution_candidates(
        acc.needs, acc.stock, acc.homologacion_table, ctx.params
    )
    acc.needs, sub_audit = apply_operational_substitution(acc.needs, candidates)
    acc.transferencias_demanda = build_transferencias_demanda(sub_audit)
    acc.resumen_homologacion = build_resumen_homologacion(
        acc.homologacion_audit, sub_audit
    )

    # Build homologated-from map for plan provenance.
    if not sub_audit.empty:
        acc.homologated_from = {
            (row["sku_id_nacional"], row["ubicacion"]): row["sku_id_importado"]
            for _, row in sub_audit.iterrows()
        }
    return acc


# ---------------------------------------------------------------------------
# Stage 9 — Replenishment (4-layer)
# ---------------------------------------------------------------------------
def stage_replenishment(ctx: "RunContext", acc: StageOutputs) -> StageOutputs:
    log = get_logger("pipeline.stages.replenishment")
    log.info("stage.start", name="replenishment")

    distances = load_distances(ctx.paths.distancias_xlsx, strict=False)
    costs = load_costs(ctx.paths.costos_transporte_xlsx, strict=False)
    priority_cd = load_priority_cd(ctx.paths.prioridad_cd_xlsx, strict=False)
    suppliers = load_suppliers(ctx.paths.proveedores_xlsx, strict=False)
    rules = load_sku_location_rules(ctx.paths.reglas_sku_sucursal_xlsx, strict=False)

    acc.suppliers_df = suppliers.df

    # Demanda del mes actual (primer mes del forecast) — usada en sobrestock
    fc = acc.forecast.copy() if not acc.forecast.empty else pd.DataFrame()
    if not fc.empty and "mes" in fc.columns:
        fc["mes"] = pd.to_datetime(fc["mes"])
        mes_actual = fc["mes"].min()
        demanda_mes_actual = (
            fc[fc["mes"] == mes_actual]
            .groupby(["sku_id", "ubicacion"], as_index=False)["forecast_final"]
            .sum()
            .rename(columns={"forecast_final": "demanda_mes_actual"})
        )
    else:
        demanda_mes_actual = pd.DataFrame(columns=["sku_id", "ubicacion", "demanda_mes_actual"])

    result = run_replenishment(
        needs=acc.needs,
        stock=acc.stock,
        distances=distances,
        costs=costs,
        priority_cd=priority_cd,
        suppliers=suppliers,
        rules=rules,
        params=ctx.params,
        homologated_from=acc.homologated_from,
        ultima_venta=acc.ultima_venta,
        demanda_mes_actual=demanda_mes_actual,
        process_date=ctx.process_date,
        protected=acc.protected_donors,
    )
    acc.plan = result.plan
    acc.plan_audit = result.audit
    acc.resumen_replenishment = result.resumen
    return acc


# ---------------------------------------------------------------------------
# Stage 10 — Export
# ---------------------------------------------------------------------------
def stage_export(ctx: "RunContext", acc: StageOutputs) -> StageOutputs:
    log = get_logger("pipeline.stages.export")
    log.info("stage.start", name="export")

    # Build automation-source lookup for the carga file.
    autom_src: dict[tuple[str, str], str] = {}
    if not acc.automation_table.empty:
        for _, r in acc.automation_table.iterrows():
            autom_src[(r["sku_id"], r["ubicacion"])] = "excel"
    if not acc.classification_audit.empty and "clase_automatizacion" in acc.classification_audit.columns:
        for _, r in acc.classification_audit.iterrows():
            key = (r["sku_id"], r["ubicacion"])
            autom_src.setdefault(key, "classification_v3")

    acc.carga_maryun = build_carga_maryun(
        acc.plan,
        run_id=ctx.run_id,
        fecha_generacion=pd.Timestamp(ctx.process_date),
        suppliers_df=acc.suppliers_df,
        automation_source_by_pair=autom_src,
        classification_df=acc.classification,
        products=acc.products,
    )

    audits: dict[str, pd.DataFrame] = {
        "products": acc.products[["sku_id", "nombre", "marca", "familia", "tipo"]].copy()
            if not acc.products.empty else pd.DataFrame(),
        "forecast_final": acc.forecast,
        "classification_final": acc.classification,
        "classification_audit_v3": acc.classification_audit,
        "overrides_audit": acc.overrides_audit,
        "needs_audit": acc.needs_audit,
        "plan_audit": acc.plan_audit,
        "homologacion_audit": acc.homologacion_audit,
        "transferencias_demanda": acc.transferencias_demanda,
        "resumen_homologacion": acc.resumen_homologacion,
        "automation_table": acc.automation_table,
        "gate_descriptor": pd.DataFrame([ctx.gate.describe()]),
        "resumen_replenishment": pd.DataFrame([acc.resumen_replenishment]),
    }
    # Attach forecast-engine side tables, if any.
    for k, v in acc.forecast_engine_audit.items():
        audits[k] = v

    export_run(
        out_dir=ctx.out_dir,
        run_id=ctx.run_id,
        carga_maryun=acc.carga_maryun,
        audits=audits,
    )
    return acc


# ---------------------------------------------------------------------------
# Stage 10b — Power BI view (ruta FIJA, sobrescribe en cada run)
# ---------------------------------------------------------------------------
def stage_export_powerbi(ctx: "RunContext", acc: StageOutputs) -> StageOutputs:
    """Escribe la vista plana para Power BI en una ruta fija.

    Power BI siempre conecta a ``data/output/powerbi/plan_actual.xlsx`` —
    la ruta NO cambia entre runs. El archivo se sobrescribe cada vez.
    """
    log = get_logger("pipeline.stages.powerbi")
    log.info("stage.start", name="export_powerbi")

    df = build_powerbi_view(
        needs=acc.needs,
        plan=acc.plan,
        stock=acc.stock,
        products=acc.products,
        forecast=acc.forecast,
        classification=acc.classification,
        suppliers_df=acc.suppliers_df,
        run_id=ctx.run_id,
        fecha_generacion=pd.Timestamp(ctx.process_date),
    )
    write_powerbi_view(df, output_dir=ctx.paths.output_dir)
    acc.powerbi_view = df   # se reutiliza en stage_export_supabase
    return acc


# ---------------------------------------------------------------------------
# Stage 10c — Push a Supabase (backend en la nube del dashboard)
# ---------------------------------------------------------------------------
def stage_export_supabase(ctx: "RunContext", acc: StageOutputs) -> StageOutputs:
    """Empuja la corrida a Supabase (plan_semanal + plan_actual + historico).

    Se omite si Supabase no esta configurado (MARYUN_SUPABASE__ENABLED=false)
    — el pipeline local sigue funcionando sin la nube.
    """
    from app.export.supabase_sync import is_configured, push_run
    log = get_logger("pipeline.stages.supabase")
    log.info("stage.start", name="export_supabase")
    if not is_configured():
        log.info("export_supabase.skipped", reason="Supabase no configurado")
        return acc
    try:
        res = push_run(
            carga_maryun=acc.carga_maryun,
            powerbi_view=acc.powerbi_view,
            resumen=acc.resumen_replenishment,
            run_id=ctx.run_id,
            fecha_generacion=pd.Timestamp(ctx.process_date),
        )
        log.info("export_supabase.done", **{k: str(v) for k, v in res.items()})
    except Exception as exc:
        # No abortar el pipeline por un fallo de red/nube: el plan local ya quedo escrito.
        log.warning("export_supabase.failed", error=str(exc))
    return acc


# ---------------------------------------------------------------------------
# Stage 11 — Update silence registry (post-export)
# ---------------------------------------------------------------------------
def stage_update_silence(ctx: "RunContext", acc: StageOutputs) -> StageOutputs:
    """Actualiza el registro de silencio con los pares que generaron acción."""
    log = get_logger("pipeline.stages.silence")
    log.info("stage.start", name="update_silence")

    if not ctx.params.silence.enabled:
        return acc

    update_registry(
        registry=acc.silence_registry,
        plan=acc.plan,
        process_date=ctx.process_date,
        run_id=ctx.run_id,
        path=ctx.paths.silence_registry_csv,
    )
    return acc


# ---------------------------------------------------------------------------
# Stage 12 — Savings summary (post-silence update)
# ---------------------------------------------------------------------------
def stage_savings_summary(ctx: "RunContext", acc: StageOutputs) -> StageOutputs:
    """Agrega el ahorro histórico y escribe ``resumen_ahorro.json`` + log line.

    Lee todos los ``carga_maryun.csv`` bajo ``data/output/`` (incluido el que
    acabamos de escribir en ``stage_export``) y produce hoy/semana/mes.
    """
    import json

    log = get_logger("pipeline.stages.savings")
    log.info("stage.start", name="savings_summary")

    history = load_historical_carga(ctx.paths.output_dir)
    resumen = compute_ahorro_resumen(history, pd.Timestamp(ctx.process_date))

    # Persistir el JSON en la carpeta de esta corrida.
    out_path = ctx.out_dir / "resumen_ahorro.json"
    out_path.write_text(
        json.dumps(resumen.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # Log line destacada — se ve en consola al final del run.
    def _fmt(n: float) -> str:
        return f"${int(round(n)):,}".replace(",", ".")

    log.info(
        "savings.summary",
        hoy_clp=round(resumen.ahorro_hoy, 0),
        semana_clp=round(resumen.ahorro_semana, 0),
        mes_clp=round(resumen.ahorro_mes, 0),
        runs=resumen.runs_considerados,
    )
    log.info(
        "savings.message",
        mensaje=(
            f"Ahorro generado — hoy: {_fmt(resumen.ahorro_hoy)} CLP  |  "
            f"semana: {_fmt(resumen.ahorro_semana)} CLP  |  "
            f"mes: {_fmt(resumen.ahorro_mes)} CLP"
        ),
    )
    acc.resumen_ahorro = resumen.to_dict()
    return acc


__all__ = [
    "StageOutputs",
    "stage_external_sync",
    "stage_extract",
    "stage_forecast",
    "stage_classification",
    "stage_homologation_analytical",
    "stage_overrides",
    "stage_needs",
    "stage_automation_filter",
    "stage_silence_filter",
    "stage_homologation_operational",
    "stage_replenishment",
    "stage_export",
    "stage_export_powerbi",
    "stage_export_supabase",
    "stage_update_silence",
    "stage_savings_summary",
]
