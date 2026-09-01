"""Compute per-(sku, sucursal) need = demand + safety stock − stock on hand."""
from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd

from app.config.engine_params import EngineParams
from app.normalize.canonical import canonical_location, canonical_sku, is_sucursal
from app.normalize.schemas import NeedsSchema
from app.utils.logging import get_logger


# ---------------------------------------------------------------------------
# Safety-stock multipliers by ABC/XYZ class.
# Higher XYZ variability → larger z-factor; higher ABC value → larger buffer.
# These are layered on top of the base multiplier in params.needs.
# ---------------------------------------------------------------------------
_CLASS_ZFACTOR: dict[str, float] = {
    "AX": 1.0, "AY": 1.2, "AZ": 1.5,
    "BX": 0.9, "BY": 1.1, "BZ": 1.4,
    "CX": 0.7, "CY": 0.9, "CZ": 1.2,
}


def _expected_demand(forecast: pd.DataFrame, horizon_months: int) -> pd.DataFrame:
    """Sum ``forecast_final`` over the next ``horizon_months`` per (sku, ubic)."""
    if forecast.empty:
        return pd.DataFrame(columns=["sku_id", "ubicacion", "demanda_esperada", "horizon_meses"])

    f = forecast.copy()
    f["mes"] = pd.to_datetime(f["mes"])
    f = f.sort_values(["sku_id", "ubicacion", "mes"])
    # Take the first N months per group.
    f["_rank"] = f.groupby(["sku_id", "ubicacion"])["mes"].rank(method="first").astype(int)
    keep = f[f["_rank"] <= horizon_months]
    agg = keep.groupby(["sku_id", "ubicacion"], as_index=False).agg(
        demanda_esperada=("forecast_final", "sum"),
        horizon_meses=("_rank", "max"),
    )
    agg["demanda_esperada"] = agg["demanda_esperada"].astype(float).clip(lower=0.0)
    agg["horizon_meses"] = agg["horizon_meses"].fillna(0).astype(int)
    return agg


def _estimate_sigma(
    demand_history: pd.DataFrame | None,
    forecast: pd.DataFrame,
) -> pd.DataFrame:
    """Estimate a monthly demand sigma per (sku, ubicacion).

    Falls back to 0.25 × mean(forecast) when history is absent — a pragmatic
    CV≈25% assumption that stops safety-stock from collapsing to zero when
    the series is brand new.
    """
    if demand_history is not None and not demand_history.empty:
        h = demand_history.copy()
        h["mes"] = pd.to_datetime(h["mes"])
        sigma = (
            h.groupby(["sku_id", "ubicacion"], as_index=False)["demanda"]
            .std(ddof=0)
            .rename(columns={"demanda": "sigma_demanda"})
        )
        sigma["sigma_demanda"] = sigma["sigma_demanda"].fillna(0.0).astype(float)
        return sigma

    if forecast.empty:
        return pd.DataFrame(columns=["sku_id", "ubicacion", "sigma_demanda"])
    agg = forecast.groupby(["sku_id", "ubicacion"], as_index=False)["forecast_final"].mean()
    agg["sigma_demanda"] = agg["forecast_final"].astype(float) * 0.25
    return agg[["sku_id", "ubicacion", "sigma_demanda"]]


def _stock_per_sucursal(stock: pd.DataFrame) -> pd.DataFrame:
    """Restrict the stock table to sucursales and guarantee one row per pair."""
    if stock.empty:
        return pd.DataFrame(columns=["sku_id", "ubicacion", "stock_actual"])
    s = stock.copy()
    s["sku_id"] = s["sku_id"].map(canonical_sku)
    s["ubicacion"] = s["ubicacion"].map(canonical_location)
    s = s[s["ubicacion"].map(is_sucursal)]
    s = (
        s.groupby(["sku_id", "ubicacion"], as_index=False)["qty"]
        .sum()
        .rename(columns={"qty": "stock_actual"})
    )
    s["stock_actual"] = s["stock_actual"].astype(float).clip(lower=0.0)
    return s


