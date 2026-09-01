"""Dashboard INTERACTIVO en la nube — Streamlit Cloud + Supabase.

Lee el PLAN VIGENTE (último run) desde Supabase y permite ACTUAR sobre él:
marcar ejecutado/rechazado, ajustar cantidad, agregar nota. Los cambios se
escriben de vuelta a Supabase y alimentan la sincronización con Maryun
(vista ``v_para_sincronizar`` = filas 'ejecutado' del último run).

Config (Streamlit Cloud → Settings → Secrets), formato TOML:
    SUPABASE_URL = "https://xxxx.supabase.co"
    SUPABASE_SERVICE_KEY = "ey..."
    DASHBOARD_PASSWORD_HASH = "$2b$12$..."   # bcrypt (generar con auth.py)

En local usa el .env + auth_config.yaml de siempre.
"""
from __future__ import annotations

import os
import sys

# El paquete `app` vive en src/. En Streamlit Cloud el script se ejecuta sin
# `pip install -e .`, asi que agregamos src/ al sys.path para poder importar `app`.
_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import pandas as pd
import streamlit as st

st.set_page_config(page_title="Reposición Maryun", page_icon="📦", layout="wide")


# ---------------------------------------------------------------------------
# 1. Bootstrap de credenciales: st.secrets (nube) → variables de entorno
# ---------------------------------------------------------------------------
def _bootstrap_secrets() -> None:
    try:
        sec = st.secrets
    except Exception:
        return
    mapping = {
        "SUPABASE_URL": "MARYUN_SUPABASE__URL",
        "SUPABASE_SERVICE_KEY": "MARYUN_SUPABASE__SERVICE_KEY",
        "SUPABASE_ANON_KEY": "MARYUN_SUPABASE__ANON_KEY",
    }
    for k, env in mapping.items():
        if k in sec and sec[k]:
            os.environ[env] = str(sec[k])
    if os.environ.get("MARYUN_SUPABASE__URL"):
        os.environ["MARYUN_SUPABASE__ENABLED"] = "true"


_bootstrap_secrets()

from app.config.settings import reset_settings_cache  # noqa: E402
reset_settings_cache()

from app.export import supabase_sync as sb  # noqa: E402
from app.dashboard import consolidado as cons  # noqa: E402
from app.dashboard import gerencial as ger  # noqa: E402


# ---------------------------------------------------------------------------
# 2. Autenticación (hash desde st.secrets o auth_config.yaml local)
# ---------------------------------------------------------------------------
def _password_hash() -> bytes | None:
    try:
        if "DASHBOARD_PASSWORD_HASH" in st.secrets and st.secrets["DASHBOARD_PASSWORD_HASH"]:
            return str(st.secrets["DASHBOARD_PASSWORD_HASH"]).encode("utf-8")
    except Exception:
        pass
    from pathlib import Path
    import yaml
    p = Path(__file__).parent / "auth_config.yaml"
    if p.exists():
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        h = data.get("password_hash")
        return h.encode("utf-8") if h else None
    return None


def _require_password() -> str:
    import bcrypt
    h = _password_hash()
    if h is None:
        st.error("No hay contraseña configurada (DASHBOARD_PASSWORD_HASH en Secrets).")
        st.stop()
    if st.session_state.get("_authed"):
        return st.session_state.get("_user", "usuario")
    st.title("🔒 Reposición Maryun")
    pwd = st.text_input("Contraseña", type="password")
    user = st.text_input("Tu nombre (para trazabilidad)", value="", placeholder="ej. Vicente")
    if st.button("Ingresar", type="primary"):
        if pwd and bcrypt.checkpw(pwd.encode("utf-8"), h):
            st.session_state["_authed"] = True
            st.session_state["_user"] = user.strip() or "usuario"
            st.rerun()
        else:
            st.error("Contraseña incorrecta.")
    st.stop()


USUARIO = _require_password()


# ---------------------------------------------------------------------------
# 3. Carga de datos (cache 5 min; botón refresca)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=300, show_spinner="Cargando plan vigente…")
def _plan() -> pd.DataFrame:
    return sb.fetch_plan_vigente()


@st.cache_data(ttl=300, show_spinner=False)
def _ahorro() -> pd.DataFrame:
    return sb.fetch_historico_ahorro()


@st.cache_data(ttl=300, show_spinner="Cargando vista analítica…")
def _plan_actual() -> pd.DataFrame:
    # Reutiliza fetch_plan_actual() (ya existe; hoy sin uso en el dashboard).
    # Solo lectura: es el snapshot analítico del último run en Supabase.
    return sb.fetch_plan_actual()


@st.cache_data(ttl=300, show_spinner="Calculando compra consolidada…")
def _consolidado(cobertura_min: float, rot_min: int, w_cob: float, w_dem: float,
                 w_rot: float, top_n: int, group_by: str, demanda_min: float) -> pd.DataFrame:
    # Cacheado por sus parámetros primitivos: no recalcula al interactuar con
    # otras pestañas. Lógica pura en app.dashboard.consolidado (sin efectos).
    base = cons.load_consolidado_params()
    p = cons.ConsolidadoParams(
        cobertura_min_pct=cobertura_min, rotacion_suc_min=int(rot_min),
        peso_cobertura=w_cob, peso_demanda=w_dem, peso_rotacion=w_rot,
        corte_prioridad_alta=base.corte_prioridad_alta,
        corte_prioridad_media=base.corte_prioridad_media,
        cd_labels=base.cd_labels, top_n=int(top_n),
    )
    return cons.build_consolidado(_plan_actual(), p, group_by=group_by,
                                  demanda_min=float(demanda_min))


@st.cache_data(ttl=300, show_spinner=False)
def _ventas_producto() -> pd.DataFrame:
    return sb.fetch_ventas_producto()


@st.cache_data(ttl=300, show_spinner=False)
def _productos_dim() -> pd.DataFrame:
    return sb.fetch_productos_dim()


@st.cache_data(ttl=300, show_spinner=False)
def _margen_producto() -> pd.DataFrame:
    return sb.fetch_margen_producto()


