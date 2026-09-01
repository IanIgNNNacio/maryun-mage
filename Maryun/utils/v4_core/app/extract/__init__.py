"""Extract layer — pulls raw data from MariaDB (or mocks) and normalises it.

Public surface
--------------
:func:`extract_all` — given a :class:`TemporalGate`, returns a dict with keys
``products``, ``stock``, ``demand`` each conforming to the corresponding
pandera schema.
"""
from app.extract.extractor import extract_all
from app.extract.mock_source import MockSource

__all__ = ["extract_all", "MockSource"]