def _classification_lookup(classification: pd.DataFrame | None) -> pd.DataFrame:
    if classification is None or classification.empty:
        return pd.DataFrame(columns=["sku_id", "ubicacion", "clase_final"])
    c = classification[["sku_id", "ubicacion", "clase_final"]].copy()
    c["clase_final"] = c["clase_final"].astype(str).str.upper().str.strip()
    return c


DEFAULT_LEAD_DIAS = 10  # lead time por defecto (días) si el proveedor no lo trae


def _resolve_lead_times(out: pd.DataFrame, suppliers=None) -> pd.Series:
    """Lead time (días) por (sku, ubicacion) desde proveedores; default 10 días.

    ``suppliers`` es un SupplierTable (app.policies.suppliers). Si es None/vacío,
    o el SKU no tiene proveedor con lead time > 0, se usa DEFAULT_LEAD_DIAS.
    """
    if suppliers is None or getattr(suppliers, "is_empty", lambda: True)():
        return pd.Series(float(DEFAULT_LEAD_DIAS), index=out.index)
    pares = out[["sku_id", "ubicacion"]].drop_duplicates()
    lookup: dict[tuple[str, str], float] = {}
    for r in pares.itertuples(index=False):
        info = suppliers.info(r.sku_id, r.ubicacion)
        d = info.lead_time_dias if info is not None else 0
        lookup[(r.sku_id, r.ubicacion)] = (
            float(d) if d and d > 0 else float(DEFAULT_LEAD_DIAS))
    return out.apply(
        lambda r: lookup.get((r["sku_id"], r["ubicacion"]), float(DEFAULT_LEAD_DIAS)),
        axis=1)


def _resolve_dias_sin_venta(
    out: pd.DataFrame,
    ultima_venta: pd.DataFrame | None,
    process_date: date | None,
) -> pd.Series:
    """Días transcurridos desde la última venta por (sku, ubicacion).

    Usa EXACTAMENTE el mismo criterio que el motor de reposición
    (``engine._build_dias_lookup``): si un par no tiene registro de venta, se
    devuelve ``inf`` (= "nunca vendió", se trata como sin venta reciente).
    """
    if ultima_venta is None or ultima_venta.empty:
        return pd.Series(float("inf"), index=out.index, dtype=float)

    today = process_date or date.today()
    if hasattr(today, "date"):          # pd.Timestamp / datetime → date
        today = today.date()

    uv = ultima_venta.copy()
    # Nombres sin guion bajo inicial: itertuples no expone atributos que
    # empiezan con "_" (los renombra a posicionales _1, _2, …).
    uv["ksku"] = uv["sku_id"].map(canonical_sku)
    uv["kubic"] = uv["ubicacion"].map(canonical_location)
    uv["kuv"] = pd.to_datetime(uv["ultima_venta"], errors="coerce")

    lookup: dict[tuple[str, str], float] = {}
    for r in uv.itertuples(index=False):
        if pd.isna(r.kuv) or r.ksku is None or r.kubic is None:
            continue
        try:
            d = float((today - r.kuv.date()).days)
        except Exception:
            continue
        key = (r.ksku, r.kubic)
        # Si hay duplicados, quedarse con la venta MÁS reciente (mínimo días).
        if key not in lookup or d < lookup[key]:
            lookup[key] = d

    sku_c = out["sku_id"].map(canonical_sku)
    ubic_c = out["ubicacion"].map(canonical_location)
    return pd.Series(
        [lookup.get((s, u), float("inf")) for s, u in zip(sku_c, ubic_c)],
        index=out.index,
        dtype=float,
    )


