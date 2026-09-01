from mage_ai.io.config import ConfigFileLoader
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd

if 'data_exporter' not in globals():
    from mage_ai.data_preparation.decorators import data_exporter

CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'


def _to_int(v, default=0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _count_api_sources_from_data2(data_2) -> tuple[int, int]:
    """
    Cuenta según columna 'accion' del df data_2:
      - OC: accion contiene 'generar'
      - Traspasos: accion contiene 'transferir' o 'despachar'
    """
    if data_2 is None:
        return 0, 0

    # data_2 puede venir como DF o dict/list; intentamos normalizar
    if isinstance(data_2, pd.DataFrame):
        df2 = data_2.copy()
    else:
        try:
            df2 = pd.DataFrame(data_2)
        except Exception:
            return 0, 0

    if df2.empty or 'accion' not in df2.columns:
        return 0, 0

    acc = df2['accion'].astype('string').fillna('')

    oc = int(acc.str.contains('generar', case=False, na=False).sum())
    trasp = int(
        acc.str.contains('transferir', case=False, na=False).sum()
        + acc.str.contains('despachar', case=False, na=False).sum()
    )

    return oc, trasp


@data_exporter
def notify_start(data, data_2, *args, **kwargs):
    """
    Notifica el fin del pipeline a Teams e incluye:
      - Filas Totales / Nuevas / Existentes (desde `data`)
      - Cantidad de OC y Traspasos (desde `data_2['accion']`)
    """
    now_cl = datetime.now(ZoneInfo("America/Santiago"))
    now = now_cl.strftime('%Y-%m-%d %H:%M:%S %Z')

    # ---- Resumen base (desde `data`) ----
    # Soporta que `data` venga como dict o como DF de 1 fila
    filas_totales_n = filas_nuevas_n = filas_existentes_n = 0

    if isinstance(data, dict):
        filas_totales_n = _to_int(data.get('rows_total', 0))
        filas_nuevas_n = _to_int(data.get('rows_inserted', 0))
        filas_existentes_n = _to_int(data.get('rows_existing_valid', 0))
    elif isinstance(data, pd.DataFrame) and not data.empty:
        row0 = data.iloc[0].to_dict()
        filas_totales_n = _to_int(row0.get('rows_total', 0))
        filas_nuevas_n = _to_int(row0.get('rows_inserted', 0))
        filas_existentes_n = _to_int(row0.get('rows_existing_valid', 0))

    # ---- Conteos OC / Traspasos (desde `data_2`) ----
    oc_n, trasp_n = _count_api_sources_from_data2(data_2)

    inicio = '🔔 Proceso de alertas de abastecimiento FINALIZADO 🔔'
    fecha = f'⌚ Fecha/hora: {now} ⌚'
    filas_totales = f'Filas Totales: {filas_totales_n}'
    filas_insertadas = f'Filas Nuevas: {filas_nuevas_n}'
    filas_existentes = f'Filas Existentes: {filas_existentes_n}'

    oc_subidos = f'API OC subidos (generar): {oc_n}'
    traspasos_subidos = f'API Traspasos subidos (transferir/despachar): {trasp_n}'

    # Teams
    send_teams_message(
        inicio=inicio,
        fecha=fecha,
        filas_totales=filas_totales,
        filas_insertadas=filas_insertadas,
        filas_existentes=filas_existentes,
        oc_subidos=oc_subidos,
        traspasos_subidos=traspasos_subidos,
    )

    print('[notifier] Notificación enviada (o intentada).')

    return {
        'status': 'finished',
        'timestamp_cl': now,
        'rows_total': filas_totales_n,
        'rows_inserted': filas_nuevas_n,
        'rows_existing_valid': filas_existentes_n,
        'oc_subidos': oc_n,
        'traspasos_subidos': trasp_n,
    }


def send_teams_message(
    inicio: str,
    fecha: str,
    filas_totales: str,
    filas_insertadas: str,
    filas_existentes: str,
    oc_subidos: str,
    traspasos_subidos: str,
):
    cfg = ConfigFileLoader(CONFIG_PATH, PROFILE)
    webhook_url = cfg.get('TEAMS_WEBHOOK_URL')

    if not webhook_url:
        print('[notifier] TEAMS_WEBHOOK_URL no configurado, no se envía mensaje a Teams.')
        return

    payload = {
        'inicio': inicio,
        'fecha': fecha,
        'filas_totales': filas_totales,
        'filas_insertadas': filas_insertadas,
        'filas_existentes': filas_existentes,
        'oc_subidos': oc_subidos,
        'traspasos_subidos': traspasos_subidos,
    }

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code >= 400:
            print(f'[notifier] Error enviando mensaje a Teams: {resp.status_code} {resp.text}')
    except Exception as e:
        print(f'[notifier] Excepción enviando mensaje a Teams: {e}')