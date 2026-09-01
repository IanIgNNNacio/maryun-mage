"""Forecast layer.

Exposes :func:`build_forecast` which returns a frame matching
``normalize.ForecastSchema``. Internally selects between the built-in
baseline (Holt-Winters) and a pre-computed Excel reader.
"""
from app.forecast.selector import build_forecast

__all__ = ["build_forecast"]
