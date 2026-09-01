"""Registro de ventana de silencio para alertas de reposición.

Objetivo: evitar enviar la misma alerta (sku, sucursal) más de una vez
dentro de una ventana de N días, para no bombardear al equipo con
notificaciones repetitivas cuando el stock sigue siendo bajo.

Flujo:
  1. Al inicio del stage ``silence_filter`` se carga el registro desde
     ``data/silence_registry.csv``.
  2. Los pares (sku, ubicacion) cuyo ``ultimo_envio`` esté dentro de los
     últimos ``window_days`` se marcan como silenciados y se excluyen
     del plan de reposición de esta corrida.
  3. Al finalizar el export se actualiza el registro con los pares que
     sí generaron acción en esta corrida (``stage_update_silence``).

El archivo CSV persiste entre corridas y no se borra automáticamente.
Si se quiere forzar el reenvío de todas las alertas, basta con eliminar
o vaciar ``data/silence_registry.csv``.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from app.normalize.canonical import canonical_location, canonical_sku
from app.utils.logging import get_logger

_COLUMNS = ["sku", "ubicacion", "ultimo_envio", "run_id"]


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=_COLUMNS)


def load_registry(path: Path) -> pd.DataFrame:
    """Carga el registro persistente. Devuelve DataFrame vacío si no existe."""
    log = get_logger("silence.registry")
    if not path.exists():
        log.info("silence.registry.missing_ok", path=str(path))
        return _empty()
    try:
        # Leer todo como string para evitar conflictos dtype/parse_dates en pandas 2.x
        df = pd.read_csv(path, dtype=str)
        df["ultimo_envio"] = pd.to_datetime(
            df["ultimo_envio"], format="%Y-%m-%d", errors="coerce"
        ).dt.date
        # Eliminar filas donde la conversión de fecha falló
        df = df[df["ultimo_envio"].apply(lambda v: v is not None and not isinstance(v, float))]
        df = df.dropna(subset=["sku", "ubicacion"])
        log.info("silence.registry.loaded", pairs=len(df), path=str(path))
        return df
    except Exception as exc:
        log.warning("silence.registry.load_failed", error=str(exc))
        return _empty()


def get_silenced_pairs(
    registry: pd.DataFrame,
    process_date: date,
    window_days: int,
) -> set[tuple[str, str]]:
    """Devuelve el conjunto de pares (sku, ubicacion) dentro de la ventana de silencio."""
    if registry.empty:
        return set()
    cutoff = process_date - timedelta(days=window_days)
    within = registry[registry["ultimo_envio"] >= cutoff]
    return {
        (canonical_sku(r.sku), canonical_location(r.ubicacion))
        for r in within.itertuples(index=False)
    }


def apply_silence_filter(
    needs: pd.DataFrame,
    silenced: set[tuple[str, str]],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Filtra ``needs`` suprimiendo los pares dentro de la ventana de silencio.

    Devuelve ``(needs_filtrado, needs_silenciados)`` para poder auditarlos.
    """
    if not silenced or needs.empty:
        return needs, pd.DataFrame(columns=needs.columns)

    mask_silenced = needs.apply(
        lambda r: (
            canonical_sku(r["sku_id"]),
            canonical_location(r["ubicacion"]),
        )
        in silenced,
        axis=1,
    )
    # Solo silenciamos filas que iban a disparar — las que no disparan
    # se dejan pasar (no generan alerta de todos modos).
    silenced_mask = mask_silenced & needs["dispara"]
    needs_out = needs.copy()
    needs_out.loc[silenced_mask, "dispara"] = False

    silenced_rows = needs[silenced_mask].copy()
    return needs_out, silenced_rows


def update_registry(
    registry: pd.DataFrame,
    plan: pd.DataFrame,
    process_date: date,
    run_id: str,
    path: Path,
) -> pd.DataFrame:
    """Actualiza el registro con los pares que generaron acción en este run.

    Solo registra las filas del plan que son de tipo 'compra' o 'cd'
    (las capas que representan una alerta real hacia afuera). Los
    movimientos internos (inmovilizado, sobrestock) no reinician el contador.
    """
    log = get_logger("silence.registry")

    if plan.empty:
        log.info("silence.registry.no_plan_to_register")
        _save(registry, path)
        return registry

    # Capas que representan alerta real al equipo de compras
    ALERT_LAYERS = {"compra", "cd"}
    alertas = plan[plan["capa"].isin(ALERT_LAYERS)][["sku_id", "destino"]].copy()
    alertas = alertas.rename(columns={"sku_id": "sku", "destino": "ubicacion"})
    alertas["sku"] = alertas["sku"].map(canonical_sku)
    alertas["ubicacion"] = alertas["ubicacion"].map(canonical_location)
    alertas = alertas.drop_duplicates(subset=["sku", "ubicacion"])
    alertas["ultimo_envio"] = process_date.isoformat()
    alertas["run_id"] = run_id

    if alertas.empty:
        log.info("silence.registry.no_alerts_this_run")
        _save(registry, path)
        return registry

    # Fusionar: las filas nuevas sobreescriben las anteriores del mismo par
    existing = registry[
        ~registry.set_index(["sku", "ubicacion"]).index.isin(
            alertas.set_index(["sku", "ubicacion"]).index
        )
    ] if not registry.empty else _empty()

    updated = pd.concat([existing, alertas[_COLUMNS]], ignore_index=True)
    _save(updated, path)
    log.info(
        "silence.registry.updated",
        pares_nuevos=len(alertas),
        total_pares=len(updated),
        path=str(path),
    )
    return updated


def _save(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df[_COLUMNS].to_csv(path, index=False)
