from mage_ai.io.config import ConfigFileLoader
import requests
import smtplib
from email.mime.text import MIMEText

CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'


def _load_cfg():
    return ConfigFileLoader(CONFIG_PATH, PROFILE)


def send_teams_message(text: str):
    cfg = ConfigFileLoader(CONFIG_PATH, PROFILE)
    webhook_url = cfg.['TEAMS_WEBHOOK_URL']  # agrega esta key en tu io_config.yaml

    if not webhook_url:
        print('[notifier] TEAMS_WEBHOOK_URL no configurado, no se envía mensaje a Teams.')
        return

    payload = {
        'text': text,
    }

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code >= 400:
            print(f'[notifier] Error enviando mensaje a Teams: {resp.status_code} {resp.text}')
    except Exception as e:
        print(f'[notifier] Excepción enviando mensaje a Teams: {e}')


def send_email(subject: str, body: str):
    cfg = ConfigFileLoader(CONFIG_PATH, PROFILE)

    host = cfg['SMTP_HOST']
    port = int(cfg['SMTP_PORT'])
    user = cfg['SMTP_USERNAME']
    password = cfg['SMTP_PASSWORD']
    from_addr = cfg['SMTP_FROM']
    to_addr = cfg['SMTP_TO']  # puedes usar lista separada por comas si quieres

    if not (host and from_addr and to_addr):
        print('[notifier] SMTP_HOST/SMTP_FROM/SMTP_TO no configurados, no se envía correo.')
        return

    msg = MIMEText(body, 'plain', 'utf-8')
    msg['Subject'] = subject
    msg['From'] = from_addr
    msg['To'] = to_addr

    try:
        with smtplib.SMTP(host, port, timeout=10) as server:
            server.starttls()
            if user and password:
                server.login(user, password)
            server.send_message(msg)
    except Exception as e:
        print(f'[notifier] Excepción enviando correo: {e}')