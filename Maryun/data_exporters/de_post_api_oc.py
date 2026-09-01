import requests
import pandas as pd

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

OC_API_URL_DEFAULT = "http://20.153.168.52/mryn/APIPRD/post_oc.php"


def map_sucursal_to_id(nombre: str):
    if nombre is None:
        return None
    key = str(nombre).strip().upper()
    return SUCURSAL_TO_ID.get(key)


@data_exporter
def export_oc(data: pd.DataFrame, *args, **kwargs):
    df = data.copy() if data is not None else pd.DataFrame()

    if df.empty:
        print("[oc] No hay filas para enviar a la API de OC.")
        return pd.DataFrame([{
            "total_ocs": 0,
            "ocs_ok": 0,
            "ocs_error": 0,
            "errores": [],
        }])

    api_url = str(kwargs.get("api_url") or OC_API_URL_DEFAULT).strip()

    for c in ["cabecera", "detalle"]:
        if c not in df.columns:
            raise ValueError(f"[oc] Falta columna requerida en DF: {c}")

    resultados = []
    ocs_ok = 0
    ocs_error = 0

    for idx, row in df.iterrows():
        cab = row.get("cabecera") or {}
        det = row.get("detalle") or []
        proveedor_debug = row.get("proveedor", "")

        # Base output por fila/post
        r_out = {
            "row_idx": idx,
            "proveedor": proveedor_debug,
            "rut_proveedor": None,
            "destino_raw": None,
            "destino_id": None,
            "items_enviados": 0,
            "success": False,
            "oc_id": None,          # viene como "OC" en éxito
            "error": "",            # puede venir vacío
            "http_status": None,
            "response_raw": "",
        }

        if not isinstance(cab, dict):
            r_out["error"] = "cabecera no es dict"
            ocs_error += 1
            resultados.append(r_out)
            continue

        if not isinstance(det, (list, tuple)) or len(det) == 0:
            r_out["error"] = "detalle vacío o inválido"
            ocs_error += 1
            resultados.append(r_out)
            continue

        # --- Normalizar cabecera ---
        rut_prov = str(cab.get("rut_proveedor", "") or "").strip()
        destino_raw = cab.get("destino", None)
        comentario_col = str(cab.get("comentario", "") or "").strip()

        r_out["rut_proveedor"] = rut_prov
        r_out["destino_raw"] = destino_raw

        comentario_payload = (
            f"detalle de las OC en ID: {comentario_col} // "
            f"OC creada por api OC en mage."
        )

        usuario = int(kwargs.get("usuario", cab.get("usuario", 1)))
        confirmar = bool(kwargs.get("confirmar", cab.get("confirmar", False)))
        autorizar = bool(kwargs.get("autorizar", cab.get("autorizar", False)))

        if rut_prov == "":
            r_out["error"] = "rut_proveedor vacío"
            ocs_error += 1
            resultados.append(r_out)
            continue

        destino_id = map_sucursal_to_id(destino_raw)
        if destino_id is None:
            r_out["error"] = f"destino sin mapear: {destino_raw}"
            ocs_error += 1
            resultados.append(r_out)
            continue

        r_out["destino_id"] = int(destino_id)

        # --- Normalizar/validar detalle ---
        df_items = pd.DataFrame(det)

        for col in ["sku", "cantidad", "precio"]:
            if col not in df_items.columns:
                r_out["error"] = f"detalle sin columna '{col}'"
                ocs_error += 1
                resultados.append(r_out)
                df_items = None
                break
        if df_items is None:
            continue

        df_items["sku"] = df_items["sku"].astype("string").fillna("").str.strip()
        df_items["cantidad"] = pd.to_numeric(df_items["cantidad"], errors="coerce").fillna(0)
        df_items["precio"] = pd.to_numeric(df_items["precio"], errors="coerce")

        df_items = df_items[
            (df_items["sku"] != "")
            & (df_items["cantidad"] > 0)
            & (df_items["precio"].notna())
        ].copy()

        if df_items.empty:
            r_out["error"] = "detalle vacío tras validación"
            ocs_error += 1
            resultados.append(r_out)
            continue

        # Consolidar SKUs repetidos
        df_items = (
            df_items.sort_values(["sku"])
                   .groupby("sku", as_index=False)
                   .agg({"cantidad": "sum", "precio": "last"})
        )

        detalle_payload = []
        for _, r in df_items.iterrows():
            detalle_payload.append({
                "sku": str(r["sku"]),
                "cantidad": float(r["cantidad"]),
                "precio": float(r["precio"]),
            })

        r_out["items_enviados"] = len(detalle_payload)

        payload = {
            "cabecera": {
                "rut_proveedor": rut_prov,
                "destino": int(destino_id),
                "comentario": comentario_payload,
                "usuario": int(usuario),
                "confirmar": bool(confirmar),
                "autorizar": bool(autorizar),
            },
            "detalle": detalle_payload,
        }

        # --- POST ---
        try:
            resp = requests.post(api_url, json=payload, timeout=30)
            r_out["http_status"] = resp.status_code
            r_out["response_raw"] = (resp.text or "")[:1000]
        except Exception as e:
            r_out["error"] = f"Excepción HTTP: {e}"
            ocs_error += 1
            resultados.append(r_out)
            continue

        if not resp.ok:
            r_out["error"] = f"HTTP {resp.status_code}"
            ocs_error += 1
            resultados.append(r_out)
            continue

        try:
            data_resp = resp.json()
        except ValueError:
            r_out["error"] = "Respuesta no JSON"
            ocs_error += 1
            resultados.append(r_out)
            continue

        success = bool(data_resp.get("success"))
        r_out["success"] = success

        if success:
            r_out["oc_id"] = data_resp.get("OC")
            ocs_ok += 1
        else:
            # Error puede venir vacío -> dejar "" si no existe
            api_error = data_resp.get("Error")
            if api_error is None:
                api_error = data_resp.get("error")  # por si viene en minúscula
            r_out["error"] = (api_error or "")
            ocs_error += 1

        resultados.append(r_out)

    df_resultados = pd.DataFrame(resultados)

    print(f"[oc] Resumen: total={len(df_resultados)}, OK={ocs_ok}, error={ocs_error}")

    # columnas resumen útiles en Mage
    df_resultados["total_ocs"] = len(df_resultados)
    df_resultados["ocs_ok"] = ocs_ok
    df_resultados["ocs_error"] = ocs_error

    # si aun quieres una lista de errores "global"
    errores_globales = df_resultados.loc[df_resultados["success"] == False, "error"].tolist()

    return df_resultados