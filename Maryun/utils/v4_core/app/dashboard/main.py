"""Dashboard gerencial Maryun — Streamlit.

Ejecutar:
    streamlit run src/app/dashboard/app.py

Antes del primer uso, configurar password:
    python -m app.dashboard.auth set-password
"""
from __future__ import annotations

import sys
from pathlib import Path

# Permite correr "streamlit run src/app/dashboard/app.py" desde la raíz del
# proyecto sin instalar el paquete — agrega ``src`` al sys.path.
_SRC = Path(__file__).resolve().parents[2]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from app.dashboard.auth import require_password
import io
from datetime import date

from app.dashboard.loaders import (
    find_latest_run,
    guardar_snapshot_historial,
    list_sucursales,
    load_all_cargas,
    load_costos_sku,
    load_demanda_real,
    load_historial_ofertas,
    load_latest_audit_sheet,
    load_pmp_por_sucursal,
    load_productos,
    load_stock_demanda,
    load_ultima_venta,
)


st.set_page_config(
    page_title="Maryun · Dashboard Gerencial",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded",
)

require_password()

# ============================================================================
# ESTILOS — Paleta Maryun (azul #1A52D6 · naranja #F5821A · blanco #FFFFFF)
# ============================================================================
st.markdown(
    """
    <style>
    /* ── Variables de marca ───────────────────────────────────────── */
    :root {
        --maryun-blue:        #1A52D6;
        --maryun-blue-dark:   #0D2B6B;
        --maryun-blue-light:  #E8EFFC;
        --maryun-orange:      #F5821A;
        --maryun-orange-light:#FEF0E3;
        --maryun-white:       #FFFFFF;
        --maryun-gray:        #F4F6FA;
    }

    /* ── Sidebar ──────────────────────────────────────────────────── */
    [data-testid="stSidebar"] {
        background: linear-gradient(180deg, #0D2B6B 0%, #1A52D6 100%) !important;
        border-right: 3px solid #F5821A;
    }
    [data-testid="stSidebar"] * {
        color: #FFFFFF !important;
    }
    [data-testid="stSidebar"] .stSelectbox label,
    [data-testid="stSidebar"] .stMultiSelect label,
    [data-testid="stSidebar"] .stRadio label {
        color: #FFFFFF !important;
        font-weight: 600;
    }
    [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p,
    [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] small,
    [data-testid="stSidebar"] .stCaption {
        color: rgba(255,255,255,0.80) !important;
    }

    /* ── Botón primario (naranja Maryun) ──────────────────────────── */
    .stButton > button[kind="primary"],
    .stDownloadButton > button {
        background: linear-gradient(135deg, #F5821A, #e06c08) !important;
        color: #FFFFFF !important;
        border: none !important;
        border-radius: 6px !important;
        font-weight: 700 !important;
        letter-spacing: 0.3px;
        box-shadow: 0 2px 6px rgba(245,130,26,0.35);
    }
    .stButton > button[kind="primary"]:hover,
    .stDownloadButton > button:hover {
        background: linear-gradient(135deg, #e06c08, #c85c00) !important;
        box-shadow: 0 4px 12px rgba(245,130,26,0.45);
    }

    /* ── Botón secundario ─────────────────────────────────────────── */
    .stButton > button[kind="secondary"] {
        background: #FFFFFF !important;
        color: #1A52D6 !important;
        border: 2px solid #1A52D6 !important;
        border-radius: 6px !important;
        font-weight: 600 !important;
    }
    .stButton > button[kind="secondary"]:hover {
        background: #E8EFFC !important;
    }

    /* ── Sidebar — botón refrescar ────────────────────────────────── */
    [data-testid="stSidebar"] .stButton > button {
        background: rgba(255,255,255,0.15) !important;
        color: #FFFFFF !important;
        border: 1px solid rgba(255,255,255,0.35) !important;
        border-radius: 6px !important;
    }
    [data-testid="stSidebar"] .stButton > button:hover {
        background: rgba(245,130,26,0.70) !important;
        border-color: #F5821A !important;
    }

    /* ── Encabezado principal ─────────────────────────────────────── */
    .maryun-header {
        background: linear-gradient(135deg, #0D2B6B 0%, #1A52D6 100%);
        padding: 22px 28px;
        border-radius: 10px;
        margin-bottom: 20px;
        display: flex;
        align-items: center;
        gap: 16px;
        border-left: 6px solid #F5821A;
        box-shadow: 0 4px 16px rgba(26,82,214,0.20);
    }
    .maryun-header h1 {
        color: #FFFFFF !important;
        font-size: 26px !important;
        font-weight: 800 !important;
        margin: 0 !important;
        letter-spacing: -0.5px;
    }
    .maryun-header p {
        color: rgba(255,255,255,0.80) !important;
        margin: 2px 0 0 0 !important;
        font-size: 13px !important;
    }
    .maryun-logo-box {
        background: #FFFFFF;
        border-radius: 8px;
        width: 52px;
        height: 52px;
        display: flex;
        align-items: center;
        justify-content: center;
        flex-shrink: 0;
    }
    .maryun-logo-m {
        font-size: 26px;
        font-weight: 900;
        color: #1A52D6;
        line-height: 1;
        font-family: Arial Black, sans-serif;
        position: relative;
    }
    .maryun-logo-m::after {
        content: '';
        display: block;
        width: 18px;
        height: 4px;
        background: #F5821A;
        border-radius: 2px;
        margin: 2px auto 0;
    }

    /* ── Tabs ─────────────────────────────────────────────────────── */
    [data-testid="stTabs"] [data-baseweb="tab-list"] {
        background: #F4F6FA;
        border-radius: 8px 8px 0 0;
        padding: 4px 4px 0;
        gap: 2px;
        border-bottom: 2px solid #1A52D6;
    }
    [data-testid="stTabs"] [data-baseweb="tab"] {
        background: transparent !important;
        color: #1A52D6 !important;
        border-radius: 6px 6px 0 0 !important;
        font-weight: 600 !important;
        padding: 8px 16px !important;
        border: none !important;
    }
    [data-testid="stTabs"] [aria-selected="true"] {
        background: #1A52D6 !important;
        color: #FFFFFF !important;
    }

    /* ── KPI cards ────────────────────────────────────────────────── */
    [data-testid="stMetric"] {
        background: #FFFFFF;
        border: 1px solid #E8EFFC;
        border-radius: 10px;
        padding: 14px 18px !important;
        border-top: 4px solid #1A52D6;
        box-shadow: 0 2px 8px rgba(26,82,214,0.08);
    }
    [data-testid="stMetricLabel"] {
        color: #0D2B6B !important;
        font-weight: 700 !important;
        font-size: 12px !important;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }
    [data-testid="stMetricValue"] {
        color: #1A52D6 !important;
        font-weight: 800 !important;
    }
    [data-testid="stMetricDelta"] { font-size: 13px !important; }

    /* ── Expanders ────────────────────────────────────────────────── */
    [data-testid="stExpander"] > summary {
        background: #E8EFFC !important;
        color: #0D2B6B !important;
        font-weight: 600 !important;
        border-radius: 6px !important;
        border-left: 4px solid #F5821A !important;
    }

    /* ── Divisor naranja ──────────────────────────────────────────── */
    hr { border-top: 2px solid #F5821A !important; opacity: 0.5; }

    /* ── Dataframes ───────────────────────────────────────────────── */
    [data-testid="stDataFrame"] thead tr th {
        background: #1A52D6 !important;
        color: #FFFFFF !important;
        font-weight: 700 !important;
    }

    /* ── Info / Warning / Success boxes ──────────────────────────── */
    [data-testid="stInfo"] {
        border-left: 5px solid #1A52D6 !important;
        background: #E8EFFC !important;
    }
    [data-testid="stWarning"] {
        border-left: 5px solid #F5821A !important;
        background: #FEF0E3 !important;
    }

    /* ── Sidebar logo / título ────────────────────────────────────── */
    .sidebar-logo {
        text-align: center;
        padding: 12px 0 8px;
    }
    .sidebar-logo .m-box {
        display: inline-block;
        background: #FFFFFF;
        border-radius: 8px;
        padding: 10px 14px;
        margin-bottom: 6px;
    }
    .sidebar-logo .m-letter {
        font-size: 32px;
        font-weight: 900;
        color: #1A52D6;
        font-family: Arial Black, sans-serif;
        display: block;
        line-height: 1;
    }
    .sidebar-logo .m-bar {
        width: 22px;
        height: 5px;
        background: #F5821A;
        border-radius: 2px;
        margin: 3px auto 0;
    }
    .sidebar-logo .brand-name {
        font-size: 18px;
        font-weight: 800;
        color: #FFFFFF;
        letter-spacing: 2px;
        text-transform: uppercase;
    }
    .sidebar-logo .brand-sub {
        font-size: 11px;
        color: rgba(255,255,255,0.70);
        letter-spacing: 1px;
    }

    /* ── Fondo general ────────────────────────────────────────────── */
    .stApp { background: #F4F6FA; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ============================================================================
# Helpers de formato
# ============================================================================
def fmt_clp(x: float) -> str:
    try:
        return f"${int(round(float(x))):,}".replace(",", ".")
    except Exception:
        return "$0"


def fmt_int(x: float) -> str:
    try:
        return f"{int(round(float(x))):,}".replace(",", ".")
    except Exception:
        return "0"


# ============================================================================
# Sidebar — filtros globales
# ============================================================================
st.sidebar.markdown(
    """
    <div class="sidebar-logo">
        <div class="m-box">
            <span class="m-letter">M</span>
            <div class="m-bar"></div>
        </div><br>
        <span class="brand-name">MARYUN</span><br>
        <span class="brand-sub">Sistema de Reposición</span>
    </div>
    """,
    unsafe_allow_html=True,
)
st.sidebar.markdown("---")

latest = find_latest_run()
if latest is None:
    st.error("No hay corridas con datos de ahorro disponibles. Ejecutá el pipeline al menos una vez después de activar el feature.")
    st.stop()

st.sidebar.markdown(
    f"<small>📅 <b>Último run</b><br>"
    f"<code style='color:#F5821A;background:rgba(0,0,0,0.3);padding:2px 6px;border-radius:4px;font-size:11px;'>"
    f"{latest.run_id}</code><br>"
    f"<span style='color:rgba(255,255,255,0.7);font-size:12px;'>{latest.fecha.strftime('%d %b %Y · %H:%M')}</span></small>",
    unsafe_allow_html=True,
)
st.sidebar.markdown(" ")
if st.sidebar.button("🔄 Refrescar datos", use_container_width=True):
    st.cache_data.clear()
    st.rerun()

st.sidebar.divider()

# Filtros
sucursales = list_sucursales()
sucursal_sel = st.sidebar.multiselect(
    "Sucursal", options=sucursales, default=[], help="Vacío = todas"
)

periodo = st.sidebar.radio(
    "Período",
    ["Últimos 7 días", "Últimos 30 días", "Mes calendario", "Últimos 90 días", "Todo"],
    index=2,
)

# Filtros por clasificación ABC/XYZ — separados para poder cruzar (ej. A + X,Y)
abc_sel = st.sidebar.multiselect(
    "Clase ABC", options=["A", "B", "C"], default=[],
    help="Vacío = todas. A = mayor impacto en ventas.",
)
xyz_sel = st.sidebar.multiselect(
    "Clase XYZ", options=["X", "Y", "Z"], default=[],
    help="Vacío = todas. X = demanda estable, Z = errática.",
)


def _filter_period(df: pd.DataFrame, ref: pd.Timestamp) -> pd.DataFrame:
    if df.empty or "fecha_dia" not in df.columns:
        return df
    ref = ref.normalize()
    if periodo == "Últimos 7 días":
        return df[df["fecha_dia"] >= ref - pd.Timedelta(days=6)]
    if periodo == "Últimos 30 días":
        return df[df["fecha_dia"] >= ref - pd.Timedelta(days=29)]
    if periodo == "Mes calendario":
        inicio = ref.replace(day=1)
        fin = (inicio + pd.offsets.MonthEnd(0)).normalize()
        return df[(df["fecha_dia"] >= inicio) & (df["fecha_dia"] <= fin)]
    if periodo == "Últimos 90 días":
        return df[df["fecha_dia"] >= ref - pd.Timedelta(days=89)]
    return df  # Todo


def _filter_sucursal(df: pd.DataFrame, col: str = "destino") -> pd.DataFrame:
    if not sucursal_sel or df.empty or col not in df.columns:
        return df
    return df[df[col].isin(sucursal_sel)]


def _filter_clase(df: pd.DataFrame, col: str = "clase_abc_xyz") -> pd.DataFrame:
    """Filtra por ABC y/o XYZ. La columna es tipo 'AX', 'BZ', etc.

    Si la columna no existe (datos viejos sin clasificación) o es 'N/A',
    esas filas se conservan sólo cuando el usuario no filtró explícitamente.
    """
    if df.empty or col not in df.columns:
        return df
    if not abc_sel and not xyz_sel:
        return df
    s = df[col].astype(str)
    mask = pd.Series(True, index=df.index)
    if abc_sel:
        mask &= s.str[0].isin(abc_sel)
    if xyz_sel:
        mask &= s.str[1].isin(xyz_sel)
    return df[mask]


# ============================================================================
# Carga de data base
# ============================================================================
hist = load_all_cargas()
if not hist.empty and "sku_id" in hist.columns:
    hist["sku_id"] = hist["sku_id"].astype(str)
hist_f = _filter_clase(_filter_sucursal(_filter_period(hist, latest.fecha)))
needs = load_latest_audit_sheet("needs_audit")
if not needs.empty and "sku_id" in needs.columns:
    needs["sku_id"] = needs["sku_id"].astype(str)
costos = load_costos_sku()
if not costos.empty and "sku_id" in costos.columns:
    costos["sku_id"] = costos["sku_id"].astype(str)
productos = load_productos()  # catálogo: sku_id → nombre, marca, familia, tipo

# ============================================================================
# Header — KPIs grandes
# ============================================================================
st.markdown(
    f"""
    <div class="maryun-header">
        <div class="maryun-logo-box">
            <div class="maryun-logo-m">M</div>
        </div>
        <div>
            <h1>Dashboard Gerencial</h1>
            <p>Sistema de Reposición Automática · {date.today().strftime('%d de %B de %Y')}</p>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)
