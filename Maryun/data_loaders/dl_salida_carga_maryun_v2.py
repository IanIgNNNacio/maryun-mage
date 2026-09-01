"""dl_salida_carga_maryun_v2 (reemplaza dl_powerbi_data).

Lee la salida FINAL de maryun_abastecimiento_V4
(logistica_v2.logistica_salida_carga_maryun) y la mapea al formato de "plan"
que esperan tr_clean_alertas_v2 y el resto del pipeline:

    sku2, sucursal_destino, accion, cantidad, sku_original, sucursal_origen,
    fecha_corte, run_id

NO recalcula nada: needs, reposicion y homologacion ya vienen resueltos de V4.

run_id (variable de pipeline):
  - seteada  -> usa esa run.
  - vacia    -> usa la de mayor fecha_generacion.

Mapeo capa -> accion (contrato downstream: 'generar' => OC; el resto => traspaso):
  compra        -> 'generar oc'
  cd            -> 'despachar'
  inmovilizado  -> 'despachar (inmovilizado)'   (parentesis -> flag en tr_clean)
  sobrestock    -> 'despachar (sobrestock)'
"""
from mage_ai.io.config import ConfigFileLoader
import clickhouse_connect
import pandas as pd

if 'data_loader' not in globals():
    from mage_ai.data_preparation.decorators import data_loader
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test

CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'
TABLE = 'logistica_v2.logistica_salida_carga_maryun'
# Puente de formato de SKU: la carga V4 trae sku_id canonico (sin ceros a la
# izquierda); mysis postea/espera el sku con ceros (12 dig, = tab_sku.sku).
# tab_sku.sku con ceros pelados == carga.sku_id (mapeo 1:1 verificado).
TABLE_SKU = 'dwh.mysis_tab_sku'

_CAPA_ACCION = {
    'compra': 'generar oc',
    'cd': 'despachar',
    'inmovilizado': 'despachar (inmovilizado)',
    'sobrestock': 'despachar (sobrestock)',
}

OUT_COLS = [
    'sku2', 'sucursal_destino', 'accion', 'cantidad',
    'sku_original', 'sucursal_origen', 'fecha_corte', 'run_id',
    'rut_proveedor', 'costo_unitario_clp',
]


def _client():
    cfg = ConfigFileLoader(CONFIG_PATH, PROFILE)
    use_https = str(cfg['CLICKHOUSE_INTERFACE']).lower() == 'https'
    return clickhouse_connect.get_client(
        host=cfg['CLICKHOUSE_HOST'],
        port=int(cfg['CLICKHOUSE_PORT']),
        username=cfg['CLICKHOUSE_USERNAME'],
        password=cfg['CLICKHOUSE_PASSWORD'],
        database=cfg['CLICKHOUSE_DATABASE'],
        secure=use_https,
    )


@data_loader
def load_data(*args, **kwargs):
    client = _client()

    run_id = kwargs.get('run_id')
    if run_id is not None:
        run_id = str(run_id).strip()
    if not run_id:
        r = client.query(f"SELECT run_id FROM {TABLE} ORDER BY fecha_generacion DESC LIMIT 1")
        if not r.result_rows:
            raise ValueError(f"{TABLE} no tiene filas; no hay run para procesar.")
        run_id = str(r.result_rows[0][0])

    print(f"[dl_salida_carga_maryun_v2] run_id = {run_id}")
    rid = run_id.replace("'", "\\'")

    df = client.query_df(
        f"""
        SELECT
            run_id,
            sku_id,
            coalesce(nullIf(homologado_desde_sku, ''), sku_id) AS sku_original,
            origen,
            destino,
            capa,
            cantidad,
            fecha_generacion,
            ifNull(rut_proveedor, '') AS rut_proveedor,
            ifNull(costo_unitario_clp, 0) AS costo_unitario_clp
        FROM {TABLE}
        WHERE run_id = '{rid}'
          AND upper(trimBoth(clase_abc_xyz)) IN ('AX', 'AY', 'BX', 'CX')
        """
    )

    if df is None or df.empty:
        print(f"[dl_salida_carga_maryun_v2] run_id {run_id} sin filas.")
        return pd.DataFrame(columns=OUT_COLS)

    # --- Puente de formato: sku_id canonico -> sku padded (lo que mysis postea) ---
    mp = client.query_df(
        f"""
        SELECT replaceRegexpOne(toString(sku), '^0+', '') AS canon, any(sku) AS padded
        FROM {TABLE_SKU}
        GROUP BY canon
        """
    )
    sku_map = dict(zip(mp['canon'].astype(str), mp['padded'].astype(str)))

    def _pad(s):
        s = str(s).strip()
        return sku_map.get(s, s)  # fallback: deja el canonico si no hay match

    sku_id_canon = df['sku_id'].astype(str).str.strip()
    sku_orig_canon = df['sku_original'].astype(str).str.strip()
    sku2_padded = sku_id_canon.map(_pad)
    sku_orig_padded = sku_orig_canon.map(_pad)

    if (sku2_padded == sku_id_canon).any():
        faltan = sorted(set(sku_id_canon[sku2_padded == sku_id_canon]))[:10]
        print(f"[dl_salida_carga_maryun_v2] AVISO: {len(set(sku_id_canon[sku2_padded == sku_id_canon]))} "
              f"sku2 sin padding en tab_sku (ej: {faltan})")

    capa = df['capa'].astype(str).str.lower().str.strip()
    accion = capa.map(_CAPA_ACCION).fillna('despachar')  # capa desconocida -> traspaso (no 'generar')

    out = pd.DataFrame({
        'sku2': sku2_padded,
        'sucursal_destino': df['destino'].astype(str).str.strip(),
        'accion': accion,
        'cantidad': pd.to_numeric(df['cantidad'], errors='coerce').fillna(0),
        'sku_original': sku_orig_padded,
        'sucursal_origen': df['origen'].astype(str).str.strip(),
        'fecha_corte': pd.to_datetime(df['fecha_generacion'], errors='coerce'),
        'run_id': df['run_id'].astype(str).str.strip(),
        'rut_proveedor': df['rut_proveedor'].astype(str).str.strip(),
        'costo_unitario_clp': pd.to_numeric(df['costo_unitario_clp'], errors='coerce'),
    })

    print(f"[dl_salida_carga_maryun_v2] filas mapeadas: {len(out)} | "
          f"accion dist: {out['accion'].value_counts().to_dict()}")
    return out[OUT_COLS]


@test
def test_output(output, *args) -> None:
    assert output is not None, 'The output is undefined'
    for col in OUT_COLS:
        assert col in output.columns, f'Falta columna esperada: {col}'
