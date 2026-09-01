"""
Santander Office Banking (empresas) — cartola de cuenta corriente a DataFrame.

Corre en el VPS bajo Xvfb, porque el portal bloquea headless: responde "Revisa
tu conexion a internet" a un navegador sin cabeza. Headful en pantalla virtual
es el mismo Chrome que usa una persona.

Reemplaza a `scraping_santander-antesservia.py`. Ese dejo de funcionar y el
diagnostico salio del HAR de una sesion de discovery del 2026-08-06: el portal
tiene tres capas de defensa y el script viejo peleaba contra la equivocada.

    Akamai Bot Manager      cookies _abck / bm_sz / ak_bmsc
                            en wslogin, eob, api.officebanking.cl
    Imperva Incapsula       cookies incap_ses / visid_incap
                            en empresas y privado.officebanking.cl
    SDK conductual          POSTs a wup-*.santander.cl/client/v3.1/web/wup
                            (el CSP del endpoint permite bcdn-god.we-stats.com)

El script viejo tenia parches para PerimeterX, que no esta en ninguna de las
tres. Lo que si tenia eran cuatro contradicciones de huella:

  1. Sobreescribia el User-Agent a Chrome 136 sin tocar userAgentMetadata, asi
     que mandaba UA=136 y sec-ch-ua=<version real>. Akamai cruza esos dos
     headers. Aqui NO se toca el UA: el real siempre es consistente.
  2. Pasaba --disable-features dos veces; Chrome se queda con el ultimo, asi
     que IsolateOrigins nunca se desactivaba. Ahora va una sola vez.
  3. Inyectaba el stealth con tab.evaluate DESPUES de navegar. El sensor corre
     en document_start y ya habia leido navigator.webdriver. Ahora va por
     Page.addScriptToEvaluateOnNewDocument.
  4. Tecleaba con dispatch_key_event(text=...) sin key ni keyCode, y no movia
     el mouse en toda la sesion. Para un SDK conductual eso es una sesion sin
     una sola pulsacion valida ni un solo evento de puntero.

Y la causa de muerte probable: no manejaba la Superclave. El portal la pide
cuando el score de riesgo sube, y borrar el perfil cada 80 corridas
(PROFILE_MAX_RUNS) garantizaba que el dispositivo volviera a ser "nuevo". Aqui
el perfil NO se rota, y si aparece un segundo factor el bloque falla con un
mensaje claro en vez de girar en vano. No se evade: eso lo autoriza una persona.

Lo que se porto del proyecto Playwright (scraping-santander/Scrapping), que
tiene el nucleo verificado contra 8.252 movimientos reales:

  - Interceptar el JSON que pide el propio portal en vez de reconstruir la
    peticion. El endpoint exige `Authorization: Bearer` y cookies; la peticion
    la hace el portal y nosotros solo leemos la respuesta.
  - hash_mov con ORDINAL dentro del grupo (fecha, monto, tipo, descripcion).
    Sin el, en enero 2026 hay 14 grupos de movimientos identicos el mismo dia,
    uno repetido 7 veces (Pago de Asigna, -$7.000.000): deduplicar por
    contenido perderia $42.000.000.
  - Validacion de saldo corrido con NuevoSaldo: si saldo[i] != saldo[i-1] +
    monto[i], falta un movimiento. Es el unico control que detecta el modo de
    falla peligroso, que es traer MENOS datos en silencio.
  - Validacion de cobertura del tramo.
  - Fechas ISO aaaa-mm-dd: en SQL el orden lexicografico de ISO es el
    cronologico, con dd-mm-yyyy el ORDER BY sale mal.
  - Tramos de 15 dias, tope de paginas con AVISO explicito (nunca truncar
    callados) y montos ilegibles que fallan en vez de valer 0.

Corregido el 2026-08-17 con 100 movimientos reales de la primera corrida:

  - El filtro de fechas no se estaba aplicando. La pantalla dispara su propia
    consulta al abrirse y `esperar()` tomaba ESA respuesta, asi que se paginaba
    sobre la ventana por defecto del portal: se pidieron dos dias y llegaron 79
    filas de otras fechas. Ahora cada respuesta se correlaciona contra
    `Result.FechaDesde`/`FechaHasta` y, si el portal contesta otro rango, el
    bloque falla en vez de entregar datos equivocados.
  - Los depositos en canje (`Depos.Docto.*`) llevan `estado: EN_CANJE`. No son
    otra serie: CONTINUAN la cadena de saldo, pero su correlativo reinicia en 1
    y va al reves. Antes rompian la validacion con cinco saltos inexistentes.
  - Salida plana, sin la columna raw_json. `Divisa` y `CCC` viven en el sobre
    `Result` y antes se perdian.
  - `hash_mov` incluye el banco, para que los correlativos de BCI y Banco de
    Chile no colisionen con los de Santander en la tabla de destino.

Y con la segunda corrida, del mismo dia:

  - Los montos son Decimal con dos decimales, no enteros. Van a entrar cuentas
    en dolares y un redondeo a entero perderia los centavos sin dejar rastro.
  - Un salto en el saldo corrido ya no es automaticamente "falta un
    movimiento": si hay hueco en el correlativo, el movimiento existe pero
    quedo fuera del rango pedido. Ver `validar_saldo_corrido`.
  - `validar_cobertura` mira `fecha_contable`, que es por lo que filtra el
    portal, y no `fecha_mov`.
  - Las llamadas a CDP tienen timeout: sin el, una conexion trabada colgaba la
    corrida entera (paso, ocho minutos, y hubo que interrumpirla a mano).

Y con la tercera, que pidio cuatro dias y trajo una sola pagina de 50:

  - Los clicks disparaban DOS veces (dispatchEvent('click') mas btn.click()).
    Con Consultar eso pedia la consulta dos veces, y la segunda respuesta
    quedaba flotando; al apretar "siguiente" se leia ESA y parecia que la
    pagina 2 no traia nada nuevo. La paginacion se cortaba en la primera
    pagina dejando 47 movimientos afuera, y terminaba en verde.
  - Ahora cada respuesta se identifica con el cursor `MovimientoDesde` /
    `MovimientoHasta` del sobre, asi que una repetida no se confunde con una
    pagina nueva, y `limpiar()` vacia lo pendiente antes de cada pagina.
  - Y si la ultima pagina leida vino LLENA, queda un AVISO: es la senal de que
    faltan movimientos.

Buenas practicas tomadas de kaihv/open-banking-chile (CONTRIBUTING, "Tips para
scraping de bancos chilenos"): login en dos pasos, navegar por clicks y no por
URLs, cerrar popups post-login, 2FA con error claro sin bypassear, delays
generosos de 2-4s, screenshots en cada paso, arrays de selectores con fallback
porque los bancos cambian el HTML.

Configuracion (variables del pipeline, secrets de Mage, entorno, o los valores
por defecto de abajo, en ese orden):

    SANTANDER_RUT       RUT del usuario de Office Banking, sin puntos
    SANTANDER_CLAVE     la clave
    SANTANDER_CUENTA    numero de cuenta SIN ceros a la izquierda
    SANTANDER_CCC       cuenta completa (el endpoint no la devuelve)
    SANTANDER_DIVISA    CLP

Que rango trae:

    desde / hasta   "2026-08-01" / "2026-08-17"   rango cerrado
    dias            7                             los N ultimos dias CONTANDO
                                                  HOY. dias=3 un lunes 17 es
                                                  15..17, no 14..17
    (nada)                                        ayer y hoy

OJO con el rango: el formulario del portal filtra por FECHA CONTABLE, no por
fecha de movimiento. Pedir 15..17 devuelve movimientos con fecha_mov del viernes
14 que se contabilizaron el lunes 17, y NO devuelve los que se contabilizaron el
propio 14. Para cuadrar contra lo que se ve en el portal hay que pensar en fecha
contable.

Otras variables opcionales:

    max_dias            15     dias por consulta; el rango se parte en tramos
    max_paginas         40     tope por tramo, con AVISO si se alcanza
    fallar_si_vacio     false  si true, un tramo sin movimientos tumba la corrida
"""

import asyncio
import hashlib
import json
import os
import random
import re
import subprocess
import time
import unicodedata
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import nest_asyncio
import pandas as pd
import pytz
import nodriver as uc

if 'data_loader' not in globals():
    from mage_ai.data_preparation.decorators import data_loader
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test

TZ_CL = pytz.timezone("America/Santiago")

# Valores por defecto. _cfg() deja que una variable del pipeline o un secret de
# Mage los tape sin tocar el codigo; estan aqui porque este pipeline vive solo en
# el VPS y no va a git.
RUT_DEFECTO = "200960203"
CUENTA_DEFECTO = "4371186"

# El sobre `Result` del endpoint NO trae CCC ni Divisa (comprobado en la corrida
# del 2026-08-17: salieron vacias las 47 filas). Se leen igual del payload por si
# el portal las agrega, y si no estan se usan estos valores. `dbg` registra una
# vez las claves reales del sobre, para poder corregir esto con evidencia.
CCC_DEFECTO = "00350243000004371186"
DIVISA_DEFECTO = "CLP"

# La clave va vacia a proposito. Rellena UNA de las dos:
#
#   a) pegala aqui entre las comillas — vale, el pipeline no sale del VPS; o
#   b) mejor: creala como secret de Mage con el nombre SANTANDER_CLAVE y deja
#      esto vacio. _cfg() la encuentra igual y no queda en el codigo del bloque,
#      que es lo que se ve en la interfaz de Mage y en los logs de edicion.
CLAVE_DEFECTO = ""

CHROME_PATH = "/usr/bin/google-chrome"
SCREENSHOT_DIR = "/opt/python-scripts/scrapings/screenshots"
PROFILE_DIR = "/opt/python-scripts/scrapings/chrome_profile"
DISPLAY_NUM = ":99"

# La portada que sirve. www.officebanking.cl responde 403 a un navegador
# automatizado, con una pagina titulada "Internet Connection Error" que parece
# un problema de red propio y no lo es.
URL_PORTADA = "https://empresas.officebanking.cl"

# Ventana fija. El script viejo rotaba entre cinco tamanos por corrida: para un
# SDK conductual, un dispositivo que cambia de resolucion cada dia es mas raro
# que uno que no cambia nunca.
ANCHO, ALTO = 1920, 1080

T_LOGIN = 120
T_MENU = 45
T_MOVIMIENTOS = 60
T_RESPUESTA = 25
# Tope por llamada a CDP. Sin esto una conexion trabada cuelga la corrida entera.
T_CDP = 20

MAX_DIAS_DEFECTO = 15
MAX_PAGINAS_DEFECTO = 40
# Cuantos movimientos devuelve el portal por pagina. Solo se usa para detectar
# truncamiento: si la ultima pagina leida vino LLENA, es que quedaron mas.
TAM_PAGINA = 50

# El endpoint de movimientos del portal empresa. Vive en su propio backend
# (eob.officebanking.cl), no en los hosts de banca personas.
PATRON_MOVIMIENTOS = r"SaldoCuentaCorriente/ObtenerMovimientos"
RUTA_LISTA = ("Result", "Detalle")

# Va en cada fila y en el hash. Cuando se sumen BCI y Banco de Chile, la tabla
# de destino se une sola y ningun correlativo puede colisionar entre bancos.
BANCO = "SANTANDER"

CARGO = "CARGO"
ABONO = "ABONO"

# Estado del movimiento.
#   LIQUIDADO  el banco lo cerro: TipoMovimiento es H (haber) o D (debe) y
#              NroMovimiento es el correlativo real de la cuenta.
#   EN_CANJE   deposito de documentos que todavia no liquida (Depos.Docto.*).
#              TipoMovimiento viene en blanco, la fecha es futura, no trae hora
#              y su NroMovimiento REINICIA en 1 contando al REVES: el 1 es el
#              ultimo en liquidarse.
#
# Verificado el 2026-08-17 contra 100 movimientos reales: los EN_CANJE no son
# otra serie, CONTINUAN la cadena de saldo del libro liquidado. Con el ultimo
# liquidado en 19.314.292, los cuatro canje encadenan exacto hasta 22.738.815.
# Por eso se conservan; pero son una FOTO QUE MUTA (cuando el cheque liquida,
# el mismo deposito reaparece con correlativo real), asi que quien los cargue
# deberia borrar los EN_CANJE de la cuenta antes de insertar, o excluirlos de
# la conciliacion hasta que liquiden. Para eso esta `extraido_en`.
LIQUIDADO = "LIQUIDADO"
EN_CANJE = "EN_CANJE"