_clase_label = (
    f"{''.join(abc_sel) or 'ABC'}·{''.join(xyz_sel) or 'XYZ'}"
    if (abc_sel or xyz_sel) else "Todas"
)
st.caption(
    f"Período activo: **{periodo}**   |   "
    f"Sucursal(es): **{', '.join(sucursal_sel) if sucursal_sel else 'Todas'}**   |   "
    f"Clase ABC/XYZ: **{_clase_label}**"
)

col1, col2, col3, col4, col5 = st.columns(5)

ahorro_periodo = float(hist_f["ahorro_clp"].sum()) if not hist_f.empty else 0.0

needs_f = _filter_sucursal(needs, col="ubicacion") if not needs.empty else needs
needs_f = _filter_clase(needs_f, col="clase_final") if not needs_f.empty else needs_f
cobertura_avg = 0.0
alertas_quiebre = 0
fill_rate = 0.0
if not needs_f.empty and "cobertura_actual_meses" in needs_f.columns:
    con_dem = needs_f[needs_f["demanda_esperada"] > 0]
    if not con_dem.empty:
        cobertura_avg = float(con_dem["cobertura_actual_meses"].clip(upper=24).mean())
        alertas_quiebre = int((con_dem["cobertura_actual_meses"] < 0.25).sum())
        # Fill Rate = % de pares (SKU, sucursal) con cobertura ≥ 1 mes
        fill_rate = float((con_dem["cobertura_actual_meses"] >= 1.0).sum()) / len(con_dem) * 100

filas_ultimo_run = int(
    hist[hist["run_id"] == latest.run_id].pipe(_filter_sucursal).shape[0]
) if not hist.empty else 0

col1.metric("💰 Ahorro en el período", fmt_clp(ahorro_periodo))
col2.metric("📊 Cobertura promedio (meses)", f"{cobertura_avg:.1f}")
col3.metric("⚠️ Alertas de quiebre", fmt_int(alertas_quiebre))
col4.metric("✅ Fill Rate", f"{fill_rate:.1f}%")
col5.metric("📋 Filas del último plan", fmt_int(filas_ultimo_run))

st.divider()

# ============================================================================
# Tabs
# ============================================================================
tab_ahorro, tab_stock, tab_forecast, tab_bodega, tab_ofertas, tab_historial = st.tabs([
    "💰 Ahorro", "📦 Stock valorizado", "🔮 Forecast vs Real",
    "🏭 KPIs Bodega", "🏷️ Ofertas & Remates", "📅 Historial Semanal",
])

# ---------------------------------------------------------------------------
# TAB 1 — AHORRO
# ---------------------------------------------------------------------------
with tab_ahorro:
    if hist_f.empty:
        st.info("No hay datos de ahorro en el período seleccionado.")
    else:
        # Serie temporal diaria
        daily = (
            hist_f.groupby("fecha_dia", as_index=False)["ahorro_clp"].sum()
            .sort_values("fecha_dia")
        )
        fig_line = px.line(
            daily, x="fecha_dia", y="ahorro_clp",
            markers=True, title="Ahorro diario (CLP)",
            labels={"fecha_dia": "Fecha", "ahorro_clp": "Ahorro CLP"},
        )
        fig_line.update_traces(line=dict(width=3))
        st.plotly_chart(fig_line, use_container_width=True)

        colA, colB = st.columns(2)

        # Desglose por capa
        por_capa = (
            hist_f.groupby("capa", as_index=False)["ahorro_clp"].sum()
            .sort_values("ahorro_clp", ascending=True)
        )
        por_capa = por_capa[por_capa["capa"] != "compra"]  # compra = baseline = 0
        fig_capa = px.bar(
            por_capa, x="ahorro_clp", y="capa", orientation="h",
            title="Ahorro por capa (CLP)",
            labels={"ahorro_clp": "CLP", "capa": "Capa"},
            color="capa",
            color_discrete_map={
                "inmovilizado": "#2ca02c",
                "sobrestock": "#ff7f0e",
                "cd": "#1f77b4",
            },
        )
        colA.plotly_chart(fig_capa, use_container_width=True)

        # Top 20 SKUs con mayor ahorro
        top_sku = (
            hist_f[hist_f["capa"] != "compra"]
            .groupby("sku_id", as_index=False)
            .agg(ahorro=("ahorro_clp", "sum"), unidades=("cantidad", "sum"))
            .sort_values("ahorro", ascending=False)
            .head(20)
        )
        # Enriquecer con nombre y marca del catálogo
        if not productos.empty:
            top_sku = top_sku.merge(
                productos[["sku_id", "nombre", "marca"]], on="sku_id", how="left"
            )
            top_sku["nombre"] = top_sku["nombre"].fillna("").astype(str)
            top_sku["marca"] = top_sku["marca"].fillna("").astype(str)
            top_sku["producto"] = top_sku.apply(
                lambda r: f"{r['nombre']} ({r['marca']})" if r["marca"] else r["nombre"],
                axis=1,
            )
        else:
            top_sku["producto"] = ""

        top_sku["ahorro_fmt"] = top_sku["ahorro"].map(fmt_clp)
        top_sku["unidades_fmt"] = top_sku["unidades"].map(fmt_int)
        colB.markdown("**Top 20 SKUs con mayor ahorro**")
        colB.dataframe(
            top_sku[["sku_id", "producto", "unidades_fmt", "ahorro_fmt"]].rename(
                columns={"unidades_fmt": "unidades", "ahorro_fmt": "ahorro_clp"}
            ),
            use_container_width=True, hide_index=True,
        )

        # Desglose por clase ABC/XYZ — el impacto de mover un AX es muy distinto
        # al de un CZ, así que conviene verlo separado.
        if "clase_abc_xyz" in hist_f.columns:
            st.markdown("#### Ahorro por clase ABC/XYZ")
            no_compra = hist_f[hist_f["capa"] != "compra"].copy()
            no_compra["clase_abc_xyz"] = no_compra["clase_abc_xyz"].fillna("N/A").astype(str)

            por_clase = (
                no_compra.groupby("clase_abc_xyz", as_index=False)
                .agg(ahorro=("ahorro_clp", "sum"), unidades=("cantidad", "sum"))
                .sort_values("ahorro", ascending=False)
            )

            # Matriz 3×3 (ABC × XYZ) con el ahorro como heatmap — lectura rápida.
            matrix = no_compra.copy()
            matrix = matrix[matrix["clase_abc_xyz"].str.match(r"^[ABC][XYZ]$")]
            if not matrix.empty:
                matrix["abc"] = matrix["clase_abc_xyz"].str[0]
                matrix["xyz"] = matrix["clase_abc_xyz"].str[1]
                heat = (
                    matrix.groupby(["abc", "xyz"], as_index=False)["ahorro_clp"].sum()
                    .pivot(index="abc", columns="xyz", values="ahorro_clp")
                    .reindex(index=["A", "B", "C"], columns=["X", "Y", "Z"])
                    .fillna(0.0)
                )
                colX, colY = st.columns([2, 3])
                fig_heat = px.imshow(
                    heat, text_auto=".2s", aspect="auto",
                    color_continuous_scale="Greens",
                    labels={"x": "XYZ", "y": "ABC", "color": "Ahorro CLP"},
                    title="Ahorro por combinación ABC × XYZ (CLP)",
                )
                colX.plotly_chart(fig_heat, use_container_width=True)

                por_clase_view = por_clase.copy()
                por_clase_view["ahorro"] = por_clase_view["ahorro"].map(fmt_clp)
                por_clase_view["unidades"] = por_clase_view["unidades"].map(fmt_int)
                por_clase_view.columns = ["Clase", "Ahorro CLP", "Unidades"]
                colY.markdown("**Detalle por clase**")
                colY.dataframe(por_clase_view, use_container_width=True, hide_index=True)
            else:
                st.info(
                    "Los datos de este período no tienen clasificación ABC/XYZ asociada "
                    "(runs previos a la incorporación del feature)."
                )

        # --- Waterfall de reposición ---
        st.markdown("#### Cascada de reposición — origen de las unidades")
        CAPA_ORDER = ["inmovilizado", "sobrestock", "cd", "compra"]
        CAPA_COLOR = {
            "inmovilizado": "#2ca02c",
            "sobrestock":   "#ff7f0e",
            "cd":           "#1f77b4",
            "compra":       "#d62728",
        }
        wf_data = (
            hist_f.groupby("capa", as_index=False)["cantidad"].sum()
        )
        wf_data = wf_data[wf_data["capa"].isin(CAPA_ORDER)].set_index("capa")
        wf_rows = [wf_data.loc[c, "cantidad"] if c in wf_data.index else 0.0
                   for c in CAPA_ORDER]
        total_units = sum(wf_rows)

        fig_wf = go.Figure(go.Waterfall(
            orientation="v",
            measure=["relative"] * len(CAPA_ORDER) + ["total"],
            x=CAPA_ORDER + ["TOTAL"],
            y=wf_rows + [None],
            text=[fmt_int(v) for v in wf_rows] + [fmt_int(total_units)],
            textposition="outside",
            connector=dict(line=dict(color="rgb(63,63,63)", width=1)),
            increasing=dict(marker=dict(color="#2ca02c")),
            decreasing=dict(marker=dict(color="#d62728")),
            totals=dict(marker=dict(color="#7f7f7f")),
        ))
        fig_wf.update_layout(
            title="Unidades repuestas por fuente (inmovilizado → sobrestock → CD → compra)",
            yaxis_title="Unidades",
            showlegend=False,
        )
        st.plotly_chart(fig_wf, use_container_width=True)
        st.caption(
            "🟢 Inmovilizado y sobrestock = stock ocioso reactivado. "
            "🔵 CD = traslado desde centro de distribución. "
            "🔴 Compra = orden externa (sin ahorro)."
        )

