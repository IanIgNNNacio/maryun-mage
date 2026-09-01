"""Construye la clasificacion de productos desde el archivo v3.

ARQUITECTURA ESTRICTA (en orden):
    1. Cargar RESULTADO_ESTRATEGICO_ABC_XYZ_v3.xlsx → fuente PRIMARIA y UNICA
       de la clasificacion base. Productos no presentes en v3 no son clasificados.
    2. Aplicar override_clasificacion.xlsx → capa SECUNDARIA que solo modifica
       pares (sku, sucursal) que YA estan en v3. Pares nuevos son ignorados.

El engine_v3 (estadistico) NO se usa para la clasificacion final. Si
``run_classification=true``, igualmente corre en paralelo para producir el
``classification_audit`` (auditoria comparativa), pero su salida no se
propaga al pipeline.
"""
from __future__ import annotations

import pandas as pd

from app.classification.baseline import classify as _classify_baseline
from app.classification.engine import run_classification_engine
from app.classification.v3_loader import apply_manual_override, load_v3_classification
from app.config.engine_params import EngineParams
from app.config.paths import ProjectPaths
from app.config.settings import Settings
from app.normalize.schemas import ClassificationSchema
from app.temporal.gate import TemporalGate
from app.utils.logging import get_logger


def _engine_fallback(
    demand: pd.DataFrame,
    products: pd.DataFrame,
    params: EngineParams,
    gate: TemporalGate | None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Clasificacion por engine/baseline cuando v3 NO esta disponible.

    Solo aplica en modo mock/dev: en produccion (mariadb) stage_external_sync
    ya garantiza que v3 existe antes de llegar aqui, asi que este camino no se
    toma con datos productivos.
    """
    if gate is not None:
        model_df, audit_df = run_classification_engine(demand, products, gate, params)
    else:
        model_df, audit_df = _classify_baseline(demand, products, params), pd.DataFrame()
    out = model_df.copy()
    out["fuente_clasificacion"] = "engine_baseline"
    out["abc_override"] = pd.NA
    out["xyz_override"] = pd.NA
    out["clase_final"] = out["clase_modelo"].astype(str)
    out["clase_fue_forzada"] = False
    out["motivo_override"] = pd.NA
    out["responsable_override"] = pd.NA
    return ClassificationSchema.validate(out, lazy=True), audit_df


def build_classification(
    demand: pd.DataFrame,
    products: pd.DataFrame,
    settings: Settings,
    params: EngineParams,
    paths: ProjectPaths,
    gate: TemporalGate | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Construye la clasificacion final aplicando v3 + override manual.

    Returns:
        (classification_df, audit_df)
            classification_df: filas (sku_id, ubicacion) presentes en v3 +
                            campos estrategicos + columnas del schema.
            audit_df: salida del engine_v3 (solo para auditoria comparativa).

    Raises:
        FileNotFoundError: si v3 no esta configurado o no existe.
    """
    log = get_logger("classification")

    # ----- 1. FUENTE PRIMARIA: v3 estrategico ------------------------------
    v3_path = paths.external_abc_xyz_xlsx
    if v3_path is None or not v3_path.exists():
        # En produccion (mariadb) stage_external_sync ya valido que v3 existe;
        # si llegamos aqui sin v3 es modo mock/dev → caer al engine baseline.
        log.warning(
            "classification.v3_unavailable_fallback_engine",
            path=str(v3_path),
            nota="v3 no disponible (modo mock/dev) — usando engine baseline",
        )
        return _engine_fallback(demand, products, params, gate)

    log.info("classification.source.primary", path=str(v3_path), mode="v3_estrategico_directo")
    base = load_v3_classification(v3_path, products)

    if base.empty:
        raise ValueError(
            f"v3 cargado pero sin pares (sku, ubicacion) que crucen con el catalogo. "
            f"Revisa que los nombres en v3 coincidan con products. Path: {v3_path}"
        )

    # ----- 2. CAPA SECUNDARIA: override manual -----------------------------
    base, override_metrics = apply_manual_override(base, paths.override_clasificacion_xlsx)
    log.info("classification.override.metrics", **override_metrics)

    # ----- 3. (Opcional) Engine v3 baseline SOLO para auditoria ------------
    audit_df = pd.DataFrame()
    if settings.run.run_classification and gate is not None:
        try:
            _, audit_df = run_classification_engine(demand, products, gate, params)
            log.info("classification.audit.engine_v3_done",
                     rows_audit=len(audit_df),
                     nota="engine_v3 solo para auditoria, no se usa para la clasificacion final")
        except Exception as exc:
            log.warning("classification.audit.engine_v3_failed", error=str(exc))

    # ----- 4. Validar contra el schema -------------------------------------
    out = base.copy()
    # Asegurar columnas del schema
    out["abc_override"] = pd.NA
    out["xyz_override"] = pd.NA
    out["clase_final"] = out["clase_modelo"].astype(str)
    out["clase_fue_forzada"] = (out["fuente_clasificacion"] == "override_manual")
    out["motivo_override"] = pd.NA
    out.loc[out["clase_fue_forzada"], "motivo_override"] = "override_manual"
    out["responsable_override"] = pd.NA

    log.info(
        "classification.final",
        total_pares=len(out),
        desde_v3=int((out["fuente_clasificacion"] == "v3_estrategico").sum()),
        desde_override=int((out["fuente_clasificacion"] == "override_manual").sum()),
    )

    return ClassificationSchema.validate(out, lazy=True), audit_df