@st.cache_data(ttl=300, show_spinner=False)
def _inmovilizado_resumen() -> pd.DataFrame:
    return sb.fetch_inmovilizado_resumen()


@st.cache_data(ttl=300, show_spinner=False)
def _mape_categoria() -> pd.DataFrame:
    return sb.fetch_mape_categoria()


@st.cache_data(ttl=300, show_spinner=False)
def _mape_producto() -> pd.DataFrame:
    return sb.fetch_mape_producto()


@st.cache_data(ttl=300, show_spinner=False)
def _margen_categoria() -> pd.DataFrame:
    return sb.fetch_margen_categoria()


@st.cache_data(ttl=300, show_spinner=False)
def _ventas_mensual(sku_correcto: str) -> pd.DataFrame:
    # v_ventas_mensual usa sku_id (sin ceros a la izquierda); sku_correcto = zfill(12).
    return sb.fetch_ventas_mensual(str(sku_correcto).lstrip("0") or "0")


@st.cache_data(ttl=300, show_spinner="Calculando panel gerencial…")
def _gerencial(cobertura_min: float, rot_min: int, w_cob: float, w_dem: float,
               w_rot: float, top_n: int, group_by: str, demanda_min: float):
    cbase = cons.load_consolidado_params()
    cp = cons.ConsolidadoParams(
        cobertura_min_pct=cobertura_min, rotacion_suc_min=int(rot_min),
        peso_cobertura=w_cob, peso_demanda=w_dem, peso_rotacion=w_rot,
        corte_prioridad_alta=cbase.corte_prioridad_alta,
        corte_prioridad_media=cbase.corte_prioridad_media,
        cd_labels=cbase.cd_labels, top_n=int(top_n))
    return ger.build_gerencial(
        _plan_actual(), ventas_producto=_ventas_producto(), productos=_productos_dim(),
        margen=_margen_producto(), cons_params=cp, ger_params=ger.load_gerencial_params(),
        group_by=group_by, demanda_min=float(demanda_min))


if not sb.is_configured():
    st.error("Supabase no está configurado. Define SUPABASE_URL y SUPABASE_SERVICE_KEY en Secrets.")
    st.stop()

st.sidebar.title("📦 Reposición Maryun")
st.sidebar.caption(f"Usuario: **{USUARIO}**")
if st.sidebar.button("🔄 Refrescar datos"):
    st.cache_data.clear()
    st.rerun()

plan = _plan()
if plan.empty:
    st.warning("No hay plan vigente en Supabase todavía. Corre el pipeline para publicar uno.")
    st.stop()

run_id = plan["run_id"].iloc[0]
fecha = plan["fecha_generacion"].iloc[0] if "fecha_generacion" in plan.columns else ""
st.sidebar.success(f"Plan vigente:\n\n`{run_id}`\n\n{fecha}")

CAPA_LABEL = {"inmovilizado": "🟣 Inmovilizado", "sobrestock": "🟠 Sobrestock",
              "cd": "🔵 CD", "compra": "🟢 Compra"}


# ---------------------------------------------------------------------------
# 4. KPIs
# ---------------------------------------------------------------------------
st.title("Plan de Reposición — Vigente")
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Acciones", f"{len(plan):,}")
c2.metric("Pendientes", f"{(plan['estado'] == 'pendiente').sum():,}")
c3.metric("Ejecutadas", f"{(plan['estado'] == 'ejecutado').sum():,}")
c4.metric("Rechazadas", f"{(plan['estado'] == 'rechazado').sum():,}")
uds = pd.to_numeric(plan["cantidad_sugerida"], errors="coerce").fillna(0).sum()
c5.metric("Unidades sugeridas", f"{uds:,.0f}")


# ---------------------------------------------------------------------------
# 5. Filtros
# ---------------------------------------------------------------------------
with st.sidebar:
    st.subheader("Filtros")
    f_capa = st.multiselect("Capa", sorted(plan["capa"].dropna().unique()))
    f_suc = st.multiselect("Sucursal destino", sorted(plan["destino"].dropna().unique()))
    f_estado = st.multiselect("Estado", ["pendiente", "ejecutado", "rechazado"],
                              default=["pendiente"])
    f_busq = st.text_input("Buscar (SKU / nombre)")

view = plan.copy()
if f_capa:
    view = view[view["capa"].isin(f_capa)]
if f_suc:
    view = view[view["destino"].isin(f_suc)]
if f_estado:
    view = view[view["estado"].isin(f_estado)]
if f_busq:
    m = (view["sku_id"].astype(str).str.contains(f_busq, case=False, na=False) |
         view["nombre"].astype(str).str.contains(f_busq, case=False, na=False))
    view = view[m]


# ---------------------------------------------------------------------------
# 6. Editor interactivo + guardar cambios
# ---------------------------------------------------------------------------
tab_plan, tab_sync, tab_kpi, tab_nac, tab_ger, tab_ind = st.tabs(
    ["📝 Plan (editar)", "✅ Para sincronizar", "📈 Ahorro",
     "🛒 Compra consolidada", "📊 Panel gerencial", "💹 Indicadores"])

