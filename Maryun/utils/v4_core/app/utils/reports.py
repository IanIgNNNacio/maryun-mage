"""Helpers to serialise audit/summary reports as CSV + Excel side-by-side."""
from __future__ import annotations

from pathlib import Path

import pandas as pd


def write_report(df: pd.DataFrame, out_dir: Path, name: str, *, also_xlsx: bool = True) -> Path:
    """Write ``df`` to ``out_dir/name.csv`` (and xlsx). Returns the CSV path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"{name}.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8")
    if also_xlsx:
        xlsx_path = out_dir / f"{name}.xlsx"
        df.to_excel(xlsx_path, index=False)
    return csv_path


def write_audit_bundle(reports: dict[str, pd.DataFrame], out_dir: Path) -> list[Path]:
    """Write a whole dict of name→DataFrame as a report bundle."""
    paths: list[Path] = []
    for name, df in reports.items():
        paths.append(write_report(df, out_dir, name))
    return paths
