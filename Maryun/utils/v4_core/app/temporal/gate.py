"""TemporalGate — single source of truth for the closed-months-only rule.

All modules that consume history (forecast, classification, homologation's
analytical transfer) must go through this gate. The gate answers:

* What is today's ``process_date``?
* What is the last *closed* month (e.g. on 2026-04-20 with tolerance=2, the
  last closed month is 2026-03)?
* Which month-starts fall inside the allowed window
  ``[history_start, last_closed_month]``?

A single :class:`TemporalGate` is built once per run and handed to every
stage. Stages never compute "today" on their own.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pandas as pd
from dateutil.relativedelta import relativedelta

from app.config.engine_params import EngineParams, get_engine_params


@dataclass(frozen=True)
class TemporalWindow:
    """Inclusive window of month-starts usable as history."""

    start: date  # first usable month-start
    end: date    # last usable month-start (i.e. last closed month)

    @property
    def months(self) -> list[date]:
        out: list[date] = []
        cur = self.start
        while cur <= self.end:
            out.append(cur)
            cur = cur + relativedelta(months=1)
        return out

    @property
    def n_months(self) -> int:
        return (self.end.year - self.start.year) * 12 + (self.end.month - self.start.month) + 1

    def __repr__(self) -> str:  # pragma: no cover
        return f"TemporalWindow({self.start:%Y-%m} → {self.end:%Y-%m}, {self.n_months} months)"


class TemporalGate:
    """Enforces closed-months-only history access.

    Parameters
    ----------
    process_date:
        "Today" for this run. Anything that happened in the same month as
        ``process_date`` is considered open and excluded.
    params:
        :class:`EngineParams` controlling ``history_start``,
        ``closed_months_only``, and ``tolerance_days_closing``.
    """

    def __init__(self, process_date: date, params: EngineParams | None = None) -> None:
        self.process_date = process_date
        self.params = params or get_engine_params()
        self._history_start = _parse_iso_date(self.params.temporal.history_start)
        self._window = self._build_window()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @property
    def history_start(self) -> date:
        return self._history_start

    @property
    def last_closed_month(self) -> date:
        """First day of the last month considered *closed*."""
        return self._window.end

    @property
    def window(self) -> TemporalWindow:
        return self._window

    def is_closed_month(self, d: date) -> bool:
        """True if the month containing ``d`` is fully closed on process_date."""
        first = _month_start(d)
        return self._history_start <= first <= self._window.end

    def filter_monthly(
        self,
        df: pd.DataFrame,
        date_column: str,
    ) -> pd.DataFrame:
        """Return rows of ``df`` whose month falls inside the allowed window.

        Rows with null dates are dropped. Input is not mutated.
        """
        if df.empty:
            return df.copy()
        if date_column not in df.columns:
            raise KeyError(f"column {date_column!r} not in DataFrame")
        out = df.copy()
        out[date_column] = pd.to_datetime(out[date_column], errors="coerce")
        out = out.dropna(subset=[date_column])
        ts_start = pd.Timestamp(self._window.start)
        # Keep only rows whose month-start <= last closed month-start
        month_starts = out[date_column].dt.to_period("M").dt.to_timestamp()
        mask = (month_starts >= ts_start) & (month_starts <= pd.Timestamp(self._window.end))
        return out.loc[mask].reset_index(drop=True)

    def describe(self) -> dict[str, str]:
        """Short dict for audit reports."""
        return {
            "process_date": self.process_date.isoformat(),
            "history_start": self._history_start.isoformat(),
            "last_closed_month": self._window.end.isoformat(),
            "n_months": str(self._window.n_months),
            "closed_months_only": str(self.params.temporal.closed_months_only),
            "tolerance_days_closing": str(self.params.temporal.tolerance_days_closing),
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _build_window(self) -> TemporalWindow:
        end = self._compute_last_closed_month()
        start = _month_start(self._history_start)
        if start > end:
            raise ValueError(
                f"history_start ({start:%Y-%m}) is after last closed month "
                f"({end:%Y-%m}) — check engine_params.yaml"
            )
        return TemporalWindow(start=start, end=end)

    def _compute_last_closed_month(self) -> date:
        """Determine last closed month with the configured tolerance.

        Rules:
        * If ``closed_months_only`` is False → return current month-start.
        * Else, the previous month is only considered *closed* once we are at
          least ``tolerance_days_closing`` days past its end. This protects
          against running the pipeline on the 1st of a month before books
          for the prior month are finalised.
        """
        tp = self.params.temporal
        cur_month_start = _month_start(self.process_date)

        if not tp.closed_months_only:
            return cur_month_start

        prev_month_start = cur_month_start - relativedelta(months=1)
        # Number of days elapsed within the current month (process_date -
        # current month start). Once we've cleared the tolerance, the
        # previous month is "closed".
        days_into_month = (self.process_date - cur_month_start).days
        if days_into_month >= tp.tolerance_days_closing:
            return prev_month_start
        # Still inside the tolerance window → last closed is the one before.
        return prev_month_start - relativedelta(months=1)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def build_temporal_gate(
    process_date: date | str,
    params: EngineParams | None = None,
) -> TemporalGate:
    """Factory — accepts a date or an ISO string."""
    if isinstance(process_date, str):
        process_date = _parse_iso_date(process_date)
    return TemporalGate(process_date=process_date, params=params)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _parse_iso_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _month_start(d: date) -> date:
    return date(d.year, d.month, 1)


__all__ = [
    "TemporalGate",
    "TemporalWindow",
    "build_temporal_gate",
    "_month_start",
]
