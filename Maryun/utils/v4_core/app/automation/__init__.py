"""Automation filter — decide which (sku, sucursal) pairs flow to the engine.

Input: ``automatizacion_sucursales.xlsx`` — one row per ``(sku_id, ubicacion)``
marked as ``automatizar`` (bool). Optionally inherit from the v3
classification audit (``clase_automatizacion`` column).
"""
from app.automation.filter import apply_automation_filter, load_automation_table

__all__ = ["apply_automation_filter", "load_automation_table"]