with tab_plan:
    MAX_EDITOR_ROWS = 400
    total_filtrado = len(view)
    cols_show = ["id", "capa", "sku_id", "nombre", "variante", "origen", "destino",
                 "cantidad_sugerida", "cantidad_ajustada", "proveedor",
                 "costo_unitario_clp", "clase_abc_xyz", "estado", "nota"]
    cols_show = [c for c in cols_show if c in view.columns]
    if total_filtrado > MAX_EDITOR_ROWS:
        st.warning(
            f"Hay **{total_filtrado:,}** filas en este filtro. El editor muestra solo las "
            f"primeras **{MAX_EDITOR_ROWS}** para cargar rápido — usa los **filtros** "
            f"(sucursal, capa, búsqueda) para acotar. Los **botones de acción en lote** de "
            f"abajo aplican a TODO lo filtrado ({total_filtrado:,} filas), no solo a lo visible."
        )
        edit_df = view.head(MAX_EDITOR_ROWS)[cols_show].reset_index(drop=True)
    else:
        st.caption(f"{total_filtrado:,} filas — edita **estado**, **cantidad ajustada** y **nota**, luego *Guardar cambios*.")
        edit_df = view[cols_show].reset_index(drop=True)

    edited = st.data_editor(
        edit_df,
        use_container_width=True, hide_index=True, height=520, key="editor",
        column_config={
            "id": st.column_config.NumberColumn("id", disabled=True),
            "estado": st.column_config.SelectboxColumn(
                "estado", options=["pendiente", "ejecutado", "rechazado"], required=True),
            "cantidad_ajustada": st.column_config.NumberColumn(
                "cant. ajustada", min_value=0, step=1,
                help="Vacío = usar cantidad sugerida"),
            "nota": st.column_config.TextColumn("nota"),
            "cantidad_sugerida": st.column_config.NumberColumn("cant. sug.", disabled=True),
            "costo_unitario_clp": st.column_config.NumberColumn("costo", disabled=True),
        },
        disabled=["capa", "sku_id", "nombre", "variante", "origen", "destino",
                  "proveedor", "clase_abc_xyz"],
    )

    col_a, col_b, col_c = st.columns([1, 1, 2])
    if col_a.button("💾 Guardar cambios", type="primary"):
        orig = edit_df.set_index("id")
        new = edited.set_index("id")
        n = 0
        for pid in new.index:
            if pid not in orig.index:
                continue
            o, x = orig.loc[pid], new.loc[pid]
            changed = {}
            if str(o["estado"]) != str(x["estado"]):
                changed["estado"] = str(x["estado"])
            if not pd.isna(x.get("cantidad_ajustada")) and \
               (pd.isna(o.get("cantidad_ajustada")) or float(o["cantidad_ajustada"]) != float(x["cantidad_ajustada"])):
                changed["cantidad_ajustada"] = float(x["cantidad_ajustada"])
            if str(o.get("nota") or "") != str(x.get("nota") or ""):
                changed["nota"] = str(x.get("nota") or "")
            if changed:
                sb.update_accion(int(pid), usuario=USUARIO, **changed)
                n += 1
        st.cache_data.clear()
        st.success(f"{n} fila(s) actualizada(s).")
        st.rerun()

    # Acciones en lote sobre lo filtrado
    ids_filtrados = view["id"].tolist()
    if col_b.button("✅ Marcar TODO lo filtrado: ejecutado"):
        sb.bulk_update_estado(ids_filtrados, "ejecutado", usuario=USUARIO)
        st.cache_data.clear()
        st.success(f"{len(ids_filtrados)} marcadas ejecutadas.")
        st.rerun()
    if col_c.button("🚫 Marcar TODO lo filtrado: rechazado"):
        sb.bulk_update_estado(ids_filtrados, "rechazado", usuario=USUARIO)
        st.cache_data.clear()
        st.warning(f"{len(ids_filtrados)} marcadas rechazadas.")
        st.rerun()

with tab_sync:
    st.caption("Filas marcadas **ejecutado** — esto es lo que el sistema de Maryun sincroniza.")
    ejec = plan[plan["estado"] == "ejecutado"].copy()
    if ejec.empty:
        st.info("Aún no hay acciones marcadas como ejecutadas.")
    else:
        ejec["cantidad_final"] = pd.to_numeric(
            ejec["cantidad_ajustada"], errors="coerce"
        ).fillna(pd.to_numeric(ejec["cantidad_sugerida"], errors="coerce"))
        oc = ejec[ejec["capa"] == "compra"]
        traspasos = ejec[ejec["capa"] != "compra"]
        cc1, cc2 = st.columns(2)
        cc1.metric("Órdenes de compra", f"{len(oc):,}")
        cc2.metric("Traspasos", f"{len(traspasos):,}")
        st.dataframe(
            ejec[["capa", "sku_id", "nombre", "variante", "origen", "destino",
                  "cantidad_final", "proveedor", "nota", "actualizado_por"]],
            use_container_width=True, hide_index=True,
        )

with tab_kpi:
    ah = _ahorro()
    if ah.empty:
        st.info("Sin histórico de ahorro todavía.")
    else:
        ah = ah.sort_values("fecha_generacion")
        st.metric("Ahorro última corrida (CLP)",
                  f"${pd.to_numeric(ah['ahorro_clp'], errors='coerce').iloc[-1]:,.0f}")
        st.line_chart(ah, x="fecha_generacion", y="ahorro_clp")
        st.bar_chart(ah, x="fecha_generacion",
                     y=["unidades_inmovilizado", "unidades_sobrestock",
                        "unidades_cd", "unidades_compra"])

