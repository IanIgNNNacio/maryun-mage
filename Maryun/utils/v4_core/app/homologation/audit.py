"""Audit reports emitted by the Homologacion layer.

Produces the three outputs required by the business:

1. ``homologacion_aplicada`` — one row per (importado, nacional, ubicacion,
   mes) forecast reinforcement that actually changed the model value.
2. ``transferencias_demanda`` — the operational substitution log (already
   emitted by ``operational.apply_operational_substitution`` but we add
   convenience aggregation here).
3. ``resumen_homologacion`` — summary counters.
"""
from __future__ import annotations

import pandas as pd


def build_homologacion_audit(reinforced_forecast: pd.DataFrame) -> pd.DataFrame:
    """Return rows where analytical homologation changed the forecast."""
    if reinforced_forecast.empty or "homologado_analiticamente" not in reinforced_forecast.columns:
        return pd.DataFrame()
    df = reinforced_forecast[reinforced_forecast["homologado_analiticamente"]].copy()
    cols = ["sku_id", "ubicacion", "mes",
            "forecast_modelo_antes_homologacion", "forecast_modelo"]
    cols = [c for c in cols if c in df.columns]
    if not cols:
        return pd.DataFrame()
    df = df[cols].rename(columns={
        "forecast_modelo_antes_homologacion": "forecast_pre_homologacion",
        "forecast_modelo": "forecast_post_homologacion",
    })
    df["delta"] = df["forecast_post_homologacion"] - df["forecast_pre_homologacion"]
    return df


def build_transferencias_demanda(substitution_audit: pd.DataFrame) -> pd.DataFrame:
    """Passthrough (already shaped correctly by ``apply_operational_substitution``)."""
    return substitution_audit.copy() if not substitution_audit.empty else pd.DataFrame()


def build_resumen_homologacion(
    reinforcement_audit: pd.DataFrame,
    substitution_audit: pd.DataFrame,
) -> pd.DataFrame:
    """High-level counters for the run audit."""
    rows = [{
        "filas_forecast_reforzadas": int(len(reinforcement_audit)),
        "pares_analiticos_afectados": (
            int(reinforcement_audit[["sku_id"]].drop_duplicates().shape[0])
            if not reinforcement_audit.empty else 0
        ),
        "sustituciones_operacionales": int(len(substitution_audit)),
        "sucursales_con_sustitucion": (
            int(substitution_audit["ubicacion"].nunique())
            if not substitution_audit.empty else 0
        ),
        "demanda_transferida_total": (
            float(substitution_audit["demanda_transferida"].sum())
            if not substitution_audit.empty else 0.0
        ),
    }]
    return pd.DataFrame(rows)
