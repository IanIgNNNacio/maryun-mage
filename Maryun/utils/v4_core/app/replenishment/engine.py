"""Replenishment engine — orchestrate the 4 layers and emit the plan."""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from app.config.engine_params import EngineParams
from app.normalize.schemas import ReplenishmentPlanSchema
from app.policies.costs import CostMatrix
from app.policies.distances import DistanceMatrix
from app.policies.priority_cd import CDPriority
from app.policies.rules import SkuLocationRules
from app.policies.suppliers import SupplierTable
from app.replenishment.layers import (
    allocate_cd,
    allocate_compra,
    allocate_inmovilizado,
    allocate_sobrestock,
)
from app.replenishment.layers.inmovilizado import Move
from app.replenishment.state import StockLedger
from app.utils.logging import get_logger


@dataclass
class ReplenishmentResult:
    plan: pd.DataFrame                        # ``ReplenishmentPlanSchema``-valid
    audit: pd.DataFrame                       # per-need trace (layer contributions)
    ledger_snapshot: pd.DataFrame             # post-run stock ledger
    resumen: dict[str, float | int] = field(default_factory=dict)


def _moves_to_df(moves: list[Move], homologated_from: dict[tuple[str, str], str] | None = None) -> pd.DataFrame:
    if not moves:
        return pd.DataFrame(columns=[
            "sku_id", "origen", "destino", "capa",
            "cantidad", "score", "motivo", "homologado_desde_sku",
        ])
    rows = []
    homologated_from = homologated_from or {}
    for m in moves:
        rows.append({
            "sku_id": m.sku_id,
            "origen": m.origen,
            "destino": m.destino,
            "capa": m.capa,
            "cantidad": float(m.cantidad),
            "score": float(m.score),
            "motivo": m.motivo,
            "homologado_desde_sku": homologated_from.get((m.sku_id, m.destino)),
        })
    return pd.DataFrame(rows)


def _apply_min_cantidad(plan_df: pd.DataFrame, min_cantidad: float) -> pd.DataFrame:
    """Redondea ``cantidad`` a entero (medio-arriba) y descarta movimientos por
    debajo del minimo. Evita ordenes/traslados de fracciones de unidad
    (ej. 0.005 uds que no vale la pena solicitar)."""
    if plan_df.empty:
        return plan_df
    out = plan_df.copy()
    # round-half-up para cantidades >= 0: int(x + 0.5)
    out["cantidad"] = (out["cantidad"].astype(float) + 0.5).astype(int).astype(float)
    out = out[out["cantidad"] >= float(min_cantidad)]
    return out.reset_index(drop=True)