# ---------------------------------------------------------------------------
# 7. Compra consolidada nacional (SOLO LECTURA — aditivo)
# ---------------------------------------------------------------------------
with tab_nac:
    st.subheader("🛒 Productos candidatos para compra consolidada nacional")
    st.caption(
        "Vista de **solo lectura**. Agrega el último plan analítico por producto "
        "a nivel nacional para detectar artículos de alta rotación / infaltables "
        "presentes en gran parte de las sucursales y evaluar compra centralizada. "
        "No modifica facturación, clasificación ni pronósticos.")

    pa = _plan_actual()
    if pa is None or pa.empty:
        st.info("Sin información disponible: la vista analítica (plan_actual) no "
                "tiene datos para el último run.")
    else:
        _p = cons.load_consolidado_params()
        with st.expander("⚙️ Parámetros (ajustables)", expanded=False):
            ca, cb, cc = st.columns(3)
            cobertura_min = ca.slider(
                "Cobertura mínima (sucursales con demanda)", 0.0, 1.0,
                float(_p.cobertura_min_pct), 0.05,
                help="Fracción de sucursales con demanda para ser candidato por cobertura.")
            rot_min = cb.slider("Mín. sucursales con alta rotación (X)", 1, 10,
                                int(_p.rotacion_suc_min), 1)
            top_n = cc.slider("Máx. productos a mostrar", 10, 500, int(_p.top_n), 10)
            cw1, cw2, cw3 = st.columns(3)
            w_cob = cw1.slider("Peso cobertura", 0.0, 1.0, float(_p.peso_cobertura), 0.05)
            w_dem = cw2.slider("Peso demanda", 0.0, 1.0, float(_p.peso_demanda), 0.05)
            w_rot = cw3.slider("Peso rotación", 0.0, 1.0, float(_p.peso_rotacion), 0.05)
            cg1, cg2 = st.columns(2)
            group_lbl = cg1.radio("Agrupar por", ["SKU", "Nombre"], horizontal=True)
            demanda_min = cg2.number_input(
                "Demanda consolidada mínima (0 = sin filtro)", min_value=0.0,
                value=0.0, step=1.0)

        group_by = "nombre" if group_lbl == "Nombre" else "sku"
        tabla = _consolidado(cobertura_min, int(rot_min), w_cob, w_dem, w_rot,
                             int(top_n), group_by, float(demanda_min))

        if tabla.empty:
            st.warning("Ningún producto cumple los criterios actuales. "
                       "Prueba bajando la cobertura mínima o el mínimo de rotación.")
        else:
            dem_series = pd.to_numeric(tabla["demanda_consolidada"], errors="coerce")
            k1, k2, k3 = st.columns(3)
            k1.metric("Productos candidatos", f"{len(tabla):,}")
            k2.metric("Demanda consolidada (candidatos)",
                      f"{dem_series.sum():,.0f}" if dem_series.notna().any()
                      else "sin información")
            k3.metric("Con alta rotación", f"{int(tabla['alta_rotacion'].sum()):,}")

            # Enriquecer con la VARIANTE del producto (productos_dim), por
            # sku_correcto = sku_id.zfill(12). Degrada a "sin información" si la
            # tabla aún no existe; try/except evita romper la pestaña.
            tabla = tabla.copy()
            try:
                _prod = _productos_dim()
                if not _prod.empty and {"sku_id", "variante"}.issubset(_prod.columns):
                    _pv = _prod[["sku_id", "variante"]].copy()
                    _pv["_skuc"] = _pv["sku_id"].astype(str).str.zfill(12)
                    _pv = _pv.drop_duplicates(subset=["_skuc"])
                    tabla = tabla.merge(_pv[["_skuc", "variante"]], left_on="sku",
                                        right_on="_skuc", how="left").drop(columns=["_skuc"])
            except Exception:
                pass
            if "variante" not in tabla.columns:
                tabla["variante"] = "sin información disponible"
            tabla["variante"] = tabla["variante"].fillna("sin información disponible")

            # Enriquecer con Margen % (margen_producto), mismo join por sku_correcto.
            try:
                _mg = _margen_producto()
                if not _mg.empty and {"sku_id", "margen_pct"}.issubset(_mg.columns):
                    _mv = _mg[["sku_id", "margen_pct"]].copy()
                    _mv["_skuc"] = _mv["sku_id"].astype(str).str.zfill(12)
                    _mv = _mv.drop_duplicates(subset=["_skuc"])
                    tabla = tabla.merge(_mv[["_skuc", "margen_pct"]], left_on="sku",
                                        right_on="_skuc", how="left").drop(columns=["_skuc"])
            except Exception:
                pass
            if "margen_pct" not in tabla.columns:
                tabla["margen_pct"] = pd.NA

            show = tabla.copy()
            show["cobertura_pct"] = (pd.to_numeric(show["cobertura_pct"],
                                                   errors="coerce") * 100).round(0)
            show["margen_pct"] = (pd.to_numeric(show["margen_pct"],
                                                errors="coerce") * 100).round(1)
            show = show.rename(columns={
                "sku": "SKU", "nombre": "Producto", "variante": "Variante",
                "margen_pct": "Margen %", "clase": "Clasificación",
                "n_suc_demanda": "Suc. con demanda", "n_suc_presente": "Suc. presente",
                "n_suc_total": "Suc. totales", "cobertura_pct": "Cobertura",
                "demanda_consolidada": "Pronóstico consolidado",
                "cant_req_consolidada": "Req. consolidado",
                "venta_historica": "Venta histórica", "alta_rotacion": "Alta rotación",
                "n_suc_rotacion": "Suc. alta rotación", "score": "Score",
                "prioridad": "Prioridad", "motivo": "Motivo"})

            _cols = [c for c in show.columns if c not in ("Variante", "Margen %")]
            if "Producto" in _cols:
                _i = _cols.index("Producto") + 1
                for _c in ("Variante", "Margen %"):
                    if _c in show.columns:
                        _cols.insert(_i, _c)
                        _i += 1
                show = show[_cols]

            st.dataframe(
                show, use_container_width=True, hide_index=True, height=560,
                column_config={
                    "Cobertura": st.column_config.ProgressColumn(
                        "Cobertura", format="%.0f%%", min_value=0.0, max_value=100.0),
                    "Score": st.column_config.ProgressColumn(
                        "Score", format="%.0f", min_value=0.0, max_value=100.0),
                    "Pronóstico consolidado": st.column_config.NumberColumn(format="%.0f"),
                    "Req. consolidado": st.column_config.NumberColumn(format="%.0f"),
                    "Margen %": st.column_config.NumberColumn(format="%.1f%%"),
                })
            st.caption(
                "ℹ️ *'Venta histórica'* aparece como **sin información disponible**: el "
                "dashboard en la nube solo lee Supabase y la venta histórica no está "
                "persistida ahí (se usa internamente para generar el pronóstico). "
                "Ordena/filtra haciendo clic en los encabezados.")
            st.download_button(
                "⬇️ Descargar CSV", data=show.to_csv(index=False).encode("utf-8"),
                file_name="compra_consolidada_nacional.csv", mime="text/csv")

