"""Read a pre-computed forecast Excel.

Used when the user supplies a forecast coming from an external model
(``run_forecast=false`` in settings). Two formats are auto-detected:

1. **Canonical** (columns ``sku_id | ubicacion | mes | forecast_modelo``) —
   the format the pipeline natively expects.

2. **v9 Power BI** (columns ``ID_KEY | Fecha | Pronostico`` + audit columns)
   produced by the user's ``SISTEMA DE PRONOSTICO v9`` script. ``ID_KEY``
   is a concatenation of SKU + sucursal name without separator
   (e.g. ``"4432007857PUERTO MONTT"``); this loader splits it by matching
   against the canonical location list.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.connectors.excel import ExcelReader
from app.normalize.canonical import CDS_CANONICOS, SUCURSALES_CANONICAS, canonical_sku
from app.utils.logging import get_logger


REQUIRED = ["sku_id", "ubicacion", "mes", "forecast_modelo"]
_V9_REQUIRED = {"ID_KEY", "Fecha", "Pronostico"}


def _is_v9_format(df: pd.DataFrame) -> bool:
    return _V9_REQUIRED.issubset(df.columns)


def _split_id_key(id_key: str, locations: tuple[str, ...]) -> tuple[str, str]:
    """Split ``"4432007857PUERTO MONTT"`` → ``("4432007857", "PUERTO MONTT")``.

    Tries the longest canonical location name first to avoid matching
    ``"SANTIAGO"`` when the real suffix is ``"CD SANTIAGO"``.
    """
    s = str(id_key).strip().upper()
    for loc in locations:
        if s.endswith(loc):
            prefix = s[: -len(loc)].strip()
            return prefix, loc
    return s, ""


def _read_v9(path: Path) -> pd.DataFrame:
    log = get_logger("forecast.reader.v9")
    # v9 exports the forecasts on sheet "Pronosticos"
    df = pd.read_excel(path, sheet_name="Pronosticos")
    missing = _V9_REQUIRED - set(df.columns)
    if missing:
        raise ValueError(f"v9 forecast file missing columns: {sorted(missing)}")

    # Longest location first so "CD SANTIAGO" wins over "SANTIAGO".
    locations = tuple(sorted(SUCURSALES_CANONICAS + CDS_CANONICOS, key=len, reverse=True))

    splits = df["ID_KEY"].map(lambda k: _split_id_key(k, locations))
    # Canonicalise sku so leading zeros match the MariaDB side ("000329..." → "329...").
    df["sku_id"] = splits.map(lambda x: canonical_sku(x[0]) or x[0])
    df["ubicacion"] = splits.map(lambda x: x[1])
    df["mes"] = pd.to_datetime(df["Fecha"])
    df["forecast_modelo"] = pd.to_numeric(df["Pronostico"], errors="coerce").fillna(0.0).clip(lower=0.0)

    unknown = df[df["ubicacion"] == ""]
    if not unknown.empty:
        # Only warn: those rows are skipped so downstream doesn't see bogus ubicacion="".
        log.warning(
            "forecast.reader.v9.unknown_location",
            rows=int(len(unknown)),
            sample=unknown["ID_KEY"].head(5).tolist(),
        )
    df = df[df["ubicacion"] != ""].copy()

    log.info("forecast.reader.v9.loaded", path=str(path), rows=len(df))
    return df[REQUIRED]


def read_forecast_excel(path: Path) -> pd.DataFrame:
    """Read a precomputed forecast. Auto-detects v9 vs canonical layout."""
    log = get_logger("forecast.reader")

    # Peek at the sheet without enforcing a schema so we can branch on layout.
    head = pd.read_excel(path, nrows=0)
    # v9 has the forecasts on a named sheet, so peeking at the first sheet may
    # not reveal the columns. Try the v9 path first if the file looks like one.
    try:
        sheets = pd.ExcelFile(path).sheet_names
    except Exception:
        sheets = []

    if "Pronosticos" in sheets:
        return _read_v9(path)

    if _is_v9_format(head):
        return _read_v9(path)

    df = ExcelReader().read_sheet(path, required_columns=REQUIRED)
    df["sku_id"] = df["sku_id"].astype(str).str.strip()
    df["ubicacion"] = df["ubicacion"].astype(str).str.upper().str.strip()
    df["mes"] = pd.to_datetime(df["mes"])
    df["forecast_modelo"] = df["forecast_modelo"].astype(float).clip(lower=0.0)
    log.info("forecast.reader.loaded", path=str(path), rows=len(df))
    return df[REQUIRED]