def _build_audit(
    needs: pd.DataFrame,
    moves_df: pd.DataFrame,
) -> pd.DataFrame:
    """Per (sku, destino) audit row: how much did each layer contribute?"""
    if needs.empty:
        return pd.DataFrame(columns=[
            "sku_id", "ubicacion", "necesidad", "cubierto_inmovilizado",
            "cubierto_sobrestock", "cubierto_cd", "cubierto_compra",
            "cubierto_total", "descubierto",
        ])

    per_layer = (
        moves_df.groupby(["sku_id", "destino", "capa"], as_index=False)["cantidad"]
        .sum()
        .rename(columns={"destino": "ubicacion"})
        if not moves_df.empty
        else pd.DataFrame(columns=["sku_id", "ubicacion", "capa", "cantidad"])
    )
    pivot = per_layer.pivot_table(
        index=["sku_id", "ubicacion"],
        columns="capa",
        values="cantidad",
        aggfunc="sum",
        fill_value=0.0,
    ).reset_index()

    for layer in ["inmovilizado", "sobrestock", "cd", "compra"]:
        col = f"cubierto_{layer}"
        if layer in pivot.columns:
            pivot = pivot.rename(columns={layer: col})
        else:
            pivot[col] = 0.0

    audit = needs[["sku_id", "ubicacion", "necesidad", "dispara"]].merge(
        pivot, on=["sku_id", "ubicacion"], how="left",
    )
    for c in ("cubierto_inmovilizado", "cubierto_sobrestock", "cubierto_cd", "cubierto_compra"):
        if c not in audit.columns:
            audit[c] = 0.0
        audit[c] = audit[c].fillna(0.0).astype(float)
    audit["cubierto_total"] = (
        audit["cubierto_inmovilizado"] + audit["cubierto_sobrestock"]
        + audit["cubierto_cd"] + audit["cubierto_compra"]
    )

    # Cobertura NETA = entradas − salidas. Si una sucursal recibio Y dono el
    # mismo SKU (traslado), restamos lo que dono para no inflar su cobertura.
    # Con el fix de "tenedores protegidos" esto deberia ser 0, pero se calcula
    # de forma defensiva para que la auditoria nunca reporte cobertura fantasma.
    if moves_df is not None and not moves_df.empty:
        salidas = (
            moves_df[moves_df["capa"].isin(["inmovilizado", "sobrestock"])]
            .groupby(["sku_id", "origen"], as_index=False)["cantidad"].sum()
            .rename(columns={"origen": "ubicacion", "cantidad": "donado_de_vuelta"})
        )
        audit = audit.merge(salidas, on=["sku_id", "ubicacion"], how="left")
    else:
        audit["donado_de_vuelta"] = 0.0
    audit["donado_de_vuelta"] = audit.get("donado_de_vuelta", 0.0)
    audit["donado_de_vuelta"] = audit["donado_de_vuelta"].fillna(0.0).astype(float)

    audit["cubierto_neto"] = (audit["cubierto_total"] - audit["donado_de_vuelta"]).clip(lower=0.0)
    audit["descubierto"] = (audit["necesidad"] - audit["cubierto_neto"]).clip(lower=0.0)
    return audit


def _build_dias_lookup(
    ultima_venta: pd.DataFrame | None,
    process_date: "date | None" = None,
) -> dict[tuple[str, str], float]:
    """Construye lookup (sku_id, ubicacion) → dias_sin_venta.

    Si no hay registro de ultima venta para un par → float('inf') (nunca vendio).
    """
    from datetime import date as _date
    from app.normalize.canonical import canonical_sku, canonical_location
    lookup: dict[tuple[str, str], float] = {}
    if ultima_venta is None or ultima_venta.empty:
        return lookup
    today = process_date or _date.today()
    if hasattr(today, "date"):          # pd.Timestamp → date
        today = today.date()
    for r in ultima_venta.itertuples(index=False):
        sku = canonical_sku(str(r.sku_id))
        ubic = canonical_location(str(r.ubicacion))
        uv = r.ultima_venta
        if sku and ubic and uv is not None:
            try:
                uv_date = uv.date() if hasattr(uv, "date") else uv
                lookup[(sku, ubic)] = float((today - uv_date).days)
            except Exception:
                pass
    return lookup


def _build_demanda_mes_lookup(demanda_mes_actual: pd.DataFrame | None) -> dict[tuple[str, str], float]:
    """Lookup (sku_id, ubicacion) → demanda del mes actual."""
    from app.normalize.canonical import canonical_sku, canonical_location
    lookup: dict[tuple[str, str], float] = {}
    if demanda_mes_actual is None or demanda_mes_actual.empty:
        return lookup
    for r in demanda_mes_actual.itertuples(index=False):
        sku = canonical_sku(str(r.sku_id))
        ubic = canonical_location(str(r.ubicacion))
        if sku and ubic:
            lookup[(sku, ubic)] = float(r.demanda_mes_actual)
    return lookup


