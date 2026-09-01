from mage_ai.io.config import ConfigFileLoader
from datetime import datetime
import requests
import smtplib
from datetime import datetime
from zoneinfo import ZoneInfo
from email.mime.text import MIMEText

if 'data_exporter' not in globals():
    from mage_ai.data_preparation.decorators import data_exporter

CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'

@data_exporter
def notify_start(*args, **kwargs):
    """
    Notifica el inicio del pipeline:
      - Mensaje a Teams
      - Correo
    """

    now_cl = datetime.now(ZoneInfo("America/Santiago"))
    now = now_cl.strftime('%Y-%m-%d %H:%M:%S %Z')

    inicio = (f'🔔 Proceso de alertas de abastecimiento INICIADO 🔔')
    fecha = (f'⌚ Fecha/hora: {now} ⌚')
    filas_totales = (f'')
    filas_insertadas = (f'')
    filas_existentes = (f'')

    # Teams
    send_teams_message(inicio, fecha, filas_totales, filas_insertadas, filas_existentes)

    print('[notifier] Notificación de inicio enviada (o intentada).')

    # Puedes devolver un dict por si quieres loguear algo en Mage
    return {
        'status': 'started',
        'timestamp_utc': now,
    }

def send_teams_message(inicio: str, fecha: str, filas_totales: str, filas_insertadas: str, filas_existentes: str):
    cfg = ConfigFileLoader(CONFIG_PATH, PROFILE)
    webhook_url = cfg['TEAMS_WEBHOOK_URL']  # agrega esta key en tu io_config.yaml

    if not webhook_url:
        print('[notifier] TEAMS_WEBHOOK_URL no configurado, no se envía mensaje a Teams.')
        return

    payload = {
        'inicio': inicio,
        'fecha': fecha,
        'filas_totales': filas_totales,
        'filas_insertadas': filas_insertadas,
        'filas_existentes': filas_existentes,
    }

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code >= 400:
            print(f'[notifier] Error enviando mensaje a Teams: {resp.status_code} {resp.text}')
    except Exception as e:
        print(f'[notifier] Excepción enviando mensaje a Teams: {e}')