# ---------------------------------------------------------------------------
# 8. Panel gerencial (SOLO LECTURA — aditivo, independiente del motor)
# ---------------------------------------------------------------------------
with tab_ger:
    st.subheader("📊 Panel gerencial de abastecimiento")
    st.caption(
        "Vista de **solo lectura** para revisión semanal: compra consolidada "
        "nacional, rotación, transversalidad, categorías y oportunidades. No "
        "modifica el motor de abastecimiento ni ningún cálculo/resultado actual.")

    pa_g = _plan_actual()
    if pa_g is None or pa_g.empty:
        st.info("Sin información disponible: la vista analítica (plan_actual) no "
                "tiene datos para el último run.")
    else:
        _cp = cons.load_consolidado_params()
        _gp = ger.load_gerencial_params()
        with st.expander("⚙️ Parámetros y filtros", expanded=False):
            x1, x2, x3 = st.columns(3)
            g_cob = x1.slider("Cobertura mínima candidato", 0.0, 1.0,
                              float(_cp.cobertura_min_pct), 0.05, key="g_cob")
            g_rot = x2.slider("Mín. sucursales alta rotación (X)", 1, 10,
                              int(_cp.rotacion_suc_min), 1, key="g_rot")
            g_top = x3.slider("Top N (rankings/heatmap)", 10, 200, int(_gp.top_n), 10, key="g_top")
            y1, y2, y3 = st.columns(3)
            gw_cob = y1.slider("Peso cobertura", 0.0, 1.0, float(_cp.peso_cobertura), 0.05, key="gw_cob")
            gw_dem = y2.slider("Peso demanda", 0.0, 1.0, float(_cp.peso_demanda), 0.05, key="gw_dem")
            gw_rot = y3.slider("Peso rotación", 0.0, 1.0, float(_cp.peso_rotacion), 0.05, key="gw_rot")
            z1, z2 = st.columns(2)
            g_group = z1.radio("Agrupar por", ["SKU", "Nombre"], horizontal=True, key="g_group")
            g_demmin = z2.number_input("Demanda consolidada mínima (0 = sin filtro)",
                                       min_value=0.0, value=0.0, step=1.0, key="g_demmin")

        gb = "nombre" if g_group == "Nombre" else "sku"
        data = _gerencial(g_cob, int(g_rot), gw_cob, gw_dem, gw_rot, int(g_top),
                          gb, float(g_demmin))
        t = data.tabla
        k = data.kpis

        if t.empty:
            st.warning("Ningún producto cumple los criterios actuales. Ajusta los parámetros.")
        else:
            # ---- Tarjetas KPI (sobre el total de candidatos) ----
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Productos candidatos", f"{k.get('n_candidatos', 0):,}")
            c2.metric("Demanda pronosticada consolidada", f"{k.get('demanda_consolidada_total', 0):,.0f}")
            cobp = k.get("cobertura_promedio")
            c3.metric("Cobertura promedio", f"{cobp*100:,.0f}%" if pd.notna(cobp) else "—")
            c4.metric("Categoría top", str(k.get("categoria_top") or "—"))
            c5, c6, c7, c8 = st.columns(4)
            c5.metric("Infaltables / críticos", f"{k.get('n_infaltables', 0):,}")
            c6.metric("Riesgo de quiebre", f"{k.get('n_riesgo_quiebre', 0):,}")
            if k.get("venta_historica_disponible"):
                c7.metric("Venta histórica consolidada (uds)", f"{k.get('venta_historica_total', 0):,.0f}")
            else:
                c7.metric("Venta histórica consolidada", "no disponible",
                          help="Corre supabase/gerencial.sql + scripts/sync_gerencial.py.")
            c8.metric("Potencial compra (CLP)", f"${k.get('potencial_clp_total', 0):,.0f}")

            # ---- Filtros sobre la tabla/gráficos ----
            f1, f2, f3 = st.columns(3)
            fam_opts = sorted(t["familia"].dropna().unique()) if "familia" in t.columns else []
            clase_opts = sorted(t["clase"].dropna().unique()) if "clase" in t.columns else []
            f_fam = f1.multiselect("Categoría", fam_opts)
            f_clase = f2.multiselect("Clasificación", clase_opts)
            f_flags = f3.multiselect("Solo mostrar", ["Infaltables", "Alta rotación", "Riesgo de quiebre"])
            tv = t.copy()
            if f_fam:
                tv = tv[tv["familia"].isin(f_fam)]
            if f_clase:
                tv = tv[tv["clase"].isin(f_clase)]
            if "Infaltables" in f_flags:
                tv = tv[tv["infaltable"].fillna(False)]
            if "Alta rotación" in f_flags:
                tv = tv[tv["alta_rotacion"].fillna(False)]
            if "Riesgo de quiebre" in f_flags:
                tv = tv[tv["riesgo_quiebre"].fillna(False)]

            if tv.empty:
                st.info("Sin filas con los filtros actuales.")
            else:
                tvi = tv.set_index("sku")

                # ---- Tabla gerencial priorizada (con variante) ----
                st.markdown("#### Tabla gerencial priorizada")
                show = tv.copy()
                show["cobertura_pct"] = (pd.to_numeric(show["cobertura_pct"], errors="coerce") * 100).round(0)
                show["margen_pct"] = (pd.to_numeric(show["margen_pct"], errors="coerce") * 100).round(1)
                order = ["sku", "nombre", "variante", "clase", "familia", "n_suc_demanda",
                         "cobertura_pct", "venta_hist_unidades", "demanda_consolidada",
                         "potencial_clp", "margen_pct", "semanas_cobertura", "concentracion",
                         "prioridad", "infaltable", "etiquetas", "motivo"]
                show = show[[c for c in order if c in show.columns]].rename(columns={
                    "sku": "Código", "nombre": "Producto", "variante": "Variante",
                    "clase": "Clasif.", "familia": "Categoría", "n_suc_demanda": "Suc. demanda",
                    "cobertura_pct": "Cobertura", "venta_hist_unidades": "Venta hist. (uds)",
                    "demanda_consolidada": "Pronóstico", "potencial_clp": "Potencial CLP",
                    "margen_pct": "Margen %",
                    "semanas_cobertura": "Sem. cobertura", "concentracion": "Concentración",
                    "prioridad": "Prioridad", "infaltable": "Infaltable",
                    "etiquetas": "Etiquetas", "motivo": "Motivo"})
                st.dataframe(
                    show, use_container_width=True, hide_index=True, height=420,
                    column_config={
                        "Cobertura": st.column_config.ProgressColumn(
                            "Cobertura", format="%.0f%%", min_value=0.0, max_value=100.0),
                        "Pronóstico": st.column_config.NumberColumn(format="%.0f"),
                        "Venta hist. (uds)": st.column_config.NumberColumn(format="%.0f"),
                        "Potencial CLP": st.column_config.NumberColumn(format="%.0f"),
                        "Margen %": st.column_config.NumberColumn(format="%.1f%%"),
                        "Sem. cobertura": st.column_config.NumberColumn(format="%.1f"),
                    })
                if not k.get("venta_historica_disponible"):
                    st.caption("ℹ️ 'Venta hist.' = **no disponible** hasta correr "
                               "`supabase/gerencial.sql` + `scripts/sync_gerencial.py`.")
                st.download_button("⬇️ Descargar CSV (gerencial)", key="dl_ger",
                                   data=show.to_csv(index=False).encode("utf-8"),
                                   file_name="panel_gerencial.csv", mime="text/csv")

                # ---- Burbujas: cobertura vs demanda ----
                st.markdown("#### Mapa cobertura × demanda")
                bub = pd.DataFrame({
                    "Cobertura %": pd.to_numeric(tv["cobertura_pct"], errors="coerce") * 100,
                    "Demanda": pd.to_numeric(tv["demanda_consolidada"], errors="coerce"),
                    "Prioridad": tv["prioridad"],
                })
                _size_hist = k.get("venta_historica_disponible")
                _scol = "venta_hist_unidades" if _size_hist else "potencial_clp"
                bub["Tamaño"] = pd.to_numeric(tv[_scol], errors="coerce").fillna(0).clip(lower=0)
                try:
                    st.scatter_chart(bub, x="Cobertura %", y="Demanda", size="Tamaño",
                                     color="Prioridad", height=360)
                    st.caption("Tamaño de burbuja = "
                               + ("venta histórica (uds)." if _size_hist
                                  else "potencial CLP (demanda×costo) — venta histórica no disponible."))
                except Exception:
                    st.info("No se pudo dibujar el gráfico de burbujas.")

                # ---- Rankings ----
                r1, r2 = st.columns(2)
                with r1:
                    st.markdown("#### Top productos (prioridad)")
                    topp = tv.sort_values("score", ascending=False).head(int(g_top)).copy()
                    topp["_lbl"] = (topp["nombre"].astype(str) + " [" +
                                    topp["sku"].astype(str).str[-4:] + "]")
                    st.bar_chart(topp.set_index("_lbl")["score"], height=320)
                with r2:
                    st.markdown("#### Ranking de categorías")
                    if not data.ranking_categorias.empty:
                        st.bar_chart(data.ranking_categorias.head(int(g_top))
                                     .set_index("categoria")["productos"], height=320)
                    else:
                        st.info("Sin datos de categoría.")

                # ---- Heatmap producto × sucursal ----
                st.markdown("#### Heatmap de demanda por sucursal (Top productos)")
                top_skus = tv.sort_values("score", ascending=False).head(
                    min(int(g_top), 25))["sku"].tolist()
                piv = ger.heatmap_pivot(pa_g, top_skus, _cp.cd_labels)
                if piv.empty:
                    st.info("Sin datos para el heatmap.")
                else:
                    name_map = tvi["nombre"].to_dict()
                    piv.index = [str(name_map.get(s, s)) for s in piv.index]
                    piv.index.name = "Producto"
                    try:
                        import altair as alt
                        hm = piv.reset_index().melt(id_vars="Producto", var_name="Sucursal",
                                                    value_name="Demanda")
                        chart = (alt.Chart(hm).mark_rect().encode(
                            x=alt.X("Sucursal:N"),
                            y=alt.Y("Producto:N", sort="-x"),
                            color=alt.Color("Demanda:Q", scale=alt.Scale(scheme="blues")),
                            tooltip=["Producto", "Sucursal", "Demanda"])
                            .properties(height=min(26 * len(piv) + 50, 600)))
                        st.altair_chart(chart, use_container_width=True)
                    except Exception:
                        st.dataframe(piv, use_container_width=True)

                # ---- Potencial por proveedor ----
                if not data.potencial_proveedor.empty:
                    st.markdown("#### Potencial de compra por proveedor (CLP)")
                    st.bar_chart(data.potencial_proveedor.head(int(g_top))
                                 .set_index("proveedor")["potencial_clp"], height=320)

                # ---- Detalle por producto + tendencia ----
                st.markdown("#### Detalle por producto")
                opciones = tv["sku"].tolist()
                labels = {s: f"{tvi.loc[s, 'nombre']} — {tvi.loc[s, 'variante']} ({s})"
                          for s in opciones}
                sel = st.selectbox("Producto", opciones,
                                   format_func=lambda s: labels.get(s, s), key="g_sel")
                det = ger.detalle_producto(t, pa_g, sel, _cp.cd_labels)
                res = det["resumen"]
                d1, d2, d3, d4 = st.columns(4)
                d1.metric("Variante", str(res.get("variante", "—")))
                d2.metric("Clasificación", str(res.get("clase", "—")))
                _cv = res.get("cobertura_pct")
                d3.metric("Cobertura", f"{_cv*100:,.0f}%" if pd.notna(_cv) else "—")
                _vh = res.get("venta_hist_unidades")
                d4.metric("Venta histórica (uds)",
                          f"{_vh:,.0f}" if pd.notna(_vh) else "no disponible")
                serie = ger.tendencia_serie(_ventas_mensual(sel),
                                            demanda_pronostico=res.get("demanda_consolidada"))
                if serie.empty:
                    st.caption("Tendencia: **no disponible** (sin venta histórica para este producto).")
                else:
                    st.line_chart(serie.pivot_table(index="mes", columns="tipo",
                                                    values="unidades", aggfunc="sum"), height=300)
                if not det["por_sucursal"].empty:
                    st.caption("Detalle por sucursal:")
                    st.dataframe(det["por_sucursal"], use_container_width=True,
                                 hide_index=True, height=240)

