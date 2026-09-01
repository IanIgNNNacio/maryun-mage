"""Layer 1 — redistribuir stock inmovilizado.

REGLA DE NEGOCIO:
    Un (SKU, sucursal_origen) es INMOVILIZADO cuando NO ha tenido venta
    (facturacion) en los ultimos >= N dias (default: 90).

    Esta condicion NO depende de la demanda esperada.
    Si el origen no tiene registro de ultima venta → se considera inmovilizado
    (nunca vendio = stock muerto).

    El origen inmovilizado cede TODO su stock disponible al destino que lo
    necesita (no hay cap).
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.config.engine_params import EngineParams
from app.normalize.canonical import canonical_location, canonical_sku, is_sucursal
from app.policies.costs import CostMatrix
from app.policies.distances import DistanceMatrix
from app.policies.rules import Rule, SkuLocationRules
from app.replenishment.ranking import Candidate, rank_candidates
from app.replenishment.state import StockLedger


@dataclass(frozen=True)
class Move:
    sku_id: str
    origen: str
    destino: str
    capa: str
    cantidad: float
    score: float
    motivo: str


def _es_inmovilizado(
    sku: str,
    ubic: str,
    dias_sin_venta: dict[tuple[str, str], float],
    umbral_dias: int,
) -> bool:
    """True si el par (sku, ubic) califica como inmovilizado.

    - Si existe registro de ultima venta: dias >= umbral_dias → inmovilizado.
    - Si NO existe registro (nunca vendio): tambien inmovilizado.
    """
    dias = dias_sin_venta.get((sku, ubic), float("inf"))
    return dias >= umbral_dias


def _inmoviles_for(
    sku: str,
    destino: str,
    ledger: StockLedger,
    dias_sin_venta: dict[tuple[str, str], float],
    umbral_dias: int,
    protected: set[tuple[str, str]],
) -> list[tuple[str, float]]:
    """Sucursales que tienen stock de este SKU y califican como inmovilizadas.

    Se EXCLUYE cualquier (sku, ubic) protegido (necesidad>0): una sucursal que
    necesita el SKU no puede donarlo (evita re-donacion / cobertura fantasma).
    """
    candidates = []
    for ubic, qty in ledger.locations_with_stock(sku):
        if ubic == destino:
            continue
        if not is_sucursal(ubic):
            continue
        if qty <= 0:
            continue
        if (sku, ubic) in protected:        # ← no donar si la sucursal necesita el SKU
            continue
        if _es_inmovilizado(sku, ubic, dias_sin_venta, umbral_dias):
            candidates.append((ubic, qty))
    return candidates


def allocate_inmovilizado(
    needs: pd.DataFrame,
    ledger: StockLedger,
    *,
    distances: DistanceMatrix,
    costs: CostMatrix,
    rules: SkuLocationRules,
    params: EngineParams,
    dias_sin_venta: dict[tuple[str, str], float] | None = None,
    protected: set[tuple[str, str]] | None = None,
) -> list[Move]:
    """Capa 1: redistribuir stock inmovilizado (>= 90 dias sin venta).

    Args:
        dias_sin_venta: lookup (sku_id, ubicacion) → dias transcurridos desde
                        la ultima facturacion. Ausente → float('inf').
        protected: set de (sku_id, ubicacion) con necesidad>0 que NO pueden donar.
    """
    moves: list[Move] = []
    if needs.empty:
        return moves

    dias_sin_venta = dias_sin_venta or {}
    protected = protected or set()
    umbral = int(params.replenishment.inmovilizado.dias_sin_venta_min)
    weights = params.replenishment.ranking

    pending = needs[needs["dispara"]].copy().sort_values(
        ["sku_id", "necesidad"], ascending=[True, False],
    )

    for _, row in pending.iterrows():
        sku = canonical_sku(row["sku_id"])
        destino = canonical_location(row["ubicacion"])
        remaining = float(row["necesidad"])
        if remaining <= 0:
            continue

        rule_dest: Rule = rules.rule(sku, destino)
        if rule_dest.bloqueado:
            continue

        sources = _inmoviles_for(sku, destino, ledger, dias_sin_venta, umbral, protected)
        if not sources:
            continue

        candidates: list[Candidate] = []
        for origen, qty in sources:
            rule_orig = rules.rule(sku, origen)
            if rule_orig.bloqueado:
                continue
            if rule_dest.solo_desde_cd:
                continue
            candidates.append(Candidate(
                origen=origen,
                destino=destino,
                km=distances.km(origen, destino),
                costo=costs.costo(origen, destino),
                share_origin=1.0,   # dona todo
                disponible=qty,
            ))

        for cand, score in rank_candidates(candidates, weights):
            if remaining <= 0:
                break
            take = ledger.debit(sku, cand.origen, min(cand.disponible, remaining))
            if take <= 0:
                continue
            ledger.credit(sku, destino, take)
            dias = dias_sin_venta.get((sku, cand.origen), float("inf"))
            dias_txt = f"{int(dias)}d" if dias != float("inf") else "sin registro"
            moves.append(Move(
                sku_id=sku,
                origen=cand.origen,
                destino=destino,
                capa="inmovilizado",
                cantidad=float(take),
                score=float(score),
                motivo=f"inmovilizado en {cand.origen} ({dias_txt} sin venta, umbral {umbral}d)",
            ))
            remaining -= take

    return moves
