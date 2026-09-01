"""Replenishment engine — 4-layer allocator.

Layers, in order of preference:
    1. inmovilizado   — redistribute from dead-stock sucursales
    2. sobrestock     — cap-limited transfer from over-stocked sucursales
    3. cd             — ship from the preferred CD (respecting CD priority)
    4. compra         — emit a purchase order to the default supplier

Each layer consumes unmet need and produces ``ReplenishmentPlanSchema`` rows.
The engine also emits an audit log (``plan_audit``) explaining — for every
need row — which layers contributed, which were blocked, and why.
"""
from app.replenishment.engine import run_replenishment, ReplenishmentResult
from app.replenishment.ranking import score_candidate

__all__ = [
    "run_replenishment",
    "ReplenishmentResult",
    "score_candidate",
]