# ---------------------------------------------------------------------------
# 9. Indicadores: margen, inmovilizado/sobrestock, servicio, MAPE (solo lectura)
# ---------------------------------------------------------------------------
with tab_ind:
    st.subheader("💹 Indicadores de abastecimiento")
    st.caption("Solo lectura. Margen, capital inmovilizado/sobrestock, nivel de "
               "servicio y exactitud del pronóstico (MAPE/WAPE). Si falta una "
               "fuente, la sección lo indica (no se inventan datos).")
    _cp_i = cons.load_consolidado_params()
    pa_i = _plan_actual()

    # ---- Rentabilidad (margen) ----
    with st.expander("💰 Rentabilidad (margen)", expanded=True):
        try:
            mc = _margen_categoria()
            if mc.empty:
                st.info("Margen no disponible. Corre `supabase/gerencial_indicadores.sql` "
                        "+ `scripts/sync_gerencial.py`.")
            else:
                mc = mc.copy()
                mc["margen_clp"] = pd.to_numeric(mc["margen_clp"], errors="coerce").fillna(0)
                mc["venta_neto_clp"] = pd.to_numeric(mc["venta_neto_clp"], errors="coerce").fillna(0)
                tot_m, tot_v = mc["margen_clp"].sum(), mc["venta_neto_clp"].sum()
                a, b = st.columns(2)
                a.metric("Margen total (CLP)", f"${tot_m:,.0f}")
                b.metric("Margen % global", f"{tot_m/tot_v*100:.1f}%" if tot_v > 0 else "—")
                st.markdown("**Margen por categoría (CLP)**")
                st.bar_chart(mc.sort_values("margen_clp", ascending=False).head(20)
                             .set_index("familia")["margen_clp"], height=300)
                sm = mc.sort_values("margen_clp", ascending=False).copy()
                sm["Margen %"] = (pd.to_numeric(sm["margen_pct"], errors="coerce") * 100).round(1)
                sm = sm[["familia", "venta_neto_clp", "margen_clp", "Margen %"]].rename(columns={
                    "familia": "Categoría", "venta_neto_clp": "Venta neta CLP", "margen_clp": "Margen CLP"})
                st.dataframe(sm, use_container_width=True, hide_index=True, height=280,
                             column_config={
                                 "Venta neta CLP": st.column_config.NumberColumn(format="%.0f"),
                                 "Margen CLP": st.column_config.NumberColumn(format="%.0f"),
                                 "Margen %": st.column_config.NumberColumn(format="%.1f%%")})
        except Exception as e:
            st.warning(f"No se pudo cargar margen: {e}")

    # ---- Capital inmovilizado / sobrestock ----
    with st.expander("🧊 Capital inmovilizado y sobrestock", expanded=True):
        try:
            inv = _inmovilizado_resumen()
            if inv.empty:
                st.info("Inmovilizado no disponible. Requiere `ultima_venta` (corre el sync) "
                        "y el DDL de indicadores.")
            else:
                inv = inv.copy()
                for c in ("valor_total_clp", "inmovilizado_clp", "sobrestock_clp"):
                    inv[c] = pd.to_numeric(inv[c], errors="coerce").fillna(0)
                a, b, c = st.columns(3)
                a.metric("Capital inmovilizado (CLP)", f"${inv['inmovilizado_clp'].sum():,.0f}")
                b.metric("Sobrestock (CLP)", f"${inv['sobrestock_clp'].sum():,.0f}")
                c.metric("Valor inventario (CLP)", f"${inv['valor_total_clp'].sum():,.0f}")
                g1, g2 = st.columns(2)
                with g1:
                    st.markdown("**Inmovilizado por sucursal (CLP)**")
                    st.bar_chart(inv.groupby("sucursal")["inmovilizado_clp"].sum()
                                 .sort_values(ascending=False), height=300)
                with g2:
                    st.markdown("**Inmovilizado por categoría (CLP)**")
                    st.bar_chart(inv.groupby("familia")["inmovilizado_clp"].sum()
                                 .sort_values(ascending=False).head(15), height=300)
        except Exception as e:
            st.warning(f"No se pudo cargar inmovilizado: {e}")

    # ---- Nivel de servicio y quiebres ----
    with st.expander("🛡️ Nivel de servicio y quiebres", expanded=True):
        try:
            if pa_i is None or pa_i.empty:
                st.info("Sin plan_actual.")
            else:
                serv = ger.servicio_por_familia(pa_i, _cp_i.cd_labels)
                if serv.empty:
                    st.info("Sin datos de servicio.")
                else:
                    td = pd.to_numeric(serv["demanda"], errors="coerce").sum()
                    tdesc = pd.to_numeric(serv["descubierto"], errors="coerce").sum()
                    tq = pd.to_numeric(serv["quiebre_clp"], errors="coerce").sum()
                    a, b = st.columns(2)
                    a.metric("Nivel de servicio proyectado", f"{(1-tdesc/td)*100:.1f}%" if td > 0 else "—")
                    b.metric("Quiebre proyectado (CLP)", f"${tq:,.0f}")
                    st.markdown("**Quiebre por categoría (CLP)**")
                    st.bar_chart(serv.set_index("familia")["quiebre_clp"].head(15), height=300)
                    ss = serv.copy()
                    ss["Fill-rate %"] = (pd.to_numeric(ss["fill_rate"], errors="coerce") * 100).round(1)
                    ss = ss[["familia", "demanda", "descubierto", "quiebre_clp", "Fill-rate %"]].rename(
                        columns={"familia": "Categoría", "demanda": "Demanda",
                                 "descubierto": "Descubierto", "quiebre_clp": "Quiebre CLP"})
                    st.dataframe(ss, use_container_width=True, hide_index=True, height=280,
                                 column_config={
                                     "Demanda": st.column_config.NumberColumn(format="%.0f"),
                                     "Descubierto": st.column_config.NumberColumn(format="%.0f"),
                                     "Quiebre CLP": st.column_config.NumberColumn(format="%.0f"),
                                     "Fill-rate %": st.column_config.NumberColumn(format="%.1f%%")})
        except Exception as e:
            st.warning(f"No se pudo calcular servicio: {e}")

    # ---- Exactitud del pronóstico (MAPE / WAPE) ----
    with st.expander("🎯 Exactitud del pronóstico (MAPE / WAPE)", expanded=True):
        try:
            mp = _mape_categoria()
            if mp.empty:
                st.info("MAPE no disponible. Requiere `pronostico_historico` + "
                        "`ventas_historicas` (corre el sync) y el DDL de indicadores.")
            else:
                mp = mp.copy()
                for c in ("err_abs", "venta_real", "sesgo", "n_obs"):
                    mp[c] = pd.to_numeric(mp[c], errors="coerce").fillna(0)
                err, real, ses = mp["err_abs"].sum(), mp["venta_real"].sum(), mp["sesgo"].sum()
                a, b = st.columns(2)
                a.metric("WAPE global", f"{err/real*100:.1f}%" if real > 0 else "—",
                         help="Σ|pronóstico−real| / Σreal en meses cerrados. Menor = mejor.")
                b.metric("Sesgo global", f"{ses/real*100:+.1f}%" if real > 0 else "—",
                         help="Σ(pronóstico−real)/Σreal. Positivo = sobreestima; negativo = subestima.")
                mp["WAPE %"] = (mp["err_abs"] / mp["venta_real"].where(mp["venta_real"] > 0) * 100).round(1)
                st.markdown("**WAPE por categoría (%) — menor es mejor**")
                st.bar_chart(mp.sort_values("WAPE %").set_index("familia")["WAPE %"], height=300)
                sw = mp.sort_values("venta_real", ascending=False)[
                    ["familia", "venta_real", "err_abs", "WAPE %", "n_obs"]].rename(columns={
                        "familia": "Categoría", "venta_real": "Venta real (uds)",
                        "err_abs": "Error abs (uds)", "n_obs": "Meses"})
                st.dataframe(sw, use_container_width=True, hide_index=True, height=280,
                             column_config={
                                 "Venta real (uds)": st.column_config.NumberColumn(format="%.0f"),
                                 "Error abs (uds)": st.column_config.NumberColumn(format="%.0f"),
                                 "WAPE %": st.column_config.NumberColumn(format="%.1f%%")})
                st.caption("ℹ️ Agregado por **categoría×mes** (no penaliza el ruido de combos "
                           "sku×sucursal sin venta). Compara el pronóstico del archivo en meses "
                           "cerrados vs venta real. Ex‑ante = exactitud real; in‑sample = error de ajuste.")

                # ---- Exactitud por PRODUCTO (productos clave) ----
                st.markdown("#### Exactitud por producto")
                pm = _mape_producto()
                if pm.empty:
                    st.info("Sin datos por producto.")
                else:
                    pm = pm.copy()
                    for c in ("err_abs", "venta_real", "pronostico_total", "sesgo", "n_meses"):
                        pm[c] = pd.to_numeric(pm[c], errors="coerce").fillna(0)
                    gpar = ger.load_gerencial_params()
                    cfa, cfb = st.columns(2)
                    min_mes = cfa.slider("Mín. meses con dato", 1, 6,
                                         int(gpar.mape_meses_min), 1, key="mape_minmes")
                    min_venta = cfb.number_input("Venta mínima (uds) — 'producto clave'",
                                                 min_value=0, value=50, step=10, key="mape_minventa")
                    pmf = pm[(pm["n_meses"] >= min_mes)
                             & (pm["venta_real"] >= float(min_venta))].copy()
                    if pmf.empty:
                        st.info("Ningún producto cumple los filtros (baja la venta mínima).")
                    else:
                        pmf["WAPE %"] = (pmf["err_abs"] / pmf["venta_real"].where(pmf["venta_real"] > 0) * 100).round(1)
                        pmf["Sesgo %"] = (pmf["sesgo"] / pmf["venta_real"].where(pmf["venta_real"] > 0) * 100).round(1)
                        prod = _productos_dim()
                        if not prod.empty and "sku_id" in prod.columns:
                            keep = [c for c in ("sku_id", "nombre", "variante", "familia") if c in prod.columns]
                            pmf = pmf.merge(prod[keep].drop_duplicates("sku_id"), on="sku_id", how="left")
                        for c in ("nombre", "variante", "familia"):
                            if c not in pmf.columns:
                                pmf[c] = "—"
                            pmf[c] = pmf[c].fillna("—")
                        st.caption(f"{len(pmf):,} productos. Ordena por **Venta real** para los "
                                   f"clave, o por **WAPE %** para ver dónde fallan los pronósticos.")
                        sp = pmf.sort_values("venta_real", ascending=False)[
                            ["nombre", "variante", "familia", "venta_real", "pronostico_total",
                             "WAPE %", "Sesgo %", "n_meses"]].rename(columns={
                                "nombre": "Producto", "variante": "Variante", "familia": "Categoría",
                                "venta_real": "Venta real (uds)", "pronostico_total": "Pronóstico (uds)",
                                "n_meses": "Meses"})
                        st.dataframe(sp, use_container_width=True, hide_index=True, height=380,
                                     column_config={
                                         "Venta real (uds)": st.column_config.NumberColumn(format="%.0f"),
                                         "Pronóstico (uds)": st.column_config.NumberColumn(format="%.0f"),
                                         "WAPE %": st.column_config.NumberColumn(format="%.1f%%"),
                                         "Sesgo %": st.column_config.NumberColumn(format="%.1f%%")})
                        st.download_button("⬇️ CSV exactitud por producto", key="dl_mape_prod",
                                           data=sp.to_csv(index=False).encode("utf-8"),
                                           file_name="exactitud_pronostico_producto.csv", mime="text/csv")
        except Exception as e:
            st.warning(f"No se pudo calcular MAPE: {e}")