# ---------------------------------------------------------------------------
# TAB 2 — STOCK VALORIZADO POR SUCURSAL
# ---------------------------------------------------------------------------
with tab_stock:
    if needs_f.empty:
        st.info("Sin datos de necesidades en el último run.")
    else:
        s = needs_f.copy()
        s["stock_actual"] = pd.to_numeric(s["stock_actual"], errors="coerce").fillna(0.0)
        s["demanda_esperada"] = pd.to_numeric(s["demanda_esperada"], errors="coerce").fillna(0.0)
        s["safety_stock"] = pd.to_numeric(s["safety_stock"], errors="coerce").fillna(0.0)
        s["cobertura_actual_meses"] = pd.to_numeric(
            s["cobertura_actual_meses"], errors="coerce"
        ).fillna(0.0)

        # ── PMP por sucursal (Stock Valorizado) ──────────────────────────
        pmp = load_pmp_por_sucursal()
        if not pmp.empty:
            s = s.merge(
                pmp[["sku_id", "ubicacion", "costo_pmp"]],
                on=["sku_id", "ubicacion"], how="left",
            )
            s["costo_pmp"] = pd.to_numeric(s["costo_pmp"], errors="coerce").fillna(0.0)
        else:
            s["costo_pmp"] = 0.0

        # Fallback: si no hay PMP para un (sku, ubicacion), usar costo global de proveedores
        s = s.merge(costos[["sku_id", "costo_unitario_clp"]], on="sku_id", how="left")
        s["costo_unitario_clp"] = pd.to_numeric(s["costo_unitario_clp"], errors="coerce").fillna(0.0)
        # costo final = PMP si > 0, sino costo global
        s["costo_final"] = s["costo_pmp"].where(s["costo_pmp"] > 0, s["costo_unitario_clp"])

        # ── Días desde última venta (desde MariaDB) ──────────────────────
        uv_raw = load_ultima_venta()
        if not uv_raw.empty:
            s = s.merge(
                uv_raw[["sku_id", "ubicacion", "dias_desde_ultima_venta"]],
                on=["sku_id", "ubicacion"], how="left",
            )
            s["dias_desde_ultima_venta"] = pd.to_numeric(
                s["dias_desde_ultima_venta"], errors="coerce"
            )
            # Sin venta jamás registrada → marcamos como inmovilizado seguro (>90)
            s["dias_desde_ultima_venta"] = s["dias_desde_ultima_venta"].fillna(9999)
            s["dias_desde_ultima_venta"] = s["dias_desde_ultima_venta"].astype(int)
            fuente_dias = "🛒 MariaDB · `reporte_ventas_completo` (última venta por sucursal)"
        else:
            # Fallback proxy: si no hay datos de venta, usamos cobertura como aproximación
            # (productos sin demanda esperada → 9999 días, productos con cobertura alta → muchos días)
            s["dias_desde_ultima_venta"] = 9999
            s.loc[s["demanda_esperada"] > 0, "dias_desde_ultima_venta"] = 0
            fuente_dias = "⚠️ Estimado (MariaDB no disponible)"

        # ── Avisos de cobertura ─────────────────────────────────────────
        total_rows = len(s)
        sin_costo = (s["costo_final"] == 0).sum()
        sin_pmp   = (s["costo_pmp"] == 0).sum()
        sin_venta = (s["dias_desde_ultima_venta"] == 9999).sum()
        if sin_costo > 0:
            st.warning(
                f"⚠️ **{sin_costo:,} SKU×sucursal ({sin_costo/total_rows*100:.1f}%)** "
                f"no tienen costo (ni PMP ni proveedores) — figuran como $0."
            )
        if not pmp.empty and sin_pmp > 0:
            st.caption(
                f"ℹ️ {sin_pmp:,} pares sin PMP — fallback a costo global de "
                f"`proveedores.xlsx`. {total_rows - sin_pmp:,} con PMP por sucursal "
                f"(`stock_valorizado.xlsx`)."
            )
        if sin_venta > 0:
            st.caption(
                f"📭 {sin_venta:,} pares sin venta histórica registrada → "
                f"clasificados como **Inmovilizado** por defecto."
            )

        # ── Criterios solicitados ────────────────────────────────────────
        # Inmovilizado : > 90 días desde última venta + stock > 0
        # Sobrestock   : cobertura ≥ 2m + última venta hace < 90 días + stock > 0
        # Normal       : el resto con stock > 0
        dias = s["dias_desde_ultima_venta"]
        cob  = s["cobertura_actual_meses"]
        s["u_inmovilizado"] = 0.0
        s["u_sobrestock"]   = 0.0
        s["u_normal"]       = 0.0

        mask_inmov  = (dias > 90) & (s["stock_actual"] > 0)
        mask_over   = (cob >= 2) & (dias < 90) & (s["stock_actual"] > 0)
        mask_normal = (~mask_inmov & ~mask_over) & (s["stock_actual"] > 0)

        s.loc[mask_inmov,  "u_inmovilizado"] = s.loc[mask_inmov,  "stock_actual"]
        s.loc[mask_over,   "u_sobrestock"]   = s.loc[mask_over,   "stock_actual"]
        s.loc[mask_normal, "u_normal"]       = s.loc[mask_normal, "stock_actual"]

        for cat in ("inmovilizado", "sobrestock", "normal"):
            s[f"clp_{cat}"] = s[f"u_{cat}"] * s["costo_final"]

        pivot = (
            s.groupby("ubicacion", as_index=False)[
                ["clp_inmovilizado", "clp_sobrestock", "clp_normal"]
            ].sum()
        )
        pivot["total"] = pivot[["clp_inmovilizado", "clp_sobrestock", "clp_normal"]].sum(axis=1)
        pivot = pivot.sort_values("total", ascending=False)

        fig = px.bar(
            pivot, y="ubicacion",
            x=["clp_inmovilizado", "clp_sobrestock", "clp_normal"],
            orientation="h",
            title="Stock valorizado por sucursal (CLP, valuado al PMP) — Inmovilizado: última venta >90d · Sobrestock: cob.≥2m + última venta <90d",
            labels={"value": "CLP", "variable": "Categoría", "ubicacion": "Sucursal",
                    "clp_inmovilizado": "Inmovilizado (última venta >90d)",
                    "clp_sobrestock":   "Sobrestock (cob.≥2m, última venta <90d)",
                    "clp_normal":       "Normal"},
            color_discrete_map={
                "clp_inmovilizado": "#d62728",
                "clp_sobrestock":   "#ff7f0e",
                "clp_normal":       "#2ca02c",
            },
        )
        fig.update_layout(legend_title_text="Categoría")
        st.plotly_chart(fig, use_container_width=True)

        # Totales globales como métricas
        mc1, mc2, mc3, mc4 = st.columns(4)
        mc1.metric("🔴 Inmovilizado (venta >90d)",  fmt_clp(pivot["clp_inmovilizado"].sum()))
        mc2.metric("🟠 Sobrestock (≥2m + venta <90d)", fmt_clp(pivot["clp_sobrestock"].sum()))
        mc3.metric("🟢 Normal",                     fmt_clp(pivot["clp_normal"].sum()))
        mc4.metric("📦 Total valorizado",           fmt_clp(pivot["total"].sum()))
        st.caption(
            f"💎 Valuación al **PMP** por sucursal (`stock_valorizado.xlsx`). "
            f"Métrica de movimiento: {fuente_dias}."
        )
        st.markdown("---")

        # Tabla numérica
        fmt_df = pivot.copy()
        for c in ["clp_inmovilizado", "clp_sobrestock", "clp_normal", "total"]:
            fmt_df[c] = fmt_df[c].map(fmt_clp)
        fmt_df.columns = ["Sucursal", "Inmovilizado (venta >90d)", "Sobrestock (≥2m, venta <90d)", "Normal", "Total"]
        st.dataframe(fmt_df, use_container_width=True, hide_index=True)

