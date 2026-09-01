"""Excel I/O adapter — thin wrapper over pandas/openpyxl.

All Excel reading in the pipeline goes through :class:`ExcelReader` so we have
a single place to handle:

* missing files (clear error)
* missing sheets (clear error)
* string columns (strip + empty → NA)
* repeated column normalisation
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.utils.logging import get_logger


class ExcelError(FileNotFoundError):
    """Raised when an expected Excel file or sheet is missing."""


class ExcelReader:
    def __init__(self) -> None:
        self._log = get_logger("connectors.excel")

    def read_sheet(
        self,
        path: Path,
        sheet: str | int = 0,
        *,
        required_columns: list[str] | None = None,
        strip_strings: bool = True,
        dtype: dict | None = None,
    ) -> pd.DataFrame:
        """Read a sheet. Validates presence + optional required columns.

        Parameters
        ----------
        dtype:
            Mapping de ``{columna: tipo}`` pasado directamente a
            ``pd.read_excel(dtype=...)``.  Útil para forzar columnas como
            ``sku`` a ``str`` y evitar que pandas convierta "000329000247"
            a int64 al inferir tipos.
        """
        if not path.exists():
            raise ExcelError(f"Excel file not found: {path}")
        try:
            df = pd.read_excel(path, sheet_name=sheet, engine="openpyxl",
                               dtype=dtype)
        except ValueError as exc:
            raise ExcelError(f"Sheet {sheet!r} not found in {path}: {exc}") from exc

        df.columns = [str(c).strip() for c in df.columns]

        if strip_strings:
            for col in df.select_dtypes(include="object").columns:
                df[col] = df[col].astype("string").str.strip()
                df.loc[df[col] == "", col] = pd.NA

        if required_columns:
            missing = [c for c in required_columns if c not in df.columns]
            if missing:
                raise ExcelError(
                    f"{path.name}[{sheet}] missing required columns: {missing}"
                )

        self._log.info("excel.read", path=str(path), sheet=str(sheet), rows=len(df))
        return df

    def read_all_sheets(self, path: Path) -> dict[str, pd.DataFrame]:
        if not path.exists():
            raise ExcelError(f"Excel file not found: {path}")
        sheets = pd.read_excel(path, sheet_name=None, engine="openpyxl")
        self._log.info("excel.read_all", path=str(path), sheets=list(sheets.keys()))
        return sheets


class ExcelWriter:
    def __init__(self) -> None:
        self._log = get_logger("connectors.excel")

    def write_single(self, df: pd.DataFrame, path: Path, sheet: str = "data") -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name=sheet, index=False)
        self._log.info("excel.write", path=str(path), rows=len(df))
        return path

    def write_multi(self, sheets: dict[str, pd.DataFrame], path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            for name, df in sheets.items():
                df.to_excel(writer, sheet_name=name[:31], index=False)  # Excel limit
        self._log.info("excel.write_multi", path=str(path), sheets=list(sheets.keys()))
        return path
