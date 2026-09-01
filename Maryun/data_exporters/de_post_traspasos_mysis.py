import requests
import pandas as pd
from mage_ai.io.config import ConfigFileLoader

if 'data_exporter' not in globals():
    from mage_ai.data_preparation.decorators import data_exporter

SUCURSAL_TO_ID = {
    "SANTIAGO": 1,
    "PUERTO MONTT": 2,
    "CONCEPCION": 3,
    "QUELLON": 4,
    "OSORNO": 5,
    "LOS ANGELES": 6,
    "CASTRO": 7,
    "PUERTO VARAS": 8,
    "CARDONAL": 9,
    "ADMINISTRACION": 10,
    "PENDIENTES": 11,
    "CD SUR": 12,
    "CD SANTIAGO": 13,
    "MUESTRA SIN RETORNO": 14,
    "DISTRIBUCION TOTAL": 15,
    "ZONA SUR TOTAL": 16,
    "ZONA SUR AUSTRAL": 17,
    "ISLA CHILOE": 18,
    "ZONA BIO BIO": 19,
    "PROVINCIA LLANQUIHUE": 20,
    "INVENTARIO STGO": 21,
    "LOS ANGELES EXPRESS": 22,
    "CONSUMOS INTERNOS": 23,
    "BORDADOS": 24,
    "VALDIVIA": 25,
    "MARKETPLACE": 26,
}

CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

TRASPASOS_API_URL_DEFAULT = 'http://20.153.168.52/mryn/APIPRD/post_traspasos.php'


def map_sucursal_to_id(nombre: str):
    if nombre is None:
        return None
    key = str(nombre).strip().upper()
    return SUCURSAL_TO_ID.get(key)


