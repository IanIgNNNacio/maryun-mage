"""Needs layer — compute replenishment triggers per (sku, ubicacion).

Input:
    forecast (one row per (sku, ubicacion, mes) — horizon months, forecast_final)
    stock    (one row per (sku, ubicacion) — on-hand units)
    classification (optional — used to scale safety_stock by clase_final)
    demand_history (optional — used to estimate sigma)

Output:
    needs_df matching ``NeedsSchema`` with columns
        sku_id, ubicacion, demanda_esperada, stock_actual, safety_stock,
        necesidad, dispara
    plus audit columns (clase_final, sigma_demanda, horizon_meses,
    cobertura_actual_meses).
"""
from app.needs.calculator import compute_needs, build_needs_audit

__all__ = ["compute_needs", "build_needs_audit"]
