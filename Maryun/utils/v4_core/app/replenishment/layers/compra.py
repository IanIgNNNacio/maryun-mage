"""Layer 4 — emit a purchase order to the default supplier.

If we still have unmet need after inmovilizado+sobrestock+CD, we schedule
a purchase. The order respects supplier MOQ and ``multiplo_compra`` so
downstream procurement can execute without extra massaging.

The move's ``origen`` is the supplier name (not a location code); the
ledger is **not** credited — purchase stock lands later, outside the
current run.
"""
from __future__ import annotations

import math

import pandas as pd

from app.config.engine_params import EngineParams
from app.normalize.canonical import canonical_location, canonical_sku
from app.policies.rules import Rule, SkuLocationRules
from app.policies.suppliers import SupplierInfo, SupplierTable
from app.replenishment.layers.inmovilizado import Move


def _round_to_multiple(qty: float, multiple: float) -> float:
    if multiple <= 0:
        return qty
    return math.ceil(qty / multiple) * multiple


def allocate_compra(
    needs: pd.DataFrame,
    *,
    suppliers: SupplierTable,
    rules: SkuLocationRules,
    params: EngineParams,
    already_moved: dict[tuple[str, str], float] | None = None,
) -> list[Move]:
    moves: list[Move] = []
    if needs.empty:
        return moves
    if already_moved is None:
        already_moved = {}

    # Consistencia con las otras capas: ordenar por saldo pendiente DESC dentro
    # de cada SKU. En compra no hay escasez real (el proveedor emite lo que se
    # pida), pero mantener el mismo orden hace el audit más legible.
    pending = needs[needs["dispara"]].copy()
    pending["_remaining"] = pending.apply(
        lambda r: max(
            0.0,
            float(r["necesidad"]) - already_moved.get(
                (canonical_sku(r["sku_id"]), canonical_location(r["ubicacion"])), 0.0,
            ),
        ),
        axis=1,
    )
    pending = pending.sort_values(
        ["sku_id", "_remaining"], ascending=[True, False],
    )

    for _, row in pending.iterrows():
        sku = canonical_sku(row["sku_id"])
        destino = canonical_location(row["ubicacion"])
        already = already_moved.get((sku, destino), 0.0)
        remaining = max(0.0, float(row["necesidad"]) - already)
        if remaining <= 0:
            continue

        rule: Rule = rules.rule(sku, destino)
        if rule.bloqueado:
            continue

        # Resolución: busca lead time específico para la sucursal destino;
        # si no existe, cae al default (fila sin ubicacion en el Excel).
        info: SupplierInfo | None = suppliers.info(sku, destino)
        proveedor = info.proveedor if info else "PROVEEDOR_DESCONOCIDO"
        moq = float(info.moq) if info else 0.0
        mult = float(info.multiplo_compra) if info else 1.0
        lead = int(info.lead_time_dias) if info else 0

        qty = max(remaining, moq)
        qty = _round_to_multiple(qty, mult)
        if qty <= 0:
            continue

        moves.append(Move(
            sku_id=sku,
            origen=proveedor,
            destino=destino,
            capa="compra",
            cantidad=float(qty),
            score=0.0,  # not ranked — purchase is the fallback
            motivo=(
                f"compra a {proveedor} (lead {lead}d, MOQ {moq:g}, múltiplo {mult:g})"
            ),
        ))
        already_moved[(sku, destino)] = already_moved.get((sku, destino), 0.0) + remaining

    return moves
