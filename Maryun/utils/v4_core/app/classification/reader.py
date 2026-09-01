"""Read a pre-computed classification Excel (when ``run_classification=false``).

Columns expected:

    sku_id | ubicacion | abc_modelo | xyz_modelo | clase_modelo
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.connectors.excel import ExcelReader
from app.utils.logging import get_logger


REQUIRED = ["sku_id", "ubicacion", "abc_modelo", "xyz_modelo", "clase_modelo"]


def read_classification_excel(path: Path) -> pd.DataFrame:
    log = get_logger("classification.reader")
    df = ExcelReader().read_sheet(path, required_columns=REQUIRED)
    df["sku_id"] = df["sku_id"].astype(str).str.strip()
    df["ubicacion"] = df["ubicacion"].astype(str).str.upper().str.strip()
    for c in ("abc_modelo", "xyz_modelo", "clase_modelo"):
        df[c] = df[c].astype(str).str.strip().str.upper()
    log.info("classification.reader.loaded", path=str(path), rows=len(df))
    return df[REQUIRED]