# ---------------------------------------------------------------------------
# TAB 3 — FORECAST VS REAL
# ---------------------------------------------------------------------------
with tab_forecast:
    st.markdown(
        "Compara el **forecast emitido** (Excel v9) contra la **demanda real** "
        "registrada en MariaDB, para los meses que ya cerraron."
    )

    fc = load_latest_audit_sheet("forecast_final")
    if fc.empty:
        st.info("Sin forecast histórico disponible.")
    else:
        fc["mes"] = pd.to_datetime(fc["mes"])
        fc_past = fc[fc["mes"] <= latest.fecha.normalize().replace(day=1)]  # solo meses cerrados

        if fc_past.empty:
            st.info(
                "El forecast disponible (Excel v9) está a futuro. "
                "Una vez que cierren los meses, aparecerá aquí el comparativo."
            )
        else:
            real = load_demanda_real(latest.fecha.strftime("%Y-%m-%d"))
            if real.empty:
                st.warning("No se pudo leer demanda real. Verificá la conexión a MariaDB.")
            else:
                fc_past = fc_past.copy()
                fc_past["sku_id"] = fc_past["sku_id"].astype(str)
                real["sku_id"] = real["sku_id"].astype(str)
                comp = fc_past[["sku_id", "ubicacion", "mes", "forecast_final"]].merge(
                    real, on=["sku_id", "ubicacion", "mes"], how="inner",
                )
                if sucursal_sel:
                    comp = comp[comp["ubicacion"].isin(sucursal_sel)]

                if comp.empty:
                    st.info("No hay intersección entre forecast y demanda real para el filtro actual.")
                else:
                    # Agregado mensual global
                    monthly = comp.groupby("mes", as_index=False).agg(
                        forecast=("forecast_final", "sum"),
                        real=("demanda", "sum"),
                    ).sort_values("mes")
                    monthly["error_abs"] = (monthly["forecast"] - monthly["real"]).abs()
                    monthly["mape"] = (monthly["error_abs"] / monthly["real"].replace(0, pd.NA)) * 100

                    # --- Gráfico Forecast vs Real con banda de tolerancia ---
                    tolerancia_pct = st.slider(
                        "Banda de tolerancia ±%", min_value=5, max_value=50,
                        value=15, step=5,
                        help="Zona verde: el real cae dentro del forecast ± este porcentaje",
                    )
                    tol = tolerancia_pct / 100.0
                    monthly["banda_sup"] = monthly["forecast"] * (1 + tol)
                    monthly["banda_inf"] = monthly["forecast"] * (1 - tol)
                    monthly["mes_str"] = monthly["mes"].dt.strftime("%b %Y")

                    fig_band = go.Figure()

                    # Banda superior (invisible, solo sirve de techo para el fill)
                    fig_band.add_trace(go.Scatter(
                        x=monthly["mes_str"], y=monthly["banda_sup"],
                        mode="lines", line=dict(width=0),
                        name=f"Límite +{tolerancia_pct}%",
                        showlegend=False,
                    ))
                    # Banda inferior — relleno hasta la superior
                    fig_band.add_trace(go.Scatter(
                        x=monthly["mes_str"], y=monthly["banda_inf"],
                        mode="lines", line=dict(width=0),
                        fill="tonexty",
                        fillcolor="rgba(144,238,144,0.35)",
                        name=f"Zona segura ±{tolerancia_pct}%",
                    ))
                    # Línea forecast (punteada)
                    fig_band.add_trace(go.Scatter(
                        x=monthly["mes_str"], y=monthly["forecast"],
                        mode="lines", name="Forecast",
                        line=dict(color="#1f77b4", width=2, dash="dot"),
                    ))
                    # Línea real (sólida con marcadores)
                    fig_band.add_trace(go.Scatter(
                        x=monthly["mes_str"], y=monthly["real"],
                        mode="lines+markers", name="Real",
                        line=dict(color="#2ca02c", width=3),
                        marker=dict(size=8, symbol="diamond"),
                    ))

                    fig_band.update_layout(
                        title="Forecast vs Real — con zona de tolerancia",
                        xaxis_title="Mes",
                        yaxis_title="Unidades",
                        legend=dict(orientation="h", yanchor="bottom", y=1.02),
                        hovermode="x unified",
                    )
                    st.plotly_chart(fig_band, use_container_width=True)

                    # --- KPIs globales: WMAPE, Bias, Fill Rate (gauge) ---
                    total_real = comp["demanda"].sum()
                    total_fc   = comp["forecast_final"].sum()
                    wmape_global = (
                        (comp["forecast_final"] - comp["demanda"]).abs().sum()
                        / total_real * 100
                    ) if total_real > 0 else 0.0
                    bias_global = float(
                        (comp["demanda"] - comp["forecast_final"]).sum()
                        / len(monthly)
                    ) if len(monthly) > 0 else 0.0
                    bias_pct = bias_global / (total_real / len(monthly)) * 100 if total_real > 0 else 0.0

                    gA, gB, gC = st.columns(3)

                    # Rango dinámico: extender si el valor real supera el máximo fijo
                    wmape_max = max(100.0, round(wmape_global * 1.2 / 10) * 10)
                    bias_abs  = max(50.0,  round(abs(bias_pct)  * 1.3 / 10) * 10)

                    # Gauge WMAPE
                    fig_gauge_wmape = go.Figure(go.Indicator(
                        mode="gauge+number",
                        value=round(wmape_global, 1),
                        number={"suffix": "%", "valueformat": ".1f",
                                "font": {"size": 36}},
                        title={"text": "WMAPE global", "font": {"size": 14}},
                        gauge={
                            "axis": {"range": [0, wmape_max],
                                     "ticksuffix": "%", "tickfont": {"size": 11}},
                            "bar":  {"color": "#1f77b4", "thickness": 0.25},
                            "bgcolor": "white",
                            "borderwidth": 0,
                            "steps": [
                                {"range": [0, wmape_max * 0.20], "color": "#c8e6c9"},
                                {"range": [wmape_max * 0.20, wmape_max * 0.45], "color": "#fff9c4"},
                                {"range": [wmape_max * 0.45, wmape_max], "color": "#ffcdd2"},
                            ],
                            "threshold": {"line": {"color": "#e53935", "width": 3},
                                          "thickness": 0.80,
                                          "value": min(15, wmape_max * 0.20)},
                        },
                    ))
                    fig_gauge_wmape.update_layout(
                        height=280, margin=dict(t=50, b=10, l=30, r=30))
                    gA.plotly_chart(fig_gauge_wmape, use_container_width=True)
                    gA.caption("🟢 < 15% excelente · 🟡 15–45% mejorable · 🔴 > 45% crítico")

                    # Gauge Forecast Bias
                    bias_disp = round(bias_pct, 1)
                    fig_gauge_bias = go.Figure(go.Indicator(
                        mode="gauge+number",
                        value=bias_disp,
                        number={"suffix": "%", "valueformat": ".1f",
                                "font": {"size": 36}},
                        title={"text": "Bias % (Real − Forecast)", "font": {"size": 14}},
                        gauge={
                            "axis": {"range": [-bias_abs, bias_abs],
                                     "ticksuffix": "%", "tickfont": {"size": 11}},
                            "bar":  {"color": "#ff7f0e", "thickness": 0.25},
                            "bgcolor": "white",
                            "borderwidth": 0,
                            "steps": [
                                {"range": [-bias_abs, -bias_abs * 0.25], "color": "#ffcdd2"},
                                {"range": [-bias_abs * 0.25, bias_abs * 0.25], "color": "#c8e6c9"},
                                {"range": [bias_abs * 0.25, bias_abs], "color": "#fff9c4"},
                            ],
                            "threshold": {"line": {"color": "#333", "width": 2},
                                          "thickness": 0.80, "value": 0},
                        },
                    ))
                    fig_gauge_bias.update_layout(
                        height=280, margin=dict(t=50, b=10, l=30, r=30))
                    gB.plotly_chart(fig_gauge_bias, use_container_width=True)
                    gB.caption("🟢 Cercano a 0 = sin sesgo · ➕ Subestimamos · ➖ Sobreestimamos")

                    # Gauge Fill Rate
                    fig_gauge_fr = go.Figure(go.Indicator(
                        mode="gauge+number",
                        value=round(fill_rate, 1),
                        number={"suffix": "%", "valueformat": ".1f",
                                "font": {"size": 36}},
                        title={"text": "Fill Rate (cobertura ≥ 1 mes)", "font": {"size": 14}},
                        gauge={
                            "axis": {"range": [0, 100],
                                     "ticksuffix": "%", "tickfont": {"size": 11}},
                            "bar":  {"color": "#2ca02c", "thickness": 0.25},
                            "bgcolor": "white",
                            "borderwidth": 0,
                            "steps": [
                                {"range": [0,  70], "color": "#ffcdd2"},
                                {"range": [70, 85], "color": "#fff9c4"},
                                {"range": [85, 100], "color": "#c8e6c9"},
                            ],
                            "threshold": {"line": {"color": "#2ca02c", "width": 3},
                                          "thickness": 0.80, "value": 85},
                        },
                    ))
                    fig_gauge_fr.update_layout(
                        height=280, margin=dict(t=50, b=10, l=30, r=30))
                    gC.plotly_chart(fig_gauge_fr, use_container_width=True)
                    gC.caption("🟢 > 85% bien abastecido · 🟡 70–85% riesgo · 🔴 < 70% crítico")

                    # --- Tabla mensual con WMAPE y Bias por mes ---
                    monthly["wmape"] = (
                        monthly["error_abs"] / monthly["real"].replace(0, pd.NA) * 100
                    )
                    monthly["bias_u"] = monthly["real"] - monthly["forecast"]

                    monthly_view = monthly[["mes", "forecast", "real",
                                            "error_abs", "mape", "wmape", "bias_u"]].copy()
                    monthly_view["mes"]       = monthly_view["mes"].dt.strftime("%Y-%m")
                    monthly_view["forecast"]  = monthly_view["forecast"].map(fmt_int)
                    monthly_view["real"]      = monthly_view["real"].map(fmt_int)
                    monthly_view["error_abs"] = monthly_view["error_abs"].map(fmt_int)
                    monthly_view["mape"]      = monthly_view["mape"].map(
                        lambda x: f"{x:.1f}%" if pd.notna(x) else "—")
                    monthly_view["wmape"]     = monthly_view["wmape"].map(
                        lambda x: f"{x:.1f}%" if pd.notna(x) else "—")
                    monthly_view["bias_u"]    = monthly_view["bias_u"].map(fmt_int)
                    monthly_view.columns = ["Mes", "Forecast", "Real",
                                            "Error abs.", "MAPE", "WMAPE", "Bias (u)"]
                    st.dataframe(monthly_view, use_container_width=True, hide_index=True)

                    # --- Bias por mes: bar chart (+ sobreestima / - subestima) ---
                    monthly["bias_pct_mes"] = (
                        (monthly["real"] - monthly["forecast"])
                        / monthly["real"].replace(0, pd.NA) * 100
                    ).fillna(0)
                    monthly["mes_str2"] = monthly["mes"].dt.strftime("%b %Y")
                    fig_bias = px.bar(
                        monthly, x="mes_str2", y="bias_pct_mes",
                        title="Bias mensual % (Real − Forecast) / Real",
                        labels={"mes_str2": "Mes", "bias_pct_mes": "Bias %"},
                        color="bias_pct_mes",
                        color_continuous_scale="RdYlGn",
                        color_continuous_midpoint=0,
                    )
                    fig_bias.add_hline(y=0, line_dash="dot", line_color="black")
                    st.plotly_chart(fig_bias, use_container_width=True)
                    st.caption(
                        "🟢 Positivo = real > forecast (subestimamos, riesgo de quiebre). "
                        "🔴 Negativo = real < forecast (sobreestimamos, riesgo de sobrestock)."
                    )

                    # --- WMAPE por sucursal ---
                    per_suc = comp.groupby("ubicacion", as_index=False).agg(
                        forecast=("forecast_final", "sum"),
                        real=("demanda", "sum"),
                        error_abs=("forecast_final", lambda x:
                            (x - comp.loc[x.index, "demanda"]).abs().sum()),
                    )
                    per_suc["wmape"] = (
                        per_suc["error_abs"] / per_suc["real"].replace(0, pd.NA) * 100
                    ).fillna(0)
                    per_suc = per_suc.sort_values("wmape", ascending=False)
                    fig_wmape = px.bar(
                        per_suc, x="ubicacion", y="wmape",
                        title="WMAPE por sucursal (%)",
                        labels={"wmape": "WMAPE %", "ubicacion": "Sucursal"},
                        color="wmape", color_continuous_scale="Reds",
                    )
                    fig_wmape.add_hline(y=15, line_dash="dash", line_color="orange",
                                        annotation_text="Target 15%")
                    st.plotly_chart(fig_wmape, use_container_width=True)

