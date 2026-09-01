"""Layer 2 — redistribuir sobrestock entre sucursales.

REGLA DE NEGOCIO:
    Una sucursal es DONANTE de sobrestock para un SKU cuando:
        1. stock_actual > factor_demanda_mes × demanda_mes_actual  (default: 1.30×)
           Es decir, tiene mas del 130% de la demanda del mes en stock.
        2. dias_sin_venta <= dias_sin_venta_max  (default: 89 dias)
           Es decir, ha tenido venta reciente (no es inmovilizado).

    Si el par (sku, sucursal) no tiene registro de ultima venta → NO califica
    como donante de sobrestock (se trata como inmovilizado potencial).

    El donante puede ceder hasta cap_fraction (30%) del excedente sobre el umbral,
    dejando colchon de variabilidad en la sucursal origen.
"""
from __future__ import annotations

import pandas as pd

from app.config.engine_params import EngineParams
from app.normalize.canonical import canonical_location, canonical_sku, is_sucursal
from app.policies.costs import CostMatrix
from app.policies.distances import DistanceMatrix
from app.policies.rules import Rule, SkuLocationRules
from app.replenishment.layers.inmovilizado import Move
from app.replenishment.ranking import Candidate, rank_candidates
from app.replenishment.state import StockLedger


def _exceso_sobrestock(
    sku: str,
    ubic: str,
    ledger: StockLedger,
    dias_sin_venta: dict[tuple[str, str], float],
    demanda_mes: dict[tuple[str, str], float],
    factor: float,
    dias_max: int,
) -> float:
    """Unidades de sobrestock disponibles para donar.

    Retorna 0 si:
    - No hay venta reciente (dias > dias_max) → es inmovilizado, no sobrestock.
    - No hay registro de venta → float('inf') dias → no califica.
    - Stock no supera el umbral (factor × demanda_mes).
    """
    dias = dias_sin_venta.get((sku, ubic), float("inf"))
    if dias > dias_max:
        return 0.0   # sin venta reciente: es inmovilizado, no sobrestock

    dem_mes = demanda_mes.get((sku, ubic), 0.0)
    if dem_mes <= 0:
        return 0.0   # sin demanda en el mes → no hay umbral de referencia

    umbral = factor * dem_mes
    stock_actual = ledger.available(sku, ubic)
    return max(0.0, stock_actual - umbral)


def allocate_sobrestock(
    needs: pd.DataFrame,
    ledger: StockLedger,
    *,
    distances: DistanceMatrix,
    costs: CostMatrix,
    rules: SkuLocationRules,
    params: EngineParams,
    already_moved: dict[tuple[str, str], float] | None = None,
    dias_sin_venta: dict[tuple[str, str], float] | None = None,
    demanda_mes: dict[tuple[str, str], float] | None = None,
    protected: set[tuple[str, str]] | None = None,
) -> list[Move]:
    """Capa 2: redistribuir sobrestock (stock > 130% demanda mes, con venta <= 89d).

    Args:
        dias_sin_venta: lookup (sku_id, ubicacion) → dias desde ultima facturacion.
        demanda_mes: lookup (sku_id, ubicacion) → demanda del mes actual.
        protected: set de (sku_id, ubicacion) con necesidad>0 que NO pueden donar.
    """
    moves: list[Move] = []
    if needs.empty:
        return moves

    dias_sin_venta = dias_sin_venta or {}
    demanda_mes = demanda_mes or {}
    protected = protected or set()
    weights = params.replenishment.ranking
    cap = float(params.replenishment.overstock.cap_fraction)
    factor = float(params.replenishment.overstock.factor_demanda_mes)
    dias_max = int(params.replenishment.overstock.dias_sin_venta_max)
    min_excess = float(params.replenishment.overstock.min_donor_excess_units)

    if already_moved is None:
        already_moved = {}

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
    pending = pending.sort_values(["sku_id", "_remaining"], ascending=[True, False])

    for _, row in pending.iterrows():
        sku = canonical_sku(row["sku_id"])
        destino = canonical_location(row["ubicacion"])
        already = already_moved.get((sku, destino), 0.0)
        remaining = max(0.0, float(row["necesidad"]) - already)
        if remaining <= 0:
            continue

        rule_dest: Rule = rules.rule(sku, destino)
        if rule_dest.bloqueado or rule_dest.solo_desde_cd:
            continue

        donors = []
        for ubic, qty in ledger.locations_with_stock(sku):
            if ubic == destino or not is_sucursal(ubic):
                continue
            if (sku, ubic) in protected:        # ← no donar si la sucursal necesita el SKU
                continue
            rule_orig = rules.rule(sku, ubic)
            if rule_orig.bloqueado:
                continue
            exceso = _exceso_sobrestock(
                sku, ubic, ledger, dias_sin_venta, demanda_mes, factor, dias_max,
            )
            if exceso < min_excess:
                continue
            donable = min(qty, exceso * cap)
            if donable <= 0:
                continue
            donors.append((ubic, donable, qty, exceso))

        if not donors:
            continue

        candidates = [
            Candidate(
                origen=ubic,
                destino=destino,
                km=distances.km(ubic, destino),
                costo=costs.costo(ubic, destino),
                share_origin=(donable / qty) if qty > 0 else 1.0,
                disponible=donable,
            )
            for (ubic, donable, qty, _exceso) in donors
        ]

        for cand, score in rank_candidates(candidates, weights):
            if remaining <= 0:
                break
            take = ledger.debit(sku, cand.origen, min(cand.disponible, remaining))
            if take <= 0:
                continue
            ledger.credit(sku, destino, take)
            dias = dias_sin_venta.get((sku, cand.origen), float("inf"))
            dias_txt = f"{int(dias)}d" if dias != float("inf") else "sin registro"
            dem = demanda_mes.get((sku, cand.origen), 0.0)
            moves.append(Move(
                sku_id=sku,
                origen=cand.origen,
                destino=destino,
                capa="sobrestock",
                cantidad=float(take),
                score=float(score),
                motivo=(
                    f"sobrestock en {cand.origen}: "
                    f"stock>{factor:.0%} demanda_mes({dem:.0f}), "
                    f"{dias_txt} sin venta, dona {int(cap*100)}% excedente"
                ),
            ))
            remaining -= take
            already_moved[(sku, destino)] = already_moved.get((sku, destino), 0.0) + take

    return moves