def run_replenishment(
    needs: pd.DataFrame,
    stock: pd.DataFrame,
    *,
    distances: DistanceMatrix,
    costs: CostMatrix,
    priority_cd: CDPriority,
    suppliers: SupplierTable,
    rules: SkuLocationRules,
    params: EngineParams,
    homologated_from: dict[tuple[str, str], str] | None = None,
    ultima_venta: pd.DataFrame | None = None,
    demanda_mes_actual: pd.DataFrame | None = None,
    process_date: "date | None" = None,
    protected: set[tuple[str, str]] | None = None,
) -> ReplenishmentResult:
    """Return the full replenishment plan + audit for a given needs frame.

    ``homologated_from`` is the (sku_nacional, sucursal) → sku_importado map
    produced by the operational homologation layer: when non-empty, plan
    rows inherit the original importado SKU for traceability.
    """
    log = get_logger("replenishment.engine")
    ledger = StockLedger(stock)
    protected = protected or set()

    # Lookups derivados de ultima_venta y demanda_mes_actual
    dias_lookup = _build_dias_lookup(ultima_venta, process_date)
    demanda_mes_lookup = _build_demanda_mes_lookup(demanda_mes_actual)
    log.info(
        "replenishment.mobility_lookups",
        pares_ultima_venta=len(dias_lookup),
        pares_demanda_mes=len(demanda_mes_lookup),
    )

    # Tracks per-(sku, destino) how much has already been sourced across layers,
    # so each subsequent layer only chases the residual.
    moved_acc: dict[tuple[str, str], float] = {}
    all_moves: list[Move] = []

    layers_order = params.replenishment.layers_order

    for layer in layers_order:
        if layer == "inmovilizado":
            part = allocate_inmovilizado(
                needs, ledger,
                distances=distances, costs=costs, rules=rules, params=params,
                dias_sin_venta=dias_lookup,
                protected=protected,
            )
            for m in part:
                moved_acc[(m.sku_id, m.destino)] = (
                    moved_acc.get((m.sku_id, m.destino), 0.0) + m.cantidad
                )
            all_moves.extend(part)
        elif layer == "sobrestock":
            part = allocate_sobrestock(
                needs, ledger,
                distances=distances, costs=costs, rules=rules, params=params,
                already_moved=moved_acc,
                dias_sin_venta=dias_lookup,
                demanda_mes=demanda_mes_lookup,
                protected=protected,
            )
            all_moves.extend(part)
        elif layer == "cd":
            part = allocate_cd(
                needs, ledger,
                distances=distances, costs=costs, rules=rules,
                priority_cd=priority_cd, params=params,
                already_moved=moved_acc,
            )
            all_moves.extend(part)
        elif layer == "compra":
            part = allocate_compra(
                needs,
                suppliers=suppliers, rules=rules, params=params,
                already_moved=moved_acc,
            )
            all_moves.extend(part)
        else:
            log.warning("replenishment.engine.unknown_layer", layer=layer)

    plan_df = _moves_to_df(all_moves, homologated_from=homologated_from)
    # Redondea a entero y descarta movimientos por debajo del minimo (ej. 0.005 uds).
    plan_df = _apply_min_cantidad(plan_df, params.replenishment.min_cantidad_unidades)
    if not plan_df.empty:
        plan_df = ReplenishmentPlanSchema.validate(plan_df, lazy=True)

    audit_df = _build_audit(needs, plan_df)

    resumen = {
        "filas_plan": int(len(plan_df)),
        "sku_destino_pares": int(audit_df[audit_df["dispara"]].shape[0]) if "dispara" in audit_df.columns else 0,
        "unidades_totales": float(plan_df["cantidad"].sum()) if not plan_df.empty else 0.0,
        "unidades_inmovilizado": float(audit_df["cubierto_inmovilizado"].sum()) if not audit_df.empty else 0.0,
        "unidades_sobrestock": float(audit_df["cubierto_sobrestock"].sum()) if not audit_df.empty else 0.0,
        "unidades_cd": float(audit_df["cubierto_cd"].sum()) if not audit_df.empty else 0.0,
        "unidades_compra": float(audit_df["cubierto_compra"].sum()) if not audit_df.empty else 0.0,
        "unidades_descubiertas": float(audit_df["descubierto"].sum()) if not audit_df.empty else 0.0,
    }
    log.info("replenishment.engine.done", **{k: v for k, v in resumen.items()})

    return ReplenishmentResult(
        plan=plan_df,
        audit=audit_df,
        ledger_snapshot=ledger.snapshot(),
        resumen=resumen,
    )
