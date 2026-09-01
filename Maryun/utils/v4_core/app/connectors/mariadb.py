"""MariaDB connector — SQLAlchemy engine + thin query helper.

Only two things live here:
* :func:`build_engine` — constructs a SQLAlchemy engine from Settings.
* :class:`MariaDBConnector` — ``run_query(sql, params) -> DataFrame``.

Everything higher level (which SQL to run, how to shape results) belongs to
``app.extract``.
"""
from __future__ import annotations

from typing import Any

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.config.settings import Settings, get_settings
from app.utils.logging import get_logger


def build_engine(settings: Settings | None = None) -> Engine:
    s = settings or get_settings()
    url = s.mariadb.sqlalchemy_url()
    # pool_pre_ping avoids stale-connection errors after long runs.
    return create_engine(url, pool_pre_ping=True, future=True)


class MariaDBConnector:
    """Run parametrised SQL against MariaDB, return DataFrames."""

    def __init__(self, engine: Engine | None = None, settings: Settings | None = None) -> None:
        self._engine = engine or build_engine(settings)
        self._log = get_logger("connectors.mariadb")

    @property
    def engine(self) -> Engine:
        return self._engine

    def run_query(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
    ) -> pd.DataFrame:
        """Execute ``sql`` and return a DataFrame. ``params`` uses ``:name`` style."""
        self._log.debug("mariadb.query", params=params, sql_preview=sql[:120])
        with self._engine.connect() as conn:
            df = pd.read_sql(text(sql), conn, params=params or {})
        self._log.info("mariadb.query.done", rows=len(df), cols=len(df.columns))
        return df

    def test_connection(self) -> bool:
        try:
            with self._engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception as exc:  # pragma: no cover
            self._log.error("mariadb.connection.failed", error=str(exc))
            return False

    def close(self) -> None:
        self._engine.dispose()
