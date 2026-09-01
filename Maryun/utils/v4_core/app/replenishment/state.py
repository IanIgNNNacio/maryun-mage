"""Mutable stock ledger — the engine's scratchpad while it allocates moves.

Every layer consults the ledger for "how much can I take from origin X?"
and then debits it once a move is committed. The ledger therefore reflects
*post-allocation* inventory at any point — which lets later layers see
what the earlier ones already moved.
"""
from __future__ import annotations

from collections import defaultdict

import pandas as pd

from app.normalize.canonical import canonical_location, canonical_sku


class StockLedger:
    """In-memory (sku, ubicacion) → units remaining ledger."""

    __slots__ = ("_balance",)

    def __init__(self, stock: pd.DataFrame):
        self._balance: dict[tuple[str, str], float] = defaultdict(float)
        if stock is None or stock.empty:
            return
        for r in stock.itertuples(index=False):
            sku = canonical_sku(getattr(r, "sku_id"))
            ubic = canonical_location(getattr(r, "ubicacion"))
            qty = float(getattr(r, "qty") or 0.0)
            if sku and ubic:
                self._balance[(sku, ubic)] += qty

    # ----- queries ---------------------------------------------------------
    def available(self, sku_id: str, ubicacion: str) -> float:
        return max(0.0, self._balance.get((canonical_sku(sku_id), canonical_location(ubicacion)), 0.0))

    def locations_with_stock(self, sku_id: str) -> list[tuple[str, float]]:
        sku = canonical_sku(sku_id)
        return [(ubic, qty) for (s, ubic), qty in self._balance.items() if s == sku and qty > 0]

    # ----- mutations -------------------------------------------------------
    def debit(self, sku_id: str, ubicacion: str, qty: float) -> float:
        """Remove up to ``qty`` from (sku, ubicacion); return actual debit."""
        key = (canonical_sku(sku_id), canonical_location(ubicacion))
        cur = max(0.0, self._balance.get(key, 0.0))
        take = max(0.0, min(qty, cur))
        self._balance[key] = cur - take
        return take

    def credit(self, sku_id: str, ubicacion: str, qty: float) -> None:
        key = (canonical_sku(sku_id), canonical_location(ubicacion))
        self._balance[key] = max(0.0, self._balance.get(key, 0.0)) + max(0.0, qty)

    # ----- export ----------------------------------------------------------
    def snapshot(self) -> pd.DataFrame:
        rows = [
            {"sku_id": s, "ubicacion": u, "qty": q}
            for (s, u), q in self._balance.items()
            if q > 0
        ]
        return pd.DataFrame(rows, columns=["sku_id", "ubicacion", "qty"])
