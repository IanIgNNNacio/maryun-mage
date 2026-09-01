"""Data loaders con caching para el dashboard.

Todos los ``@st.cache_data(ttl=300)``: Streamlit refresca como máximo cada 5
minutos. El botón "🔄 Refrescar datos" limpia el cache manualmente.

Fuentes:
    * ``data/output/<run_id>/carga_maryun.csv`` — histórico de planes y ahorro.
    * ``data/output/<run_id>/run_audit.xlsx`` — audits del último run.
    * ``MariaDB`` — demanda real histórica (para forecast vs real).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import streamlit as st

from app.config.paths import get_paths


@dataclass(frozen=True)
class LatestRun:
    run_id: str
    out_dir: Path
    fecha: pd.Timestamp


def _output_root() -> Path:
    return get_paths().output_dir


# ---------------------------------------------------------------------------
# Histórico consolidado de cargas
# ---------------------------------------------------------------------------
@st.cache_data(ttl=300, show_spinner="Cargando histórico de corridas…")
def load_all_cargas() -> pd.DataFrame:
    """Concatena todos los ``carga_maryun.csv`` bajo ``data/output/``.

    Corridas sin la columna ``ahorro_clp`` (anteriores al feature) se ignoran.
    """
    root = _output_root()
    frames: list[pd.DataFrame] = []
    if not root.exists():
        return pd.DataFrame()

    for run_dir in sorted(root.iterdir()):
        if not run_dir.is_dir():
            continue
        p = run_dir / "carga_maryun.csv"
        if not p.exists():
            continue
        try:
            df = pd.read_csv(p)
        except Exception:
            continue
        if "ahorro_clp" not in df.columns:
            continue
        df["fecha_generacion"] = pd.to_datetime(df["fecha_generacion"], errors="coerce")

        # Backfill clase_abc_xyz desde el run_audit.xlsx si el CSV no la trae
        # (runs anteriores a la incorporación del feature).
        if "clase_abc_xyz" not in df.columns:
            audit_p = run_dir / "run_audit.xlsx"
            cls_map: pd.DataFrame | None = None
            if audit_p.exists():
                try:
                    cls = pd.read_excel(audit_p, sheet_name="classification_final")
                    if {"sku_id", "ubicacion", "clase_final"}.issubset(cls.columns):
                        cls_map = (
                            cls[["sku_id", "ubicacion", "clase_final"]]
                            .rename(columns={"ubicacion": "destino",
                                             "clase_final": "clase_abc_xyz"})
                            .drop_duplicates(["sku_id", "destino"])
                        )
                except Exception:
                    cls_map = None
            if cls_map is not None:
                df = df.merge(cls_map, on=["sku_id", "destino"], how="left")
                df["clase_abc_xyz"] = df["clase_abc_xyz"].fillna("N/A").astype(str)
            else:
                df["clase_abc_xyz"] = "N/A"
        else:
            df["clase_abc_xyz"] = df["clase_abc_xyz"].fillna("N/A").astype(str)

        frames.append(df)

    if not frames:
        return pd.DataFrame()

    out = pd.concat(frames, ignore_index=True)
    out["fecha_dia"] = out["fecha_generacion"].dt.normalize()
    out["ahorro_clp"] = pd.to_numeric(out["ahorro_clp"], errors="coerce").fillna(0.0)
    out["cantidad"] = pd.to_numeric(out["cantidad"], errors="coerce").fillna(0.0)
    return out


# ---------------------------------------------------------------------------
# Último run
# ---------------------------------------------------------------------------
@st.cache_data(ttl=300)
def find_latest_run() -> LatestRun | None:
    root = _output_root()
    if not root.exists():
        return None
    runs = [p for p in root.iterdir() if p.is_dir() and (p / "carga_maryun.csv").exists()]
    if not runs:
        return None
    # run_id tiene formato YYYYMMDD-hhmmss-<suffix> → sort alfabético = cronológico.
    latest = sorted(runs, key=lambda p: p.name)[-1]
    df = pd.read_csv(latest / "carga_maryun.csv", nrows=1)
    fecha = pd.to_datetime(df["fecha_generacion"].iloc[0]) if not df.empty else pd.NaT
    return LatestRun(run_id=latest.name, out_dir=latest, fecha=fecha)


@st.cache_data(ttl=300, show_spinner="Leyendo audit del último run…")
def load_latest_audit_sheet(sheet: str) -> pd.DataFrame:
    """Lee una hoja específica del ``run_audit.xlsx`` del último run."""
    latest = find_latest_run()
    if latest is None:
        return pd.DataFrame()
    p = latest.out_dir / "run_audit.xlsx"
    if not p.exists():
        return pd.DataFrame()
    try:
        return pd.read_excel(p, sheet_name=sheet)
    except Exception:
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Costos por SKU (para valorizar stock)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# PMP por SKU × sucursal — desde stock_valorizado.xlsx
# ---------------------------------------------------------------------------
@st.cache_data(ttl=600, show_spinner=False)
def load_pmp_por_sucursal() -> pd.DataFrame:
    """Lee el archivo Stock Valorizado y devuelve PMP por (sku_id, ubicacion).

    Fuente:  data/reference/stock_valorizado.xlsx
    Columnas devueltas:  sku_id, ubicacion, costo_pmp, unidades_pmp

    Usa COSTO_PPM (Precio Medio Ponderado de la plataforma) — más preciso que
    el costo global de proveedores.xlsx porque captura el costo real con que
    cada sucursal contabiliza su stock.
    """
    path = get_paths().stock_valorizado_xlsx
    if not path.exists():
        return pd.DataFrame(columns=["sku_id", "ubicacion", "costo_pmp", "unidades_pmp"])

    from app.normalize.canonical import canonical_location, canonical_sku

    try:
        df = pd.read_excel(path)
    except Exception:
        return pd.DataFrame(columns=["sku_id", "ubicacion", "costo_pmp", "unidades_pmp"])

    # Mapear columnas (el archivo tiene encabezados en mayúscula)
    rename_map = {}
    for raw, target in [
        ("BODEGA", "ubicacion"), ("SKU", "sku_id"),
        ("COSTO_PPM", "costo_pmp"), ("UNIDADES", "unidades_pmp"),
    ]:
        if raw in df.columns:
            rename_map[raw] = target
    df = df.rename(columns=rename_map)

    required = {"sku_id", "ubicacion", "costo_pmp"}
    if not required.issubset(df.columns):
        return pd.DataFrame(columns=["sku_id", "ubicacion", "costo_pmp", "unidades_pmp"])

    df["sku_id"]     = df["sku_id"].astype(str).map(canonical_sku)
    df["ubicacion"]  = df["ubicacion"].astype(str).map(canonical_location)
    df["costo_pmp"]  = pd.to_numeric(df["costo_pmp"], errors="coerce").fillna(0.0)
    if "unidades_pmp" in df.columns:
        df["unidades_pmp"] = pd.to_numeric(df["unidades_pmp"], errors="coerce").fillna(0.0)
    else:
        df["unidades_pmp"] = 0.0

    # Si hay duplicados (mismo sku+ubi), tomamos el costo > 0 más alto
    df = df.sort_values("costo_pmp", ascending=False)
    df = df.drop_duplicates(subset=["sku_id", "ubicacion"], keep="first")
    return df[["sku_id", "ubicacion", "costo_pmp", "unidades_pmp"]]


@st.cache_data(ttl=600)
def load_costos_sku() -> pd.DataFrame:
    """Devuelve un mapa (sku_id) → costo_unitario_clp, tomando el mejor costo
    (prioridad 1 → 2 → ...) desde ``proveedores.xlsx``.
    """
    path = get_paths().proveedores_xlsx
    if not path.exists():
        return pd.DataFrame(columns=["sku_id", "costo_unitario_clp"])
    df = pd.read_excel(path)
    # En el Excel la columna se llama "sku" — la normalizamos a "sku_id" para
    # que el merge con el resto del dashboard funcione sin fricciones.
    if "sku_id" not in df.columns and "sku" in df.columns:
        df = df.rename(columns={"sku": "sku_id"})
    if df.empty or "sku_id" not in df.columns:
        return pd.DataFrame(columns=["sku_id", "costo_unitario_clp"])

    from app.normalize.canonical import canonical_sku

    df["sku_id"] = df["sku_id"].astype(str).map(canonical_sku)
    df["costo_unitario_clp"] = pd.to_numeric(
        df.get("costo_unitario_clp", 0.0), errors="coerce"
    ).fillna(0.0)
    if "prioridad" in df.columns:
        df = df.sort_values("prioridad", ascending=True)
    return df.drop_duplicates("sku_id", keep="first")[["sku_id", "costo_unitario_clp"]]


# ---------------------------------------------------------------------------
# Demanda real (MariaDB) — para forecast vs real
# ---------------------------------------------------------------------------
@st.cache_data(ttl=600, show_spinner="Consultando demanda real en MariaDB…")
def load_demanda_real(process_date: str) -> pd.DataFrame:
    """Consulta demanda mensual histórica hasta el último mes cerrado.

    Usa el mismo ``TemporalGate`` que el pipeline, así la ventana queda
    alineada con lo que el motor considera "cerrado".
    """
    from app.config.engine_params import load_engine_params
    from app.extract.extractor import extract_all
    from app.temporal.gate import TemporalGate

    try:
        params = load_engine_params()
        gate = TemporalGate(
            process_date=pd.Timestamp(process_date).date(),
            params=params,
        )
        result = extract_all(gate=gate)
        dem = result["demand"].copy()
    except Exception as exc:
        st.warning(f"No se pudo leer demanda real desde MariaDB: {exc}")
        return pd.DataFrame()

    dem["mes"] = pd.to_datetime(dem["mes"])
    dem["demanda"] = pd.to_numeric(dem["demanda"], errors="coerce").fillna(0.0)
    return dem[["sku_id", "ubicacion", "mes", "demanda"]]


# ---------------------------------------------------------------------------
# Sucursales disponibles (para el filtro del sidebar)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Stock Demanda (opcional — días reales en bodega)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=600, show_spinner="Consultando días en bodega desde MariaDB…")
def load_stock_demanda() -> pd.DataFrame:
    """Consulta último ingreso por SKU × sucursal desde MariaDB.

    Calcula ``dias_en_bodega`` como días transcurridos desde
    ``ultimo_ingreso_sucursal``. Si la conexión falla devuelve
    DataFrame vacío y el dashboard estima los días desde cobertura.

    Columnas devueltas:
        sku_id, ubicacion, dias_en_bodega,
        ultimo_ingreso_sucursal, ultima_compra_global
    """
    from app.connectors.mariadb import MariaDBConnector
    from app.extract.queries import load_sql
    from app.normalize.canonical import canonical_location, canonical_sku

    try:
        conn = MariaDBConnector()
        df = conn.run_query(load_sql("stock_demanda"))
    except Exception as exc:
        st.warning(f"No se pudo leer stock en bodega desde MariaDB: {exc}")
        return pd.DataFrame()

    if df.empty:
        return pd.DataFrame()

    # Normalizar identificadores al mismo estándar que el pipeline
    df["sku_id"]    = df["sku_id"].astype(str).map(canonical_sku)
    df["ubicacion"] = df["sucursal"].astype(str).map(canonical_location)
    df = df.drop(columns=["sucursal"], errors="ignore")

    # Calcular días desde el último ingreso a esa sucursal
    df["ultimo_ingreso_sucursal"] = pd.to_datetime(
        df["ultimo_ingreso_sucursal"], errors="coerce"
    )
    df["ultima_compra_global"] = pd.to_datetime(
        df["ultima_compra_global"], errors="coerce"
    )
    hoy = pd.Timestamp.today().normalize()
    df["dias_en_bodega"] = (hoy - df["ultimo_ingreso_sucursal"]).dt.days

    # Si no hay ingreso en esa sucursal, usar fecha de última compra global
    sin_ingreso = df["dias_en_bodega"].isna()
    df.loc[sin_ingreso, "dias_en_bodega"] = (
        hoy - df.loc[sin_ingreso, "ultima_compra_global"]
    ).dt.days

    df["dias_en_bodega"] = pd.to_numeric(
        df["dias_en_bodega"], errors="coerce"
    ).fillna(0).clip(lower=0).astype(int)

    return df[["sku_id", "ubicacion", "dias_en_bodega",
               "ultimo_ingreso_sucursal", "ultima_compra_global"]]


# ---------------------------------------------------------------------------
# Última venta por SKU × sucursal — desde MariaDB
# ---------------------------------------------------------------------------
@st.cache_data(ttl=600, show_spinner="Consultando última venta por SKU × sucursal…")
def load_ultima_venta() -> pd.DataFrame:
    """Devuelve la fecha de última venta y los días transcurridos por (sku, ubicacion).

    Columnas devueltas:
        sku_id, ubicacion, ultima_venta, dias_desde_ultima_venta
    """
    from app.connectors.mariadb import MariaDBConnector
    from app.extract.queries import load_sql
    from app.normalize.canonical import canonical_location, canonical_sku

    try:
        conn = MariaDBConnector()
        df = conn.run_query(load_sql("ultima_venta"))
    except Exception as exc:
        st.warning(f"No se pudo leer última venta desde MariaDB: {exc}")
        return pd.DataFrame(
            columns=["sku_id", "ubicacion", "ultima_venta", "dias_desde_ultima_venta"]
        )

    if df.empty:
        return pd.DataFrame(
            columns=["sku_id", "ubicacion", "ultima_venta", "dias_desde_ultima_venta"]
        )

    df["sku_id"]    = df["sku_id"].astype(str).map(canonical_sku)
    df["ubicacion"] = df["ubicacion"].astype(str).map(canonical_location)
    df["ultima_venta"] = pd.to_datetime(df["ultima_venta"], errors="coerce")

    hoy = pd.Timestamp.today().normalize()
    df["dias_desde_ultima_venta"] = (hoy - df["ultima_venta"]).dt.days
    df["dias_desde_ultima_venta"] = pd.to_numeric(
        df["dias_desde_ultima_venta"], errors="coerce"
    ).clip(lower=0)
    return df[["sku_id", "ubicacion", "ultima_venta", "dias_desde_ultima_venta"]]


# ---------------------------------------------------------------------------
# Historial semanal de ofertas
# ---------------------------------------------------------------------------
def _historial_path() -> "Path":
    from pathlib import Path
    p = Path(__file__).resolve().parents[3] / "reportes_stock" / "historial" / "historial_stock_semanal.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load_historial_ofertas() -> pd.DataFrame:
    """Lee el historial acumulativo de snapshots semanales."""
    p = _historial_path()
    if not p.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(p, dtype=str)
    except Exception:
        return pd.DataFrame()


def guardar_snapshot_historial(df_nuevo: pd.DataFrame) -> None:
    """Agrega filas al historial (append) sin sobrescribir."""
    p = _historial_path()
    prev = load_historial_ofertas()
    combined = pd.concat([prev, df_nuevo], ignore_index=True)
    combined.to_csv(p, index=False, encoding="utf-8")


@st.cache_data(ttl=600)
def list_sucursales() -> list[str]:
    from app.normalize.canonical import SUCURSALES_CANONICAS

    return sorted(SUCURSALES_CANONICAS)


# ---------------------------------------------------------------------------
# Catálogo de productos (nombre, marca, familia)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=600, show_spinner=False)
def load_productos() -> pd.DataFrame:
    """Lee el catálogo de productos desde el audit del último run.

    Columnas garantizadas: sku_id (str), nombre, marca, familia, tipo.
    Si el audit no tiene la hoja, cae a un DataFrame vacío.
    """
    df = load_latest_audit_sheet("products")
    if df.empty:
        return pd.DataFrame(columns=["sku_id", "nombre", "marca", "familia", "tipo"])
    df["sku_id"] = df["sku_id"].astype(str)
    for col in ("nombre", "marca", "familia", "tipo"):
        if col not in df.columns:
            df[col] = ""
        else:
            df[col] = df[col].fillna("").astype(str)
    return df[["sku_id", "nombre", "marca", "familia", "tipo"]]
