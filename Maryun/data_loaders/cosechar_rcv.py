"""
Baja el RCV del SII y devuelve el CSV crudo.

Adaptado del script de Ian (sii_libro_compras_ventas.py), que ya resolvia la
parte dificil: el login contra zeusr, el flujo diferido (generar archivo ->
esperar -> descargar gzip), el lote de varios periodos con una sola espera y la
fusion de los cuatro estados contables.

Lo que se le quito: la generacion del .xlsx y la interaccion por consola. Este
bloque no interpreta los datos — devuelve las lineas del CSV tal como las
entrega el SII. Quien las lee es el ERP, con el mismo codigo que usa su consola
manual (domain/sii/sii-service.ts::ingestarRcvCrudo).

Existe porque las peticiones al SII desde Vercel tardan demasiado. Esto corre en
el VPS y entrega el resultado por HTTP.

Configuracion (variables del pipeline o secrets):
    SII_RUT       12345678-9
    SII_CLAVE     la clave del SII

Que periodos trae: ver resolver_periodos(). En resumen, cada trigger elige uno
de estos y no vuelve a tocarse:

    meses: 2            el mes en curso y el anterior
    anio: "anterior"    los doce meses del anio pasado
    anio: 2024          los doce meses de 2024
    desde/hasta         un rango cerrado
    periodos: [...]     la lista exacta, si hace falta algo suelto
"""

import gzip
import os
import re
import time
import uuid

import requests

if 'data_loader' not in globals():
    from mage_ai.data_preparation.decorators import data_loader

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)

TOKEN_PATTERNS = [
    r"(?:^|[?&;])TOKEN=([A-Za-z0-9\-_.]+)",
    r"(?:^|[?&;])token=([A-Za-z0-9\-_.]+)",
    r"name\s*=\s*[\"']?(?:TOKEN|token)[\"']?[^>]*value\s*=\s*[\"']([^\"']+)[\"']",
    r"(?:TOKEN|token)\s*[:=]\s*[\"']?([A-Za-z0-9\-_.]+)",
]

# Los cuatro estados reales del SII. "TODOS" no es uno: es pedirlos los cuatro.
# El ERP los guarda por separado —cada documento lleva la pasada en que vino— y
# por eso aqui NO se fusionan: se entrega una tanda por estado.
ESTADOS = ["REGISTRO", "PENDIENTE", "RECLAMADO", "NO_INCLUIR"]

CTRL_URL = "https://www4.sii.cl/consdcvinternetui/services/data/facadeService/getCtrlAsync"


def _cfg(kwargs, nombre, obligatorio=True):
    """Busca un valor donde Mage puede tenerlo, en orden de preferencia.

    1. Variables del pipeline (kwargs) — es como Mage las pasa a los bloques.
    2. Secrets de Mage — quedan cifrados y no aparecen en la interfaz.
    3. Entorno del contenedor — si se inyectan por docker-compose.

    Se miran las tres porque fallar por buscar en el sitio equivocado es un
    error que cuesta ver: el valor existe, solo que en otra puerta.
    """
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


# ── Que meses traer ─────────────────────────────────────────────────
# Un trigger no deberia llevar meses escritos a mano: el 1 de septiembre
# ["202608","202607"] sigue trayendo agosto y julio, calladamente, y nadie mira
# un ETL que no falla. Aqui se declara la intencion —"los dos ultimos",
# "el anio pasado"— y las fechas salen solas en cada corrida.

def _norm(v):
    """Acepta 202608, "202608", "2026-08" y "2026/08". Devuelve "202608"."""
    s = str(v).strip().replace("-", "").replace("/", "").replace(" ", "")
    if len(s) != 6 or not s.isdigit() or not 1 <= int(s[4:]) <= 12:
        raise RuntimeError(
            "Periodo %r no valido. Usa YYYYMM o YYYY-MM (por ejemplo 202608)." % (v,)
        )
    return s


