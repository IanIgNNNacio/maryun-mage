"""Panel gerencial de abastecimiento — lógica pura (SOLO LECTURA).

Capa de visualización gerencial, **independiente y aditiva**. NO modifica ni
recalcula el motor de abastecimiento, ni facturación, clasificación o pronósticos:
solo LEE datasets ya materializados (en Supabase) y los agrega para mostrar KPIs.

Fuentes (todas existentes, solo lectura):
  * ``plan_actual``        — snapshot analítico del último run (pronóstico, stock,
                             clasificación, costo, proveedor por sucursal).
  * ``v_ventas_producto``  — venta histórica consolidada por producto (sincronizada
                             desde MariaDB por gerencial_sync; unidades).
  * ``productos_dim``      — catálogo: nombre + VARIANTE (+ color/talla/critico).

Reutiliza ``consolidado.build_consolidado`` (sin modificarlo) como base de candidatos
y la enriquece. Regla de datos faltantes: si un dato no existe se marca
``"no disponible por falta de datos"`` o se excluye — nunca se inventa.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path

import numpy as np
import pandas as pd

from app.dashboard import consolidado as cons

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None  # type: ignore

NO_DISPONIBLE = "no disponible por falta de datos"
SIN_INFO = cons.SIN_INFO
_WEEKS_PER_MONTH = 365.0 / 7.0 / 12.0  # ≈ 4.345


# ---------------------------------------------------------------------------
# Parámetros configurables (umbrales). Defaults documentados.
# ---------------------------------------------------------------------------
@dataclass
class GerencialParams:
    top_n: int = 50                      # filas en rankings/heatmap
    infaltable_cobertura_min: float = 0.80   # cobertura para "infaltable" (+ rotación)
    hhi_concentrado: float = 0.50        # HHI ≥ → demanda "Concentrada"
    semanas_cobertura_critica: float = 2.0   # < → riesgo de quiebre por cobertura
    variacion_alta_pct: float = 0.20     # |variación| ≥ → "Demanda creciente/decreciente"
    inmovilizado_dias: int = 90          # días sin venta para "inmovilizado"
    sobrestock_factor: float = 1.30      # stock > factor×demanda_mes → sobrestock
    mape_meses_min: int = 2              # mín. meses cerrados para mostrar MAPE de un sku


def _default_params_path() -> Path:
    return Path(__file__).resolve().parents[1] / "config" / "gerencial_params.yaml"


def load_gerencial_params(path: Path | None = None) -> GerencialParams:
    target = path or _default_params_path()
    try:
        if yaml is None or not target.exists():
            return GerencialParams()
        with target.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except Exception:
        return GerencialParams()
    if not isinstance(raw, dict):
        return GerencialParams()
    valid = {f.name for f in fields(GerencialParams)}
    clean = {k: v for k, v in raw.items() if k in valid}
    try:
        return GerencialParams(**clean)
    except Exception:
        return GerencialParams()


# ---------------------------------------------------------------------------
# Resultado estructurado
# ---------------------------------------------------------------------------
@dataclass
class GerencialData:
    tabla: pd.DataFrame                  # candidatos enriquecidos (con variante)
    kpis: dict                           # tarjetas + flags de disponibilidad
    ranking_categorias: pd.DataFrame
    riesgo_quiebre: pd.DataFrame
    potencial_proveedor: pd.DataFrame


def _skuc(s) -> str:
    """sku_id -> sku_correcto (zfill 12), misma convención que plan_actual."""
    return str(s).strip().zfill(12)


def _lower(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.columns = [str(c).strip().lower() for c in out.columns]
    return out


# ---------------------------------------------------------------------------
# Auxiliares por producto desde plan_actual (concentración, semanas, costo...)
# ---------------------------------------------------------------------------
def _aux_por_producto(plan_actual: pd.DataFrame, cd_labels) -> pd.DataFrame:
    df = _lower(plan_actual)
    if "sku_correcto" not in df.columns or "sucursal" not in df.columns:
        return pd.DataFrame()
    cd = {str(s).upper() for s in cd_labels}
    df = df[~df["sucursal"].astype(str).str.upper().isin(cd)]
    if df.empty:
        return pd.DataFrame()

    for c in ("stock", "demanda_mes_actual", "descubierto", "costo_unitario_clp"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    key = "sku_correcto"
    g = df.groupby(key)
    aux = pd.DataFrame(index=g.size().index)
    aux["stock_total"] = g["stock"].sum() if "stock" in df.columns else np.nan
    aux["demanda_total"] = g["demanda_mes_actual"].sum() if "demanda_mes_actual" in df.columns else np.nan
    aux["descubierto_total"] = g["descubierto"].sum() if "descubierto" in df.columns else 0.0
    if "costo_unitario_clp" in df.columns:
        aux["costo_unitario_clp"] = g["costo_unitario_clp"].median()
    else:
        aux["costo_unitario_clp"] = np.nan
    for c in ("familia", "tipo", "marca", "proveedor"):
        aux[c] = g[c].first() if c in df.columns else None

    # HHI de concentración de la demanda entre sucursales (1 = concentrado).
    if "demanda_mes_actual" in df.columns:
        tot = g["demanda_mes_actual"].transform("sum")
        share = np.where(tot > 0, df["demanda_mes_actual"] / tot, 0.0)
        hhi = df.assign(_s2=share ** 2).groupby(key)["_s2"].sum()
        aux["hhi"] = hhi
        # sin demanda → HHI no informativo
        aux.loc[aux["demanda_total"].fillna(0) <= 0, "hhi"] = np.nan
    else:
        aux["hhi"] = np.nan

    # Semanas de cobertura nacional = stock*semanas_mes / demanda_mensual.
    dem = aux["demanda_total"].replace(0, np.nan)
    aux["semanas_cobertura"] = (aux["stock_total"] * _WEEKS_PER_MONTH / dem)
    return aux.reset_index()


# ---------------------------------------------------------------------------
# Etiquetas (reglas claras, basadas en datos; nunca manuales)
# ---------------------------------------------------------------------------
def _etiquetas(row, params: GerencialParams) -> str:
    tags: list[str] = []
    cob = row.get("cobertura_pct")
    if pd.notna(cob):
        if cob >= params.infaltable_cobertura_min:
            tags.append("Alta cobertura")
        if cob >= 0.6:
            tags.append("Producto transversal")
    if bool(row.get("alta_rotacion")):
        tags.append("Alta rotación")
    if bool(row.get("infaltable")):
        tags.append("Infaltable")
    if bool(row.get("riesgo_quiebre")):
        tags.append("Riesgo de quiebre")
    var = row.get("variacion_pct")
    if pd.notna(var) and abs(var) >= params.variacion_alta_pct:
        tags.append("Demanda creciente" if var > 0 else "Demanda decreciente")
    tags.append("Compra nacional sugerida")
    tags.append("Revisar semanalmente")
    return " · ".join(tags)


# ---------------------------------------------------------------------------
# Orquestador principal
# ---------------------------------------------------------------------------
def build_gerencial(
    plan_actual: pd.DataFrame,
    *,
    ventas_producto: pd.DataFrame | None = None,
    productos: pd.DataFrame | None = None,
    margen: pd.DataFrame | None = None,
    cons_params: cons.ConsolidadoParams | None = None,
    ger_params: GerencialParams | None = None,
    group_by: str = "sku",
    demanda_min: float = 0.0,
) -> GerencialData:
    cons_params = cons_params or cons.ConsolidadoParams()
    ger_params = ger_params or GerencialParams()

    vacio = GerencialData(cons._empty_output(), {}, pd.DataFrame(),
                          pd.DataFrame(), pd.DataFrame())
    if plan_actual is None or len(plan_actual) == 0:
        return vacio

    cand = cons.build_consolidado(plan_actual, cons_params,
                                  group_by=group_by, demanda_min=demanda_min)

    aux = _aux_por_producto(plan_actual, cons_params.cd_labels)

    # ----- riesgo de quiebre (sobre TODOS los productos, no solo candidatos) -----
    riesgo = pd.DataFrame()
    if not aux.empty:
        r = aux[pd.to_numeric(aux["descubierto_total"], errors="coerce").fillna(0) > 0].copy()
        if not r.empty:
            riesgo = r[["sku_correcto", "demanda_total", "stock_total",
                        "descubierto_total", "semanas_cobertura"]].sort_values(
                "descubierto_total", ascending=False).reset_index(drop=True)

    if cand.empty:
        return GerencialData(cand, {"n_candidatos": 0}, pd.DataFrame(),
                             riesgo, pd.DataFrame())

    tabla = cand.copy()  # 'sku' == sku_correcto

    # ----- merge auxiliares por producto -----
    if not aux.empty:
        tabla = tabla.merge(aux, left_on="sku", right_on="sku_correcto", how="left")
        if "sku_correcto" in tabla.columns:
            tabla = tabla.drop(columns=["sku_correcto"])
    for c in ("hhi", "semanas_cobertura", "descubierto_total", "costo_unitario_clp",
              "familia", "tipo", "marca", "proveedor", "stock_total"):
        if c not in tabla.columns:
            tabla[c] = np.nan

    # ----- merge VARIANTE (productos_dim) -----
    if productos is not None and not productos.empty and "sku_id" in productos.columns:
        p = productos.copy()
        p["_skuc"] = p["sku_id"].map(_skuc)
        keep = [c for c in ("variante", "color", "talla", "critico", "familia",
                            "marca", "tipo") if c in p.columns]
        p = p[["_skuc"] + keep].drop_duplicates(subset=["_skuc"])
        # familia/tipo/marca de productos_dim solo si faltan en aux
        tabla = tabla.merge(p, left_on="sku", right_on="_skuc", how="left",
                            suffixes=("", "_prod"))
        for c in ("familia", "tipo", "marca"):
            pc = f"{c}_prod"
            if pc in tabla.columns:
                tabla[c] = tabla[c].fillna(tabla[pc])
                tabla = tabla.drop(columns=[pc])
        if "_skuc" in tabla.columns:
            tabla = tabla.drop(columns=["_skuc"])
    for c in ("variante", "color", "talla"):
        if c not in tabla.columns:
            tabla[c] = SIN_INFO
        tabla[c] = tabla[c].fillna(SIN_INFO)
    if "critico" not in tabla.columns:
        tabla["critico"] = False
    tabla["critico"] = tabla["critico"].fillna(False).astype(bool)

    # ----- merge VENTA HISTÓRICA (v_ventas_producto) -----
    venta_disponible = ventas_producto is not None and not ventas_producto.empty \
        and "sku_id" in ventas_producto.columns
    if venta_disponible:
        v = ventas_producto.copy()
        v["_skuc"] = v["sku_id"].map(_skuc)
        vcols = {"total_unidades": "venta_hist_unidades",
                 "unidades_12m": "venta_hist_12m"}
        have = [c for c in vcols if c in v.columns]
        v = v[["_skuc"] + have].rename(columns=vcols).drop_duplicates(subset=["_skuc"])
        tabla = tabla.merge(v, left_on="sku", right_on="_skuc", how="left")
        if "_skuc" in tabla.columns:
            tabla = tabla.drop(columns=["_skuc"])
        if "venta_hist_unidades" in tabla.columns:
            tabla["venta_hist_unidades"] = pd.to_numeric(
                tabla["venta_hist_unidades"], errors="coerce")
    else:
        tabla["venta_hist_unidades"] = np.nan
        tabla["venta_hist_12m"] = np.nan

    # ----- merge MARGEN (margen_producto) -----
    if margen is not None and not margen.empty and "sku_id" in margen.columns:
        m = margen.copy()
        m["_skuc"] = m["sku_id"].map(_skuc)
        have = [c for c in ("margen_clp", "margen_pct", "venta_neto_clp")
                if c in m.columns]
        m = m[["_skuc"] + have].drop_duplicates(subset=["_skuc"])
        tabla = tabla.merge(m, left_on="sku", right_on="_skuc", how="left")
        if "_skuc" in tabla.columns:
            tabla = tabla.drop(columns=["_skuc"])
    for c in ("margen_clp", "margen_pct", "venta_neto_clp"):
        if c not in tabla.columns:
            tabla[c] = np.nan

    # ----- KPIs derivados por fila -----
    dem = pd.to_numeric(tabla["demanda_consolidada"], errors="coerce")
    costo = pd.to_numeric(tabla["costo_unitario_clp"], errors="coerce")
    tabla["potencial_clp"] = (dem * costo).round(0)
    tabla["concentracion"] = np.where(
        tabla["hhi"].isna(), SIN_INFO,
        np.where(tabla["hhi"] >= ger_params.hhi_concentrado, "Concentrado", "Distribuido"))
    tabla["infaltable"] = tabla["critico"] | (
        (pd.to_numeric(tabla["cobertura_pct"], errors="coerce") >= ger_params.infaltable_cobertura_min)
        & tabla["alta_rotacion"].fillna(False))
    tabla["riesgo_quiebre"] = pd.to_numeric(
        tabla["descubierto_total"], errors="coerce").fillna(0) > 0
    # variación proyectada: pronóstico mes vs promedio histórico mensual (12m/12)
    if venta_disponible and "venta_hist_12m" in tabla.columns:
        prom_mes = pd.to_numeric(tabla["venta_hist_12m"], errors="coerce") / 12.0
        tabla["variacion_pct"] = np.where(
            prom_mes > 0, (dem - prom_mes) / prom_mes, np.nan)
    else:
        tabla["variacion_pct"] = np.nan

    tabla["etiquetas"] = tabla.apply(lambda r: _etiquetas(r, ger_params), axis=1)

    # venta histórica para mostrar: número si hay dato, si no "no disponible"
    if not venta_disponible:
        tabla["venta_historica"] = NO_DISPONIBLE

    # ----- KPIs resumen -----
    fam_top = None
    if "familia" in tabla.columns and tabla["familia"].notna().any():
        vc = tabla["familia"].dropna().value_counts()
        fam_top = vc.index[0] if len(vc) else None
    kpis = {
        "n_candidatos": int(len(tabla)),
        "demanda_consolidada_total": float(dem.fillna(0).sum()),
        "cobertura_promedio": float(pd.to_numeric(tabla["cobertura_pct"], errors="coerce").mean()),
        "n_infaltables": int(tabla["infaltable"].sum()),
        "n_riesgo_quiebre": int(len(riesgo)),
        "categoria_top": fam_top,
        "venta_historica_total": (float(pd.to_numeric(tabla["venta_hist_unidades"], errors="coerce").fillna(0).sum())
                                  if venta_disponible else None),
        "venta_historica_disponible": bool(venta_disponible),
        "potencial_clp_total": float(pd.to_numeric(tabla["potencial_clp"], errors="coerce").fillna(0).sum()),
    }

    # ----- ranking de categorías -----
    ranking_cat = pd.DataFrame()
    if "familia" in tabla.columns and tabla["familia"].notna().any():
        ranking_cat = (tabla.assign(_dem=dem.fillna(0),
                                    _pot=pd.to_numeric(tabla["potencial_clp"], errors="coerce").fillna(0))
                       .groupby(tabla["familia"].fillna("(sin categoría)"))
                       .agg(productos=("sku", "count"),
                            demanda=("_dem", "sum"),
                            potencial_clp=("_pot", "sum"))
                       .reset_index().rename(columns={"familia": "categoria"})
                       .sort_values("productos", ascending=False).reset_index(drop=True))

    # ----- potencial por proveedor -----
    pot_prov = pd.DataFrame()
    if "proveedor" in tabla.columns and tabla["proveedor"].notna().any():
        pot_prov = (tabla.assign(_pot=pd.to_numeric(tabla["potencial_clp"], errors="coerce").fillna(0),
                                _dem=dem.fillna(0))
                    .groupby(tabla["proveedor"].fillna("(sin proveedor)"))
                    .agg(productos=("sku", "count"),
                         demanda=("_dem", "sum"),
                         potencial_clp=("_pot", "sum"))
                    .reset_index().rename(columns={"proveedor": "proveedor"})
                    .sort_values("potencial_clp", ascending=False).reset_index(drop=True))

    # enriquecer riesgo con nombre/variante
    if not riesgo.empty:
        info = tabla[["sku", "nombre", "variante"]].drop_duplicates(subset=["sku"])
        riesgo = riesgo.merge(info, left_on="sku_correcto", right_on="sku", how="left")
        riesgo = riesgo.drop(columns=[c for c in ("sku",) if c in riesgo.columns])

    return GerencialData(tabla=tabla, kpis=kpis, ranking_categorias=ranking_cat,
                         riesgo_quiebre=riesgo, potencial_proveedor=pot_prov)


# ---------------------------------------------------------------------------
# Helpers para visualizaciones puntuales (los llama la UI)
# ---------------------------------------------------------------------------
def heatmap_pivot(plan_actual: pd.DataFrame, skus_correctos, cd_labels) -> pd.DataFrame:
    """Pivote producto×sucursal de la demanda pronosticada (para heatmap)."""
    df = _lower(plan_actual)
    if not {"sku_correcto", "sucursal", "demanda_mes_actual"}.issubset(df.columns):
        return pd.DataFrame()
    cd = {str(s).upper() for s in cd_labels}
    df = df[~df["sucursal"].astype(str).str.upper().isin(cd)]
    df = df[df["sku_correcto"].isin(list(skus_correctos))]
    if df.empty:
        return pd.DataFrame()
    df["demanda_mes_actual"] = pd.to_numeric(df["demanda_mes_actual"], errors="coerce").fillna(0)
    piv = df.pivot_table(index="sku_correcto", columns="sucursal",
                         values="demanda_mes_actual", aggfunc="sum", fill_value=0)
    return piv


def tendencia_serie(ventas_mensual: pd.DataFrame, demanda_pronostico: float | None = None) -> pd.DataFrame:
    """Serie tidy [mes, tipo, unidades] para el gráfico de tendencia.

    ``ventas_mensual`` = filas (mes, unidades) de v_ventas_mensual para un sku.
    Si falta venta histórica, devuelve DataFrame vacío (la UI muestra 'no disponible').
    """
    if ventas_mensual is None or ventas_mensual.empty or "mes" not in ventas_mensual.columns:
        return pd.DataFrame(columns=["mes", "tipo", "unidades"])
    s = ventas_mensual.copy()
    s["mes"] = pd.to_datetime(s["mes"], errors="coerce")
    s["unidades"] = pd.to_numeric(s["unidades"], errors="coerce")
    s = s.dropna(subset=["mes"]).sort_values("mes")
    s["tipo"] = "Venta histórica"
    out = s[["mes", "tipo", "unidades"]]
    if demanda_pronostico is not None and pd.notna(demanda_pronostico) and not s.empty:
        prox = (s["mes"].max() + pd.offsets.MonthBegin(1))
        out = pd.concat([out, pd.DataFrame(
            {"mes": [prox], "tipo": ["Pronóstico"], "unidades": [float(demanda_pronostico)]})],
            ignore_index=True)
    return out


def detalle_producto(tabla: pd.DataFrame, plan_actual: pd.DataFrame,
                     sku_correcto: str, cd_labels) -> dict:
    """Resumen nacional de un producto + su detalle por sucursal."""
    fila = tabla[tabla["sku"] == sku_correcto]
    resumen = fila.iloc[0].to_dict() if not fila.empty else {}
    df = _lower(plan_actual)
    por_sucursal = pd.DataFrame()
    if {"sku_correcto", "sucursal"}.issubset(df.columns):
        cd = {str(s).upper() for s in cd_labels}
        d = df[(df["sku_correcto"] == sku_correcto)
               & (~df["sucursal"].astype(str).str.upper().isin(cd))]
        cols = [c for c in ("sucursal", "stock", "demanda_mes_actual", "cant_req",
                            "cobertura_meses", "descubierto") if c in d.columns]
        if cols:
            por_sucursal = d[cols].sort_values(
                "demanda_mes_actual" if "demanda_mes_actual" in cols else cols[0],
                ascending=False).reset_index(drop=True)
    return {"resumen": resumen, "por_sucursal": por_sucursal}


def servicio_por_familia(plan_actual: pd.DataFrame, cd_labels) -> pd.DataFrame:
    """Nivel de servicio proyectado y quiebre en CLP por familia (desde plan_actual).

    fill_rate = 1 − Σdescubierto/Σdemanda ; quiebre_clp = Σ descubierto×costo.
    Solo usa datos existentes; sin inventar.
    """
    df = _lower(plan_actual)
    if not {"familia", "demanda_mes_actual", "descubierto"}.issubset(df.columns):
        return pd.DataFrame()
    cd = {str(s).upper() for s in cd_labels}
    if "sucursal" in df.columns:
        df = df[~df["sucursal"].astype(str).str.upper().isin(cd)]
    for c in ("demanda_mes_actual", "descubierto", "costo_unitario_clp"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    costo = df["costo_unitario_clp"] if "costo_unitario_clp" in df.columns else 0.0
    df = df.assign(_quiebre_clp=df["descubierto"] * costo)
    g = df.groupby("familia", as_index=False).agg(
        demanda=("demanda_mes_actual", "sum"),
        descubierto=("descubierto", "sum"),
        quiebre_clp=("_quiebre_clp", "sum"))
    g["fill_rate"] = np.where(g["demanda"] > 0, 1 - g["descubierto"] / g["demanda"], np.nan)
    return g.sort_values("quiebre_clp", ascending=False).reset_index(drop=True)