# ---------------------------------------------------------------------------
# TAB 4 — KPIs BODEGA
# ---------------------------------------------------------------------------
with tab_bodega:
    if needs_f.empty:
        st.info("Sin datos de necesidades en el último run.")
    else:
        n = needs_f.copy()
        n["cobertura_actual_meses"] = pd.to_numeric(
            n["cobertura_actual_meses"], errors="coerce"
        ).fillna(0.0).clip(upper=36)
        n["stock_actual"] = pd.to_numeric(n["stock_actual"], errors="coerce").fillna(0.0)
        n["demanda_esperada"] = pd.to_numeric(n["demanda_esperada"], errors="coerce").fillna(0.0)

        # --- Semáforo por sucursal ---
        st.markdown("### 🚦 Estado por sucursal")
        con_dem_n = n[n["demanda_esperada"] > 0]
        if not con_dem_n.empty:
            suc_kpi = con_dem_n.groupby("ubicacion").agg(
                cobertura=("cobertura_actual_meses", "mean"),
                quiebres=("cobertura_actual_meses", lambda x: int((x < 0.25).sum())),
                total_sku=("sku_id", "count"),
            ).reset_index()
            suc_kpi["pct_quiebre"] = (
                suc_kpi["quiebres"] / suc_kpi["total_sku"] * 100
            ).round(1)
            suc_kpi["cobertura"] = suc_kpi["cobertura"].round(1)

            def _semaforo(row: pd.Series) -> str:
                if row["cobertura"] < 1.0 or row["pct_quiebre"] > 10:
                    return "🔴 Crítico"
                if row["cobertura"] < 2.0 or row["pct_quiebre"] > 5:
                    return "🟡 Atención"
                return "🟢 OK"

            suc_kpi["estado"] = suc_kpi.apply(_semaforo, axis=1)
            suc_kpi = suc_kpi.sort_values("cobertura")
            suc_kpi_view = suc_kpi[
                ["estado", "ubicacion", "cobertura", "quiebres", "pct_quiebre", "total_sku"]
            ].rename(columns={
                "estado": "Estado",
                "ubicacion": "Sucursal",
                "cobertura": "Cobertura prom. (m)",
                "quiebres": "SKUs en quiebre",
                "pct_quiebre": "% quiebre",
                "total_sku": "Total SKUs",
            })
            st.dataframe(suc_kpi_view, use_container_width=True, hide_index=True)
            n_rojo   = int((suc_kpi["estado"] == "🔴 Crítico").sum())
            n_amarillo = int((suc_kpi["estado"] == "🟡 Atención").sum())
            n_verde  = int((suc_kpi["estado"] == "🟢 OK").sum())
            st.caption(
                f"🔴 Crítico: **{n_rojo}** sucursales · "
                f"🟡 Atención: **{n_amarillo}** · "
                f"🟢 OK: **{n_verde}**  — "
                "Criterio: 🔴 cobertura < 1m o >10% quiebres · "
                "🟡 cobertura < 2m o >5% quiebres"
            )
        st.divider()

        colA, colB = st.columns(2)

        # Cobertura promedio por sucursal
        cob_suc = (
            n[n["demanda_esperada"] > 0]
            .groupby("ubicacion", as_index=False)["cobertura_actual_meses"].mean()
            .sort_values("cobertura_actual_meses")
        )
        fig_cob = px.bar(
            cob_suc, y="ubicacion", x="cobertura_actual_meses",
            orientation="h", title="Cobertura promedio por sucursal (meses)",
            labels={"cobertura_actual_meses": "Meses", "ubicacion": "Sucursal"},
            color="cobertura_actual_meses", color_continuous_scale="RdYlGn",
        )
        colA.plotly_chart(fig_cob, use_container_width=True)

        # Rotación: unidades demanda / stock promedio.
        # Proxy: demanda_esperada (12 meses) / stock_actual. Alta = rota bien.
        rot = n.copy()
        rot = rot[(rot["stock_actual"] > 0) & (rot["demanda_esperada"] > 0)]
        rot["rotacion"] = rot["demanda_esperada"] / rot["stock_actual"]
        rot_suc = (
            rot.groupby("ubicacion", as_index=False)["rotacion"].mean()
            .sort_values("rotacion", ascending=False)
        )
        fig_rot = px.bar(
            rot_suc, y="ubicacion", x="rotacion",
            orientation="h", title="Rotación promedio por sucursal (12m)",
            labels={"rotacion": "Veces/año (proxy)", "ubicacion": "Sucursal"},
            color="rotacion", color_continuous_scale="Blues",
        )
        colB.plotly_chart(fig_rot, use_container_width=True)

        # Alertas de quiebre detalladas
        st.markdown("### ⚠️ SKUs en riesgo de quiebre (cobertura < 0.25 meses)")
        quiebre = n[
            (n["demanda_esperada"] > 0) & (n["cobertura_actual_meses"] < 0.25)
        ].copy()
        if quiebre.empty:
            st.success("Sin alertas de quiebre con los filtros actuales.")
        else:
            quiebre = quiebre.sort_values(
                ["ubicacion", "demanda_esperada"], ascending=[True, False]
            )
            quiebre_view = quiebre[[
                "sku_id", "ubicacion", "clase_final", "stock_actual",
                "demanda_esperada", "cobertura_actual_meses",
            ]].head(200)
            quiebre_view["cobertura_actual_meses"] = quiebre_view["cobertura_actual_meses"].round(2)
            st.dataframe(quiebre_view, use_container_width=True, hide_index=True)
            st.caption(f"Mostrando hasta 200 filas. Total en alerta: {len(quiebre):,}".replace(",", "."))

        # Distribución por clase (ABC/XYZ)
        st.markdown("### Distribución de SKUs por clase final")
        clase = n.groupby("clase_final", as_index=False).size().rename(columns={"size": "n"})
        fig_clase = px.bar(
            clase.sort_values("n", ascending=False),
            x="clase_final", y="n", title="Cantidad de pares (SKU, sucursal) por clase",
            labels={"clase_final": "Clase", "n": "Cantidad"},
            color_discrete_sequence=["#1A52D6"],
        )
        st.plotly_chart(fig_clase, use_container_width=True)

        # --- Fuente de clasificación ABC/XYZ ---
        with st.expander("🔬 Fuente de clasificación ABC/XYZ (externo vs. modelo)", expanded=False):
            cls_df = load_latest_audit_sheet("classification_final")
            if cls_df.empty or "clase_fue_forzada" not in cls_df.columns:
                st.info("No hay datos de clasificación en el run actual.")
            else:
                n_total = len(cls_df)
                n_ext   = int(cls_df["clase_fue_forzada"].sum())
                n_model = n_total - n_ext

                kc1, kc2, kc3 = st.columns(3)
                kc1.metric("Total pares (SKU × sucursal)", f"{n_total:,}".replace(",", "."))
                kc2.metric(
                    "Desde archivo externo ABC/XYZ",
                    f"{n_ext:,}".replace(",", "."),
                    f"{n_ext / n_total * 100:.1f}% del total",
                )
                kc3.metric(
                    "Solo por modelo pipeline",
                    f"{n_model:,}".replace(",", "."),
                    f"{n_model / n_total * 100:.1f}% del total",
                )

                # Cuántos realmente cambiaron de clase
                cambiados = cls_df[
                    cls_df["clase_fue_forzada"]
                    & (cls_df["clase_modelo"].astype(str) != cls_df["clase_final"].astype(str))
                ].copy()
                if not cambiados.empty:
                    st.markdown(
                        f"**{len(cambiados):,}** pares cambiaron de clase respecto al modelo "
                        f"(el resto ya coincidía).".replace(",", ".")
                    )
                    cambio_dist = (
                        cambiados.groupby(["clase_modelo", "clase_final"])
                        .size()
                        .reset_index(name="n")
                        .sort_values("n", ascending=False)
                        .head(20)
                    )
                    cambio_dist.columns = ["Clase modelo", "Clase externo", "Cantidad"]
                    st.dataframe(cambio_dist, use_container_width=True, hide_index=True)
                else:
                    st.success("El archivo externo confirma todas las clases del modelo.")

                # Distribución externo vs. modelo por ABC
                col_pie1, col_pie2 = st.columns(2)
                dist_ext = (
                    cls_df[cls_df["clase_fue_forzada"]]
                    .groupby("clase_final").size().reset_index(name="n")
                )
                dist_mod = (
                    cls_df[~cls_df["clase_fue_forzada"]]
                    .groupby("clase_modelo").size().reset_index(name="n")
                    .rename(columns={"clase_modelo": "clase_final"})
                )
                if not dist_ext.empty:
                    fig_ext = px.bar(
                        dist_ext.sort_values("n", ascending=False),
                        x="clase_final", y="n",
                        title="Clases — archivo externo",
                        labels={"clase_final": "Clase", "n": "SKU×sucursal"},
                        color_discrete_sequence=["#F5821A"],
                    )
                    col_pie1.plotly_chart(fig_ext, use_container_width=True)
                if not dist_mod.empty:
                    fig_mod = px.bar(
                        dist_mod.sort_values("n", ascending=False),
                        x="clase_final", y="n",
                        title="Clases — solo modelo pipeline",
                        labels={"clase_final": "Clase", "n": "SKU×sucursal"},
                        color_discrete_sequence=["#1A52D6"],
                    )
                    col_pie2.plotly_chart(fig_mod, use_container_width=True)

                st.caption(
                    "Para actualizar con un nuevo archivo externo: "
                    "`python -m app.cli update-abc-xyz` → `python -m app.cli run`"
                )


