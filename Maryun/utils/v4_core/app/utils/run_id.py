"""Run identifiers.

A ``run_id`` uniquely labels one execution of the pipeline. Format:
``YYYYMMDD-HHMMSS-<slug>`` where ``slug`` is 6 random alphanum chars.
"""
from __future__ import annotations

import re
import secrets
import string
from datetime import datetime


_ALPHABET = string.ascii_lowercase + string.digits
_PATTERN = re.compile(r"^(\d{8})-(\d{6})-([a-z0-9]{6})$")


def make_run_id(now: datetime | None = None, slug: str | None = None) -> str:
    """Generate a fresh run id. ``now``/``slug`` are for deterministic tests."""
    ts = now or datetime.now()
    s = slug or "".join(secrets.choice(_ALPHABET) for _ in range(6))
    return f"{ts.strftime('%Y%m%d')}-{ts.strftime('%H%M%S')}-{s}"


def parse_run_id(run_id: str) -> tuple[datetime, str]:
    """Return (timestamp, slug) or raise ``ValueError`` for bad input."""
    m = _PATTERN.match(run_id)
    if not m:
        raise ValueError(f"invalid run_id: {run_id!r}")
    date_part, time_part, slug = m.groups()
    ts = datetime.strptime(date_part + time_part, "%Y%m%d%H%M%S")
    return ts, slug
