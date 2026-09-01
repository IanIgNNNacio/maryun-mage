"""CD priority — which distribution centre feeds which sucursal first."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from app.connectors.excel import ExcelError, ExcelReader
from app.normalize.canonical import CDS_CANONICOS, canonical_location, is_cd
from app.utils.logging import get_logger


SHEET = "prioridad_cd"
REQUIRED = ["ubicacion", "cd", "prioridad"]


@dataclass(frozen=True)
class CDPriority:
    df: pd.DataFrame                                    # [ubicacion, cd, prioridad]
    ordered: dict[str, list[str]]                       # sucursal → [cd ordered by prio asc]

    def preferred_cds(self, ubicacion: str) -> list[str]:
        """Return the ordered CD list for ``ubicacion`` (first = highest priority).

        Falls back to the global CD list (alphabetical) when nothing is
        configured, so callers always get a deterministic order.
        """
        key = canonical_location(ubicacion)
        return self.ordered.get(key) or sorted(CDS_CANONICOS)

    def is_empty(self) -> bool:
        return self.df.empty


def load_priority_cd(path: Path, *, strict: bool = False) -> CDPriority:
    log = get_logger("policies.priority_cd")
    if not path.exists():
        if strict:
            raise FileNotFoundError(f"CD priority file missing: {path}")
        log.info("policies.priority_cd.optional_missing", path=str(path))
        return CDPriority(df=pd.DataFrame(columns=REQUIRED), ordered={})

    try:
        df = ExcelReader().read_sheet(path, sheet=SHEET, required_columns=REQUIRED)
    except ExcelError as exc:
        if strict:
            raise
        log.warning("policies.priority_cd.read_failed", error=str(exc))
        return CDPriority(df=pd.DataFrame(columns=REQUIRED), ordered={})

    df = df.copy()
    df["ubicacion"] = df["ubicacion"].map(canonical_location)
    df["cd"] = df["cd"].map(canonical_location)
    df["prioridad"] = pd.to_numeric(df["prioridad"], errors="coerce").astype("Int64")
    df = df.dropna(subset=["ubicacion", "cd", "prioridad"])

    # Only keep rows whose CD is a real CD.
    df = df[df["cd"].map(is_cd)].reset_index(drop=True)

    ordered: dict[str, list[str]] = {}
    for suc, sub in df.sort_values(["ubicacion", "prioridad"]).groupby("ubicacion", sort=False):
        ordered[suc] = list(dict.fromkeys(sub["cd"].tolist()))

    log.info("policies.priority_cd.loaded", sucursales=len(ordered), rows=len(df))
    return CDPriority(df=df, ordered=ordered)
