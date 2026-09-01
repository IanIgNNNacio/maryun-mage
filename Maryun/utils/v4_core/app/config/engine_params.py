"""Typed loader for ``engine_params.yaml`` — tunable business parameters.

These are values that change with business rules (ranking weights, the
overstock cap, safety-stock multipliers) rather than with the environment.
They live in a YAML file so non-developers can tweak them without touching
Python code.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from app.config.paths import ProjectPaths, get_paths


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------
class TemporalParams(BaseModel):
    history_start: str = "2024-01-01"  # ISO; inclusive
    closed_months_only: bool = True
    tolerance_days_closing: int = 2  # grace period after month-end


class ForecastParams(BaseModel):
    method: Literal["holt_winters", "naive", "mean"] = "holt_winters"
    horizon_months: int = 3
    seasonal_periods: int = 12
    min_history_months: int = 6
    fallback_to_naive_below_min: bool = True


class ClassificationParams(BaseModel):
    abc_thresholds: list[float] = Field(default_factory=lambda: [0.80, 0.95])  # cum-revenue
    xyz_cv_thresholds: list[float] = Field(default_factory=lambda: [0.50, 1.00])  # coefficient of variation


class HomologationParams(BaseModel):
    enabled: bool = True
    analytical_weight_national: float = 0.30  # used when reinforcing Maryun forecast
    operational_substitution_enabled: bool = True
    # When substituting: only trigger if these locations all have zero stock.
    substitution_trigger_zero_at: list[str] = Field(
        default_factory=lambda: ["sucursal", "CD SUR", "CD SANTIAGO"]
    )


class OverridesParams(BaseModel):
    require_motivo: bool = True
    require_responsable: bool = True
    fail_on_missing_sku: bool = True


class InmovilizadoParams(BaseModel):
    """Regla de inmovilizado — basada en dias desde la ultima venta (facturacion).

    Un (sku, sucursal) es INMOVILIZADO si no registra venta hace >= N dias.
    NO depende de la demanda esperada.
    """
    dias_sin_venta_min: int = 90  # >= este umbral → inmovilizado (dona todo)


class OverstockParams(BaseModel):
    cap_fraction: float = 0.30  # max fraction of local stock that can donate
    min_donor_excess_units: int = 1
    # Sobrestock: stock > factor_demanda_mes * demanda_mes_actual Y vendio <= dias_sin_venta_max.
    factor_demanda_mes: float = 1.30   # stock debe superar 130% de la demanda del mes
    dias_sin_venta_max: int = 89       # debe haber vendido hace <= este umbral


class RankingWeights(BaseModel):
    w_distance: float = 0.50
    w_cost: float = 0.35
    w_origin_impact: float = 0.15

    @model_validator(mode="after")
    def _sum_to_one(self) -> "RankingWeights":
        total = self.w_distance + self.w_cost + self.w_origin_impact
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"ranking weights must sum to 1.0 (got {total:.4f})")
        return self


class ReplenishmentParams(BaseModel):
    layers_order: list[str] = Field(
        default_factory=lambda: ["inmovilizado", "sobrestock", "cd", "compra"]
    )
    inmovilizado: InmovilizadoParams = Field(default_factory=InmovilizadoParams)
    overstock: OverstockParams = Field(default_factory=OverstockParams)
    ranking: RankingWeights = Field(default_factory=RankingWeights)
    respect_prioridad_cd: bool = True
    respect_reglas_sku_sucursal: bool = True
    # Cantidad minima por movimiento: se redondea a entero y se descartan
    # movimientos por debajo de este valor (evita pedir fracciones ej. 0.005 uds).
    min_cantidad_unidades: float = 1.0


class NeedsParams(BaseModel):
    safety_stock_multiplier: float = 1.5  # in sigmas
    coverage_months_default: float = 1.5
    trigger_below_safety: bool = True
    # Regla de RECENCIA DE VENTA — un (sku, sucursal) que no registra venta en
    # los ultimos N dias NO puede ser "requerido": no genera compra ni traslado
    # de entrada (necesidad=0, dispara=False). Evita reponer/comprar productos
    # que no rotan en esa sucursal. Es independiente de la demanda esperada.
    # "Nunca vendio" (sin registro de venta) se trata como sin venta reciente.
    bloquear_sin_venta_reciente: bool = True
    requerido_dias_sin_venta_max: int = 90  # > este umbral → no requerido


class SilenceParams(BaseModel):
    """Ventana de silencio — suprime alertas repetitivas por N días."""
    enabled: bool = True
    window_days: int = 7   # días de silencio tras un envío


class EngineParams(BaseModel):
    """Top-level tunable parameters."""

    temporal: TemporalParams = Field(default_factory=TemporalParams)
    forecast: ForecastParams = Field(default_factory=ForecastParams)
    classification: ClassificationParams = Field(default_factory=ClassificationParams)
    homologation: HomologationParams = Field(default_factory=HomologationParams)
    overrides: OverridesParams = Field(default_factory=OverridesParams)
    replenishment: ReplenishmentParams = Field(default_factory=ReplenishmentParams)
    needs: NeedsParams = Field(default_factory=NeedsParams)
    silence: SilenceParams = Field(default_factory=SilenceParams)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------
def _default_params_path(paths: ProjectPaths | None = None) -> Path:
    p = paths or get_paths()
    return p.config_dir / "engine_params.yaml"


def load_engine_params(path: Path | None = None) -> EngineParams:
    """Load + validate engine parameters from YAML. Missing file → defaults."""
    target = path or _default_params_path()
    if not target.exists():
        return EngineParams()
    with target.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return EngineParams(**raw)


@lru_cache(maxsize=1)
def get_engine_params() -> EngineParams:
    return load_engine_params()


def reset_engine_params_cache() -> None:
    get_engine_params.cache_clear()
