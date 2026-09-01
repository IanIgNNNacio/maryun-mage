"""Automation filter — restrict which SKUs/sucursales trigger the engine."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.connectors.excel import ExcelError, ExcelReader
from app.normalize.canonical import canonical_location, canonical_sku
from app.utils.logging import get_logger


SHEET = "automatizacion"
REQUIRED = ["sku_id", "ubicacion", "automatizar"]


def load_automation_table(path: Path, *, strict: bool = False) -> pd.DataFrame:
    log = get_logger("automation.filter")
    if not path.exists():
        if strict:
            raise FileNotFoundError(f"Automation file missing: {path}")
        log.info("automation.filter.optional_missing", path=str(path))
        return pd.DataFrame(columns=REQUIRED)

    try:
        df = ExcelReader().read_sheet(path, sheet=SHEET, required_columns=REQUIRED)
    except ExcelError as exc:
        if strict:
            raise
        log.warning("automation.filter.read_failed", error=str(exc))
        return pd.DataFrame(columns=REQUIRED)

    df = df.copy()
    df["sku_id"] = df["sku_id"].map(canonical_sku)
    df["ubicacion"] = df["ubicacion"].map(canonical_location)
    df["automatizar"] = df["automatizar"].astype("string").str.strip().str.lower().isin(
        {"true", "1", "yes", "si", "sí", "y", "t", "x", "✓"}
    )
    log.info("automation.filter.loaded", rows=len(df))
    return df[REQUIRED]


def apply_automation_filter(
    needs: pd.DataFrame,
    automation: pd.DataFrame,
    classification_audit: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Keep only rows where ``automatizar=True`` (falling back to v3 audit).

    If neither source lists a given ``(sku, ubicacion)``, that row is kept
    (default-on policy), but the engine will mark it as ``fuente_autom=default``.
    """
    log = get_logger("automation.filter")
    if needs.empty:
        return needs

    allow: dict[tuple[str, str], bool] = {}
    if not automation.empty:
        for _, row in automation.iterrows():
            allow[(row["sku_id"], row["ubicacion"])] = bool(row["automatizar"])

    # Fall back to the classification audit (clase_automatizacion)
    if classification_audit is not None and not classification_audit.empty \
            and "clase_automatizacion" in classification_audit.columns:
        for _, row in classification_audit.iterrows():
            key = (row["sku_id"], row["ubicacion"])
            if key not in allow:
                allow[key] = row["clase_automatizacion"] in {"AUTOMATIZAR", "SEMI-AUTO"}

    if not allow:
        return needs  # nothing configured → default on for everyone

    def _decide(s: str, u: str) -> bool:
        return allow.get((s, u), True)

    mask = needs.apply(lambda r: _decide(r["sku_id"], r["ubicacion"]), axis=1)
    out = needs.loc[mask].reset_index(drop=True)
    log.info("automation.filter.applied", kept=len(out), dropped=int((~mask).sum()))
    return out