# ---------------------------------------------------------------------------
# HELPER — clasificación por días en bodega
# ---------------------------------------------------------------------------
_RANGOS = [
    (0,   30,           "Sin acción",              0,  "#FFFFFF"),
    (31,  60,           "Promoción leve",          10, "#FFF9C4"),
    (61,  90,           "Oferta destacada",        25, "#FFE0B2"),
    (91,  float("inf"), "REMATE",                  40, "#FFCDD2"),
]

def _clasificar_dias(dias: float) -> tuple[str, int, str]:
    for lo, hi, label, dto, color in _RANGOS:
        if lo <= dias <= hi:
            return label, dto, color
    return "REMATE", 40, "#FFCDD2"

def _build_ofertas_df(needs_src: pd.DataFrame, costos_src: pd.DataFrame,
                       productos_src: pd.DataFrame,
                       stock_demanda_src: pd.DataFrame) -> pd.DataFrame:
    """Construye el DataFrame de ofertas uniendo todas las fuentes.

    Criterios alineados con tab_stock:
      - Inmovilizado: dias_desde_ultima_venta > 90 + stock > 0
      - Sobrestock  : cobertura ≥ 2m + dias_desde_ultima_venta < 90 + stock > 0
      - Valuación   : PMP por sucursal (fallback a costo global de proveedores)
    """
    df = needs_src.copy()
    df["stock_actual"]    = pd.to_numeric(df.get("stock_actual", 0),    errors="coerce").fillna(0)
    df["demanda_esperada"]= pd.to_numeric(df.get("demanda_esperada", 0),errors="coerce").fillna(0)
    df["cobertura_actual_meses"] = pd.to_numeric(
        df.get("cobertura_actual_meses", 0), errors="coerce"
    ).fillna(0).clip(upper=120)

    # Días desde última venta (MariaDB) — métrica principal para inmov/sobrestock
    uv = load_ultima_venta()
    if not uv.empty:
        uv_sub = uv[["sku_id", "ubicacion", "dias_desde_ultima_venta"]].copy()
        uv_sub["sku_id"]    = uv_sub["sku_id"].astype(str)
        uv_sub["ubicacion"] = uv_sub["ubicacion"].astype(str)
        df = df.merge(uv_sub, on=["sku_id", "ubicacion"], how="left")
        df["dias_desde_ultima_venta"] = pd.to_numeric(
            df["dias_desde_ultima_venta"], errors="coerce"
        ).fillna(9999).astype(int)
        df["fuente_dias"] = "Última venta (MariaDB)"
    else:
        # Fallback a stock_demanda (días desde último ingreso)
        if not stock_demanda_src.empty and "dias_en_bodega" in stock_demanda_src.columns:
            sd = stock_demanda_src[["sku_id", "ubicacion", "dias_en_bodega"]].copy()
            sd["sku_id"]    = sd["sku_id"].astype(str)
            sd["ubicacion"] = sd["ubicacion"].astype(str)
            df = df.merge(sd, on=["sku_id", "ubicacion"], how="left")
            df["dias_desde_ultima_venta"] = pd.to_numeric(
                df["dias_en_bodega"], errors="coerce"
            ).fillna(df["cobertura_actual_meses"] * 30).round(0).astype(int)
            df["fuente_dias"] = "Stock Demanda (fallback)"
        else:
            df["dias_desde_ultima_venta"] = (df["cobertura_actual_meses"] * 30).round(0).astype(int)
            df["fuente_dias"] = "Estimado (cobertura)"

    # Para presentación mantenemos columna dias_en_bodega con la misma métrica
    df["dias_en_bodega"] = df["dias_desde_ultima_venta"]

    # Filtrar: inmovilizados (venta >90d) + sobrestock (cob.≥2m, venta <90d)
    mask_inmov = (df["dias_desde_ultima_venta"] > 90) & (df["stock_actual"] > 0)
    mask_over  = (df["cobertura_actual_meses"] >= 2) & (df["dias_desde_ultima_venta"] < 90) & (df["stock_actual"] > 0)
    df = df[mask_inmov | mask_over].copy().reset_index(drop=True)

    if df.empty:
        return df

    # Clasificación por días → estrategia y descuento
    clasif = df["dias_desde_ultima_venta"].apply(_clasificar_dias)
    df["estrategia"]    = clasif.apply(lambda x: x[0])
    df["descuento_pct"] = clasif.apply(lambda x: x[1])
    df["color"]         = clasif.apply(lambda x: x[2])

    # ── Costo: PMP por sucursal con fallback a costo global ──────────
    pmp_df = load_pmp_por_sucursal()
    if not pmp_df.empty:
        df = df.merge(
            pmp_df[["sku_id", "ubicacion", "costo_pmp"]],
            on=["sku_id", "ubicacion"], how="left",
        )
        df["costo_pmp"] = pd.to_numeric(df["costo_pmp"], errors="coerce").fillna(0)
    else:
        df["costo_pmp"] = 0.0

    df = df.merge(costos_src[["sku_id", "costo_unitario_clp"]], on="sku_id", how="left")
    df["costo_unitario_clp"] = pd.to_numeric(
        df["costo_unitario_clp"], errors="coerce"
    ).fillna(0)

    # Costo final: PMP si > 0, sino costo global
    df["costo_final"] = df["costo_pmp"].where(df["costo_pmp"] > 0, df["costo_unitario_clp"])
    df["valor_stock_clp"] = (df["stock_actual"] * df["costo_final"]).round(0)
    df["ahorro_potencial_clp"] = (
        df["valor_stock_clp"] * df["descuento_pct"] / 100
    ).round(0)

    # Join nombre
    if not productos_src.empty:
        df = df.merge(productos_src[["sku_id", "nombre", "marca"]], on="sku_id", how="left")
        df["nombre"] = df.get("nombre", pd.Series([""] * len(df))).fillna("").astype(str)
        df["marca"]  = df.get("marca",  pd.Series([""] * len(df))).fillna("").astype(str)
    else:
        df["nombre"] = ""
        df["marca"]  = ""

    return df.sort_values(["dias_en_bodega"], ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------------------
# TAB 5 — OFERTAS & REMATES
# ---------------------------------------------------------------------------
with tab_ofertas:
    sd_raw     = load_stock_demanda()
    df_of_full = _build_ofertas_df(needs, costos, productos, sd_raw)

    # Aplicar filtro de sucursal del sidebar
    df_of = _filter_sucursal(df_of_full, col="ubicacion") if not df_of_full.empty else df_of_full
    df_of = _filter_clase(df_of, col="clase_final")       if not df_of.empty else df_of

    # Sub-filtro de estrategia dentro del tab
    estrategias_disp = ["Promoción leve", "Oferta destacada", "REMATE"]
    estrategia_sel = st.multiselect(
        "Filtrar por estrategia", options=estrategias_disp,
        default=estrategias_disp, key="of_estrategia",
    )

    if not df_of.empty and estrategia_sel:
        df_of = df_of[df_of["estrategia"].isin(estrategia_sel + ["Sin acción"])]

    # Fuente de datos
    fuente_dias = df_of["fuente_dias"].iloc[0] if not df_of.empty and "fuente_dias" in df_of.columns else "—"
    if fuente_dias == "Estimado (cobertura)":
        st.info(
            "📌 **Días en bodega estimados** desde cobertura actual "
            "(no se pudo conectar a MariaDB o no hay datos de ingresos). "
            "Los inmovilizados se clasifican como REMATE (≥ 91 días)."
        )
    else:
        st.success(
            "✅ Días en bodega calculados desde **último ingreso a sucursal** en MariaDB "
            f"· {fmt_int(len(df_of_full))} registros cargados"
        )

    if df_of.empty or df_of[df_of["estrategia"] != "Sin acción"].empty:
        st.info("No hay productos con acción requerida para los filtros actuales.")
    else:
        df_activos = df_of[df_of["estrategia"] != "Sin acción"].copy()

        # ── KPIs ─────────────────────────────────────────────────────────────
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("🏷️ Productos con acción",   fmt_int(len(df_activos)))
        k2.metric("💸 Valor en riesgo (CLP)",   fmt_clp(df_activos["valor_stock_clp"].sum()))
        k3.metric("🔴 En REMATE",
                  fmt_int((df_activos["estrategia"] == "REMATE").sum()))
        k4.metric("📅 Días prom. en bodega",
                  f"{df_activos['dias_en_bodega'].mean():.0f} d")

        st.divider()

        # ── Gráficos ─────────────────────────────────────────────────────────
        col_g1, col_g2 = st.columns(2)

        # Donut por estrategia
        dist_est = df_activos.groupby("estrategia", as_index=False).agg(
            cantidad=("sku_id", "count"),
            valor=("valor_stock_clp", "sum"),
        )
        fig_donut = px.pie(
            dist_est, values="cantidad", names="estrategia",
            hole=0.55, title="Distribución por estrategia",
            color="estrategia",
            color_discrete_map={
                "Promoción leve":   "#FFF176",
                "Oferta destacada": "#FFB74D",
                "REMATE":           "#EF5350",
            },
        )
        fig_donut.update_traces(textinfo="label+percent")
        col_g1.plotly_chart(fig_donut, use_container_width=True)

        # Valor en riesgo por sucursal × estrategia
        pivot_suc = df_activos.groupby(
            ["ubicacion", "estrategia"], as_index=False
        )["valor_stock_clp"].sum().sort_values("valor_stock_clp", ascending=False)
        fig_suc = px.bar(
            pivot_suc, x="valor_stock_clp", y="ubicacion",
            color="estrategia", orientation="h",
            title="Valor en riesgo por sucursal (CLP)",
            labels={"valor_stock_clp": "CLP", "ubicacion": "Sucursal",
                    "estrategia": "Estrategia"},
            color_discrete_map={
                "Promoción leve":   "#FFF176",
                "Oferta destacada": "#FFB74D",
                "REMATE":           "#EF5350",
            },
        )
        col_g2.plotly_chart(fig_suc, use_container_width=True)

        # Treemap: valor por familia × estrategia
        if "clase_final" in df_activos.columns:
            tree_df = df_activos.groupby(
                ["clase_final", "estrategia"], as_index=False
            )["valor_stock_clp"].sum()
            tree_df = tree_df[tree_df["valor_stock_clp"] > 0]
            if not tree_df.empty:
                fig_tree = px.treemap(
                    tree_df,
                    path=["estrategia", "clase_final"],
                    values="valor_stock_clp",
                    color="valor_stock_clp",
                    color_continuous_scale="Reds",
                    title="Valor en riesgo: Estrategia → Clase ABC/XYZ",
                )
                st.plotly_chart(fig_tree, use_container_width=True)

        # Scatter: días vs valor, tamaño = unidades
        fig_scatter = px.scatter(
            df_activos,
            x="dias_en_bodega", y="valor_stock_clp",
            size="stock_actual", color="estrategia",
            hover_data=["sku_id", "nombre", "ubicacion", "descuento_pct"],
            title="Días en bodega vs Valor (tamaño = unidades)",
            labels={"dias_en_bodega": "Días en bodega",
                    "valor_stock_clp": "Valor CLP",
                    "estrategia": "Estrategia"},
            color_discrete_map={
                "Promoción leve":   "#F9A825",
                "Oferta destacada": "#E65100",
                "REMATE":           "#B71C1C",
            },
        )
        fig_scatter.add_vline(x=30, line_dash="dot", line_color="gray")
        fig_scatter.add_vline(x=60, line_dash="dot", line_color="orange")
        fig_scatter.add_vline(x=90, line_dash="dot", line_color="red")
        st.plotly_chart(fig_scatter, use_container_width=True)

        # ── Tabla detalle ─────────────────────────────────────────────────────
        st.markdown("#### Detalle de productos")
        cols_tabla = ["estrategia", "sku_id", "nombre", "marca", "ubicacion",
                      "clase_final", "dias_en_bodega", "stock_actual",
                      "descuento_pct", "valor_stock_clp", "ahorro_potencial_clp"]
        cols_disp  = [c for c in cols_tabla if c in df_activos.columns]
        tabla_view = df_activos[cols_disp].copy()
        tabla_view["valor_stock_clp"]      = tabla_view["valor_stock_clp"].map(fmt_clp)
        tabla_view["ahorro_potencial_clp"] = tabla_view["ahorro_potencial_clp"].map(fmt_clp)
        tabla_view["descuento_pct"]        = tabla_view["descuento_pct"].map(lambda x: f"{x}%")
        tabla_view.columns = [c.replace("_", " ").title() for c in cols_disp]
        st.dataframe(tabla_view, use_container_width=True, hide_index=True)

        # ── Exportar Excel ────────────────────────────────────────────────────
        st.markdown("#### Exportar")
        def _generar_excel_ofertas(df_exp: pd.DataFrame) -> bytes:
            buf = io.BytesIO()
            sucursales_exp = sorted(df_exp["ubicacion"].dropna().unique())
            with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                df_exp.to_excel(writer, sheet_name="GLOBAL", index=False)
                for suc in sucursales_exp:
                    df_s = df_exp[df_exp["ubicacion"] == suc]
                    if not df_s.empty:
                        df_s.to_excel(writer, sheet_name=str(suc)[:31], index=False)
            return buf.getvalue()

        xlsx_bytes = _generar_excel_ofertas(df_activos[cols_disp])
        st.download_button(
            label="⬇️ Descargar Excel de Ofertas & Remates",
            data=xlsx_bytes,
            file_name=f"ofertas_remates_{date.today().isoformat()}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        # ──────────────────────────────────────────────────────────────────
        # 📣 CAMPAÑA DE COMUNICACIÓN (Módulo 3)
        # ──────────────────────────────────────────────────────────────────
        st.markdown("---")
        st.markdown("### 📣 Enviar campaña de comunicación a sucursales")
        st.caption(
            "Genera un mensaje personalizado por sucursal con sus productos críticos, "
            "ranking y valor en riesgo — lo previsualizás y después confirmás el envío."
        )

        # Importar helpers del Módulo 3 (sys.path → reportes_stock/)
        import sys as _sys
        _rep_path = str(Path(__file__).resolve().parents[3] / "reportes_stock")
        if _rep_path not in _sys.path:
            _sys.path.insert(0, _rep_path)
        try:
            from modulo3_campana import (  # type: ignore
                generar_mensaje_sucursal,
                enviar_email,
                enviar_whatsapp,
                _ranking_sucursales,
                logger as _camp_logger,
            )
            from config import (  # type: ignore
                CAMPANA_NOMBRE,
                CONTACTOS_SUCURSALES,
                LOG_CAMPANA,
                SMTP_USER as _SMTP_USER_CFG,
            )
            _camp_ok = True
        except Exception as _exc:
            st.error(f"No se pudo cargar el módulo de campaña: {_exc}")
            _camp_ok = False

        if _camp_ok:
            st.info(f"🏷️  Campaña configurada: **{CAMPANA_NOMBRE}**")

            # Preparar DataFrame en el formato que espera el módulo
            df_camp = df_activos.rename(columns={
                "sku_id":    "sku",
                "ubicacion": "sucursal",
            }).copy()
            df_camp["sku"]      = df_camp["sku"].astype(str)
            df_camp["sucursal"] = df_camp["sucursal"].astype(str)

            ranking_df = _ranking_sucursales(df_camp)

            # Sucursales disponibles = las que tienen productos críticos Y contacto
            sucursales_disp = sorted(df_camp["sucursal"].dropna().unique())
            sucursales_con_contacto = [
                s for s in sucursales_disp
                if CONTACTOS_SUCURSALES.get(s) or CONTACTOS_SUCURSALES.get(s.upper())
            ]
            sucursales_sin_contacto = [
                s for s in sucursales_disp if s not in sucursales_con_contacto
            ]

            if sucursales_sin_contacto:
                st.warning(
                    f"⚠️ Sin contacto configurado en `config.py` (quedan excluidas): "
                    f"{', '.join(sucursales_sin_contacto)}"
                )

            c1, c2 = st.columns([1, 2])
            with c1:
                canal = st.radio(
                    "Canal de envío",
                    ["Email", "WhatsApp", "Ambos", "Solo preview (no enviar)"],
                    index=3,
                )
            with c2:
                suc_sel = st.multiselect(
                    "Sucursales destino",
                    sucursales_con_contacto,
                    default=sucursales_con_contacto,
                )

            # ── Aviso si SMTP no está configurado ─────────────────────────
            if canal in ("Email", "Ambos"):
                if not _SMTP_USER_CFG or _SMTP_USER_CFG.startswith("tu_email"):
                    st.error(
                        "❌ SMTP no configurado. Editá `reportes_stock/config.py` "
                        "(SMTP_USER, SMTP_PASSWORD) antes de enviar por email."
                    )

            # ── Generar previews ──────────────────────────────────────────
            if st.button("👁️ Generar preview de mensajes", type="secondary"):
                st.session_state["_camp_previews"] = {}
                for suc in suc_sel:
                    try:
                        html, wa = generar_mensaje_sucursal(suc, df_camp, ranking_df)
                        st.session_state["_camp_previews"][suc] = (html, wa)
                    except Exception as exc:
                        st.error(f"Error generando mensaje para {suc}: {exc}")

            previews = st.session_state.get("_camp_previews", {})
            if previews:
                st.markdown(f"#### Preview de mensajes ({len(previews)} sucursales)")
                for suc, (html, wa) in previews.items():
                    contacto = (
                        CONTACTOS_SUCURSALES.get(suc)
                        or CONTACTOS_SUCURSALES.get(suc.upper(), {})
                    )
                    with st.expander(
                        f"📍 {suc}  —  {contacto.get('nombre_jefe', '?')}  "
                        f"·  📧 {', '.join(contacto.get('email', []))}  "
                        f"·  📱 {contacto.get('whatsapp', '—')}",
                        expanded=False,
                    ):
                        tab_html, tab_wa = st.tabs(["🖥️ Email (HTML)", "📱 WhatsApp (texto)"])
                        with tab_html:
                            st.components.v1.html(html, height=600, scrolling=True)
                        with tab_wa:
                            st.code(wa, language=None)

                # ── Botón de envío con confirmación ───────────────────────
                if canal != "Solo preview (no enviar)":
                    st.markdown("---")
                    st.warning(
                        f"⚠️ Vas a enviar **{len(previews)}** mensajes por **{canal}**. "
                        "Esta acción no se puede deshacer."
                    )
                    confirmar = st.checkbox("Confirmo el envío a todas las sucursales seleccionadas")
                    if confirmar and st.button("🚀 Enviar campaña ahora", type="primary"):
                        excel_tmp = None
                        if canal in ("Email", "Ambos"):
                            # Guardamos el Excel ya generado como adjunto temporal
                            excel_tmp = Path(get_paths().output_dir).parent / "tmp_campana.xlsx"
                            excel_tmp.write_bytes(xlsx_bytes)

                        resultados = []
                        progress = st.progress(0.0)
                        for i, (suc, (html, wa)) in enumerate(previews.items(), 1):
                            contacto = (
                                CONTACTOS_SUCURSALES.get(suc)
                                or CONTACTOS_SUCURSALES.get(suc.upper(), {})
                            )
                            ok_email = ok_wa = None
                            if canal in ("Email", "Ambos"):
                                dest = contacto.get("email") or []
                                if dest:
                                    ok_email = enviar_email(suc, html, dest, excel_tmp)
                                    _camp_logger.info(
                                        f"[DASHBOARD] email {suc} → {dest} : "
                                        f"{'OK' if ok_email else 'FAIL'}"
                                    )
                            if canal in ("WhatsApp", "Ambos"):
                                num = contacto.get("whatsapp")
                                if num:
                                    ok_wa = enviar_whatsapp(suc, num, wa)
                                    _camp_logger.info(
                                        f"[DASHBOARD] whatsapp {suc} → {num} : "
                                        f"{'OK' if ok_wa else 'FAIL'}"
                                    )
                            resultados.append({
                                "Sucursal":  suc,
                                "Email":     "✅" if ok_email else ("❌" if ok_email is False else "—"),
                                "WhatsApp":  "✅" if ok_wa    else ("❌" if ok_wa    is False else "—"),
                            })
                            progress.progress(i / len(previews))

                        st.success(f"Campaña enviada — {len(resultados)} sucursales procesadas.")
                        st.dataframe(
                            pd.DataFrame(resultados),
                            hide_index=True,
                            use_container_width=True,
                        )
                        st.caption(f"📝 Log detallado: `{LOG_CAMPANA}`")

                        # Limpiar preview y temporal
                        st.session_state.pop("_camp_previews", None)
                        if excel_tmp and excel_tmp.exists():
                            try:
                                excel_tmp.unlink()
                            except Exception:
                                pass

            # ── Ver log de envíos anteriores ──────────────────────────────
            with st.expander("📝 Ver log de envíos anteriores"):
                if Path(LOG_CAMPANA).exists():
                    try:
                        log_txt = Path(LOG_CAMPANA).read_text(encoding="utf-8")
                        st.code(log_txt[-4000:], language="log")
                    except Exception as exc:
                        st.info(f"No se pudo leer el log: {exc}")
                else:
                    st.info("Aún no hay envíos registrados.")


# ---------------------------------------------------------------------------
# TAB 6 — HISTORIAL SEMANAL
# ---------------------------------------------------------------------------
with tab_historial:
    st.markdown(
        "Registro acumulativo semana a semana del estado de productos con acción comercial. "
        "Permite cruzar **qué estrategia tuvo impacto** en qué productos y sucursales."
    )

    historial = load_historial_ofertas()

    # ── Registrar snapshot ────────────────────────────────────────────────────
    with st.expander("➕ Registrar snapshot de esta semana", expanded=historial.empty):
        st.markdown("Captura el estado actual de productos con acción requerida.")

        accion_opciones = [
            "Ninguna", "Remate", "Evento especial",
            "Oferta / promoción", "Liquidación fin de temporada", "Otra",
        ]
        accion_sel = st.selectbox("Acción comercial esta semana", accion_opciones)
        if accion_sel == "Otra":
            accion_sel = st.text_input("Describí la acción:")
        impacto_txt = st.text_area(
            "Impacto / resultado observado (opcional)",
            placeholder="Ej: Se vendieron 40 unidades del SKU 1234 en sucursal Santiago",
        )

        if st.button("💾 Guardar snapshot semanal", type="primary"):
            sd_snap  = load_stock_demanda()
            df_snap_full = _build_ofertas_df(needs, costos, productos, sd_snap)
            df_snap  = _filter_sucursal(df_snap_full, col="ubicacion")
            df_snap  = df_snap[df_snap["estrategia"] != "Sin acción"].copy()

            if df_snap.empty:
                st.warning("No hay productos con acción requerida actualmente.")
            else:
                semana_num = date.today().isocalendar()[1]
                snap_cols  = {
                    "semana":             semana_num,
                    "fecha_descarga":     date.today().isoformat(),
                    "sku":                df_snap["sku_id"].values,
                    "nombre":             df_snap.get("nombre", pd.Series([""] * len(df_snap))).values,
                    "sucursal":           df_snap["ubicacion"].values,
                    "dias_en_bodega":     df_snap["dias_en_bodega"].values,
                    "stock_actual":       df_snap["stock_actual"].values,
                    "clasificacion":      df_snap["estrategia"].values,
                    "estrategia_periodo": df_snap["estrategia"].values,
                    "accion_comercial":   accion_sel,
                    "impacto_observado":  impacto_txt or "—",
                    "valor_stock_clp":    df_snap["valor_stock_clp"].values,
                }
                df_nuevo = pd.DataFrame(snap_cols)
                guardar_snapshot_historial(df_nuevo)
                st.success(f"✅ {len(df_nuevo)} productos registrados — Semana {semana_num}")
                st.cache_data.clear()
                historial = load_historial_ofertas()

    if historial.empty:
        st.info("Aún no hay registros. Guardá el primer snapshot usando el panel de arriba.")
    else:
        historial["dias_en_bodega"] = pd.to_numeric(historial["dias_en_bodega"], errors="coerce")
        historial["stock_actual"]   = pd.to_numeric(historial["stock_actual"],   errors="coerce")
        historial["valor_stock_clp"]= pd.to_numeric(historial.get("valor_stock_clp", pd.Series()), errors="coerce").fillna(0)
        historial["semana"]         = pd.to_numeric(historial["semana"],         errors="coerce")
        historial["producto_key"]   = historial["sku"].astype(str) + " · " + historial["sucursal"].astype(str)

        semanas_disp = sorted(historial["semana"].dropna().unique().astype(int))

        # ── KPIs del historial ────────────────────────────────────────────────
        h1, h2, h3, h4 = st.columns(4)
        h1.metric("📅 Semanas registradas",  len(semanas_disp))
        h2.metric("📦 Productos únicos",      historial["sku"].nunique())
        h3.metric("🏪 Sucursales",            historial["sucursal"].nunique())
        interv = historial[historial["accion_comercial"] != "Ninguna"]["semana"].nunique()
        h4.metric("🎯 Semanas con intervención", int(interv))

        st.divider()

        # ── Evolución días en bodega ──────────────────────────────────────────
        st.markdown("#### Evolución de días en bodega por producto")

        productos_unicos = sorted(historial["producto_key"].unique())
        prods_sel = st.multiselect(
            "Seleccioná productos a comparar (vacío = top 10 por días)",
            options=productos_unicos,
            default=[],
            key="hist_prods",
        )
        if not prods_sel:
            top10 = (
                historial.groupby("producto_key")["dias_en_bodega"].max()
                .nlargest(10).index.tolist()
            )
            df_evol = historial[historial["producto_key"].isin(top10)]
        else:
            df_evol = historial[historial["producto_key"].isin(prods_sel)]

        if not df_evol.empty:
            fig_evol = px.line(
                df_evol.sort_values("semana"),
                x="semana", y="dias_en_bodega",
                color="producto_key", markers=True,
                title="Evolución de días en bodega (semana a semana)",
                labels={"semana": "Semana", "dias_en_bodega": "Días en bodega",
                        "producto_key": "Producto · Sucursal"},
            )
            # Marcar semanas con intervención
            semanas_interv = historial[
                historial["accion_comercial"] != "Ninguna"
            ]["semana"].unique()
            for sw in semanas_interv:
                fig_evol.add_vline(
                    x=sw, line_dash="dash", line_color="green", opacity=0.5,
                    annotation_text="Intervención", annotation_position="top",
                )
            fig_evol.update_layout(hovermode="x unified")
            st.plotly_chart(fig_evol, use_container_width=True)

        # ── Tabla comparativa: mejoraron / empeoraron ─────────────────────────
        st.markdown("#### Comparativo: evolución por producto")
        if len(semanas_disp) >= 2:
            sem_ini = semanas_disp[0]
            sem_fin = semanas_disp[-1]
            df_ini  = historial[historial["semana"] == sem_ini].groupby(
                "producto_key")["dias_en_bodega"].max().rename("dias_inicio")
            df_fin  = historial[historial["semana"] == sem_fin].groupby(
                "producto_key")["dias_en_bodega"].max().rename("dias_fin")
            comp_df = pd.concat([df_ini, df_fin], axis=1).dropna()
            comp_df["variacion"] = comp_df["dias_fin"] - comp_df["dias_inicio"]
            comp_df["tendencia"] = comp_df["variacion"].apply(
                lambda v: "✅ Mejoró" if v < -5 else ("🔴 Empeoró" if v > 5 else "⚠️ Sin cambio")
            )
            comp_df = comp_df.reset_index().rename(columns={"producto_key": "Producto · Sucursal"})
            comp_df.columns = ["Producto · Sucursal",
                               f"Días sem. {sem_ini}", f"Días sem. {sem_fin}",
                               "Variación (días)", "Tendencia"]

            col_tab, col_pie = st.columns([3, 2])
            col_tab.dataframe(
                comp_df.sort_values("Variación (días)"),
                use_container_width=True, hide_index=True,
            )
            tend_cnt = comp_df["Tendencia"].value_counts().reset_index()
            tend_cnt.columns = ["Tendencia", "Cantidad"]
            fig_tend = px.pie(tend_cnt, values="Cantidad", names="Tendencia",
                              hole=0.5, title="Resumen de tendencias",
                              color="Tendencia",
                              color_discrete_map={
                                  "✅ Mejoró":    "#81C784",
                                  "🔴 Empeoró":   "#E57373",
                                  "⚠️ Sin cambio": "#FFD54F",
                              })
            col_pie.plotly_chart(fig_tend, use_container_width=True)
        else:
            st.info("Necesitás al menos 2 semanas registradas para ver el comparativo.")

        # ── Intervenciones comerciales ────────────────────────────────────────
        st.markdown("#### Intervenciones comerciales registradas")
        interv_df = (
            historial[historial["accion_comercial"] != "Ninguna"]
            .groupby(["semana", "accion_comercial", "impacto_observado"])
            .agg(productos=("sku", "nunique"), sucursales=("sucursal", "nunique"))
            .reset_index()
            .sort_values("semana")
        )
        if interv_df.empty:
            st.info("No se han registrado intervenciones comerciales aún.")
        else:
            st.dataframe(interv_df, use_container_width=True, hide_index=True)

        # ── Exportar historial ────────────────────────────────────────────────
        buf_hist = io.BytesIO()
        historial.drop(columns=["producto_key"], errors="ignore").to_excel(
            buf_hist, index=False, engine="openpyxl"
        )
        st.download_button(
            label="⬇️ Descargar historial completo (Excel)",
            data=buf_hist.getvalue(),
            file_name=f"historial_stock_semanal_{date.today().isoformat()}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


# ============================================================================
# Footer
# ============================================================================
st.divider()
st.caption(
    f"Pipeline run: `{latest.run_id}` · generado {latest.fecha.strftime('%Y-%m-%d')} · "
    "datos leídos de `data/output/`. El dashboard no modifica nada — solo lee."
)
