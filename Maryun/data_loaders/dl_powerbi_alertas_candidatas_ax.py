import os
import pandas as pd
from mage_ai.io.config import ConfigFileLoader
import msal
import requests

if 'data_loader' not in globals():
    from mage_ai.data_preparation.decorators import data_loader
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test

# === CONFIGURACIÓN ===
CONFIG_PATH = '/home/src/Maryun/io_config.yaml'
PROFILE = 'maryun'
cfg = ConfigFileLoader(CONFIG_PATH, PROFILE)
TENANT_ID = cfg['POWERBI_TENANT_ID']
print("TENANT_ID:", TENANT_ID)
CLIENT_ID = cfg['POWERBI_CLIENT_ID']         # GUID como string, sin int()
print("CLIENT_ID:", CLIENT_ID)
CLIENT_SECRET = cfg['POWERBI_CLIENT_SECRET']
print("CLIENT_SECRET:", CLIENT_SECRET)
GROUP_ID = cfg['POWERBI_GROUP_ID']
print("GROUP_ID:", GROUP_ID)

AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
print("AUTHORITY:", AUTHORITY)
SCOPE = ["https://analysis.windows.net/powerbi/api/.default"]
print("SCOPE:", SCOPE)
POWERBI_API_BASE = "https://api.powerbi.com/v1.0/myorg/"
print("POWERBI_API_BASE:", POWERBI_API_BASE)

@data_loader
def load_data(*args, **kwargs):
    """
    Data loader de MageAI para conectarse a Power BI y devolver información.
    
    Por defecto lista los reports del workspace definido en POWERBI_GROUP_ID,
    pero también permite pasar group_id vía kwargs.
    
    Ejemplo de uso en Mage:
      - Config de bloque con kwargs: {"group_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"}
    """
    # group_id = kwargs.get('group_id') or os.getenv('POWERBI_GROUP_ID')
    group_id = GROUP_ID

    if not group_id:
        raise ValueError(
            "Debe especificarse el 'group_id' via kwargs o la variable "
            "de entorno POWERBI_GROUP_ID."
        )

    df_reports = list_reports(group_id)

    # Aquí podrías filtrar o transformar antes de devolver
    return df_reports


@test
def test_output(output, *args) -> None:
    """
    Template code for testing the output of the block.
    """
    assert output is not None, 'The output is undefined'

def get_access_token() -> str:
    """
    Obtiene el access token de Azure AD usando client_credentials.
    """
    if not TENANT_ID or not CLIENT_ID or not CLIENT_SECRET:
        raise ValueError(
            "Faltan variables de entorno: "
            "POWERBI_TENANT_ID, POWERBI_CLIENT_ID o POWERBI_CLIENT_SECRET."
        )

    app = msal.ConfidentialClientApplication(
        CLIENT_ID,
        authority=AUTHORITY,
        client_credential=CLIENT_SECRET,
    )

    result = app.acquire_token_for_client(scopes=SCOPE)

    if "access_token" not in result:
        raise Exception(f"Error obteniendo token: {result}")

    print("ACCESS_TOKEN:", result["access_token"])
    return result["access_token"]


def list_reports(group_id: str) -> pd.DataFrame:
    """
    Llama a la API de Power BI para listar los reports de un workspace (group).
    Devuelve un DataFrame con info básica de los reports.
    """
    token = get_access_token()

    headers = {
        "Authorization": f"Bearer {token}"
    }

    url = f"{POWERBI_API_BASE}/groups/{group_id}/reports"
    resp = requests.get(url, headers=headers)

    # DEBUG: mostrar más información si falla
    if resp.status_code != 200:
        try:
            print("Error Power BI:", resp.status_code, resp.text)
        except Exception:
            pass
        resp.raise_for_status()

    data = resp.json()

    reports = data.get("value", [])
    if not reports:
        return pd.DataFrame()

    df = pd.json_normalize(reports)
    df = df.rename(
        columns={
            "id": "report_id",
            "name": "report_name",
            "webUrl": "report_web_url",
            "embedUrl": "report_embed_url",
        }
    )

    return df