"""Transport cost matrix — CLP per unit between every pair of locations."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from app.connectors.excel import ExcelError, ExcelReader
from app.normalize.canonical import canonical_location
from app.utils.logging import get_logger


SHEET = "costos"
REQUIRED = ["origen", "destino", "costo_clp_por_unidad"]


@dataclass(frozen=True)
class CostMatrix:
    df: pd.DataFrame                                    # [origen, destino, costo_clp_por_unidad]
    lookup: dict[tuple[str, str], float]                # canonical key → CLP/unit

    def costo(self, origen: str, destino: str) -> float:
        a, b = canonical_location(origen), canonical_location(destino)
        return self.lookup.get((a, b), float("inf"))

    def is_empty(self) -> bool:
        return self.df.empty


def load_costs(path: Path, *, strict: bool = False) -> CostMatrix:
    log = get_logger("policies.costs")
    if not path.exists():
        if strict:
            raise FileNotFoundError(f"Costs file missing: {path}")
        log.info("policies.costs.optional_missing", path=str(path))
        return CostMatrix(df=pd.DataFrame(columns=REQUIRED), lookup={})

    try:
        df = ExcelReader().read_sheet(path, sheet=SHEET, required_columns=REQUIRED)
    except ExcelError as exc:
        if strict:
            raise
        log.warning("policies.costs.read_failed", error=str(exc))
        return CostMatrix(df=pd.DataFrame(columns=REQUIRED), lookup={})

    df = df.copy()
    df["origen"] = df["origen"].map(canonical_location)
    df["destino"] = df["destino"].map(canonical_location)
    df["costo_clp_por_unidad"] = pd.to_numeric(df["costo_clp_por_unidad"], errors="coerce").astype(float)
    df = df.dropna(subset=["origen", "destino", "costo_clp_por_unidad"])
    lookup = {
        (r.origen, r.destino): float(r.costo_clp_por_unidad)
        for r in df.itertuples(index=False)
    }
    # Symmetric fallback: only fill if the reverse pair is missing.
    for (a, b), v in list(lookup.items()):
        lookup.setdefault((b, a), v)

    log.info("policies.costs.loaded", rows=len(df))
    return CostMatrix(df=df.reset_index(drop=True), lookup=lookup)
