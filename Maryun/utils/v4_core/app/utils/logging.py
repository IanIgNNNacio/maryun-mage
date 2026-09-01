"""Structured logging setup (structlog + stdlib).

Use :func:`configure_logging` once at pipeline entry. After that, call
:func:`get_logger` from any module.
"""
from __future__ import annotations

import logging
import sys

import structlog

from app.config.settings import Settings, get_settings


_CONFIGURED = False


def configure_logging(settings: Settings | None = None) -> None:
    """Configure stdlib + structlog. Idempotent."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    s = settings or get_settings()

    # Windows terminals default to cp1252 which cannot encode common structured-log
    # characters like "→" or "✅". Force UTF-8 on the handles we use for logging.
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # pragma: no cover — best-effort on exotic streams
                pass

    level = getattr(logging, s.log.level.upper(), logging.INFO)
    logging.basicConfig(
        stream=sys.stdout,
        level=level,
        format="%(message)s",
    )

    processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=False),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer()
        if s.log.json
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
    _CONFIGURED = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger. Configures logging on first call."""
    if not _CONFIGURED:
        configure_logging()
    return structlog.get_logger(name) if name else structlog.get_logger()
