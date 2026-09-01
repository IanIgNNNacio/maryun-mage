"""Consolidación del ahorro a través de todas las corridas persistidas.

Cada corrida deja un ``carga_maryun.csv`` en ``data/output/<run_id>/``. Para
responder "¿cuánto ahorramos hoy / esta semana / este mes?" recorremos todas
esas carpetas, leemos la columna ``ahorro_clp`` y agregamos por
``fecha_generacion``.

Notas operativas
----------------
* ``hoy``     → filas cuyo ``fecha_generacion`` coincide con la fecha pedida.
* ``semana``  → últimos 7 días calendario hacia atrás (inclusive).
* ``mes``     → mismo mes calendario que la fecha pedida.

Si una misma (sku, origen, destino) aparece en múltiples corridas del mismo día
(porque se relanzó el pipeline), la ventana de silencio debería haberlo
filtrado, pero si no, se suman todas — es transparente al usuario y refleja
el plan realmente emitido al ERP.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from app.utils.logging import get_logger


_CARGA_FILENAME = "carga_maryun.csv"


@dataclass
class AhorroResumen:
    """Totales en CLP listos para logging/dashboard/JSON."""

    fecha_referencia: pd.Timestamp
    ahorro_hoy: float = 0.0
    ahorro_semana: float = 0.0
    ahorro_mes: float = 0.0
    # Desglose por capa en la ventana "mes" — útil para el dashboard.
    por_capa_mes: dict[str, float] = field(default_factory=dict)
    # Metadatos
    runs_considerados: int = 0
    filas_consideradas: int = 0

    def to_dict(self) -> dict[str, float | str | int | dict[str, float]]:
        return {
            "fecha_referencia": self.fecha_referencia.strftime("%Y-%m-%d"),
            "ahorro_hoy_clp": float(self.ahorro_hoy),
            "ahorro_semana_clp": float(self.ahorro_semana),
            "ahorro_mes_clp": float(self.ahorro_mes),
            "por_capa_mes_clp": {k: float(v) for k, v in self.por_capa_mes.items()},
            "runs_considerados": int(self.runs_considerados),
            "filas_consideradas": int(self.filas_consideradas),
        }


def load_historical_carga(output_dir: Path) -> pd.DataFrame:
    """Concatena todos los ``carga_maryun.csv`` bajo ``output_dir``.

    Devuelve un DataFrame vacío (con columnas esperadas) si no hay corridas.
    Corridas con CSV malformado o sin la columna ``ahorro_clp`` (corridas
    anteriores al feature) se ignoran con warning — no se inventan valores.
    """
    log = get_logger("savings.aggregator")
    frames: list[pd.DataFrame] = []
    runs_ok = 0
    runs_skipped = 0

    if not output_dir.exists():
        log.warning("savings.output_dir.missing", path=str(output_dir))
        return pd.DataFrame(columns=[
            "run_id", "fecha_generacion", "sku_id", "capa",
            "cantidad", "costo_unitario_clp", "ahorro_clp",
        ])

    for run_dir in sorted(output_dir.iterdir()):
        if not run_dir.is_dir():
            continue
        carga_path = run_dir / _CARGA_FILENAME
        if not carga_path.exists():
            continue
        try:
            df = pd.read_csv(carga_path)
        except Exception as exc:
            log.warning("savings.carga.read_error", run=run_dir.name, error=str(exc))
            runs_skipped += 1
            continue
        if "ahorro_clp" not in df.columns:
            # Corrida anterior al feature — no se puede recalcular retroactivamente
            # sin el costo unitario exacto, que acá YA viene calculado. Se ignora
            # silenciosamente para no contaminar el total con ceros.
            runs_skipped += 1
            continue
        df["fecha_generacion"] = pd.to_datetime(df["fecha_generacion"], errors="coerce")
        frames.append(df)
        runs_ok += 1

    log.info("savings.history.loaded", runs_ok=runs_ok, runs_skipped=runs_skipped)
    if not frames:
        return pd.DataFrame(columns=[
            "run_id", "fecha_generacion", "sku_id", "capa",
            "cantidad", "costo_unitario_clp", "ahorro_clp",
        ])
    return pd.concat(frames, ignore_index=True)


def compute_ahorro_resumen(
    history: pd.DataFrame,
    fecha_referencia: pd.Timestamp,
) -> AhorroResumen:
    """Agrega el ahorro en las ventanas hoy / últimos 7 días / mes calendario."""
    resumen = AhorroResumen(fecha_referencia=pd.Timestamp(fecha_referencia).normalize())

    if history.empty or "ahorro_clp" not in history.columns:
        return resumen

    df = history.copy()
    df["fecha_generacion"] = pd.to_datetime(df["fecha_generacion"], errors="coerce")
    df = df.dropna(subset=["fecha_generacion"])
    df["fecha_dia"] = df["fecha_generacion"].dt.normalize()
    df["ahorro_clp"] = pd.to_numeric(df["ahorro_clp"], errors="coerce").fillna(0.0)

    ref = resumen.fecha_referencia
    inicio_semana = ref - pd.Timedelta(days=6)  # últimos 7 días inclusive
    inicio_mes = ref.replace(day=1)
    fin_mes = (inicio_mes + pd.offsets.MonthEnd(0)).normalize()

    resumen.ahorro_hoy = float(df.loc[df["fecha_dia"] == ref, "ahorro_clp"].sum())
    resumen.ahorro_semana = float(
        df.loc[(df["fecha_dia"] >= inicio_semana) & (df["fecha_dia"] <= ref), "ahorro_clp"].sum()
    )
    mask_mes = (df["fecha_dia"] >= inicio_mes) & (df["fecha_dia"] <= fin_mes)
    resumen.ahorro_mes = float(df.loc[mask_mes, "ahorro_clp"].sum())

    # Desglose por capa dentro del mes.
    if "capa" in df.columns:
        por_capa = (
            df.loc[mask_mes].groupby("capa")["ahorro_clp"].sum().round(0).to_dict()
        )
        resumen.por_capa_mes = {str(k): float(v) for k, v in por_capa.items()}

    resumen.runs_considerados = int(df["run_id"].nunique()) if "run_id" in df.columns else 0
    resumen.filas_consideradas = int(len(df))
    return resumen
