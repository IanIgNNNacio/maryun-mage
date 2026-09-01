"""Distance matrix — kilometres between every pair of locations."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from app.connectors.excel import ExcelError, ExcelReader
from app.normalize.canonical import canonical_location
from app.utils.logging import get_logger


SHEET = "distancias"
REQUIRED = ["origen", "destino", "km"]


@dataclass(frozen=True)
class DistanceMatrix:
    df: pd.DataFrame                                    # [origen, destino, km]
    lookup: dict[tuple[str, str], float]                # canonical key → km

    def km(self, origen: str, destino: str) -> float:
        a, b = canonical_location(origen), canonical_location(destino)
        return self.lookup.get((a, b), float("inf"))

    def is_empty(self) -> bool:
        return self.df.empty


def load_distances(path: Path, *, strict: bool = False) -> DistanceMatrix:
    log = get_logger("policies.distances")
    if not path.exists():
        if strict:
            raise FileNotFoundError(f"Distances file missing: {path}")
        log.info("policies.distances.optional_missing", path=str(path))
        return DistanceMatrix(df=pd.DataFrame(columns=REQUIRED), lookup={})

    try:
        df = ExcelReader().read_sheet(path, sheet=SHEET, required_columns=REQUIRED)
    except ExcelError as exc:
        if strict:
            raise
        log.warning("policies.distances.read_failed", error=str(exc))
        return DistanceMatrix(df=pd.DataFrame(columns=REQUIRED), lookup={})

    df = df.copy()
    df["origen"] = df["origen"].map(canonical_location)
    df["destino"] = df["destino"].map(canonical_location)
    df["km"] = pd.to_numeric(df["km"], errors="coerce").astype(float)
    df = df.dropna(subset=["origen", "destino", "km"])
    lookup = {(r.origen, r.destino): float(r.km) for r in df.itertuples(index=False)}
    # Symmetric: add reverse pair if not present.
    for (a, b), v in list(lookup.items()):
        lookup.setdefault((b, a), v)

    log.info("policies.distances.loaded", rows=len(df))
    return DistanceMatrix(df=df.reset_index(drop=True), lookup=lookup)
