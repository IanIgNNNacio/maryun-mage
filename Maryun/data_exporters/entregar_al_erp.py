"""
Entrega la cartola cosechada al ERP.

Manda el CSV **sin interpretar** a `POST /api/banking/cartola`. El ERP lo lee
con el mismo codigo que usa la carga manual de una cartola y lo guarda
aplicando sus reglas: con que huella deduplica, que cuenta como el mismo
movimiento, como se reescriben los depositos en canje.

Por eso el ETL no escribe directo en la base. Si lo hiciera, la formula de la
huella tendria que existir tambien aqui, en Python, y ya vive en dos sitios
dentro del ERP con un comentario advirtiendo que si difieren, reimportar una
cartola vieja duplica la plata.

El `hash_mov` que calcula el data_loader viaja igual en el CSV, pero el ERP lo
ignora a proposito: calcula el suyo.

Idempotencia, garantizada en la base y no en este bloque:
    · UNIQUE (cuenta, correlativo) donde el movimiento esta LIQUIDADO
    · UNIQUE (cuenta, huella) para lo que no trae correlativo

Configuracion (variables del pipeline o secrets):
    CARTOLA_DESTINO     preview | vps | vercel | ambos   (por defecto: preview)
    ERP_URL_PREVIEW / ERP_SECRET_PREVIEW
    ERP_URL_VPS     / ERP_SECRET_VPS
    ERP_URL_VERCEL  / ERP_SECRET_VERCEL
"""

import io
import os
import time

import requests

if 'data_exporter' not in globals():
    from mage_ai.data_preparation.decorators import data_exporter

# El orden y los nombres son el contrato con el parser del ERP: si cambian aqui,
# hay que cambiar SANTANDER_HEADERS en domain/banking/statement-parse.ts.
COLUMNAS = (
    "banco", "cuenta", "ccc", "divisa",
    "nro_movimiento", "fecha_mov", "fecha_contable", "hora",
    "descripcion", "monto", "tipo", "saldo",
    "estado", "codigo_movimiento", "sucursal", "codigo_sucursal",
    "hash_mov", "ordinal", "extraido_en",
)

DESTINOS = {
    "preview": ("ERP_URL_PREVIEW", "ERP_SECRET_PREVIEW"),
    "vps": ("ERP_URL_VPS", "ERP_SECRET_VPS"),
    "vercel": ("ERP_URL_VERCEL", "ERP_SECRET_VERCEL"),
}


def _cfg(kwargs, nombre, obligatorio=True):
    """Variables del pipeline, luego secrets, luego el entorno."""
    v = kwargs.get(nombre)
    if not v:
        try:
            from mage_ai.data_preparation.shared.secrets import get_secret_value
            v = get_secret_value(nombre)
        except Exception:
            v = None
    if not v:
        v = os.environ.get(nombre)
    if not v and obligatorio:
        raise RuntimeError(
            "Falta %s. Ponlo como variable del pipeline, como secret de Mage "
            "o en el entorno del contenedor." % nombre
        )
    return v


def _destinos_elegidos(kwargs):
    elegido = (_cfg(kwargs, "CARTOLA_DESTINO", obligatorio=False) or "preview").strip().lower()
    if elegido == "ambos":
        return ["vercel", "vps"]
    if elegido not in DESTINOS:
        raise RuntimeError(
            "CARTOLA_DESTINO=%r no es valido. Usa: preview, vps, vercel o ambos." % elegido
        )
    return [elegido]


def _config(kwargs, destino):
    var_url, var_secret = DESTINOS[destino]
    return _cfg(kwargs, var_url).rstrip("/"), _cfg(kwargs, var_secret)


def _a_csv(df):
    """DataFrame -> CSV con las 19 columnas, en el orden del contrato.

    `monto` y `saldo` son Decimal y se serializan con str(), que da la forma
    canonica `-7000000.00`. Nada de float por el camino: el ERP lee ese formato
    con su propio parser porque el punto es decimal, no separador de miles.

    Los nulos van como celda vacia, no como "nan": el parser trata la celda
    vacia como null y "nan" seria una glosa con ese texto.
    """
    faltan = [c for c in COLUMNAS if c not in df.columns]
    if faltan:
        raise RuntimeError(
            "El DataFrame no trae las columnas %s. El contrato con el ERP son "
            "estas 19: %s" % (faltan, ", ".join(COLUMNAS))
        )
    buf = io.StringIO()
    df[list(COLUMNAS)].to_csv(buf, index=False, na_rep="", lineterminator="\n")
    return buf.getvalue()


