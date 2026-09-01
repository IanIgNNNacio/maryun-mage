"""Pipeline CLI entrypoint — shim over :mod:`app.cli` for ``pyproject`` scripts."""
from __future__ import annotations

from app.cli import app as cli

__all__ = ["cli"]


if __name__ == "__main__":  # pragma: no cover
    cli()
