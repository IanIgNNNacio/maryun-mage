"""Individual layer allocators — each returns a list of ``Move`` rows.

They are wired together by ``replenishment.engine.run_replenishment``. A
layer never mutates state on its own; instead it asks the shared
``StockLedger`` for availability and commits debits only through the
engine's ``commit`` helper.
"""
from app.replenishment.layers.inmovilizado import allocate_inmovilizado
from app.replenishment.layers.sobrestock import allocate_sobrestock
from app.replenishment.layers.cd import allocate_cd
from app.replenishment.layers.compra import allocate_compra

__all__ = [
    "allocate_inmovilizado",
    "allocate_sobrestock",
    "allocate_cd",
    "allocate_compra",
]
