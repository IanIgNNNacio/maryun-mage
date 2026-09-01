"""Compra consolidada nacional — lógica pura (SOLO LECTURA).

Identifica productos que conviene comprar de forma **consolidada a nivel
nacional** (alta rotación / infaltables presentes en gran parte de las
sucursales) para negociar mejor precio con proveedores.

Consume EXCLUSIVAMENTE datos que ya existen en Supabase: la tabla
``plan_actual`` (snapshot analítico del último run), que trae por
``(sucursal, sku)``: pronóstico del mes (``demanda_mes_actual``),
clasificación ABC/XYZ (``clase_abc_xyz``), stock, costo y proveedor.

Este módulo NO recalcula ni modifica facturación, clasificación ni
pronósticos: solo LEE esos campos y los agrega. No tiene dependencia de
Streamlit (para poder testearse de forma aislada).

Regla de datos faltantes (conservadora): si un dato no existe, se marca como
``"sin información disponible"`` o se excluye del cálculo — nunca se inventa.
En particular, la **venta histórica** no está persistida en Supabase, por lo
que siempre se reporta como sin información.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path

import numpy as np
import pandas as pd

try:  # yaml ya es dependencia del proyecto; si faltara, se usan defaults.
    import yaml
except Exception:  # pragma: no cover
    yaml = None  # type: ignore

SIN_INFO = "sin información disponible"

# Nombres de columna esperados (en minúscula; el DataFrame de entrada se
# normaliza a minúscula, de modo que funciona tanto con la tabla de Supabase
# —columnas en minúscula— como con el export PowerBI —columnas en MAYÚSCULA—).
_COL_SUC = "sucursal"
_COL_NOMBRE = "nombre"
_COL_SKU = "sku_correcto"
_COL_SKU_ALT = "sku"
_COL_CLASE = "clase_abc_xyz"
_COL_DEMANDA = "demanda_mes_actual"
_COL_CANTREQ = "cant_req"

# Orden y nombres de columnas de salida (contrato estable para la UI/tests).
OUTPUT_COLS = [
    "sku", "nombre", "clase",
    "n_suc_demanda", "n_suc_presente", "n_suc_total", "cobertura_pct",
    "demanda_consolidada", "cant_req_consolidada", "venta_historica",
    "alta_rotacion", "n_suc_rotacion", "score", "prioridad", "motivo",
]


@dataclass
class ConsolidadoParams:
    """Umbrales y pesos configurables. Defaults documentados.

    Para ajustar: editar ``src/app/config/consolidado_params.yaml`` o mover los
    controles en la pestaña del dashboard (sobreescriben estos defaults en vivo).
    """
    cobertura_min_pct: float = 0.50   # fracción mín. de sucursales con demanda
    rotacion_suc_min: int = 3         # nº mín. de sucursales con XYZ='X'
    peso_cobertura: float = 0.50      # pesos del score (idealmente suman 1)
    peso_demanda: float = 0.30
    peso_rotacion: float = 0.20
    corte_prioridad_alta: float = 66.0   # score ≥ → "Alta"
    corte_prioridad_media: float = 33.0  # score ≥ → "Media" (si no, "Baja")
    cd_labels: tuple[str, ...] = ("CD SANTIAGO", "CD SUR")  # excluidos del universo
    top_n: int = 200                  # máximo de filas a devolver (rendimiento)

    @property
    def peso_total(self) -> float:
        return self.peso_cobertura + self.peso_demanda + self.peso_rotacion


def _default_params_path() -> Path:
    # este archivo: src/app/dashboard/consolidado.py  →  src/app/config/...
    return Path(__file__).resolve().parents[1] / "config" / "consolidado_params.yaml"


def load_consolidado_params(path: Path | None = None) -> ConsolidadoParams:
    """Carga umbrales desde YAML; si no existe o falla, devuelve defaults.

    Mismo patrón que ``load_engine_params`` (degradación elegante). Ignora
    claves desconocidas para no romper ante archivos con campos extra.
    """
    target = path or _default_params_path()
    try:
        if yaml is None or not target.exists():
            return ConsolidadoParams()
        with target.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except Exception:
        return ConsolidadoParams()
    if not isinstance(raw, dict):
        return ConsolidadoParams()
    valid = {f.name for f in fields(ConsolidadoParams)}
    clean = {k: v for k, v in raw.items() if k in valid}
    if isinstance(clean.get("cd_labels"), list):
        clean["cd_labels"] = tuple(str(x) for x in clean["cd_labels"])
    try:
        return ConsolidadoParams(**clean)
    except Exception:
        return ConsolidadoParams()


def _empty_output() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="object") for c in OUTPUT_COLS})


def _es_rotacion(clase) -> bool:
    """True si la clasificación ABC/XYZ indica alta rotación (XYZ='X').

    Lee la clasificación existente; no la recalcula. Formato esperado tipo
    'AX', 'BZ'… → el último carácter es la letra XYZ.
    """
    if not isinstance(clase, str):
        return False
    c = clase.strip().upper()
    return len(c) >= 2 and c[-1] == "X"


def _moda_clase(serie: pd.Series) -> str:
    s = serie.dropna().astype(str).str.strip()
    s = s[(s != "") & (s.str.upper() != "N/A")]
    if s.empty:
        return "—"
    m = s.mode()
    return str(m.iloc[0]) if len(m) else str(s.iloc[0])


def build_consolidado(
    plan_actual: pd.DataFrame,
    params: ConsolidadoParams | None = None,
    *,
    group_by: str = "sku",
    demanda_min: float = 0.0,
) -> pd.DataFrame:
    """Agrega ``plan_actual`` a nivel nacional y devuelve productos candidatos.

    Parameters
    ----------
    plan_actual : DataFrame con grano (sucursal, sku). Columnas reconocidas
        (en cualquier capitalización): sucursal, nombre, sku_correcto/sku,
        clase_abc_xyz, demanda_mes_actual, cant_req.
    params : umbrales/pesos. Si None, usa defaults.
    group_by : "sku" (default, por identificador) o "nombre".
    demanda_min : filtro opcional de demanda consolidada mínima (0 = sin filtro).

    Returns
    -------
    DataFrame con columnas ``OUTPUT_COLS`` (vacío si no hay datos/candidatos).
    Nunca lanza por datos faltantes: degrada a "sin información".
    """
    params = params or ConsolidadoParams()

    if plan_actual is None or len(plan_actual) == 0:
        return _empty_output()

    df = plan_actual.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]

    # Sin la dimensión sucursal no hay análisis nacional posible.
    if _COL_SUC not in df.columns:
        return _empty_output()

    # Excluir centros de distribución del universo de sucursales.
    cd = {str(s).upper() for s in params.cd_labels}
    df = df[~df[_COL_SUC].astype(str).str.upper().isin(cd)]
    if df.empty:
        return _empty_output()

    n_suc_total = int(df[_COL_SUC].nunique())
    if n_suc_total == 0:
        return _empty_output()

    has_demanda = _COL_DEMANDA in df.columns
    has_cantreq = _COL_CANTREQ in df.columns
    has_clase = _COL_CLASE in df.columns

    df["_dem"] = (pd.to_numeric(df[_COL_DEMANDA], errors="coerce").fillna(0.0)
                  if has_demanda else 0.0)
    df["_cantreq"] = (pd.to_numeric(df[_COL_CANTREQ], errors="coerce").fillna(0.0)
                      if has_cantreq else 0.0)
    df["_rot"] = df[_COL_CLASE].apply(_es_rotacion) if has_clase else False

    # Clave de agregación.
    if group_by == "nombre" and _COL_NOMBRE in df.columns:
        key_col = _COL_NOMBRE
    elif _COL_SKU in df.columns:
        key_col = _COL_SKU
    elif _COL_SKU_ALT in df.columns:
        key_col = _COL_SKU_ALT
    elif _COL_NOMBRE in df.columns:
        key_col = _COL_NOMBRE
    else:
        return _empty_output()
    df["_key"] = df[key_col].astype(str)

    sku_disp = _COL_SKU if _COL_SKU in df.columns else (
        _COL_SKU_ALT if _COL_SKU_ALT in df.columns else None)

    grp = df.groupby("_key", sort=False)
    base = pd.DataFrame(index=grp.size().index)
    base["nombre"] = grp[_COL_NOMBRE].first() if _COL_NOMBRE in df.columns else SIN_INFO
    base["sku"] = grp[sku_disp].first() if sku_disp else base.index.to_series().values
    base["clase"] = grp[_COL_CLASE].agg(_moda_clase) if has_clase else "—"
    base["n_suc_presente"] = grp[_COL_SUC].nunique()
    base["_demanda_sum"] = grp["_dem"].sum()
    base["cant_req_consolidada"] = grp["_cantreq"].sum()

    # Conteos condicionales por sucursal (nunique sobre subconjuntos).
    suc_dem = df.loc[df["_dem"] > 0].groupby("_key")[_COL_SUC].nunique()
    base["n_suc_demanda"] = suc_dem.reindex(base.index).fillna(0).astype(int)
    suc_rot = df.loc[df["_rot"]].groupby("_key")[_COL_SUC].nunique()
    base["n_suc_rotacion"] = suc_rot.reindex(base.index).fillna(0).astype(int)

    # Cobertura: con demanda si está disponible; si no, por presencia.
    suc_base = base["n_suc_demanda"] if has_demanda else base["n_suc_presente"]
    base["cobertura_pct"] = suc_base / n_suc_total
    base["rot_frac"] = base["n_suc_rotacion"] / n_suc_total
    base["alta_rotacion"] = base["n_suc_rotacion"] >= 1
    base["n_suc_total"] = n_suc_total

    # Candidato si cumple ≥1 condición (umbrales configurables).
    mask = ((base["cobertura_pct"] >= params.cobertura_min_pct)
            | (base["n_suc_rotacion"] >= params.rotacion_suc_min))
    base = base[mask]
    if demanda_min and demanda_min > 0:
        base = base[base["_demanda_sum"] >= float(demanda_min)]
    if base.empty:
        return _empty_output()

    # Score transparente (0–100). demanda_norm = min-max entre candidatos.
    dem_max = float(base["_demanda_sum"].max())
    if not np.isfinite(dem_max) or dem_max <= 0:
        dem_max = 1.0
    base["demanda_norm"] = (base["_demanda_sum"] / dem_max).clip(0, 1)
    wsum = params.peso_total or 1.0
    base["score"] = (100.0 * (
        params.peso_cobertura * base["cobertura_pct"]
        + params.peso_demanda * base["demanda_norm"]
        + params.peso_rotacion * base["rot_frac"]
    ) / wsum).round(1)

    def _prio(s: float) -> str:
        if s >= params.corte_prioridad_alta:
            return "Alta"
        if s >= params.corte_prioridad_media:
            return "Media"
        return "Baja"

    base["prioridad"] = base["score"].apply(_prio)

    # Columnas para mostrar: si no hay demanda, se reporta sin información.
    base["demanda_consolidada"] = base["_demanda_sum"] if has_demanda else np.nan
    if not has_demanda:
        base["n_suc_demanda"] = np.nan
    base["venta_historica"] = SIN_INFO

    def _motivo(row) -> str:
        partes: list[str] = []
        cob = f"{row['cobertura_pct']:.0%}"
        if has_demanda:
            partes.append(
                f"Demanda pronosticada en {int(row['n_suc_demanda'])} de "
                f"{n_suc_total} sucursales ({cob})")
        else:
            partes.append(
                f"Presente en {int(row['n_suc_presente'])} de "
                f"{n_suc_total} sucursales ({cob})")
        if row["n_suc_rotacion"] >= 1:
            partes.append(
                f"Alta rotación (X) en {int(row['n_suc_rotacion'])} sucursal(es)")
        return "; ".join(partes)

    base["motivo"] = base.apply(_motivo, axis=1)

    out = (base.sort_values("score", ascending=False)
           .head(int(params.top_n))
           .reset_index(drop=True))
    return out[OUTPUT_COLS]