@data_exporter
def export_traspasos(data, *args, **kwargs):
    df = data.copy() if data is not None else pd.DataFrame()

    if df is None or df.empty:
        print('[traspasos] No hay filas para enviar a la API de traspasos.')
        return pd.DataFrame([{
            'total_grupos': 0,
            'traspasos_ok': 0,
            'traspasos_error': 0,
            'errores': [],
        }])

    api_url = TRASPASOS_API_URL_DEFAULT

    # Normalizar sucursales
    df['sucursal_origen'] = df['sucursal_origen'].astype(str).fillna('').str.strip()
    df['sucursal_destino'] = df['sucursal_destino'].astype(str).fillna('').str.strip()

    # Mapear a IDs
    df['origen_id'] = df['sucursal_origen'].apply(map_sucursal_to_id)
    df['destino_id'] = df['sucursal_destino'].apply(map_sucursal_to_id)

    unmapped_origen = df[df['origen_id'].isna()]['sucursal_origen'].unique()
    unmapped_destino = df[df['destino_id'].isna()]['sucursal_destino'].unique()
    print("Sucursales origen sin mapear:", unmapped_origen)
    print("Sucursales destino sin mapear:", unmapped_destino)

    required_cols = [
        'sucursal_origen',
        'sucursal_destino',
        'sku_original',
        'accion',
        'cantidad',
        'comentario',
        'origen_id',
        'destino_id',
    ]
    for c in required_cols:
        if c not in df.columns:
            raise ValueError(f'[traspasos] Falta columna requerida en DF: {c}')

    # Filtrar grupos válidos (IDs mapeados)
    base_mask = (
        df['sucursal_origen'].ne('')
        & df['sucursal_destino'].ne('')
        & df['origen_id'].notna()
        & df['destino_id'].notna()
    )
    df = df[base_mask].copy()

    if df.empty:
        print('[traspasos] No quedan grupos válidos (sucursales / mapeo).')
        return pd.DataFrame([{
            'total_grupos': 0,
            'traspasos_ok': 0,
            'traspasos_error': 0,
            'errores': [],
        }])

    resultados = []
    traspasos_ok = 0
    traspasos_error = 0

    for idx, row in df.iterrows():
        suc_origen = row['sucursal_origen']
        suc_destino = row['sucursal_destino']
        origen_id = row['origen_id']
        destino_id = row['destino_id']
        comentario_col = str(row.get('comentario', '') or '')

        # Resultado base por grupo
        r_out = {
            'row_idx': idx,
            'sucursal_origen': suc_origen,
            'sucursal_destino': suc_destino,
            'origen_id': int(origen_id) if pd.notna(origen_id) else None,
            'destino_id': int(destino_id) if pd.notna(destino_id) else None,
            'items_enviados': 0,
            'success': False,
            'traspaso_id': None,
            'error': '',
            'http_status': None,
            'response_raw': '',
        }

        # Construir detalle desde listas
        try:
            df_items = pd.DataFrame({
                'sku_original': row['sku_original'],
                'cantidad': row['cantidad'],
                'accion': row['accion'],
            })
        except Exception as e:
            r_out['error'] = f'Error construyendo detalle: {e}'
            traspasos_error += 1
            resultados.append(r_out)
            continue

        # Normalizar items
        df_items['sku_original'] = df_items['sku_original'].astype(str).fillna('').str.strip()
        df_items['cantidad'] = pd.to_numeric(df_items['cantidad'], errors='coerce').fillna(0)
        df_items['accion'] = df_items['accion'].astype(str).fillna('').str.strip()

        # Filtrar transferencias válidas
        mask_det = (
            df_items['sku_original'].ne('')
            & (df_items['cantidad'] > 0)
            & df_items['accion'].str.contains('transferir', case=False, na=False)
        )
        df_items = df_items[mask_det]

        if df_items.empty:
            r_out['error'] = 'Sin items válidos para transferir'
            traspasos_error += 1
            resultados.append(r_out)
            continue

        # Agrupar por SKU para evitar repetidos
        df_det = df_items.groupby('sku_original', as_index=False)['cantidad'].sum()

        detalle = []
        for _, rr in df_det.iterrows():
            sku = str(rr['sku_original'])
            cantidad = float(rr['cantidad'])
            if cantidad > 0:
                detalle.append({'sku': sku, 'cantidad': cantidad})

        if not detalle:
            r_out['error'] = 'Detalle vacío tras filtros/agrupación'
            traspasos_error += 1
            resultados.append(r_out)
            continue

        r_out['items_enviados'] = len(detalle)

        comentario_payload = (
            f"detalle de los traspasos en ID: {comentario_col} // "
            f"OC creada por api traspasos en mage."
        )

        payload = {
            'cabecera': {
                'origen': int(origen_id),
                'destino': int(destino_id),
                'comentario': comentario_payload,
                'usuario': 73,
            },
            'detalle': detalle,
        }

        # POST
        try:
            resp = requests.post(api_url, json=payload, timeout=30)
            r_out['http_status'] = resp.status_code
            r_out['response_raw'] = (resp.text or '')[:1000]
        except Exception as e:
            r_out['error'] = f'Excepción HTTP: {e}'
            traspasos_error += 1
            resultados.append(r_out)
            continue

        if not resp.ok:
            r_out['error'] = f'HTTP {resp.status_code}'
            traspasos_error += 1
            resultados.append(r_out)
            continue

        # Parse JSON (según formato de la imagen)
        try:
            data_resp = resp.json()
        except ValueError:
            r_out['error'] = 'Respuesta no JSON'
            traspasos_error += 1
            resultados.append(r_out)
            continue

        success = bool(data_resp.get('success'))
        r_out['success'] = success

        if success:
            r_out['traspaso_id'] = data_resp.get('traspaso')
            traspasos_ok += 1
        else:
            # Error podría venir vacío -> dejar "" si no existe
            api_error = data_resp.get('Error')
            if api_error is None:
                api_error = data_resp.get('error')  # por si viene en minúscula
            r_out['error'] = (api_error or '')  # <-- aquí queda "" si viene vacío
            traspasos_error += 1

        resultados.append(r_out)

    df_resultados = pd.DataFrame(resultados)

    print(
        f"[traspasos] Resumen: grupos={len(df_resultados)}, "
        f"OK={traspasos_ok}, error={traspasos_error}"
    )

    # Si quieres, puedes dejar el resumen como columnas repetidas (útil en Mage)
    df_resultados['total_grupos'] = len(df_resultados)
    df_resultados['traspasos_ok'] = traspasos_ok
    df_resultados['traspasos_error'] = traspasos_error

    return df_resultados