def _menos(periodo, n):
    """El periodo n meses antes. Cuenta en meses absolutos para no equivocarse
    en los saltos de anio, que es donde siempre se falla."""
    total = int(periodo[:4]) * 12 + int(periodo[4:]) - 1 - n
    return "%04d%02d" % (total // 12, total % 12 + 1)


def _rango(desde, hasta):
    """Del mas reciente al mas antiguo, ambos incluidos."""
    if desde > hasta:
        desde, hasta = hasta, desde
    salida, p = [], hasta
    while p >= desde:
        salida.append(p)
        p = _menos(p, 1)
    return salida


def resolver_periodos(kwargs, hoy=None):
    """Traduce las variables del trigger a una lista de YYYYMM.

    Se mira en este orden, y la primera que venga manda:

        periodos  ["202608", "2026-07"]   la lista exacta
        desde     "202501"                rango cerrado. Sin `hasta`, llega al
        hasta     "202512"                 mes en curso; sin `desde`, es solo
                                           ese mes.
        anio      2024 | "actual" | "anterior"
        meses     2                        los N ultimos, contando el actual

    Sin ninguna, el mes en curso — que es lo que hace la corrida diaria.

    "actual"/"anterior" existen para que un trigger anual no haya que tocarlo en
    enero. El anio en curso se corta en el mes de hoy: pedir meses que aun no
    han pasado gasta una consulta por cada uno para no traer nada.
    """
    hoy = hoy or time.strftime("%Y%m")

    if kwargs.get("periodos"):
        crudos = kwargs["periodos"]
        if isinstance(crudos, str):
            # Un trigger puede traerlo como texto: "202608,202607".
            crudos = [c for c in re.split(r"[,\s]+", crudos) if c]
        return sorted({_norm(p) for p in crudos}, reverse=True)

    if kwargs.get("desde") or kwargs.get("hasta"):
        desde = _norm(kwargs.get("desde") or kwargs.get("hasta"))
        hasta = _norm(kwargs.get("hasta") or hoy)
        return _rango(desde, hasta)

    anio = kwargs.get("anio")
    if anio not in (None, ""):
        texto = str(anio).strip().lower()
        if texto in ("actual", "este", "en_curso"):
            n = int(hoy[:4])
        elif texto in ("anterior", "pasado", "ultimo"):
            n = int(hoy[:4]) - 1
        else:
            try:
                n = int(texto)
            except ValueError:
                raise RuntimeError(
                    "anio=%r no valido. Usa un numero (2024) o "
                    "\"actual\"/\"anterior\"." % (anio,)
                )
            if not 2000 <= n <= 2100:
                raise RuntimeError("anio=%r fuera de rango." % (anio,))
        # El anio en curso se corta en el mes de hoy; los pasados van enteros.
        fin = hoy if n == int(hoy[:4]) else "%04d12" % n
        return _rango("%04d01" % n, fin)

    meses = kwargs.get("meses")
    if meses not in (None, ""):
        try:
            n = int(meses)
        except (TypeError, ValueError):
            raise RuntimeError("meses=%r no es un numero." % (meses,))
        if n < 1:
            raise RuntimeError("meses=%r tiene que ser 1 o mas." % (meses,))
        return [_menos(hoy, i) for i in range(n)]

    return [hoy]


class SiiAuth:
    def __init__(self, session, token, rut, dv):
        self.session = session
        self.token = token
        self.rut = rut
        self.dv = dv


def _es_limite_de_sesiones(body):
    """El SII no dice «limite alcanzado» de forma clara: hay que reconocerlo.

    Importa distinguirlo de un fallo de credenciales, porque la accion es otra:
    aqui no hay nada que arreglar, hay que esperar a que caduque una sesion.
    """
    if not body:
        return False
    low = body.lower()
    return (
        "maximo de sesiones autenticadas" in low
        or "01.01.123.500.709.27" in body
        or "01.01.192.500.709.27" in body
    )


def _token_de_texto(text):
    for pattern in TOKEN_PATTERNS:
        m = re.search(pattern, text or "", re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def _seguir_redirecciones(session, body):
    """El SII reparte cookies por varios dominios suyos antes de dar el token."""
    urls = re.findall(r"https://[^\s'\"]+", body or "")
    vistas = []
    for url in urls:
        url = url.rstrip(";")
        if ".sii.cl/" not in url.lower() or url in vistas:
            continue
        vistas.append(url)
        if len(vistas) > 3:
            break
        try:
            session.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
        except requests.RequestException:
            pass


def _cookies(auth):
    partes = ["%s=%s" % (c.name, c.value) for c in auth.session.cookies]
    nombres = set(c.name.upper() for c in auth.session.cookies)
    if "TOKEN" not in nombres:
        partes.append("TOKEN=%s" % auth.token)
    if "CSESSIONID" not in nombres:
        partes.append("CSESSIONID=%s" % auth.token)
    return "; ".join(partes)


def login(rut_completo, clave):
    cuerpo, dv = rut_completo.split("-", 1)

    for intento in range(1, 4):
        session = requests.Session()
        try:
            session.get(
                "https://zeusr.sii.cl/AUT2000/InicioAutenticacion/IngresoRutClave.html",
                headers={"User-Agent": USER_AGENT}, timeout=30,
            )
            resp = session.post(
                "https://zeusr.sii.cl/cgi_AUT2000/CAutInicio.cgi",
                data={
                    "rutcntr": rut_completo, "rut": cuerpo, "dv": dv, "clave": clave,
                    "referencia": "https://zeusr.sii.cl/AUT2000/InicioAutenticacion/IngresoRutClave.html",
                    "411": "",
                },
                headers={
                    "Origin": "https://zeusr.sii.cl",
                    "Referer": "https://zeusr.sii.cl/AUT2000/InicioAutenticacion/IngresoRutClave.html",
                    "User-Agent": USER_AGENT,
                },
                timeout=30,
            )
            if not resp.ok:
                if intento < 3:
                    time.sleep(0.45)
                    continue
                raise RuntimeError("El SII respondio %s al autenticar" % resp.status_code)

            if _es_limite_de_sesiones(resp.text):
                raise RuntimeError(
                    "El SII rechazo el login por limite de sesiones activas para este RUT. "
                    "No es un problema de credenciales: hay otras sesiones abiertas "
                    "(el portal, otro proceso) y hay que esperar a que caduquen."
                )

            _seguir_redirecciones(session, resp.text)
            try:
                session.get("https://www4.sii.cl/consdcvinternetui/",
                            headers={"User-Agent": USER_AGENT}, timeout=30)
            except requests.RequestException:
                pass

            token = None
            for c in session.cookies:
                if c.name.upper() == "TOKEN":
                    token = c.value
                    break
            token = token or _token_de_texto(resp.headers.get("Location", "")) or _token_de_texto(resp.text)

            if token:
                return SiiAuth(session, token, cuerpo, dv)
            if intento < 3:
                time.sleep(0.45)
        except requests.RequestException as ex:
            if intento >= 3:
                raise RuntimeError("Error de red al autenticar en el SII: %s" % ex)
            time.sleep(0.45)

    raise RuntimeError("No se obtuvo token de sesion del SII tras 3 intentos")


def logout(auth):
    """Cerrar la sesion no es opcional.

    Una sesion que queda abierta sigue ocupando plaza un buen rato, y el cupo
    por RUT es corto. Cuando se agota, al siguiente —un contador, por ejemplo—
    le sale «limite de sesiones alcanzado» sin forma de saber de donde salieron.
    """
    try:
        auth.session.get(
            "https://zeusr.sii.cl/cgi_AUT2000/autTermino.cgi",
            headers={
                "User-Agent": USER_AGENT,
                "Referer": "https://misiir.sii.cl/",
                "Cookie": _cookies(auth),
            },
            timeout=30,
        )
    except requests.RequestException:
        pass


def _lineas_de(parsed):
    """El SII devuelve el CSV como lista de lineas dentro de un JSON."""
    data = parsed.get("data") if isinstance(parsed, dict) else None
    return [l for l in (data or []) if l and l.strip()]


def fetch_en_linea(auth, operacion, periodo, estado):
    """Endpoint directo. Sirve para PENDIENTE, RECLAMADO y NO_INCLUIR."""
    es_venta = operacion == "VENTA"
    endpoint = "getDetalleVentaExport" if es_venta else "getDetalleCompraExport"

    resp = auth.session.post(
        "https://www4.sii.cl/consdcvinternetui/services/data/facadeService/%s" % endpoint,
        json={
            "metaData": {
                "conversationId": auth.token,
                "transactionId": str(uuid.uuid4()),
                "namespace": "cl.sii.sdi.lob.diii.consdcv.data.api.interfaces.FacadeService/%s" % endpoint,
            },
            "data": {
                "accionRecaptcha": "RCV_DDETV" if es_venta else "RCV_DDETC",
                "rutEmisor": auth.rut, "dvEmisor": auth.dv,
                "ptributario": periodo, "estadoContab": estado,
                "codTipoDoc": 0, "operacion": operacion,
                "tokenRecaptcha": "t-o-k-e-n-web",
            },
        },
        headers={"User-Agent": USER_AGENT, "Cookie": _cookies(auth)},
        timeout=60,
    )
    if not resp.ok:
        raise RuntimeError("El SII respondio %s para %s/%s" % (resp.status_code, estado, periodo))
    return _lineas_de(resp.json())


def _post_ctrl(auth, data):
    resp = auth.session.post(
        CTRL_URL,
        json={
            "metaData": {
                "conversationId": auth.token,
                "transactionId": str(uuid.uuid4()),
                "namespace": "cl.sii.sdi.lob.diii.consdcv.data.api.interfaces.FacadeService/getCtrlAsync",
            },
            "data": data,
        },
        headers={"User-Agent": USER_AGENT, "Cookie": _cookies(auth)},
        timeout=60,
    )
    return resp if resp.ok else None


def _ci(d, key):
    """El SII no garantiza el uso de mayusculas en sus campos."""
    if not isinstance(d, dict):
        return None
    for k, v in d.items():
        if k.lower() == key.lower():
            return v
    return None


def _listar_ctrl(auth, operacion, periodo):
    resp = _post_ctrl(auth, {
        "rutEmisor": auth.rut, "dvEmisor": auth.dv, "ptributario": periodo,
        "generaCtrl": False, "operacion": operacion, "estadoContab": "",
        "totDoc": "", "accionRecaptcha": "", "tokenRecaptcha": "",
    })
    if resp is None:
        return []
    try:
        items = (resp.json() or {}).get("data") or []
    except ValueError:
        return []
    salida = []
    for i in items:
        salida.append({
            "id": _ci(i, "caId"),
            "blob_id": _ci(i, "caIdBlob") or "",
            "operacion": "VENTA" if _ci(i, "compraOVenta") == "V" else "COMPRA",
            "estado": (_ci(i, "caEstado") or "").strip(),
        })
    return salida


def _descargar_ctrl(auth, control):
    if not control.get("blob_id") or not control.get("id"):
        raise RuntimeError("Control diferido sin identificadores para descargar")
    resp = auth.session.get(
        "https://www4.sii.cl/consdcvinternetui/services/data/facadeService/"
        "obtenerArchivoBLOB/%s/%s/%s/%s" % (control["blob_id"], auth.rut, auth.rut, control["id"]),
        headers={
            "User-Agent": USER_AGENT,
            "Referer": "https://www4.sii.cl/consdcvinternetui/index.html",
            "Cookie": _cookies(auth),
        },
        timeout=120,
    )
    if not resp.ok:
        raise RuntimeError("No se pudo descargar el archivo diferido: %s" % resp.status_code)
    return [l for l in gzip.decompress(resp.content).decode("utf-8").split("\n") if l.strip()]


def fetch_diferido_lote(auth, operacion, periodos, estado):
    """REGISTRO no cabe en el endpoint directo: hay que encargar el archivo.

    El SII responde 0 filas en vez de dar error cuando el detalle es demasiado
    extenso —igual que el aviso «Descargar detalles» de su sitio—, asi que un
    periodo grande pareceria vacio.

    Se encargan TODOS los periodos primero y se comparte una sola espera: el SII
    los genera en paralelo, y un anio pasa de doce esperas encadenadas a una.

    La espera crece con el numero de periodos y termina en cuanto no queda
    ninguno por generar. Antes era fija —unos once segundos— y con doce meses el
    SII no llegaba: los que faltaban salian con cero filas, sin error, y un mes
    entero podia perderse sin que nadie lo notara. Devuelve tambien los que se
    quedaron sin archivo, para que quien llama pueda quejarse.
    """
    previos = {}
    for p in periodos:
        previos[p] = set(c["id"] for c in _listar_ctrl(auth, operacion, p) if c["operacion"] == operacion)

    for p in periodos:
        _post_ctrl(auth, {
            "rutEmisor": auth.rut, "dvEmisor": auth.dv, "ptributario": p,
            "codTipoDoc": 0, "generaCtrl": True, "operacion": operacion,
            "estadoContab": estado, "totDoc": "0",
            "accionRecaptcha": "RCV_DDETVA" if operacion == "VENTA" else "RCV_DDETCA",
            "tokenRecaptcha": "t-o-k-e-n-web",
        })

    # Un mes suelto suele estar en cinco segundos; doce, no. Se sale en cuanto
    # estan todos, asi que el tope solo se alcanza cuando algo va mal de verdad.
    tope = min(20 + 8 * len(periodos), 240)
    limite = time.time() + tope

    time.sleep(5)
    nuevos = {}
    pendientes = list(periodos)
    while True:
        siguen = []
        for p in pendientes:
            lista = [c for c in _listar_ctrl(auth, operacion, p) if c["operacion"] == operacion]
            recientes = [c for c in lista if c["id"] not in previos[p]]
            if recientes:
                nuevos[p] = recientes
            else:
                siguen.append(p)
        pendientes = siguen
        if not pendientes or time.time() >= limite:
            break
        time.sleep(4)

    if pendientes:
        print("  AVISO: el SII no genero el archivo de %s tras %ds: %s"
              % (estado, tope, ", ".join(sorted(pendientes))))

    salida = {}
    for p in periodos:
        lineas = []
        for c in nuevos.get(p, []):
            if c["estado"].upper() == "TERMINADO":
                trozo = _descargar_ctrl(auth, c)
                # Solo la cabecera del primero: las siguientes son continuacion.
                lineas.extend(trozo if not lineas else trozo[1:])
        salida[p] = lineas
    return salida, pendientes


@data_loader
def cosechar_rcv(*args, **kwargs):
    """Devuelve una tanda por (estado, periodo), con el CSV sin interpretar.

    Variables del pipeline, todas opcionales:

        operacion   COMPRA (por defecto) o VENTA
        estados     por defecto los cuatro; VENTA solo admite REGISTRO

        meses       2                  el mes en curso y el anterior
        anio        "anterior" | 2024  los doce meses de ese anio
        desde/hasta "202501"/"202506"  un rango cerrado
        periodos    ["202608", ...]    la lista exacta

        fallar_si_falta   true por defecto — ver mas abajo

    Sin ninguna de las cuatro ultimas, el mes en curso.

    `fallar_si_falta` corta la corrida si el SII no llego a generar el archivo
    de algun periodo. Por defecto si: un mes que no viene se parece demasiado a
    un mes vacio, y un ETL que termina en verde con datos de menos es peor que
    uno que falla. Ponlo en false solo si de verdad esperas meses sin actividad.
    """
    operacion = (kwargs.get("operacion") or "COMPRA").upper()
    periodos = resolver_periodos(kwargs)
    # El SII solo publica REGISTRO para ventas: pedirle los otros tres devuelve
    # vacio y gasta una consulta por periodo.
    estados = kwargs.get("estados") or (["REGISTRO"] if operacion == "VENTA" else ESTADOS)
    fallar = str(kwargs.get("fallar_si_falta", True)).strip().lower() not in ("false", "0", "no")

    rut = _cfg(kwargs, "SII_RUT")
    clave = _cfg(kwargs, "SII_CLAVE")

    print("%s . %d periodo(s): %s" % (operacion, len(periodos), ", ".join(periodos)))

    t_inicio = time.time()
    auth = login(rut, clave)
    print("Login en %.1fs" % (time.time() - t_inicio))

    tandas = []
    sin_archivo = []
    try:
        for estado in estados:
            t0 = time.time()
            if estado == "REGISTRO":
                por_periodo, faltan = fetch_diferido_lote(auth, operacion, periodos, estado)
                sin_archivo.extend("%s/%s" % (estado, p) for p in faltan)
            else:
                por_periodo = {}
                for p in periodos:
                    try:
                        por_periodo[p] = fetch_en_linea(auth, operacion, p, estado)
                    except Exception as ex:
                        # Un periodo que falla no tumba los demas: faltaran sus
                        # documentos, y eso se ve en el recuento del exportador.
                        print("  %s/%s: ERROR %s" % (estado, p, ex))
                        por_periodo[p] = []
                        sin_archivo.append("%s/%s" % (estado, p))

            for p in sorted(por_periodo, reverse=True):
                lineas = por_periodo[p]
                print("  %s/%s: %d fila(s)" % (estado, p, max(0, len(lineas) - 1)))
                tandas.append({
                    "operacion": operacion,
                    "estadoContab": estado,
                    # El ERP usa YYYY-MM; el SII, YYYYMM.
                    "periodo": "%s-%s" % (p[:4], p[4:6]),
                    "lineas": lineas,
                })
            # El tiempo por estado, separado: es el numero que dice si la
            # lentitud esta en el SII o en el guardado del ERP.
            print("  %s: %.1fs en el SII" % (estado, time.time() - t0))
    finally:
        # En `finally` a proposito: si algo revienta a mitad, la sesion tiene
        # que cerrarse igual o se queda ocupando plaza del cupo del RUT.
        logout(auth)

    print("Cosecha completa en %.1fs" % (time.time() - t_inicio))

    # Despues del logout, nunca antes: la sesion se cierra pase lo que pase.
    if sin_archivo and fallar:
        raise RuntimeError(
            "El SII no entrego %d de %d combinaciones: %s. "
            "No se entrega nada a medias — relanza, o pon fallar_si_falta=false "
            "si esos periodos estan vacios de verdad."
            % (len(sin_archivo), len(periodos) * len(estados), ", ".join(sin_archivo))
        )

    return tandas