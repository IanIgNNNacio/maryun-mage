"""Layer 3 — ship from a distribution centre.

CDs always feed sucursales (never each other in this version). We respect
``prioridad_cd`` when configured — the first CD with available stock wins,
skipping to the next if it's empty. If no priority is set we fall back to
the ranking score.

Rule interaction:
    * rule.solo_desde_cd=CD SUR     → only that CD may source the sucursal
                                      (enforced here; everything else is skipped).
    * rule.bloqueado at destino     → skip.
"""
from __future__ import annotations

import pandas as pd

from app.config.engine_params import EngineParams
from app.normalize.canonical import canonical_location, canonical_sku, is_cd
from app.policies.costs import CostMatrix
from app.policies.distances import DistanceMatrix
from app.policies.priority_cd import CDPriority
from app.policies.rules import Rule, SkuLocationRules
from app.replenishment.layers.inmovilizado import Move
from app.replenishment.ranking import Candidate, rank_candidates
from app.replenishment.state import StockLedger


def allocate_cd(
    needs: pd.DataFrame,
    ledger: StockLedger,
    *,
    distances: DistanceMatrix,
    costs: CostMatrix,
    rules: SkuLocationRules,
    priority_cd: CDPriority,
    params: EngineParams,
    already_moved: dict[tuple[str, str], float] | None = None,
) -> list[Move]:
    moves: list[Move] = []
    if needs.empty:
        return moves

    weights = params.replenishment.ranking
    respect_priority = bool(params.replenishment.respect_prioridad_cd)
    if already_moved is None:
        already_moved = {}

    # Regla de escasez: ordenar destinos por saldo pendiente DESC (después de
    # inmovilizado + sobrestock) dentro de cada SKU. El destino con mayor gap
    # se sirve primero desde los CDs — cuando un CD se vacía, el remanente
    # parcial va al siguiente destino en la lista ordenada.
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

        rule_dest: Rule = rules.rule(sku, destino)
        if rule_dest.bloqueado:
            continue

        # Restrict to allowed CDs.
        allowed_cds: list[str] | None = None
        if rule_dest.solo_desde_cd:
            allowed_cds = [canonical_location(rule_dest.solo_desde_cd)]
        elif respect_priority:
            allowed_cds = priority_cd.preferred_cds(destino)

        # Build (cd, available) pairs for this sku.
        cd_pool = [
            (ubic, qty) for ubic, qty in ledger.locations_with_stock(sku) if is_cd(ubic)
        ]
        if allowed_cds is not None:
            pri_order = {cd: i for i, cd in enumerate(allowed_cds)}
            cd_pool = [p for p in cd_pool if p[0] in pri_order]
            cd_pool.sort(key=lambda p: pri_order[p[0]])

        if not cd_pool:
            continue

        # If we respect priority, go strictly in priority order (no ranking).
        if respect_priority and allowed_cds is not None and rule_dest.solo_desde_cd is None:
            for origen, qty in cd_pool:
                if remaining <= 0:
                    break
                take = ledger.debit(sku, origen, min(qty, remaining))
                if take <= 0:
                    continue
                ledger.credit(sku, destino, take)
                score = 1.0  # priority-driven, not ranking-driven
                moves.append(Move(
                    sku_id=sku,
                    origen=origen,
                    destino=destino,
                    capa="cd",
                    cantidad=float(take),
                    score=float(score),
                    motivo=f"{origen} (prioridad CD)",
                ))
                remaining -= take
                already_moved[(sku, destino)] = already_moved.get((sku, destino), 0.0) + take
            continue

        # Otherwise, score the available CDs by distance/cost/origin-impact.
        candidates = [
            Candidate(
                origen=origen,
                destino=destino,
                km=distances.km(origen, destino),
                costo=costs.costo(origen, destino),
                share_origin=(min(remaining, qty) / qty) if qty > 0 else 1.0,
                disponible=qty,
            )
            for (origen, qty) in cd_pool
        ]
        for cand, score in rank_candidates(candidates, weights):
            if remaining <= 0:
                break
            take = ledger.debit(sku, cand.origen, min(cand.disponible, remaining))
            if take <= 0:
                continue
            ledger.credit(sku, destino, take)
            moves.append(Move(
                sku_id=sku,
                origen=cand.origen,
                destino=destino,
                capa="cd",
                cantidad=float(take),
                score=float(score),
                motivo=f"{cand.origen} (ranking)",
            ))
            remaining -= take
            already_moved[(sku, destino)] = already_moved.get((sku, destino), 0.0) + take

    return moves