# Esquema de salida. Plano y en este orden: es el contrato con lo que venga
# despues (Postgres o un POST al ERP). Clave primaria sugerida: hash_mov, que
# ya lleva banco y cuenta dentro. Indice util: (banco, cuenta, fecha_mov).
COLUMNAS = (
    "banco", "cuenta", "ccc", "divisa",
    "nro_movimiento", "fecha_mov", "fecha_contable", "hora",
    "descripcion", "monto", "tipo", "saldo",
    "estado", "codigo_movimiento", "sucursal", "codigo_sucursal",
    "hash_mov", "ordinal", "extraido_en",
)

# Frases que delatan un segundo factor. Se detecta por TEXTO y no por selector:
# el DOM cambia, el texto que ve la persona casi no.
PALABRAS_2FA = (
    "SUPERCLAVE", "SUPER CLAVE", "CLAVE DINAMICA", "CODIGO DE VERIFICACION",
    "INGRESA TU TOKEN", "INGRESE SU TOKEN", "TARJETA DE COORDENADAS",
    "SEGUNDO FACTOR", "AUTORIZA EN TU APP", "APRUEBA EN TU APP",
    "NOTIFICACION ENVIADA", "CODIGO SMS",
)

PALABRAS_RECHAZO = (
    "CLAVE INCORRECTA", "USUARIO O CLAVE", "DATOS INCORRECTOS", "BLOQUEAD",
    "RECHAZAD", "DENEGAD", "NO AUTORIZAD", "INTENTO FALLID",
)

PALABRAS_DESAFIO = ("CAPTCHA", "NO SOY UN ROBOT", "VERIFICA QUE ERES HUMANO")

# Popups post-login: ofertas, encuestas, avisos de cambio de clave. Se cierran
# por texto, tolerantes: si no estan, se sigue.
TEXTOS_POPUP = (
    "ACEPTAR", "CONTINUAR", "ENTENDIDO", "CERRAR", "MAS TARDE",
    "NO, GRACIAS", "OMITIR", "RECORDAR DESPUES",
)

# Arrays de selectores con fallback, porque los bancos cambian el HTML. Se
# prueban en orden hasta que uno responda.
SEL_FECHA_DESDE = (
    "#FechaDesde",
    "input[id*='echaDesde']",
    "input[name*='FechaDesde']",
    "input[id*='desde' i]",
)
SEL_FECHA_HASTA = (
    "#FechaHasta",
    "input[id*='echaHasta']",
    "input[name*='FechaHasta']",
    "input[id*='hasta' i]",
)
SEL_CONSULTAR = (
    "button[data-bind*='BuscarMovimientos']",
    "a[data-bind*='BuscarMovimientos']",
    "button[id*='onsultar']",
)
# Hay DOS controles PaginaSiguiente: uno es de la linea de credito asociada
# (data-bind con "Cred"). Hay que excluirlo o se pagina la tabla equivocada.
SEL_SIGUIENTE = "a[data-bind*='PaginaSiguiente']"

XVFB_PROCESS = None


# ── Configuracion ─────────────────────────────────────────────────────────────

