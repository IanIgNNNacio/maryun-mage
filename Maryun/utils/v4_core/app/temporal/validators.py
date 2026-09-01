"""Validators that assert a DataFrame respects the temporal window."""
from __future__ import annotations

import pandas as pd

from app.temporal.gate import TemporalGate


class TemporalViolation(ValueError):
    """Raised when history contains months outside the allowed window."""


def assert_within_window(
    df: pd.DataFrame,
    date_column: str,
    gate: TemporalGate,
    *,
    context: str = "",
) -> None:
    """Raise ``TemporalViolation`` if ``df`` has rows outside the gate window."""
    if df.empty:
        return
    if date_column not in df.columns:
        raise KeyError(f"column {date_column!r} not in DataFrame")

    dates = pd.to_datetime(df[date_column], errors="coerce").dropna()
    if dates.empty:
        return
    month_starts = dates.dt.to_period("M").dt.to_timestamp()
    lo = pd.Timestamp(gate.window.start)
    hi = pd.Timestamp(gate.window.end)
    out_of_window = month_starts[(month_starts < lo) | (month_starts > hi)]
    if not out_of_window.empty:
        n = len(out_of_window)
        sample = sorted({d.strftime("%Y-%m") for d in out_of_window.head(5)})
        where = f" [{context}]" if context else ""
        raise TemporalViolation(
            f"{n} rows fall outside the temporal window "
            f"[{gate.window.start:%Y-%m} → {gate.window.end:%Y-%m}]{where}. "
            f"Sample months: {sample}"
        )
