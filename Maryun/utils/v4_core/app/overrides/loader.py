"""Excel loaders for the two override files."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.config.engine_params import EngineParams
from app.connectors.excel import ExcelError, ExcelReader
from app.normalize.canonical import canonical_location, canonical_sku
from app.utils.logging import get_logger


FORECAST_SHEET = "override_forecast"
# Acepta "sku" (nuevo) o "sku_id" (legado) — se resuelve en el loader.
FORECAST_REQUIRED = ["ubicacion", "mes", "forecast_override"]

CLASSIFICATION_SHEET = "override_clasificacion"
CLASSIFICATION_REQUIRED = ["ubicacion"]  # sku/sku_id + abc/xyz opcionales


class OverrideError(ValueError):
    """Raised when an override file is malformed."""


def load_forecast_overrides(
    path: Path,
    params: EngineParams,
    *,
    strict: bool = True,
) -> pd.DataFrame:
    log = get_logger("overrides.loader")
    if not path.exists():
        if strict:
            raise OverrideError(f"Forecast override file missing: {path}")
        log.info("overrides.forecast.optional_missing", path=str(path))
        return _empty_forecast()

    try:
        df = ExcelReader().read_sheet(path, sheet=FORECAST_SHEET, required_columns=FORECAST_REQUIRED)
    except ExcelError as exc:
        raise OverrideError(str(exc)) from exc

    df = df.copy()
    # Acepta "sku" (nuevo) o "sku_id" (legado).
    if "sku" in df.columns and "sku_id" not in df.columns:
        df = df.rename(columns={"sku": "sku_id"})
    if "sku_id" not in df.columns:
        raise OverrideError("El archivo de overrides de forecast no tiene columna 'sku' ni 'sku_id'")
    df["sku_id"] = df["sku_id"].map(canonical_sku)
    df["ubicacion"] = df["ubicacion"].map(canonical_location)
    df["mes"] = pd.to_datetime(df["mes"])
    df["forecast_override"] = pd.to_numeric(df["forecast_override"], errors="coerce")
    for col in ("motivo", "responsable"):
        if col not in df.columns:
            df[col] = pd.NA

    _validate_forecast(df, params)
    log.info("overrides.forecast.loaded", rows=len(df))
    return df[["sku_id", "ubicacion", "mes", "forecast_override", "motivo", "responsable"]]


def load_classification_overrides(
    path: Path,
    params: EngineParams,
    *,
    strict: bool = True,
) -> pd.DataFrame:
    log = get_logger("overrides.loader")
    if not path.exists():
        if strict:
            raise OverrideError(f"Classification override file missing: {path}")
        log.info("overrides.classification.optional_missing", path=str(path))
        return _empty_classification()

    try:
        df = ExcelReader().read_sheet(
            path, sheet=CLASSIFICATION_SHEET,
            required_columns=CLASSIFICATION_REQUIRED,
        )
    except ExcelError as exc:
        raise OverrideError(str(exc)) from exc

    df = df.copy()
    # Acepta "sku" (nuevo) o "sku_id" (legado).
    if "sku" in df.columns and "sku_id" not in df.columns:
        df = df.rename(columns={"sku": "sku_id"})
    if "sku_id" not in df.columns:
        raise OverrideError("El archivo de overrides de clasificación no tiene columna 'sku' ni 'sku_id'")
    df["sku_id"] = df["sku_id"].map(canonical_sku)
    df["ubicacion"] = df["ubicacion"].map(canonical_location)
    for col in ("abc_override", "xyz_override"):
        if col not in df.columns:
            df[col] = pd.NA
        else:
            df[col] = df[col].astype("string").str.strip().str.upper()
    for col in ("motivo", "responsable"):
        if col not in df.columns:
            df[col] = pd.NA

    _validate_classification(df, params)
    log.info("overrides.classification.loaded", rows=len(df))
    return df[["sku_id", "ubicacion", "abc_override", "xyz_override", "motivo", "responsable"]]


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------
def _validate_forecast(df: pd.DataFrame, params: EngineParams) -> None:
    op = params.overrides
    errors: list[str] = []

    bad_val = df[df["forecast_override"].isna() | (df["forecast_override"] < 0)]
    if not bad_val.empty:
        errors.append(f"{len(bad_val)} rows with missing or negative forecast_override")

    dup = df.duplicated(subset=["sku_id", "ubicacion", "mes"], keep=False)
    if dup.any():
        errors.append(f"{int(dup.sum())} duplicate (sku,ubicacion,mes) rows")

    if op.require_motivo:
        miss = df["motivo"].isna() | (df["motivo"].astype(str).str.strip() == "")
        if miss.any():
            errors.append(f"{int(miss.sum())} rows missing motivo (required by policy)")
    if op.require_responsable:
        miss = df["responsable"].isna() | (df["responsable"].astype(str).str.strip() == "")
        if miss.any():
            errors.append(f"{int(miss.sum())} rows missing responsable (required by policy)")

    if errors:
        raise OverrideError("; ".join(errors))


def _validate_classification(df: pd.DataFrame, params: EngineParams) -> None:
    op = params.overrides
    errors: list[str] = []

    none = df["abc_override"].isna() & df["xyz_override"].isna()
    if none.any():
        errors.append(f"{int(none.sum())} rows with no abc_override and no xyz_override")

    bad_abc = df["abc_override"].dropna().astype(str).str.upper().isin(["A", "B", "C"])
    if not bad_abc.all():
        errors.append("abc_override contains values outside A/B/C")
    bad_xyz = df["xyz_override"].dropna().astype(str).str.upper().isin(["X", "Y", "Z"])
    if not bad_xyz.all():
        errors.append("xyz_override contains values outside X/Y/Z")

    dup = df.duplicated(subset=["sku_id", "ubicacion"], keep=False)
    if dup.any():
        errors.append(f"{int(dup.sum())} duplicate (sku,ubicacion) rows")

    if op.require_motivo:
        miss = df["motivo"].isna() | (df["motivo"].astype(str).str.strip() == "")
        if miss.any():
            errors.append(f"{int(miss.sum())} rows missing motivo (required by policy)")
    if op.require_responsable:
        miss = df["responsable"].isna() | (df["responsable"].astype(str).str.strip() == "")
        if miss.any():
            errors.append(f"{int(miss.sum())} rows missing responsable (required by policy)")

    if errors:
        raise OverrideError("; ".join(errors))


def _empty_forecast() -> pd.DataFrame:
    return pd.DataFrame(columns=["sku_id", "ubicacion", "mes", "forecast_override", "motivo", "responsable"])


def _empty_classification() -> pd.DataFrame:
    return pd.DataFrame(columns=["sku_id", "ubicacion", "abc_override", "xyz_override", "motivo", "responsable"])