def _cfg(kwargs, nombre, defecto=None):
    """Busca un valor donde Mage puede tenerlo, en orden de preferencia.

    Variables del pipeline, luego secrets de Mage, luego el entorno del
    contenedor, luego el defecto del codigo. Se miran los cuatro porque fallar
    por buscar en el sitio equivocado es un error que cuesta ver: el valor
    existe, solo que en otra puerta.
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
    if not v:
        v = defecto
    if not v:
        raise RuntimeError(
            "Falta %s. Ponlo como variable del pipeline, como secret de Mage "
            "o en el entorno del contenedor." % nombre
        )
    return v


def resolver_rango(kwargs, hoy=None):
    """Traduce las variables del trigger a (desde, hasta).

    Se mira en este orden y la primera que venga manda:

        desde / hasta   "2026-08-01" / "2026-08-17"   rango cerrado. Sin
                                                       `hasta`, hasta hoy.
        dias            7                             los N ultimos dias
                                                       CONTANDO HOY
        (nada)                                        ayer y hoy

    Un trigger no deberia llevar fechas escritas a mano: el mes siguiente
    seguiria trayendo agosto calladamente, y nadie mira un ETL que no falla.

    >>> resolver_rango({"dias": 3}, date(2026, 8, 17))
    (datetime.date(2026, 8, 15), datetime.date(2026, 8, 17))
    >>> resolver_rango({}, date(2026, 8, 17))
    (datetime.date(2026, 8, 16), datetime.date(2026, 8, 17))
    """
    hoy = hoy or datetime.now(TZ_CL).date()

    def _iso(v):
        if isinstance(v, date):
            return v
        try:
            return date.fromisoformat(str(v).strip()[:10])
        except ValueError:
            raise RuntimeError(
                "Fecha %r no valida. Usa aaaa-mm-dd (por ejemplo 2026-08-01)." % (v,)
            )

    if kwargs.get("desde") or kwargs.get("hasta"):
        desde = _iso(kwargs.get("desde") or kwargs.get("hasta"))
        hasta = _iso(kwargs.get("hasta") or hoy)
        if desde > hasta:
            desde, hasta = hasta, desde
        return desde, hasta

    dias = kwargs.get("dias")
    if dias not in (None, ""):
        try:
            n = int(dias)
        except (TypeError, ValueError):
            raise RuntimeError("dias=%r no es un numero." % (dias,))
        if n < 1:
            raise RuntimeError("dias=%r tiene que ser 1 o mas." % (dias,))
        return hoy - timedelta(days=n - 1), hoy

    return hoy - timedelta(days=1), hoy


def tramos_fechas(desde, hasta, max_dias):
    """Parte un rango en tramos consultables. Contiguos y sin solaparse.

    Office Banking abre con una ventana de 15 dias, asi que un rango largo se
    pide por partes.

    >>> tramos_fechas(date(2026, 1, 1), date(2026, 1, 10), 4)
    [(datetime.date(2026, 1, 1), datetime.date(2026, 1, 4)), (datetime.date(2026, 1, 5), datetime.date(2026, 1, 8)), (datetime.date(2026, 1, 9), datetime.date(2026, 1, 10))]
    """
    if desde > hasta:
        raise RuntimeError("rango invertido: %s > %s" % (desde, hasta))
    if max_dias < 1:
        raise RuntimeError("max_dias tiene que ser 1 o mas, llego %r" % (max_dias,))
    salida, inicio = [], desde
    while inicio <= hasta:
        fin = min(inicio + timedelta(days=max_dias - 1), hasta)
        salida.append((inicio, fin))
        inicio = fin + timedelta(days=1)
    return salida


def to_ui_date(d):
    """Formato que espera el formulario del portal."""
    return d.strftime("%d/%m/%Y")


# ── Xvfb y perfil ─────────────────────────────────────────────────────────────

def start_xvfb(dbg):
    global XVFB_PROCESS
    subprocess.run(["pkill", "-f", "Xvfb %s" % DISPLAY_NUM], capture_output=True)
    time.sleep(0.5)
    lock = "/tmp/.X%s-lock" % DISPLAY_NUM.replace(":", "")
    if os.path.exists(lock):
        try:
            os.remove(lock)
        except OSError:
            pass
    XVFB_PROCESS = subprocess.Popen(
        ["Xvfb", DISPLAY_NUM, "-screen", "0", "%dx%dx24" % (ANCHO, ALTO),
         "-ac", "+extension", "GLX"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1.5)
    if XVFB_PROCESS.poll() is not None:
        dbg("Xvfb no arranco")
        return False
    os.environ["DISPLAY"] = DISPLAY_NUM
    return True


def stop_xvfb():
    global XVFB_PROCESS
    if XVFB_PROCESS and XVFB_PROCESS.poll() is None:
        XVFB_PROCESS.terminate()
        try:
            XVFB_PROCESS.wait(timeout=5)
        except subprocess.TimeoutExpired:
            XVFB_PROCESS.kill()
    XVFB_PROCESS = None


def liberar_perfil(dbg):
    """Mata un Chrome zombi que tenga tomado el perfil.

    Importa porque Chromium, al encontrar el perfil ocupado, NO abre una
    instancia nueva: le pasa el pedido a la que ya existe y se cierra, dejando
    al driver hablando con un target muerto. Los sintomas no apuntan nunca a la
    causa: paginas de error de red, campos que no se llenan, esperas que nunca
    terminan.

    El lock es un symlink cuyo destino es "<host>-<pid>".

    A diferencia del script viejo, el perfil NO se borra. Rotarlo cada N
    corridas es lo que hacia que el dispositivo pareciera nuevo y disparaba la
    Superclave: un perfil estable es lo que mantiene la confianza del banco.
    """
    lock = os.path.join(PROFILE_DIR, "SingletonLock")
    if not os.path.islink(lock):
        return
    try:
        destino = os.readlink(lock)
    except OSError:
        return
    posible = destino.rsplit("-", 1)[-1]
    if not posible.isdigit():
        return
    pid = int(posible)
    try:
        os.kill(pid, 0)
    except OSError:
        dbg("lock rancio, el proceso %d ya no esta" % pid)
        try:
            os.remove(lock)
        except OSError:
            pass
        return
    dbg("el perfil lo tiene tomado el pid %d; se cierra" % pid)
    try:
        os.kill(pid, 15)
        time.sleep(2)
        os.kill(pid, 9)
    except OSError:
        pass


# ── Comportamiento humano ─────────────────────────────────────────────────────

def _h(base, spread=0.3):
    """Delay con distribucion humana: 5% de las veces una pausa larga.

    La referencia pide delays generosos de 2-4s porque estos portales son
    lentos; la distribucion evita que todas las pausas midan lo mismo.
    """
    if random.random() < 0.05:
        return base * random.uniform(3.0, 6.0)
    return max(0.05, random.gauss(base, base * spread))


# Codigo fisico de la tecla. Sin `code` ni `windows_virtual_key_code` el evento
# llega sin keyCode y un SDK conductual ve pulsaciones que ningun teclado
# genera. Es el fallo mas caro del script viejo.
def _code_de(char):
    if char.isdigit():
        return "Digit%s" % char, ord(char)
    if char.isalpha():
        return "Key%s" % char.upper(), ord(char.upper())
    especiales = {
        "@": ("Digit2", 50), ".": ("Period", 190), "-": ("Minus", 189),
        "_": ("Minus", 189), "!": ("Digit1", 49), "#": ("Digit3", 51),
        "$": ("Digit4", 52), "%": ("Digit5", 53), "&": ("Digit7", 55),
        "*": ("Digit8", 56), "(": ("Digit9", 57), ")": ("Digit0", 48),
        "+": ("Equal", 187), "=": ("Equal", 187), "/": ("Slash", 191),
        ",": ("Comma", 188), " ": ("Space", 32),
    }
    return especiales.get(char, ("", 0))


def _delay_de(char):
    """Los simbolos y las mayusculas cuestan mas que las minusculas."""
    if not char.isalnum():
        return random.uniform(0.15, 0.28), random.uniform(0.05, 0.12)
    if char.isupper():
        return random.uniform(0.12, 0.22), random.uniform(0.04, 0.10)
    if char.isdigit():
        return random.uniform(0.10, 0.18), random.uniform(0.04, 0.09)
    return random.uniform(0.06, 0.14), random.uniform(0.03, 0.08)


async def _teclear(tab, texto):
    """Tipeo caracter a caracter con eventos de teclado completos.

    NUNCA se registra el texto: por aqui pasa la clave.
    """
    for char in texto:
        if random.random() < 0.08:
            await asyncio.sleep(random.uniform(0.3, 0.7))
        code, vk = _code_de(char)
        abajo, arriba = _delay_de(char)
        comun = dict(
            key=char, code=code, text=char, unmodified_text=char,
            windows_virtual_key_code=vk, native_virtual_key_code=vk,
        )
        await tab.send(uc.cdp.input_.dispatch_key_event(type_="keyDown", **comun))
        await asyncio.sleep(abajo)
        await tab.send(uc.cdp.input_.dispatch_key_event(type_="keyUp", **comun))
        await asyncio.sleep(arriba)
    await asyncio.sleep(random.uniform(0.2, 0.5))


async def _tab_key(tab):
    for tipo in ("keyDown", "keyUp"):
        await tab.send(uc.cdp.input_.dispatch_key_event(
            type_=tipo, key="Tab", code="Tab",
            windows_virtual_key_code=9, native_virtual_key_code=9,
        ))
        await asyncio.sleep(0.15)


async def _pasear_mouse(tab, pasos=None):
    """Mueve el puntero por la pantalla.

    El script viejo no generaba un solo evento de puntero en toda la sesion.
    Para un motor de biometria conductual eso es mas raro que cualquier
    fingerprint: una sesion humana sin mouse no existe.
    """
    x, y = random.randint(200, ANCHO - 200), random.randint(200, ALTO - 200)
    for _ in range(pasos or random.randint(3, 6)):
        dx, dy = random.randint(-350, 350), random.randint(-250, 250)
        destino_x = min(max(20, x + dx), ANCHO - 20)
        destino_y = min(max(20, y + dy), ALTO - 20)
        # Interpolado: un salto instantaneo de 300px tampoco lo hace una mano.
        for i in range(1, 9):
            ix = x + (destino_x - x) * i / 8.0
            iy = y + (destino_y - y) * i / 8.0
            try:
                await tab.send(uc.cdp.input_.dispatch_mouse_event(
                    type_="mouseMoved", x=ix, y=iy,
                ))
            except Exception:
                return
            await asyncio.sleep(random.uniform(0.012, 0.035))
        x, y = destino_x, destino_y
        await asyncio.sleep(_h(0.4))


async def _warmup(tab, dbg):
    dbg("warmup")
    await asyncio.sleep(_h(2.0))
    await _pasear_mouse(tab)
    for _ in range(random.randint(2, 4)):
        await tab.evaluate(
            "window.scrollBy({top: %d, behavior: 'smooth'})" % random.randint(150, 400)
        )
        await asyncio.sleep(_h(1.2))
    await asyncio.sleep(_h(2.0))
    await tab.evaluate("window.scrollTo({top: 0, behavior: 'smooth'})")
    await asyncio.sleep(_h(1.0))


# ── Stealth ───────────────────────────────────────────────────────────────────
# Va por Page.addScriptToEvaluateOnNewDocument, que corre en document_start y en
# cada frame nuevo. El script viejo lo inyectaba con tab.evaluate despues de
# navegar: el sensor ya habia leido navigator.webdriver.
#
# Deliberadamente corto. No se toca el User-Agent ni se falsean plugins con
# formas que no calzan con el Chrome real: una mentira mal cosida delata mas
# que la verdad. Solo se tapan las huellas que deja la automatizacion.

_STEALTH_JS = r"""
(() => {
  try {
    if (navigator.webdriver) {
      Object.defineProperty(Navigator.prototype, 'webdriver', {get: () => false});
    }
  } catch (e) {}
  try {
    if (window.outerHeight === 0) {
      Object.defineProperty(window, 'outerHeight', {get: () => window.innerHeight + 88});
      Object.defineProperty(window, 'outerWidth', {get: () => window.innerWidth});
    }
  } catch (e) {}
  try {
    const q = window.navigator.permissions.query.bind(navigator.permissions);
    const envuelto = (p) => p && p.name === 'notifications'
      ? Promise.resolve({state: Notification.permission})
      : q(p);
    window.navigator.permissions.query = envuelto;
    const ts = Function.prototype.toString;
    Function.prototype.toString = function () {
      if (this === envuelto) return 'function query() { [native code] }';
      return ts.call(this);
    };
  } catch (e) {}
})();
"""


# ── Interceptacion ────────────────────────────────────────────────────────────

# Respaldo: envuelve XMLHttpRequest DENTRO del frame y acumula las respuestas
# en un atributo del <body>. Es lo que hacia el script viejo y se conserva solo
# como red de seguridad, porque tiene dos defectos: se ejecuta en el mundo real
# de la pagina (el SDK antifraude tambien envuelve XHR y puede notar que
# alguien llego antes) y se pierde si el frame se recarga.
#
# Se instala solo si la via CDP no capturo nada. Ver Captura.
_XHR_RESPALDO_JS = r"""
(() => {
    if (window.__cartola_xhr__) return 'ya';
    window.__cartola_xhr__ = true;
    const script = document.createElement('script');
    script.textContent = `
        (() => {
            if (window.__cartola_patched__) return;
            window.__cartola_patched__ = true;
            document.body.setAttribute('data-cartola', '[]');
            const abrir = XMLHttpRequest.prototype.open;
            const enviar = XMLHttpRequest.prototype.send;
            XMLHttpRequest.prototype.open = function (m, u, ...r) {
                this.__cartola_url__ = u;
                return abrir.call(this, m, u, ...r);
            };
            XMLHttpRequest.prototype.send = function (...a) {
                this.addEventListener('load', function () {
                    const u = (this.__cartola_url__ || '').toLowerCase();
                    if (u.indexOf('obtenermovimientos') === -1) return;
                    try {
                        const acc = JSON.parse(document.body.getAttribute('data-cartola') || '[]');
                        acc.push(this.responseText);
                        document.body.setAttribute('data-cartola', JSON.stringify(acc));
                    } catch (e) {}
                });
                return enviar.call(this, ...a);
            };
        })();
    `;
    document.head.appendChild(script);
    script.remove();
    return 'instalado';
})()
"""

_XHR_VACIAR_JS = """
(() => {
    const acc = document.body ? document.body.getAttribute('data-cartola') : null;
    if (document.body) document.body.setAttribute('data-cartola', '[]');
    return acc || '[]';
})()
"""


class Captura:
    """Lee las respuestas del endpoint de movimientos.

    Es la pieza central: en vez de reconstruir la peticion (que exige
    `Authorization: Bearer` de 690 caracteres y ~8 KB de cookies de Akamai), se
    maneja la UI y se escucha lo que el propio portal pide.

    Via principal: el dominio Network de CDP, que no es observable desde JS.
    Funciona sobre el iframe de eob.officebanking.cl porque se lanza Chrome con
    site-per-process desactivado, asi que el subframe comparte el target de la
    pagina. Ojo: eso solo pasa porque el flag --disable-features va una sola vez
    en la linea de comandos; en el script viejo iba dos veces y Chrome se
    quedaba con el ultimo, con lo que IsolateOrigins nunca se desactivaba. De
    ahi que ese script tuviera que parchear XHR dentro del frame.

    Via de respaldo: ese mismo parche, que se instala solo si la CDP no capturo
    nada. Si hiciera falta, queda un AVISO: significa que el subframe salio a
    otro proceso y conviene saberlo.

    El cuerpo se pide en LoadingFinished y no en ResponseReceived: en
    ResponseReceived todavia puede no estar completo en el buffer.
    """

    def __init__(self, tab, patron):
        self.tab = tab
        self.patron = re.compile(patron, re.I)
        self.esperando = {}
        # `llegados` son respuestas sin clasificar; `aceptados` las que pasaron
        # el filtro de rango; `descartados` las que no (se guardan para poder
        # decir en el error QUE rango trajo el portal en vez del pedido).
        self.llegados = []
        self.aceptados = []
        self.descartados = []
        self.sin_verificar = 0
        self.errores = []
        self.ambito = None
        self.respaldo_instalado = False
        self.uso_respaldo = False

    async def instalar(self):
        # Con buffer explicito: por defecto Chrome descarta cuerpos pronto y
        # getResponseBody contesta -32000 "No resource with given identifier
        # found", que es justo lo que paso el 2026-08-17 (seis veces). Cuando
        # eso ocurre se cae al respaldo XHR, pero conviene que no ocurra.
        await self.tab.send(uc.cdp.network.enable(
            max_total_buffer_size=100_000_000,
            max_resource_buffer_size=20_000_000,
        ))
        self.tab.add_handler(uc.cdp.network.ResponseReceived, self._on_response)
        self.tab.add_handler(uc.cdp.network.LoadingFinished, self._on_finished)

    def usar_frame(self, ambito):
        """Registra el frame de la cartola, por si hay que caer al respaldo."""
        self.ambito = ambito

    async def instalar_respaldo(self, dbg):
        if self.respaldo_instalado or self.ambito is None:
            return False
        estado = await self.ambito.ev(_XHR_RESPALDO_JS)
        self.respaldo_instalado = estado in ("instalado", "ya")
        dbg("respaldo XHR en el frame: %s" % estado)
        return self.respaldo_instalado

    async def _on_response(self, ev, *_):
        try:
            url = ev.response.url or ""
        except Exception:
            return
        if self.patron.search(url):
            self.esperando[str(ev.request_id)] = url

    async def _on_finished(self, ev, *_):
        url = self.esperando.pop(str(ev.request_id), None)
        if url is None:
            return
        try:
            res = await self.tab.send(
                uc.cdp.network.get_response_body(request_id=ev.request_id)
            )
        except Exception as ex:
            self.errores.append("no se pudo leer el cuerpo de %s: %s" % (url[:60], ex))
            return
        cuerpo = res[0] if isinstance(res, tuple) else res
        self._guardar(cuerpo)

    def _guardar(self, cuerpo):
        try:
            self.llegados.append(json.loads(cuerpo))
        except (TypeError, ValueError) as ex:
            self.errores.append("respuesta no es JSON: %s" % ex)

    async def _cosechar_respaldo(self):
        """Trae lo que junto el parche del frame, si esta instalado."""
        if not self.respaldo_instalado or self.ambito is None:
            return 0
        crudo = await self.ambito.ev(_XHR_VACIAR_JS)
        try:
            textos = json.loads(crudo or "[]")
        except (TypeError, ValueError):
            return 0
        antes = len(self.llegados)
        for texto in textos:
            self._guardar(texto)
        nuevos = len(self.llegados) - antes
        if nuevos:
            self.uso_respaldo = True
        return nuevos

    async def limpiar(self):
        """Tira todo lo pendiente. ANTES de cada consulta y de cada pagina.

        Importa por dos motivos:

        - La pantalla dispara su propia consulta al abrirse, con la ventana por
          defecto del portal. Si esa respuesta se cuela, se pagina sobre el
          resultado equivocado y salen movimientos fuera del rango.
        - Una respuesta rezagada de la consulta anterior se confundiria con la
          de la pagina siguiente. Paso el 2026-08-17: la pagina 2 "respondio"
          en 1,9 s con los mismos 50 movimientos y la paginacion se corto ahi,
          dejando 47 movimientos afuera.

        Vacia tambien el buffer del frame, no solo las listas: el respaldo XHR
        acumula en un atributo del <body> que sobrevive a esto.
        """
        self.llegados = []
        self.aceptados = []
        self.descartados = []
        self.sin_verificar = 0
        if self.respaldo_instalado and self.ambito is not None:
            await self.ambito.ev(_XHR_VACIAR_JS)

    def _clasificar(self, coincide):
        """Reparte lo llegado en aceptados/descartados. True si hay aceptados.

        `coincide(payload)` devuelve True (es la respuesta pedida), False (es
        de otro rango: se descarta) o None (el payload no trae con que
        verificar: se acepta, pero se cuenta para avisar).
        """
        pendientes, self.llegados = self.llegados, []
        for payload in pendientes:
            veredicto = coincide(payload) if coincide else True
            if veredicto is False:
                self.descartados.append(payload)
            else:
                if veredicto is None:
                    self.sin_verificar += 1
                self.aceptados.append(payload)
        return bool(self.aceptados)

    def rangos_descartados(self):
        """Que rangos trajo el portal, para poder explicarlo en un error."""
        vistos = []
        for payload in self.descartados:
            d = campo_result(payload, "FechaDesde")
            h = campo_result(payload, "FechaHasta")
            etiqueta = "%s..%s" % (str(d)[:10] or "?", str(h)[:10] or "?")
            if etiqueta not in vistos:
                vistos.append(etiqueta)
        return vistos

    def vaciar(self):
        """Devuelve lo aceptado desde la ultima llamada y limpia."""
        aceptados, self.aceptados = self.aceptados, []
        return aceptados

    async def esperar(self, timeout_s, coincide=None):
        """Espera una respuesta que pase `coincide`, por cualquiera de las dos vias."""
        fin = time.time() + timeout_s
        while time.time() < fin:
            await self._cosechar_respaldo()
            if self._clasificar(coincide):
                return True
            await asyncio.sleep(0.25)
        await self._cosechar_respaldo()
        return self._clasificar(coincide)


def filas_de_payload(payload):
    """Saca la lista de movimientos del JSON del portal."""
    actual = payload
    for parte in RUTA_LISTA:
        if not isinstance(actual, dict):
            return []
        actual = actual.get(parte)
    return [f for f in (actual or []) if isinstance(f, dict)]


def campo_result(payload, clave):
    """Lee un campo del sobre `Result`, fuera de la lista de movimientos.

    Ahi vive el rango `FechaDesde`/`FechaHasta` con que el portal respondio, que
    es lo que permite distinguir la respuesta que pedimos de la que la pantalla
    dispara sola al abrirse, y el cursor `MovimientoDesde`/`MovimientoHasta`,
    que identifica QUE pagina es.

    Las claves reales del sobre, vistas el 2026-08-17: CCC, Description, Divisa,
    Editable, Errores, FechaDesde, FechaHasta, ID, MovimientoDesde,
    MovimientoHasta, TimeStamp.

    >>> campo_result({"Result": {"Divisa": "CLP"}}, "Divisa")
    'CLP'
    >>> campo_result({"otra": 1}, "Divisa") is None
    True
    """
    if not isinstance(payload, dict):
        return None
    sobre = payload.get("Result")
    if not isinstance(sobre, dict):
        return None
    valor = sobre.get(clave)
    return valor if valor not in ("", []) else None


def cursor_de(payload):
    """Identifica QUE pagina es una respuesta, con el cursor del propio portal.

    El sobre `Result` trae `MovimientoDesde` y `MovimientoHasta`: el rango de
    correlativos que devuelve esa pagina. Dos respuestas con el mismo cursor
    son la misma pagina, aunque hayan llegado por peticiones distintas.

    Es lo unico que distingue "ya no hay mas paginas" de "la respuesta que lei
    era una repetida". Sin esto la paginacion se cortaba en la primera pagina y
    parecia exito.

    >>> cursor_de({"Result": {"MovimientoDesde": "000131297",
    ...                       "MovimientoHasta": "000131342"}})
    ('000131297', '000131342')
    >>> cursor_de({"Result": {"Detalle": []}}) is None
    True
    """
    desde = campo_result(payload, "MovimientoDesde")
    hasta = campo_result(payload, "MovimientoHasta")
    if desde is None and hasta is None:
        return None
    return (str(desde), str(hasta))


def coincide_con(desde, hasta):
    """Filtro que acepta solo la respuesta del rango pedido.

    Devuelve None cuando el payload no trae el rango: en ese caso se acepta
    igual (no vamos a tirar datos por no poder verificarlos) pero queda
    contado, para avisar que la comprobacion no se pudo hacer.

    >>> f = coincide_con(date(2026, 8, 16), date(2026, 8, 17))
    >>> f({"Result": {"FechaDesde": "2026-08-16T00:00:00", "FechaHasta": "2026-08-17T00:00:00"}})
    True
    >>> f({"Result": {"FechaDesde": "2026-08-01T00:00:00", "FechaHasta": "2026-08-17T00:00:00"}})
    False
    >>> f({"Result": {"Detalle": []}}) is None
    True
    """
    def _filtro(payload):
        crudo_desde = campo_result(payload, "FechaDesde")
        crudo_hasta = campo_result(payload, "FechaHasta")
        if crudo_desde is None or crudo_hasta is None:
            return None
        try:
            return parsear_fecha(crudo_desde) == desde and parsear_fecha(crudo_hasta) == hasta
        except RuntimeError:
            return None
    return _filtro


# ── Frames ────────────────────────────────────────────────────────────────────

class Ambito:
    """Evalua JS dentro de un frame concreto, en un mundo aislado.

    La cartola vive en un iframe de eob.officebanking.cl dentro del cascaron
    privado.officebanking.cl/portal-fob, asi que evaluar en la pagina principal
    no encuentra nada. El mundo aislado ademas no es visible para la pagina.

    El contextId se cachea: el script viejo creaba un mundo nuevo en cada
    llamada. Si el frame navega el contexto muere, y entonces se recrea una vez.
    """

    def __init__(self, tab, frame_id, nombre="cartola"):
        self.tab = tab
        self.frame_id = frame_id
        self.nombre = nombre
        self._ctx = None

    async def _contexto(self, forzar=False):
        if self._ctx is not None and not forzar:
            return self._ctx
        self._ctx = await self.tab.send(uc.cdp.page.create_isolated_world(
            frame_id=uc.cdp.page.FrameId(self.frame_id),
            world_name="%s_%d" % (self.nombre, random.randint(1000, 9999)),
            grant_univeral_access=True,
        ))
        return self._ctx

    async def ev(self, expresion, reintento=True):
        # Con timeout a proposito: `tab.send` no lo trae y si la conexion CDP se
        # traba se queda esperando para siempre. Paso el 2026-08-17 corriendo el
        # bloque desde el editor: quedo colgado ocho minutos en el chequeo de
        # paginacion y hubo que interrumpirlo a mano.
        try:
            ctx = await asyncio.wait_for(self._contexto(), timeout=T_CDP)
            res = await asyncio.wait_for(self.tab.send(uc.cdp.runtime.evaluate(
                expression=expresion, context_id=ctx, return_by_value=True,
                await_promise=True, user_gesture=True,
            )), timeout=T_CDP)
        except Exception:
            if not reintento:
                return None
            self._ctx = None
            return await self.ev(expresion, reintento=False)

        obj = res[0] if isinstance(res, tuple) else res
        valor = getattr(obj, "value", None)
        if valor is None:
            # Un false booleano llega sin `value` en algunas versiones de CDP.
            desc = getattr(obj, "description", None)
            if desc == "true":
                return True
            if desc == "false":
                return False
        return valor


async def _recorrer_frames(tab):
    """Todos los frameId del arbol, de la raiz hacia abajo."""
    arbol = await tab.send(uc.cdp.page.get_frame_tree())
    salida = []

    def visitar(nodo):
        frame = getattr(nodo, "frame", None)
        if frame is not None:
            fid = getattr(frame, "id_", None)
            if fid:
                salida.append((fid, getattr(frame, "url", "") or ""))
        for hijo in (getattr(nodo, "child_frames", None) or []):
            visitar(hijo)

    visitar(arbol)
    return salida


async def buscar_ambito(tab, sondas, dbg, nombre="cartola"):
    """Devuelve el Ambito del primer frame donde exista alguna de las sondas.

    `sondas` es un array de selectores con fallback: los bancos cambian el
    HTML, asi que se prueba en orden hasta que uno responda.
    """
    consulta = ",".join("!!document.querySelector(%s)" % json.dumps(s) for s in sondas)
    for fid, url in await _recorrer_frames(tab):
        ambito = Ambito(tab, fid, nombre)
        encontrado = await ambito.ev("[%s].some(Boolean)" % consulta)
        if encontrado:
            dbg("%s encontrado en frame %s" % (nombre, (url or "principal")[:70]))
            return ambito
    return None


async def texto_de_todos_los_frames(tab):
    """Texto visible de todos los frames, normalizado para comparar."""
    trozos = []
    for fid, _url in await _recorrer_frames(tab):
        valor = await Ambito(tab, fid, "texto").ev("document.body ? document.body.innerText : ''")
        if valor:
            trozos.append(str(valor))
    return normalizar_texto(" ".join(trozos))


# ── Deteccion de 2FA y rechazos ───────────────────────────────────────────────

async def inspeccionar_portal(tab):
    """Clasifica lo que el portal esta pidiendo, leyendo su texto."""
    texto = await texto_de_todos_los_frames(tab)
    return {
        "pide_2fa": tuple(p for p in PALABRAS_2FA if p in texto),
        "rechazo": tuple(p for p in PALABRAS_RECHAZO if p in texto),
        "desafio": tuple(p for p in PALABRAS_DESAFIO if p in texto),
    }


class SegundoFactorRequerido(RuntimeError):
    """El portal pide Superclave. No se automatiza: la autoriza una persona."""


class LoginRechazado(RuntimeError):
    """El portal rechazo las credenciales. NO se reintenta."""


# ── Normalizacion ─────────────────────────────────────────────────────────────

def normalizar_texto(valor):
    """Mayusculas, sin acentos, con los espacios colapsados.

    >>> normalizar_texto("  Transf.  José   Pérez ")
    'TRANSF. JOSE PEREZ'
    """
    if valor is None:
        return ""
    sin_tildes = "".join(
        c for c in unicodedata.normalize("NFD", str(valor))
        if unicodedata.category(c) != "Mn"
    )
    return re.sub(r"\s+", " ", sin_tildes).strip().upper()


# Dos decimales SIEMPRE, incluso en pesos. Van a entrar cuentas en dolares y
# un monto redondeado a entero perderia los centavos sin dejar rastro. Ademas
# fija la forma del texto que entra al hash: 179940 y 179940.00 tienen que dar
# el mismo hash siempre.
CENTAVO = Decimal("0.01")


def parsear_monto(valor):
    """Monto a Decimal con dos decimales. Falla si no se entiende.

    Un monto que silenciosamente vale 0 es peor que un error en una
    conciliacion bancaria: cuadra mal y nadie sabe por que. Se cubren los
    parentesis contables, que son un cargo.

    >>> parsear_monto(-29750.0), parsear_monto("$ 179.940"), parsear_monto("(1.234)")
    (Decimal('-29750.00'), Decimal('179940.00'), Decimal('-1234.00'))
    >>> parsear_monto("1.234,50")
    Decimal('1234.50')
    """
    if valor is None or valor == "" or isinstance(valor, bool):
        raise RuntimeError("monto vacio o ilegible: %r" % (valor,))
    if isinstance(valor, (int, float, Decimal)):
        # Por str y no por float(): Decimal(0.1) arrastra el error del binario.
        return Decimal(str(valor)).quantize(CENTAVO, rounding=ROUND_HALF_UP)
    texto = str(valor).strip()
    negativo = texto.startswith("(") and texto.endswith(")")
    # Formato chileno: el punto separa miles y la coma decimales.
    limpio = re.sub(r"[^\d,.\-]", "", texto).replace(".", "").replace(",", ".")
    if not limpio or limpio in ("-", ".", "-."):
        raise RuntimeError("monto ilegible: %r" % (valor,))
    try:
        numero = Decimal(limpio).quantize(CENTAVO, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise RuntimeError("monto ilegible: %r" % (valor,))
    return -abs(numero) if negativo else numero


def parsear_fecha(valor):
    """Fecha del portal a `date`. Acepta ISO con hora y dd/mm/aaaa.

    >>> parsear_fecha("2026-08-06T00:00:00"), parsear_fecha("06/08/2026")
    (datetime.date(2026, 8, 6), datetime.date(2026, 8, 6))
    """
    if isinstance(valor, date):
        return valor
    texto = str(valor or "").strip()
    if not texto:
        raise RuntimeError("fecha vacia")
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", texto)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", texto)
    if m:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    raise RuntimeError("fecha ilegible: %r" % (valor,))


def _limpiar_espacios(valor):
    """Colapsa los espacios internos conservando texto, mayusculas y acentos.

    El banco rellena la descripcion con espacios: '0775938102 Transf.' seguido
    de sesenta espacios y 'COMERCIAL'. Sale de un campo de ancho fijo.

    >>> _limpiar_espacios("0775938102 Transf.        COMERCIAL  ")
    '0775938102 Transf. COMERCIAL'
    """
    return re.sub(r"\s+", " ", str(valor or "")).strip()


def normalizar_movimientos(crudos, cuenta, banco=BANCO, extraido_en=None,
                           ccc=CCC_DEFECTO, divisa=DIVISA_DEFECTO):
    """Filas del endpoint a movimientos normalizados, con hash idempotente.

    El `hash_mov` incluye un ORDINAL dentro del grupo (estado, fecha, monto,
    tipo, descripcion). No es un detalle: en la cartola real de enero 2026 hay
    14 grupos de movimientos exactamente identicos el mismo dia, uno repetido 7
    veces (Pago de Asigna, -$7.000.000). Con una clave por contenido, 6 de esos
    7 se descartan como duplicados y se pierden $42.000.000. En la corrida del
    2026-08-17, con solo dos dias, el ordinal ya llego a 3.

    Y es estable entre corridas porque la fecha es parte de la clave de
    agrupacion: pedir 01-15 o 01-30 produce los mismos hashes para los dias en
    comun.

    El `banco` va en la firma para que los correlativos de BCI o Banco de Chile
    no puedan colisionar con los de Santander en la tabla de destino.

    `TipoMovimiento` manda sobre el signo: si el banco mandara un cargo en
    positivo, la D lo corrige. Cuando viene en blanco el movimiento esta
    EN_CANJE y el signo se decide por EsCargo/EsAbono.
    """
    sello = extraido_en or datetime.now(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Por NroMovimiento, que es el correlativo del banco: da un orden estable
    # aunque el portal devuelva las paginas en otro orden.
    def clave_orden(f):
        return (str(f.get("NroMovimiento") or ""), str(f.get("Descripcion") or ""))

    grupos = {}
    salida = []
    for cruda in sorted(crudos, key=clave_orden):
        fecha = parsear_fecha(
            cruda.get("FechaTransaccion")
            or cruda.get("FechaContableMovimiento")
            or cruda.get("FechaContable")
        )
        monto = parsear_monto(
            cruda.get("Monto") if cruda.get("Monto") is not None else cruda.get("Importe")
        )
        tipo_banco = (cruda.get("TipoMovimiento") or "").strip().upper()
        estado = LIQUIDADO if tipo_banco in ("H", "D") else EN_CANJE

        if tipo_banco == "D" or cruda.get("EsCargo") is True:
            tipo = CARGO
        elif tipo_banco == "H" or cruda.get("EsAbono") is True:
            tipo = ABONO
        else:
            tipo = CARGO if monto < 0 else ABONO
        monto = -abs(monto) if tipo == CARGO else abs(monto)

        descripcion = _limpiar_espacios(
            cruda.get("Descripcion") or cruda.get("DetalleMovimiento")
        )
        desc_norm = normalizar_texto(descripcion)

        # El estado entra en la agrupacion Y en la firma: un deposito en canje
        # y su version liquidada son registros distintos y no deben compartir
        # hash ni robarse el ordinal.
        grupo = (estado, fecha.isoformat(), monto, tipo, desc_norm)
        ordinal = grupos.get(grupo, 0)
        grupos[grupo] = ordinal + 1

        crudo_saldo = cruda.get("NuevoSaldo")
        try:
            saldo = parsear_monto(crudo_saldo) if crudo_saldo not in (None, "") else None
        except RuntimeError:
            saldo = None

        fecha_contable = None
        for candidata in ("FechaContableMovimiento", "FechaContable"):
            if cruda.get(candidata):
                try:
                    fecha_contable = parsear_fecha(cruda[candidata]).isoformat()
                    break
                except RuntimeError:
                    continue

        firma = "|".join([
            banco, str(cuenta), estado, fecha.isoformat(), str(monto), tipo,
            desc_norm, str(ordinal),
        ])
        salida.append({
            "banco": banco,
            "cuenta": str(cuenta),
            # El sobre manda si los trae; si no, el valor configurado. El
            # endpoint real no los devolvio en la corrida del 2026-08-17.
            "ccc": _limpiar_espacios(cruda.get("_ccc")) or ccc or None,
            "divisa": _limpiar_espacios(cruda.get("_divisa")) or divisa or None,
            "nro_movimiento": _limpiar_espacios(cruda.get("NroMovimiento")) or None,
            "fecha_mov": fecha.isoformat(),
            "fecha_contable": fecha_contable,
            # HoraTransaccion. OJO: es la hora en que se ORIGINO la operacion,
            # no la hora en que entro a la cuenta. Verificado el 2026-08-17: hay
            # tres movimientos con hora 23:27 ubicados al principio del
            # correlativo del lunes, o sea del domingo por la noche. Sirve como
            # dato, no para ordenar: el orden cronologico es nro_movimiento.
            "hora": _limpiar_espacios(cruda.get("HoraTransaccion")) or None,
            "descripcion": descripcion,
            "monto": monto,
            "tipo": tipo,
            "saldo": saldo,
            "estado": estado,
            "codigo_movimiento": _limpiar_espacios(cruda.get("CodigoMovimiento")) or None,
            "sucursal": _limpiar_espacios(cruda.get("GlosaSucursal") or cruda.get("Sucursal")) or None,
            "codigo_sucursal": _limpiar_espacios(cruda.get("CodigoSucursal")) or None,
            "hash_mov": hashlib.sha256(firma.encode("utf-8")).hexdigest(),
            "ordinal": ordinal,
            "extraido_en": sello,
        })
    return salida


# ── Validacion ────────────────────────────────────────────────────────────────

def _hueco_correlativo(previo, actual):
    """True si entre dos movimientos falta al menos un correlativo.

    Tambien devuelve True cuando no se puede saber, que es el caso del salto
    entre el ultimo liquidado y el primer canje: ahi las dos numeraciones no
    tienen relacion. Se prefiere el falso "hay hueco" al falso "falta plata".

    Los liquidados suben (131324, 131325, ...) y los EN_CANJE bajan (4, 3, 2,
    1), asi que la distancia se mide en el sentido de cada serie.

    >>> liq = lambda n: {"estado": "LIQUIDADO", "nro_movimiento": n}
    >>> _hueco_correlativo(liq("000131324"), liq("000131326"))
    True
    >>> _hueco_correlativo(liq("000131324"), liq("000131325"))
    False
    >>> canje = lambda n: {"estado": "EN_CANJE", "nro_movimiento": n}
    >>> _hueco_correlativo(canje("000000004"), canje("000000003"))
    False
    >>> _hueco_correlativo(canje("000000004"), canje("000000002"))
    True
    >>> _hueco_correlativo(liq("000131345"), canje("000000004"))
    True
    """
    estado_previo, estado_actual = previo.get("estado"), actual.get("estado")
    if estado_previo != estado_actual:
        return True
    try:
        n_previo = int(previo["nro_movimiento"])
        n_actual = int(actual["nro_movimiento"])
    except (TypeError, ValueError):
        return True
    # El canje se recorre al reves, asi que la distancia se mide invertida.
    distancia = n_actual - n_previo if estado_actual == LIQUIDADO else n_previo - n_actual
    return distancia > 1


def validar_saldo_corrido(movs):
    """Verifica que saldo[i] == saldo[i-1] + monto[i]. Devuelve los saltos.

    El riesgo peor de un scraper de cartola no es fallar, es traer MENOS
    movimientos y no darse cuenta. Un salto de saldo delata exactamente eso: una
    pagina que no cargo. Deduplicar solo detecta repeticiones; esto detecta
    ausencias.

    El orden de la cadena NO es por fecha: es por correlativo. Los liquidados
    ascienden (131250 -> 131345) y los EN_CANJE continuan esa misma cadena pero
    con su numeracion al REVES (4, 3, 2, 1), porque el portal numera el canje
    del ultimo al primero en liquidar. Ordenar por fecha mezcla las dos series
    y produce saltos que no existen: con los datos del 2026-08-17, cinco.

    Un salto NO siempre es un movimiento perdido. NuevoSaldo es el saldo de la
    cuenta entera, asi que si el rango pedido deja fuera un movimiento del medio
    la cadena salta de forma legitima. Paso el 2026-08-17: pidiendo 15..17 quedo
    fuera el 131325 (un deposito SERVIPAG con fecha contable del 14) y el saldo
    salto exactamente sus 558.586.

    Por eso cada salto se clasifica mirando si el correlativo es contiguo:

        hay hueco en el correlativo  el movimiento existe pero quedo fuera del
                                     filtro. Informativo.
        correlativo contiguo         no hay donde esconder el dinero: falta un
                                     movimiento de verdad. GRAVE.

    `ok` solo es False por los graves, para que el aviso signifique algo.
    """
    con_saldo = [m for m in movs if m["saldo"] is not None]
    if len(con_saldo) < 2:
        return {
            "se_pudo_validar": False,
            "ok": True,
            "saltos": [],
            "graves": [],
            "resumen": "saldo corrido no validado: %d de %d filas traen saldo"
                       % (len(con_saldo), len(movs)),
        }

    def por_correlativo(m):
        return m["nro_movimiento"] or ""

    orden = (
        sorted([m for m in con_saldo if m["estado"] == LIQUIDADO], key=por_correlativo)
        + sorted([m for m in con_saldo if m["estado"] != LIQUIDADO],
                 key=por_correlativo, reverse=True)
    )
    saltos = []
    for previo, actual in zip(orden, orden[1:]):
        esperado = previo["saldo"] + actual["monto"]
        if esperado == actual["saldo"]:
            continue
        saltos.append({
            "fecha": actual["fecha_mov"],
            "descripcion": actual["descripcion"][:60],
            "desde_mov": previo["nro_movimiento"],
            "hasta_mov": actual["nro_movimiento"],
            "esperado": esperado,
            "reportado": actual["saldo"],
            "diferencia": actual["saldo"] - esperado,
            "hueco_correlativo": _hueco_correlativo(previo, actual),
        })

    graves = [x for x in saltos if not x["hueco_correlativo"]]
    fuera = len(saltos) - len(graves)

    if not saltos:
        resumen = "saldo corrido OK en %d filas" % len(orden)
    elif not graves:
        resumen = (
            "saldo corrido OK en %d filas; %d salto(s) explicados por "
            "movimientos fuera del rango pedido (hay hueco en el correlativo)"
            % (len(orden), fuera)
        )
    else:
        resumen = (
            "saldo corrido con %d salto(s) SIN hueco de correlativo: falta un "
            "movimiento de verdad" % len(graves)
        )
        if fuera:
            resumen += " (y %d explicados por el filtro)" % fuera

    return {
        "se_pudo_validar": True,
        "ok": not graves,
        "saltos": saltos,
        "graves": graves,
        "resumen": resumen,
    }


def validar_cobertura(movs, desde, hasta):
    """Avisa si aparecen movimientos fuera del rango pedido.

    Se mira `fecha_contable`, NO `fecha_mov`: el formulario del portal filtra
    por fecha contable. Comprobado el 2026-08-17 pidiendo 15..17, que devolvio
    26 movimientos con fecha_mov del viernes 14 porque se contabilizaron el
    lunes 17. Mirar fecha_mov daria un aviso en cada corrida, y un aviso que
    salta siempre deja de significar algo.

    Los EN_CANJE quedan fuera del control a proposito: son depositos por
    liquidar y su fecha es futura por definicion.
    """
    avisos = []
    liquidados = [m for m in movs if m["estado"] == LIQUIDADO and m["fecha_contable"]]
    fuera = [m["fecha_contable"] for m in liquidados
             if not (desde.isoformat() <= m["fecha_contable"] <= hasta.isoformat())]
    if fuera:
        avisos.append(
            "%d movimiento(s) con fecha contable fuera de %s..%s (por ejemplo "
            "%s): el formulario no filtro como esperabamos"
            % (len(fuera), desde, hasta, min(fuera))
        )
    return avisos


# ── Screenshots ───────────────────────────────────────────────────────────────

async def screenshot(tab, etiqueta):
    """Screenshot en cada paso. Es lo unico que permite depurar sin repetir.

    OJO: estas imagenes contienen datos bancarios. El directorio no debe
    publicarse.
    """
    try:
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)
        sello = datetime.now(pytz.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        await tab.save_screenshot("%s/%s_%s.png" % (SCREENSHOT_DIR, sello, etiqueta))
    except Exception:
        pass


# ── Login ─────────────────────────────────────────────────────────────────────

_JS_CERRAR_POPUP = """
(() => {
    const textos = %s;
    const norm = v => (v || '').normalize('NFD')
        .replace(/[\\u0300-\\u036f]/g, '').replace(/\\s+/g, ' ').trim().toUpperCase();
    // Solo dentro de algo que parezca un modal. Un "Aceptar" suelto puede ser
    // el boton de un formulario, y apretarlo a ciegas dispara una consulta que
    // nadie pidio.
    const contenedores = document.querySelectorAll(
        '[role=dialog], [class*=modal], [class*=Modal], [class*=popup], ' +
        '[class*=Popup], [class*=overlay], [class*=Overlay]');
    for (const caja of contenedores) {
        if (!caja.offsetParent && caja.getClientRects().length === 0) continue;
        for (const btn of caja.querySelectorAll('button, a, [role=button]')) {
            if (!btn.offsetParent) continue;
            if (textos.indexOf(norm(btn.textContent)) === -1) continue;
            btn.click();
            return norm(btn.textContent);
        }
    }
    return null;
})()
"""


async def cerrar_popups(tab, dbg):
    """Cierra ofertas, encuestas y avisos que tapan el menu post-login.

    Practica de la referencia. Tolerante a proposito: si no hay popup, se sigue.
    Se buscan en todos los frames y solo dentro de contenedores que parezcan un
    modal, y se comparan textos sin acentos porque el portal escribe "Mas
    tarde" y "Más tarde" segun la pantalla.
    """
    consulta = _JS_CERRAR_POPUP % json.dumps(list(TEXTOS_POPUP))
    cerrados = 0
    # Varias pasadas: cerrar uno puede descubrir el siguiente.
    for _ in range(4):
        cerrado_en_esta = False
        for fid, _url in await _recorrer_frames(tab):
            texto = await Ambito(tab, fid, "popup").ev(consulta)
            if texto:
                cerrados += 1
                cerrado_en_esta = True
                dbg("popup cerrado con '%s'" % texto)
                await asyncio.sleep(_h(1.2))
        if not cerrado_en_esta:
            break
    return cerrados


async def ingresar(tab, rut, clave, dbg):
    """Login en dos pasos dentro del iframe de wslogin.

    Particularidades del portal, descubiertas el 2026-08-06:

    - El formulario vive en un iframe de wslogin.officebanking.cl que recien se
      crea al apretar "Ingresar" en la portada.
    - Hay DOS iframes de ese host: uno es telemetria de 0x0 que apunta a /ping.
    - Un solo campo de RUT (#username), no uno de empresa y otro de usuario. El
      portal reformatea lo que se escribe.

    NO se reintenta un login fallido: arriesgaria el bloqueo de la cuenta.
    """
    dbg("click en Ingresar")
    for intento in range(5):
        try:
            btn = await tab.find("Ingresar", best_match=True, timeout=5)
            if btn:
                await _pasear_mouse(tab, pasos=1)
                await btn.click()
                dbg("Ingresar apretado (intento %d)" % (intento + 1))
                break
        except Exception as ex:
            dbg("Ingresar no encontrado (intento %d): %s" % (intento + 1, ex))
        await asyncio.sleep(1.0)

    await asyncio.sleep(_h(3.0))

    ambito = None
    for intento in range(40):
        ambito = await buscar_ambito(
            tab, ("#username", "input[name='rut']", "input[placeholder='RUT']"),
            dbg, nombre="login",
        )
        if ambito:
            break
        if intento % 10 == 0:
            dbg("esperando el formulario de login [%d]" % intento)
        await asyncio.sleep(0.5)

    if not ambito:
        await screenshot(tab, "error_sin_formulario")
        raise RuntimeError(
            "No aparecio el formulario de login. Revisa el screenshot en %s: "
            "si dice 'Revisa tu conexion a internet', el portal detecto el "
            "navegador; si dice 'Internet Connection Error', se entro por "
            "www.officebanking.cl en vez de empresas.officebanking.cl."
            % SCREENSHOT_DIR
        )

    await asyncio.sleep(_h(2.0))
    await screenshot(tab, "s01_login")

    sel_rut = await _primer_selector(ambito, ("#username", "input[name='rut']",
                                             "input[placeholder='RUT']"))
    sel_pw = await _primer_selector(ambito, ("#password", "input[type='password']"))
    sel_btn = await _primer_selector(ambito, ("#doLoginButton", "button[type='submit']"))
    if not sel_rut or not sel_pw:
        raise RuntimeError(
            "El formulario cambio: no se encontro el campo de RUT o de clave. "
            "Selectores probados: %s / %s" % (SEL_RUT_PROBADOS, SEL_PW_PROBADOS)
        )
    dbg("selectores de login: rut=%s clave=%s boton=%s" % (sel_rut, sel_pw, sel_btn))

    await ambito.ev(
        "(() => {const el = document.querySelector(%s); if (el) {el.focus(); el.click();} })()"
        % json.dumps(sel_rut)
    )
    await asyncio.sleep(_h(0.4))
    await _teclear(tab, rut)

    escrito = await ambito.ev(
        "(() => {const el = document.querySelector(%s); return el ? el.value : ''; })()"
        % json.dumps(sel_rut)
    )
    digitos = re.sub(r"\D", "", str(escrito or ""))
    if digitos != re.sub(r"\D", "", rut):
        # El portal reformatea (19150357-0 -> 19.150.357-0), asi que se comparan
        # digitos y no strings. Si aun asi no calza, el tipeo no llego.
        dbg("el RUT no quedo escrito (%r); se usa el setter nativo" % escrito)
        await ambito.ev("""
            (() => {
                const el = document.querySelector(%s);
                if (!el) return false;
                el.focus();
                const nv = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value');
                nv.set.call(el, %s);
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                return true;
            })()
        """ % (json.dumps(sel_rut), json.dumps(rut)))
    else:
        dbg("RUT escrito")

    await asyncio.sleep(_h(0.5))
    await _tab_key(tab)
    await asyncio.sleep(_h(0.6))

    await ambito.ev(
        "(() => {const el = document.querySelector(%s); if (el) {el.focus(); el.click();} })()"
        % json.dumps(sel_pw)
    )
    await asyncio.sleep(_h(0.4))
    await _teclear(tab, clave)

    largo = await ambito.ev(
        "(() => {const el = document.querySelector(%s); return el ? el.value.length : 0; })()"
        % json.dumps(sel_pw)
    )
    # Solo el largo. La clave no se registra en ninguna parte.
    dbg("clave escrita (%s caracteres)" % largo)
    if not largo:
        raise RuntimeError(
            "La clave no quedo escrita en el formulario. Es sintoma de que el "
            "campo esta en otro frame o que el portal lo bloqueo."
        )

    await screenshot(tab, "s02_credenciales")

    # El portal necesita un momento para armar sus tokens antifraude antes de
    # aceptar el submit. Apretar de inmediato es una senal por si mismo.
    await asyncio.sleep(_h(4.0, spread=0.2))

    enviado = await ambito.ev("""
        (() => {
            const btn = document.querySelector(%s);
            if (btn) {
                btn.disabled = false;
                btn.classList.remove('disabled');
                ['mousedown', 'mouseup', 'click'].forEach(
                    n => btn.dispatchEvent(new MouseEvent(n, {bubbles: true, cancelable: true})));
                return 'boton';
            }
            const pw = document.querySelector(%s);
            if (pw) {
                pw.dispatchEvent(new KeyboardEvent(
                    'keydown', {key: 'Enter', keyCode: 13, bubbles: true}));
                return 'enter';
            }
            return 'nada';
        })()
    """ % (json.dumps(sel_btn or "#doLoginButton"), json.dumps(sel_pw)))
    dbg("submit por %s" % enviado)

    return await esperar_post_login(tab, dbg)


SEL_RUT_PROBADOS = "#username, input[name='rut'], input[placeholder='RUT']"
SEL_PW_PROBADOS = "#password, input[type='password']"


async def _primer_selector(ambito, candidatos):
    """El primero de la lista que exista en el frame. None si ninguno."""
    for sel in candidatos:
        existe = await ambito.ev(
            "!!document.querySelector(%s)" % json.dumps(sel)
        )
        if existe:
            return sel
    return None


async def esperar_post_login(tab, dbg):
    """Espera la sesion, cortando temprano si hay 2FA o rechazo.

    El script viejo esperaba en silencio hasta el timeout. Si el portal pedia
    Superclave, quemaba dos minutos y terminaba con "Login fallo. URL: ..." sin
    decir por que.
    """
    fin = time.time() + T_LOGIN
    ultima_url = ""
    revisiones = 0

    while time.time() < fin:
        await asyncio.sleep(1.0)
        revisiones += 1
        try:
            url = tab.url or ""
        except Exception as ex:
            raise RuntimeError("el navegador se cayo durante el login: %s" % ex)

        if url != ultima_url:
            dbg("URL -> %s" % url[:90])
            ultima_url = url

        if "privado.officebanking.cl" in url:
            dbg("login OK por URL")
            return True

        if revisiones % 4 == 0:
            estado = await inspeccionar_portal(tab)
            if estado["pide_2fa"]:
                await screenshot(tab, "s03_segundo_factor")
                raise SegundoFactorRequerido(
                    "El portal pide un segundo factor (%s). No se automatiza: "
                    "lo autoriza una persona.\n"
                    "Que hacer: abrir Office Banking a mano con este mismo RUT, "
                    "completar la Superclave y dejar la sesion abierta. El "
                    "perfil persistente en %s conserva la confianza del "
                    "dispositivo y las corridas siguientes no vuelven a "
                    "pedirla.\n"
                    "Si esto se repite todos los dias, el score de riesgo del "
                    "portal esta subiendo: NO borres el perfil, eso lo empeora."
                    % (", ".join(estado["pide_2fa"]), PROFILE_DIR)
                )
            if estado["rechazo"]:
                await screenshot(tab, "s03_rechazado")
                raise LoginRechazado(
                    "El portal rechazo el ingreso (%s). No se reintenta, para "
                    "no arriesgar el bloqueo de la cuenta por intentos "
                    "fallidos. Revisa el RUT y la clave."
                    % ", ".join(estado["rechazo"])
                )
            if estado["desafio"]:
                await screenshot(tab, "s03_desafio")
                raise RuntimeError(
                    "El portal muestra un desafio (%s) que debe resolver una "
                    "persona. No se resuelven captchas."
                    % ", ".join(estado["desafio"])
                )

        if revisiones % 5 == 0:
            try:
                el = await tab.find("Cuentas Corrientes", best_match=False, timeout=1)
                if el:
                    dbg("login OK por menu")
                    return True
            except Exception:
                pass

    await screenshot(tab, "s03_login_timeout")
    raise RuntimeError(
        "No se detecto sesion en %ds. Ultima URL: %s. Revisa el screenshot en %s."
        % (T_LOGIN, ultima_url[:90], SCREENSHOT_DIR)
    )


# ── Navegacion al detalle de movimientos ──────────────────────────────────────

_JS_CLICK_CC = """
(() => {
    const el = document.querySelector('a.has-sub');
    if (!el) return false;
    ['mouseenter', 'mouseover', 'mousedown', 'mouseup', 'click'].forEach(
        n => el.dispatchEvent(new MouseEvent(n, {bubbles: true, cancelable: true, composed: true})));
    if (typeof el.click === 'function') el.click();
    return true;
})()
"""

_JS_CLICK_SALDOS = """
(() => {
    const norm = v => (v || '').normalize('NFD')
        .replace(/[\\u0300-\\u036f]/g, '').trim().toLowerCase();
    const disparar = el => {
        if (!el) return false;
        const host = el.closest('app-office-banking-link') || el;
        for (const t of [el, host]) {
            ['mouseenter', 'mousedown', 'mouseup', 'click'].forEach(
                n => t.dispatchEvent(new MouseEvent(n, {bubbles: true, cancelable: true, composed: true})));
        }
        if (typeof el.click === 'function') el.click();
        return true;
    };
    const busca = raiz => Array.from(raiz.querySelectorAll('a.obLink,a'))
        .find(a => norm(a.textContent) === 'saldos y movimientos');

    const sub = document.querySelector('div#SubMenu');
    if (sub) { const l = busca(sub); if (l) return disparar(l); }
    const ul = Array.from(document.querySelectorAll('ul.lista-funcs'))
        .find(u => !u.className.includes('close'));
    if (ul) { const l = busca(ul); if (l) return disparar(l); }
    const suelto = Array.from(document.querySelectorAll('a.obLink,a'))
        .find(a => norm(a.textContent) === 'saldos y movimientos' && a.offsetParent !== null);
    return suelto ? disparar(suelto) : false;
})()
"""


async def abrir_movimientos(tab, dbg):
    """Navega por clicks hasta la pantalla de saldos y movimientos.

    Es una SPA: no se confia en URLs, se navega por clicks. Devuelve el Ambito
    del frame que tiene el formulario de fechas.
    """
    fin = time.time() + T_MOVIMIENTOS

    while time.time() < fin:
        ambito = await buscar_ambito(tab, SEL_FECHA_DESDE, dbg)
        if ambito:
            return ambito

        try:
            el = await tab.find("Cuentas Corrientes", best_match=True, timeout=3)
            if el:
                await el.click()
                dbg("Cuentas Corrientes por find()")
        except Exception:
            try:
                if await tab.evaluate(_JS_CLICK_CC):
                    dbg("Cuentas Corrientes por JS")
            except Exception:
                pass

        apretado = False
        for intento in range(20):
            await asyncio.sleep(0.4)
            try:
                el = await tab.find("Saldos y movimientos", best_match=True, timeout=1)
                if el:
                    await el.click()
                    apretado = True
                    dbg("Saldos y movimientos por find() [%d]" % (intento + 1))
                    break
            except Exception:
                pass
            try:
                if await tab.evaluate(_JS_CLICK_SALDOS):
                    apretado = True
                    dbg("Saldos y movimientos por JS [%d]" % (intento + 1))
                    break
            except Exception:
                pass

        if not apretado:
            await asyncio.sleep(_h(1.0))
            continue

        for _ in range(30):
            await asyncio.sleep(0.5)
            ambito = await buscar_ambito(tab, SEL_FECHA_DESDE, dbg)
            if ambito:
                return ambito

        await asyncio.sleep(_h(1.0))

    await screenshot(tab, "error_sin_movimientos")
    return None


# ── Consulta de un tramo ──────────────────────────────────────────────────────

_JS_ESTADO_SIGUIENTE = r"""
(() => {
    const todos = Array.from(document.querySelectorAll(%s));
    // Se excluye el paginador de la linea de credito asociada.
    const btn = todos.find(a => !((a.getAttribute('data-bind') || '').includes('Cred')));
    if (!btn) return 'sin_boton';
    if (btn.hasAttribute('disabled')) return 'deshabilitado';
    // El portal no deshabilita el control: lo pinta gris. Un color con las tres
    // componentes parecidas es gris.
    const m = (window.getComputedStyle(btn).color || '').match(/rgb\((\d+),\s*(\d+),\s*(\d+)\)/);
    if (m) {
        const [r, g, b] = [+m[1], +m[2], +m[3]];
        if (Math.abs(r - g) < 30 && Math.abs(g - b) < 30) return 'gris';
    }
    return 'habilitado';
})()
"""

_JS_CLICK_SIGUIENTE = r"""
(() => {
    const todos = Array.from(document.querySelectorAll(%s));
    const btn = todos.find(a => !((a.getAttribute('data-bind') || '').includes('Cred')));
    if (!btn) return 'sin_boton';
    // Una sola vez: dispatchEvent('click') YA ejecuta el binding de Knockout.
    // Sumarle btn.click() lo ejecutaba dos veces y pedia dos paginas.
    btn.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true}));
    btn.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true}));
    btn.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
    return 'apretado';
})()
"""


async def _set_fecha(ambito, selector, valor):
    """Escribe una fecha y avisa a Knockout.

    La pantalla es Knockout.js: sin el evento `change` el viewmodel no se
    entera y la consulta sale con la fecha vieja.
    """
    return await ambito.ev("""
        (() => {
            const el = document.querySelector(%s);
            if (!el) return 'sin_campo';
            el.focus();
            const nv = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value');
            nv.set.call(el, %s);
            ['input', 'change', 'blur', 'keyup'].forEach(
                n => el.dispatchEvent(new Event(n, {bubbles: true})));
            if (typeof ko !== 'undefined') {
                try { ko.utils.triggerEvent(el, 'change'); } catch (e) {}
            }
            el.blur();
            return el.value;
        })()
    """ % (json.dumps(selector), json.dumps(valor)))


async def _apretar(ambito, selector):
    return await ambito.ev("""
        (() => {
            const btn = document.querySelector(%s);
            if (!btn) return 'sin_boton';
            // Una sola vez. Ver _JS_CLICK_SIGUIENTE: el doble disparo pedia dos
            // veces la misma consulta y la respuesta sobrante se colaba luego
            // como si fuera la pagina siguiente.
            btn.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true}));
            btn.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true}));
            btn.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
            return 'apretado';
        })()
    """ % json.dumps(selector))


async def consultar_tramo(tab, ambito, captura, desde, hasta, max_paginas, dbg):
    """Consulta un tramo y devuelve sus filas crudas.

    Devuelve (filas, avisos). Los avisos son problemas que no impiden entregar
    lo extraido pero que alguien tiene que mirar.
    """
    avisos = []
    coincide = coincide_con(desde, hasta)

    # La pantalla dispara su propia consulta al abrirse, con la ventana por
    # defecto del portal. Se le da tiempo a que aterrice y se tira: si se
    # colara, `esperar` la tomaria por buena y paginariamos sobre el resultado
    # equivocado. Ademas del descarte por rango, esto evita la carrera.
    await asyncio.sleep(_h(1.5))
    await captura.limpiar()

    sel_desde = await _primer_selector(ambito, SEL_FECHA_DESDE)
    sel_hasta = await _primer_selector(ambito, SEL_FECHA_HASTA)
    sel_consultar = await _primer_selector(ambito, SEL_CONSULTAR)
    if not (sel_desde and sel_hasta):
        raise RuntimeError(
            "El formulario de fechas cambio: no se encontro desde/hasta. "
            "Selectores probados: %s | %s"
            % (", ".join(SEL_FECHA_DESDE), ", ".join(SEL_FECHA_HASTA))
        )

    r1 = await _set_fecha(ambito, sel_desde, to_ui_date(desde))
    await asyncio.sleep(_h(0.5))
    r2 = await _set_fecha(ambito, sel_hasta, to_ui_date(hasta))
    await asyncio.sleep(_h(0.5))
    dbg("fechas %s -> %s (campos quedaron en %r / %r)" % (desde, hasta, r1, r2))

    if not sel_consultar:
        raise RuntimeError(
            "No se encontro el boton de consultar. Selectores probados: %s"
            % ", ".join(SEL_CONSULTAR)
        )
    dbg("Consultar: %s" % await _apretar(ambito, sel_consultar))

    filas = []
    vistos = set()
    cursores = set()
    sobre_registrado = []

    def sumar(payloads):
        """Suma las filas nuevas. Devuelve (filas_nuevas, paginas_nuevas)."""
        nuevas = 0
        paginas = 0
        for payload in payloads:
            # Una sola vez por corrida: deja en el log que campos trae de verdad
            # el sobre Result. Sin esto no hay como saber por que salieron
            # vacios CCC y Divisa, porque el payload crudo ya no se guarda.
            if not sobre_registrado:
                sobre = payload.get("Result") if isinstance(payload, dict) else None
                if isinstance(sobre, dict):
                    sobre_registrado.append(True)
                    dbg("claves del sobre Result: %s"
                        % ", ".join(sorted(k for k in sobre if k != "Detalle")))

            # Una respuesta con un cursor ya visto es la MISMA pagina que
            # llego dos veces, no una pagina nueva. Distinguirlo es lo que
            # evita que la paginacion se corte creyendo que ya no hay mas.
            cursor = cursor_de(payload)
            if cursor is not None:
                if cursor in cursores:
                    continue
                cursores.add(cursor)
            paginas += 1

            # Divisa y CCC viven en el sobre, no en cada movimiento. Se pegan
            # aqui para que la normalizacion no tenga que ver el payload.
            divisa = campo_result(payload, "Divisa")
            ccc = campo_result(payload, "CCC")
            for fila in filas_de_payload(payload):
                # Se deduplica por NroMovimiento, el correlativo del banco: es
                # unico por movimiento, asi que dos movimientos identicos el
                # mismo dia sobreviven los dos. Deduplicar por contenido
                # perderia plata.
                #
                # Los EN_CANJE reinician su numeracion en 1, asi que se les
                # antepone el estado para que no choquen con un liquidado.
                nro = str(fila.get("NroMovimiento") or "").strip()
                en_canje = not (fila.get("TipoMovimiento") or "").strip()
                clave = ("C" if en_canje else "L") + nro if nro else None
                if clave is not None:
                    if clave in vistos:
                        continue
                    vistos.add(clave)
                fila = dict(fila)
                fila["_divisa"] = divisa
                fila["_ccc"] = ccc
                filas.append(fila)
                nuevas += 1
        return nuevas, paginas

    llego = await captura.esperar(T_RESPUESTA, coincide)

    # Si la via CDP no vio nada, el subframe puede haber salido a otro proceso.
    # Se instala el respaldo dentro del frame y se vuelve a consultar UNA vez.
    if not llego and await captura.instalar_respaldo(dbg):
        avisos.append(
            "la captura por CDP no vio la respuesta y hubo que usar el respaldo "
            "XHR dentro del frame: revisa que --disable-features siga yendo una "
            "sola vez en browser_args"
        )
        dbg("reintento de Consultar con el respaldo puesto")
        await asyncio.sleep(_h(1.5))
        await _apretar(ambito, sel_consultar)
        llego = await captura.esperar(T_RESPUESTA, coincide)

    # Un reintento mas cuando SI hubo respuestas pero ninguna del rango pedido:
    # normalmente significa que Knockout no tomo las fechas y el portal contesto
    # su ventana por defecto.
    if not llego and captura.descartados:
        dbg("llegaron respuestas de otro rango (%s); se reintenta"
            % ", ".join(captura.rangos_descartados()))
        await asyncio.sleep(_h(1.5))
        await _set_fecha(ambito, sel_desde, to_ui_date(desde))
        await asyncio.sleep(_h(0.5))
        await _set_fecha(ambito, sel_hasta, to_ui_date(hasta))
        await asyncio.sleep(_h(0.5))
        await _apretar(ambito, sel_consultar)
        llego = await captura.esperar(T_RESPUESTA, coincide)

    if not llego:
        # Se falla en vez de devolver lo que sea. Entregar la ventana por
        # defecto del portal como si fuera el rango pedido es el peor resultado
        # posible: parece exito y los datos estan mal.
        if captura.descartados:
            raise RuntimeError(
                "Se pidio %s..%s pero el portal solo respondio con %s. El "
                "formulario de fechas no tomo el valor: revisa "
                "SEL_FECHA_DESDE/HASTA y el screenshot en %s. No se entregan "
                "los datos del rango equivocado."
                % (desde, hasta, " / ".join(captura.rangos_descartados()), SCREENSHOT_DIR)
            )
        raise RuntimeError(
            "El tramo %s..%s no produjo ninguna respuesta de ObtenerMovimientos "
            "en %ds. Revisa el screenshot en %s." % (desde, hasta, T_RESPUESTA, SCREENSHOT_DIR)
        )

    if captura.sin_verificar:
        avisos.append(
            "%d respuesta(s) del tramo %s..%s no traian FechaDesde/FechaHasta, "
            "asi que no se pudo comprobar que fueran del rango pedido"
            % (captura.sin_verificar, desde, hasta)
        )

    en_pagina, _ = sumar(captura.vaciar())
    ultima_pagina_llena = en_pagina >= TAM_PAGINA
    dbg("pagina 1: %d fila(s)" % en_pagina)
    await screenshot(tab, "pagina_1_%s" % desde)

    agotado = False
    for pagina in range(2, max_paginas + 1):
        estado = None
        for _ in range(6):
            estado = await ambito.ev(_JS_ESTADO_SIGUIENTE % json.dumps(SEL_SIGUIENTE))
            if estado == "habilitado":
                break
            await asyncio.sleep(0.5)
        if estado != "habilitado":
            dbg("no hay pagina %d (%s)" % (pagina, estado))
            agotado = True
            break

        # Se tira lo pendiente ANTES de pedir la pagina: una respuesta rezagada
        # de la consulta anterior se leeria como si fuera esta.
        await captura.limpiar()
        await asyncio.sleep(_h(0.8))
        apretado = await ambito.ev(_JS_CLICK_SIGUIENTE % json.dumps(SEL_SIGUIENTE))
        if apretado != "apretado":
            dbg("no se pudo apretar siguiente en la pagina %d (%s)" % (pagina, apretado))
            break

        # Se espera una respuesta que ademas sea una PAGINA nueva, no una
        # repetida: por eso el ciclo mira `paginas` y no solo si llego algo.
        en_pagina = paginas_nuevas = 0
        fin_espera = time.time() + T_RESPUESTA
        while time.time() < fin_espera:
            if not await captura.esperar(max(2, fin_espera - time.time()), coincide):
                break
            en_pagina, paginas_nuevas = sumar(captura.vaciar())
            if paginas_nuevas:
                break
            dbg("pagina %d: llego una respuesta repetida, se sigue esperando" % pagina)

        if not paginas_nuevas:
            avisos.append(
                "la pagina %d del tramo %s..%s no trajo una pagina nueva en %ds. "
                "La cartola queda INCOMPLETA: se leyeron %d movimiento(s) y la "
                "ultima pagina venia llena, asi que hay mas."
                % (pagina, desde, hasta, T_RESPUESTA, len(filas))
            )
            break

        ultima_pagina_llena = en_pagina >= TAM_PAGINA
        dbg("pagina %d: %d fila(s) nueva(s), total %d" % (pagina, en_pagina, len(filas)))
        if not en_pagina:
            agotado = True
            break
    else:
        # Nunca truncar en silencio: si se llego al tope, hay que decirlo.
        avisos.append(
            "se alcanzo el tope de %d paginas en el tramo %s..%s: la cartola "
            "puede estar incompleta. Sube max_paginas o acorta max_dias."
            % (max_paginas, desde, hasta)
        )

    # La senal mas clara de truncamiento: la ultima pagina leida venia llena.
    # Si de verdad no hubiera mas, la ultima vendria a medias.
    if ultima_pagina_llena and not agotado:
        avisos.append(
            "el tramo %s..%s termino con una pagina LLENA (%d filas): faltan "
            "movimientos casi con seguridad. Revisa el control de paginacion "
            "(%s) en el screenshot de %s."
            % (desde, hasta, TAM_PAGINA, SEL_SIGUIENTE, SCREENSHOT_DIR)
        )

    return filas, avisos


# ── Scraper ───────────────────────────────────────────────────────────────────

async def cosechar(rut, clave, cuenta, desde, hasta, max_dias, max_paginas, dbg):
    liberar_perfil(dbg)
    if not start_xvfb(dbg):
        raise RuntimeError("No se pudo levantar Xvfb en %s" % DISPLAY_NUM)

    navegador = None
    try:
        os.makedirs(PROFILE_DIR, exist_ok=True)

        config = uc.Config(
            browser_executable_path=CHROME_PATH,
            headless=False,
            sandbox=False,
            user_data_dir=PROFILE_DIR,
            lang="es-CL,es;q=0.9,en-US;q=0.8,en;q=0.7",
            browser_args=[
                "--window-size=%d,%d" % (ANCHO, ALTO),
                "--display=%s" % DISPLAY_NUM,
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--disable-sync",
                "--disable-notifications",
                "--mute-audio",
                "--log-level=3",
                "--force-color-profile=srgb",
                "--password-store=basic",
                "--use-mock-keychain",
                "--disable-breakpad",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                # Una sola vez. El script viejo lo pasaba dos veces y Chrome se
                # queda con el ultimo, asi que IsolateOrigins nunca se
                # desactivaba y la mitad de la lista no hacia nada.
                "--disable-features=TranslateUI,IsolateOrigins,site-per-process",
            ],
        )

        navegador = await uc.start(config=config)
        tab = await navegador.get("about:blank")

        # En document_start y en cada frame nuevo, antes de que corra el sensor.
        try:
            await tab.send(uc.cdp.page.add_script_to_evaluate_on_new_document(
                source=_STEALTH_JS,
            ))
            dbg("stealth instalado en document_start")
        except Exception as ex:
            dbg("AVISO: no se pudo instalar el stealth: %s" % ex)

        # NO se sobreescribe el User-Agent. Era el fallo mas caro del script
        # viejo: mandaba UA=Chrome/136 y sec-ch-ua con la version real, y
        # Akamai cruza esos dos headers. El UA real siempre es consistente.

        captura = Captura(tab, PATRON_MOVIMIENTOS)
        await captura.instalar()
        dbg("captura de ObtenerMovimientos instalada (via CDP)")

        dbg("navegando a la portada")
        tab = await navegador.get(URL_PORTADA)
        for _ in range(20):
            await asyncio.sleep(0.5)
            if "officebanking" in (tab.url or ""):
                break
        dbg("URL: %s" % (tab.url or "")[:90])

        await _warmup(tab, dbg)

        # Si el perfil conserva la sesion, el portal entra directo y no hay
        # formulario que llenar. Es lo que evita pedir la Superclave a diario.
        ya_dentro = False
        if "privado.officebanking.cl" in (tab.url or ""):
            ya_dentro = True
        else:
            try:
                if await tab.find("Cuentas Corrientes", best_match=False, timeout=2):
                    ya_dentro = True
            except Exception:
                pass

        if ya_dentro:
            dbg("sesion reutilizada del perfil persistente")
        else:
            await ingresar(tab, rut, clave, dbg)

        await asyncio.sleep(_h(2.0))
        await screenshot(tab, "s04_post_login")
        await cerrar_popups(tab, dbg)
        await _pasear_mouse(tab, pasos=2)

        ambito = await abrir_movimientos(tab, dbg)
        if ambito is None:
            raise RuntimeError(
                "No se pudo abrir Saldos y movimientos en %ds. Revisa el "
                "screenshot en %s." % (T_MOVIMIENTOS, SCREENSHOT_DIR)
            )
        captura.usar_frame(ambito)

        tramos = tramos_fechas(desde, hasta, max_dias)
        dbg("%d tramo(s) de hasta %d dias" % (len(tramos), max_dias))

        crudos = []
        avisos = []
        for t_desde, t_hasta in tramos:
            t0 = time.time()
            filas, avisos_tramo = await consultar_tramo(
                tab, ambito, captura, t_desde, t_hasta, max_paginas, dbg,
            )
            avisos.extend(avisos_tramo)
            crudos.extend(filas)
            dbg("tramo %s..%s: %d fila(s) en %.1fs"
                % (t_desde, t_hasta, len(filas), time.time() - t0))
            await asyncio.sleep(_h(2.5))

        avisos.extend(captura.errores)
        return crudos, avisos

    finally:
        try:
            if navegador:
                navegador.stop()
        except Exception:
            pass
        stop_xvfb()


# ── Entrada de Mage ───────────────────────────────────────────────────────────

@data_loader
def cosechar_cartola(*args, **kwargs):
    """Devuelve la cartola normalizada como DataFrame, plano y sin JSON anidado.

    Columnas:

        banco              SANTANDER. Constante, pero deja la tabla lista para
                           unir BCI y Banco de Chile sin ambiguedad
        cuenta             numero de cuenta, sin ceros a la izquierda
        ccc                cuenta completa que devuelve el banco
        divisa             CLP
        nro_movimiento     correlativo del banco. Es el orden cronologico
        fecha_mov          aaaa-mm-dd (ISO: el orden lexicografico es el
                           cronologico, y con dd-mm-yyyy el ORDER BY sale mal)
        fecha_contable     aaaa-mm-dd. Difiere de fecha_mov en ~1 de cada 3
                           filas: lo del viernes tarde se contabiliza el lunes.
                           Es POR ESTA que filtra el formulario del portal
        hora               hh:mm en que se ORIGINO la operacion; informativa
        descripcion        completa, con los espacios de relleno colapsados
        monto              Decimal con dos decimales, negativo en los cargos
        tipo               CARGO | ABONO
        saldo              saldo posterior al movimiento (NuevoSaldo)
        estado             LIQUIDADO | EN_CANJE
        codigo_movimiento  codigo de tipo de transaccion del banco (0973, 0943)
        sucursal           glosa de la sucursal
        codigo_sucursal    codigo de la sucursal
        hash_mov           sha256 idempotente. Clave primaria sugerida: ya
                           lleva banco y cuenta dentro
        ordinal            posicion dentro del grupo de movimientos identicos.
                           Explica por que cuatro filas iguales tienen hash
                           distinto
        extraido_en        UTC ISO de esta corrida. Necesario para los EN_CANJE,
                           que son una foto que muta

    Falla si un tramo se queda sin datos y `fallar_si_vacio` es true. Por
    defecto no: hay dias sin movimientos de verdad. Pero cualquier aviso de
    integridad (salto de saldo, tope de paginas, pagina que no respondio) se
    imprime: un ETL que termina en verde con datos de menos es peor que uno
    que falla.

    Y si el portal responde con un rango distinto del pedido, `consultar_tramo`
    levanta una excepcion en vez de entregar esos datos: parecer exito con la
    ventana equivocada es el peor resultado posible.
    """
    nest_asyncio.apply()

    registro = []

    def dbg(msg):
        linea = "%s | %s" % (datetime.now(pytz.utc).isoformat(), msg)
        registro.append(linea)
        print("[LOG] %s" % linea, flush=True)

    rut = _cfg(kwargs, "SANTANDER_RUT", RUT_DEFECTO)
    clave = _cfg(kwargs, "SANTANDER_CLAVE", CLAVE_DEFECTO)
    cuenta = _cfg(kwargs, "SANTANDER_CUENTA", CUENTA_DEFECTO)

    desde, hasta = resolver_rango(kwargs)
    max_dias = int(kwargs.get("max_dias") or MAX_DIAS_DEFECTO)
    max_paginas = int(kwargs.get("max_paginas") or MAX_PAGINAS_DEFECTO)
    fallar_si_vacio = str(kwargs.get("fallar_si_vacio", False)).strip().lower() \
        in ("true", "1", "si", "yes")

    # El RUT si, la clave nunca.
    print("Cuenta %s . rango %s..%s (el portal filtra por fecha contable)"
          % (cuenta, desde, hasta), flush=True)

    t_inicio = time.time()
    loop = asyncio.get_event_loop()
    crudos, avisos = loop.run_until_complete(
        cosechar(rut, clave, cuenta, desde, hasta, max_dias, max_paginas, dbg)
    )
    dbg("cosecha en %.1fs: %d fila(s) crudas" % (time.time() - t_inicio, len(crudos)))

    movs = normalizar_movimientos(
        crudos, cuenta, BANCO,
        ccc=_cfg(kwargs, "SANTANDER_CCC", CCC_DEFECTO),
        divisa=_cfg(kwargs, "SANTANDER_DIVISA", DIVISA_DEFECTO),
    )

    reporte = validar_saldo_corrido(movs)
    print("  %s" % reporte["resumen"], flush=True)
    for salto in reporte["saltos"][:5]:
        print("      %s entre %s y %s: falta %s  (%s)"
              % ("SALTO GRAVE" if not salto["hueco_correlativo"] else "salto explicado",
                 salto["desde_mov"], salto["hasta_mov"], salto["diferencia"],
                 "correlativo contiguo, falta un movimiento"
                 if not salto["hueco_correlativo"]
                 else "hay hueco de correlativo: quedo fuera del rango pedido"),
              flush=True)
    if not reporte["ok"]:
        avisos.append(reporte["resumen"])

    avisos.extend(validar_cobertura(movs, desde, hasta))

    for aviso in avisos:
        print("  AVISO: %s" % aviso, flush=True)

    columnas = list(COLUMNAS)
    if not movs:
        if fallar_si_vacio:
            raise RuntimeError(
                "No se extrajo ningun movimiento entre %s y %s. Con "
                "fallar_si_vacio=true eso es un error: un rango vacio se parece "
                "demasiado a una extraccion que fallo en silencio." % (desde, hasta)
            )
        print("Sin movimientos entre %s y %s" % (desde, hasta), flush=True)
        return pd.DataFrame(columns=columnas)

    df = pd.DataFrame(movs, columns=columnas)
    # Decimal y no int: con cuentas en dolares los centavos importan, y sumar
    # floats para reportar totales de plata es pedir un descuadre.
    abonos = sum((m["monto"] for m in movs if m["tipo"] == ABONO), Decimal("0"))
    cargos = sum((m["monto"] for m in movs if m["tipo"] == CARGO), Decimal("0"))
    en_canje = int((df["estado"] == EN_CANJE).sum())
    print("%d movimiento(s) . abonos %s . cargos %s . %d hash(es) distintos"
          % (len(df), format(abonos, ",f"), format(cargos, ",f"),
             df["hash_mov"].nunique()), flush=True)
    if en_canje:
        # Se avisa siempre: quien carga esto tiene que decidir que hace con
        # ellos, porque el mismo deposito vuelve con correlativo real al
        # liquidarse y se contaria dos veces.
        print("  %d movimiento(s) EN_CANJE (depositos sin liquidar, fecha futura, "
              "correlativo propio al reves). Borralos por cuenta antes de cada "
              "carga o excluyelos de la conciliacion." % en_canje, flush=True)
    print("Listo en %.1fs" % (time.time() - t_inicio), flush=True)
    return df


@test
def test_output(output, *args) -> None:
    assert output is not None, "El bloque no devolvio nada"
    assert isinstance(output, pd.DataFrame), "Tiene que devolver un DataFrame"

    assert list(output.columns) == list(COLUMNAS), (
        "El esquema cambio. Esperado: %s | Recibido: %s"
        % (", ".join(COLUMNAS), ", ".join(output.columns))
    )

    if output.empty:
        return

    # El hash es la clave de idempotencia: si se repite, el upsert de destino
    # perderia movimientos.
    repetidos = int(output["hash_mov"].duplicated().sum())
    assert repetidos == 0, "%d hash_mov repetido(s): el upsert perderia datos" % repetidos

    assert set(output["tipo"].unique()) <= {CARGO, ABONO}, \
        "tipo tiene valores fuera de CARGO/ABONO"
    assert set(output["estado"].unique()) <= {LIQUIDADO, EN_CANJE}, \
        "estado tiene valores fuera de LIQUIDADO/EN_CANJE"
    assert (output.loc[output["tipo"] == CARGO, "monto"] <= 0).all(), \
        "hay CARGOs con monto positivo"
    assert (output.loc[output["tipo"] == ABONO, "monto"] >= 0).all(), \
        "hay ABONOs con monto negativo"
    for columna in ("fecha_mov", "fecha_contable"):
        fechas = output[columna].dropna()
        assert fechas.str.match(r"^\d{4}-\d{2}-\d{2}$").all(), \
            "%s tiene que ser ISO aaaa-mm-dd" % columna

    # Los montos son Decimal a proposito: con cuentas en dolares un float
    # arrastra error binario y un int se come los centavos.
    assert all(isinstance(v, Decimal) for v in output["monto"]), \
        "monto tiene que ser Decimal, no float ni int"

    # Un solo banco y una sola cuenta por corrida: si aparecen dos, algo se
    # mezclo y el hash dejaria de ser confiable como clave.
    assert output["banco"].nunique() == 1, "vinieron varios bancos en una corrida"
    assert output["cuenta"].nunique() == 1, "vinieron varias cuentas en una corrida"

    # Los liquidados deben traer el correlativo del banco: es la unica forma de
    # ordenarlos cronologicamente y de encadenar el saldo.
    liquidados = output[output["estado"] == LIQUIDADO]
    if not liquidados.empty:
        assert liquidados["nro_movimiento"].notna().all(), \
            "hay movimientos LIQUIDADO sin nro_movimiento"
        assert not liquidados["nro_movimiento"].duplicated().any(), \
            "nro_movimiento repetido entre liquidados: se mezclaron dos consultas"