def _detalle(r):
    """El motivo que dio el ERP, para que viaje DENTRO del error que se lanza.

    Antes solo se imprimia y el traceback de Mage no lo incluia: habia que ir a
    buscar la linea de arriba en el log. Ahora va en los dos sitios.
    """
    try:
        d = r.json()
        return d.get("detail") or d.get("error") or r.text[:300]
    except Exception:
        return (r.text or "")[:300]


def _leer_respuesta(r, base, destino):
    """Devuelve el JSON, o explica por que no lo es."""
    tipo = (r.headers.get("content-type") or "").lower()
    if "application/json" in tipo:
        return r.json()

    if r.is_redirect or (300 <= r.status_code < 400):
        pista = ""
        if destino in ("vps", "vercel"):
            pista = (
                " Esos dos corren la rama production, asi que un endpoint nuevo "
                "no existe ahi hasta que se publica: prueba con CARTOLA_DESTINO=preview."
            )
        raise RuntimeError(
            "%s redirigio en vez de responder: /api/banking/cartola no esta "
            "desplegado ahi.%s" % (base, pista)
        )
    raise RuntimeError(
        "%s respondio %s con content-type %r en vez de JSON. Primeros "
        "caracteres: %r" % (base, r.status_code, tipo or "(sin cabecera)", r.text[:120])
    )


@data_exporter
def entregar_al_erp(df, *args, **kwargs):
    """Manda la cartola y resume lo que respondio el ERP."""
    if df is None or df.empty:
        print("La cartola no trae movimientos: no se envia nada.")
        return []

    csv = _a_csv(df)
    banco = str(df["banco"].iloc[0])
    cuenta = str(df["cuenta"].iloc[0])
    origen = "mage/%s/%s/%s" % (
        kwargs.get("pipeline_uuid") or "maryun_santander_cartola",
        cuenta,
        str(df["extraido_en"].iloc[0]),
    )

    destinos = _destinos_elegidos(kwargs)
    print("Cartola %s %s: %d movimientos, %d KB" % (banco, cuenta, len(df), len(csv) // 1024))
    print("Destino: %s" % ", ".join(destinos))

    resumen = []
    fallidos = []
    for destino in destinos:
        base, secret = _config(kwargs, destino)
        t0 = time.time()
        try:
            r = requests.post(
                "%s/api/banking/cartola" % base,
                json={"contenido": csv, "origen": origen},
                headers={"Authorization": "Bearer %s" % secret},
                # Sin seguir redirecciones: si el ERP redirige es que la ruta no
                # existe ahi, y seguirla convierte ese fallo claro en un 200 con
                # HTML dentro.
                allow_redirects=False,
                timeout=300,
            )
        except requests.RequestException as ex:
            print("  %s: ERROR de red %s" % (destino, ex))
            fallidos.append("%s: error de red (%s)" % (destino, ex))
            continue

        if not r.ok:
            # El ERP devuelve el motivo en `detail`: cuenta no configurada,
            # cartola ilegible, columna que falta por una migracion sin aplicar.
            detalle = _detalle(r)
            print("  %s: ERROR %s %s" % (destino, r.status_code, detalle))
            fallidos.append("%s: HTTP %s - %s" % (destino, r.status_code, detalle))
            continue

        try:
            datos = _leer_respuesta(r, base, destino)
        except RuntimeError as ex:
            print("  %s: %s" % (destino, ex))
            fallidos.append("%s: %s" % (destino, ex))
            continue

        print("  %s: %d creados, %d ya estaban, %d canje rehechos, %d descartadas (%.1fs)" % (
            destino,
            datos.get("creados", 0),
            datos.get("omitidos", 0),
            datos.get("canjeReemplazados", 0),
            len(datos.get("descartadas") or []),
            time.time() - t0,
        ))
        # Una fila descartada es plata que NO entro: hay que verla, no contarla.
        for d in (datos.get("descartadas") or [])[:10]:
            print("      descartada linea %s: %s" % (d.get("linea"), d.get("motivo")))
        for a in (datos.get("avisos") or []):
            print("      aviso: %s" % a)
        resumen.append({"destino": destino, **{k: datos.get(k) for k in
                        ("creados", "omitidos", "canjeReemplazados", "statementId")}})

    # Que falle el bloque si algo no entro: en Mage un exportador que termina
    # bien es un ETL que corrio bien, y esto no lo seria. El motivo va en el
    # mensaje, asi que el traceback solo ya dice que paso.
    if fallidos:
        raise RuntimeError("No se pudo entregar la cartola. " + " | ".join(fallidos))

    return resumen