def compute_needs(
    forecast: pd.DataFrame,
    stock: pd.DataFrame,
    params: EngineParams,
    classification: pd.DataFrame | None = None,
    demand_history: pd.DataFrame | None = None,
    suppliers=None,
    ultima_venta: pd.DataFrame | None = None,
    process_date: date | None = None,
) -> pd.DataFrame:
    """Return a ``NeedsSchema``-valid DataFrame with audit columns attached.

    Modelo de reposición (calibrado: 1 mes de demanda + lead time del proveedor):
        demanda_mensual = demanda_esperada / horizon_meses     (promedio mensual)
        safety_stock    = demanda_mensual × (lead_time_dias / 30)   (demanda del lead time)
        objetivo        = demanda_mensual + safety_stock       (1 mes + lead time)
        necesidad       = max(0, objetivo − stock_actual)
        dispara         = necesidad > 0
    ``lead_time_dias`` se toma de ``suppliers`` (proveedores.xlsx); si falta → 10 días.

    Regla de RECENCIA DE VENTA (params.needs.bloquear_sin_venta_reciente):
        Si un (sku, sucursal) NO registró venta en los últimos
        ``requerido_dias_sin_venta_max`` días (default 90), NO puede ser
        "requerido": se fuerza ``necesidad = 0`` y ``dispara = False``. Esto evita
        comprar / trasladar hacia productos que no rotan en esa sucursal. "Nunca
        vendió" (sin registro) se trata como sin venta reciente.
        ``ultima_venta`` proviene de MariaDB (acc.ultima_venta); ``process_date``
        es la fecha de referencia del run (ctx.process_date).
    """
    log = get_logger("needs.calculator")
    horizon = int(params.forecast.horizon_months)
    base_mult = float(params.needs.safety_stock_multiplier)
    default_cov = float(params.needs.coverage_months_default)
    trigger_below = bool(params.needs.trigger_below_safety)

    demand = _expected_demand(forecast, horizon)
    sigma = _estimate_sigma(demand_history, forecast)
    on_hand = _stock_per_sucursal(stock)
    clase = _classification_lookup(classification)

    if demand.empty and on_hand.empty:
        log.info("needs.calculator.empty_inputs")
        return NeedsSchema.validate(
            pd.DataFrame(
                columns=[
                    "sku_id", "ubicacion", "demanda_esperada", "stock_actual",
                    "safety_stock", "necesidad", "dispara",
                ]
            ),
            lazy=True,
        )

    # Outer-join everything so a sucursal with stock but no forecast still lands,
    # and a (sku, ubicacion) with forecast but no stock is treated as stock=0.
    out = demand.merge(on_hand, on=["sku_id", "ubicacion"], how="outer")
    out = out.merge(sigma, on=["sku_id", "ubicacion"], how="left")
    out = out.merge(clase, on=["sku_id", "ubicacion"], how="left")

    out["demanda_esperada"] = out["demanda_esperada"].fillna(0.0).astype(float)
    out["horizon_meses"] = out["horizon_meses"].fillna(horizon).astype(int)
    out["stock_actual"] = out["stock_actual"].fillna(0.0).astype(float)
    out["sigma_demanda"] = out["sigma_demanda"].fillna(0.0).astype(float)
    out["clase_final"] = out["clase_final"].fillna("CZ").astype(str)

    # Only keep sucursales (CDs are handled by the CD layer, not the needs layer).
    out = out[out["ubicacion"].map(is_sucursal)].reset_index(drop=True)

    if out.empty:
        empty = pd.DataFrame(
            columns=[
                "sku_id", "ubicacion", "demanda_esperada", "stock_actual",
                "safety_stock", "necesidad", "dispara",
            ]
        )
        log.info("needs.calculator.empty_after_filter")
        return NeedsSchema.validate(empty, lazy=True)

    # ── Stock de seguridad = demanda durante el LEAD TIME del proveedor ──────
    #   demanda_mensual = demanda_esperada / horizon_meses   (promedio mensual)
    #   safety_stock    = demanda_mensual × (lead_time_dias / 30)
    #   objetivo        = demanda_mensual (1 mes) + safety_stock
    #   necesidad       = max(0, objetivo − stock_actual)
    # lead_time_dias viene del proveedor; si falta, DEFAULT_LEAD_DIAS (10 días).
    out["lead_time_dias"] = _resolve_lead_times(out, suppliers).astype(float)
    out["demanda_mensual"] = (
        out["demanda_esperada"] / out["horizon_meses"].clip(lower=1)
    ).astype(float)
    out["safety_stock"] = (
        out["demanda_mensual"] * out["lead_time_dias"] / 30.0
    ).clip(lower=0.0).astype(float)
    objetivo = out["demanda_mensual"] + out["safety_stock"]
    out["necesidad"] = (objetivo - out["stock_actual"]).clip(lower=0.0).astype(float)

    if trigger_below:
        out["dispara"] = out["necesidad"] > 0.0
    else:
        # Only trigger when the hole is larger than half a month of demand.
        monthly_rate = out["demanda_esperada"] / out["horizon_meses"].clip(lower=1)
        threshold = monthly_rate * 0.5
        out["dispara"] = out["necesidad"] > threshold

    # ── Regla de RECENCIA: sin venta reciente → NO requerido ─────────────────
    #   Un (sku, sucursal) sin venta en los últimos N días no puede ser
    #   requerido: necesidad=0 y dispara=False. Bloquea compra, traslado de CD
    #   y redistribución HACIA ese par. Es independiente de la demanda esperada.
    #   "Nunca vendió" (sin registro) → dias_sin_venta=inf → bloqueado.
    out["dias_sin_venta"] = _resolve_dias_sin_venta(out, ultima_venta, process_date)
    out["bloqueado_sin_venta"] = False
    # Defensa: solo se bloquea si HAY datos de venta. Si ``ultima_venta`` viene
    # vacío (falló la carga desde MariaDB, o entorno de test/mock), la regla es
    # inerte — nunca se anula toda la compra por ausencia de la señal de venta.
    _hay_datos_venta = ultima_venta is not None and not ultima_venta.empty
    if _hay_datos_venta and bool(
        getattr(params.needs, "bloquear_sin_venta_reciente", False)
    ):
        umbral_dias = float(params.needs.requerido_dias_sin_venta_max)
        stale = out["dias_sin_venta"] > umbral_dias
        out["bloqueado_sin_venta"] = stale
        # Cuántos REQUERÍAN reposición y ahora se bloquean (impacto real).
        requerido_bloqueado = int((stale & (out["necesidad"] > 0.0)).sum())
        out.loc[stale, "necesidad"] = 0.0
        out.loc[stale, "dispara"] = False
        log.info(
            "needs.calculator.recencia_bloqueo",
            umbral_dias=umbral_dias,
            pares_stale=int(stale.sum()),
            requerido_bloqueado=requerido_bloqueado,
            nunca_vendio=int(np.isinf(out["dias_sin_venta"]).sum()),
        )

    # Audit-only column.
    out["cobertura_actual_meses"] = np.where(
        out["demanda_esperada"] > 0,
        (out["stock_actual"] * out["horizon_meses"]) / out["demanda_esperada"].replace(0, np.nan),
        np.inf,
    )
    out["cobertura_actual_meses"] = (
        out["cobertura_actual_meses"]
        .replace([np.inf, -np.inf], np.nan)
        .fillna(0.0)
        .astype(float)
    )

    log.info(
        "needs.calculator.done",
        rows=len(out),
        triggered=int(out["dispara"].sum()),
        total_necesidad=float(out["necesidad"].sum()),
        bloqueados_sin_venta=int(out["bloqueado_sin_venta"].sum()),
    )
    return NeedsSchema.validate(out, lazy=True)


def build_needs_audit(needs: pd.DataFrame) -> pd.DataFrame:
    """Slim audit slice — the same frame pruned to canonical audit columns."""
    if needs.empty:
        return needs
    cols = [
        "sku_id", "ubicacion", "clase_final",
        "demanda_esperada", "stock_actual", "sigma_demanda",
        "safety_stock", "necesidad", "dispara",
        "dias_sin_venta", "bloqueado_sin_venta",
        "cobertura_actual_meses", "horizon_meses",
    ]
    present = [c for c in cols if c in needs.columns]
    return needs[present].copy()
