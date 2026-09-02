"""Reparto porcentual de una factura, en Python, igual que el ERP.

Es un PORT de `repartirMonto` de maryun-erp
(`domain/accounting/split-rules.ts`), no una reinterpretacion. Vive aqui, en
Mage, y no llama al ERP a proposito: el DWH no debe depender del repo del ERP
para poder cargar. El precio de esa independencia es que las dos
implementaciones pueden separarse sin que nadie lo note, y por eso existe
`verificar_contra_casos`, que compara contra la salida real de la funcion de
TypeScript sobre 1.286 casos.

La trampa del port, y la razon de que el contraste no sea opcional:

    Math.round de JavaScript lleva el medio HACIA ARRIBA:  Math.round(2.5) == 3
    round de Python redondea al PAR mas cercano:                 round(2.5) == 2

Con un reparto 50/50 sobre $5, JavaScript da [3, 2] y Python ingenuo da [2, 3].
Los dos suman 5, asi que ninguna comprobacion de suma lo detecta: el documento
cuadra y el dinero esta en el centro de costo equivocado.

Por eso aqui se usa floor(x + 0.5), que es lo que hace JavaScript, y por eso
los porcentajes se fuerzan a float: si llegan como Decimal desde Postgres, la
aritmetica cambia y el resultado deja de coincidir.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from typing import Iterable, List, Sequence

# El archivo de casos y el hash de la regla de la que salieron. Se regenera
# desde el repo del ERP con scripts/tmp-generar-casos-reparto.ts.
CASOS_POR_DEFECTO = os.path.join(os.path.dirname(__file__), "casos-reparto.json")


def _redondear_como_javascript(x: float) -> int:
    """Math.round de JavaScript: el medio siempre hacia arriba.

    No es round() de Python, que redondea al par. Tampoco es
    Decimal.quantize(ROUND_HALF_UP), que sobre -2.5 da -3 mientras JavaScript
    da -2. Aqui no llegan negativos -el signo se aplica despues del reparto-,
    pero la formula se deja fiel de todos modos.
    """
    return math.floor(x + 0.5)


def repartir_monto(total: float, percents: Sequence[float]) -> List[int]:
    """Convierte un reparto porcentual en montos enteros que suman el total.

    Todos los tramos se redondean menos el ultimo, que absorbe el residuo. Sin
    eso, 33,33 % tres veces sobre 100.000 daria 99.999 y el asiento no cuadra
    por un peso.

    No valida nada, igual que el original: un reparto que no suma 100 se
    reparte lo mismo y el ultimo tramo se traga la diferencia. Detectarlo es
    trabajo de las invariantes de carga, no de esta funcion.
    """
    if not percents:
        return []
    total = float(total)
    montos: List[int] = []
    acumulado = 0
    for p in percents[:-1]:
        m = _redondear_como_javascript(total * float(p) / 100)
        montos.append(m)
        acumulado += m
    montos.append(int(total) - acumulado)
    return montos


def hash_de_la_regla(ruta_regla: str) -> str:
    """SHA-256 del split-rules.ts del que salieron los casos."""
    with open(ruta_regla, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def verificar_contra_casos(ruta: str = CASOS_POR_DEFECTO) -> dict:
    """Compara este port contra la salida real del TypeScript del ERP.

    Devuelve un resumen. NO lanza: quien llama decide si detener la carga, para
    que el bloque de Mage pueda registrar el detalle antes de fallar.
    """
    with open(ruta, encoding="utf-8") as f:
        datos = json.load(f)

    fallos = []
    for i, c in enumerate(datos["casos"]):
        obtenido = repartir_monto(c["total"], c["percents"])
        if obtenido != c["esperado"]:
            fallos.append({
                "caso": i,
                "total": c["total"],
                "percents": c["percents"],
                "esperado": c["esperado"],
                "obtenido": obtenido,
            })

    return {
        "ok": not fallos,
        "n_casos": len(datos["casos"]),
        "n_fallos": len(fallos),
        "hash_regla": datos["hash_regla"],
        "fallos": fallos[:20],
    }


def verificar_o_fallar(ruta: str = CASOS_POR_DEFECTO,
                       ruta_regla: str | None = None) -> dict:
    """Como la anterior, pero detiene la carga.

    Dos motivos para detenerla, y conviene distinguirlos en el mensaje:

      * el port ya no coincide con los casos -> alguien toco esta funcion
      * el hash de split-rules.ts cambio     -> alguien toco la regla en el ERP
        y no se regeneraron los casos, asi que los casos ya no prueban nada

    Publicar cifras que la contabilidad no reconoce es peor que no publicar.
    """
    r = verificar_contra_casos(ruta)

    if ruta_regla and os.path.exists(ruta_regla):
        actual = hash_de_la_regla(ruta_regla)
        r["hash_actual"] = actual
        r["regla_cambio"] = actual != r["hash_regla"]
        if r["regla_cambio"]:
            raise RuntimeError(
                "La regla de reparto del ERP cambio y los casos no se han "
                "regenerado.\n"
                "  casos generados desde: %s\n"
                "  split-rules.ts es ahora: %s\n"
                "Los casos ya no prueban nada. Regenerar con "
                "scripts/tmp-generar-casos-reparto.ts en el repo del ERP."
                % (r["hash_regla"][:16], actual[:16])
            )

    if not r["ok"]:
        lineas = "\n".join(
            "    total=%s percents=%s  ERP=%s  Mage=%s"
            % (f["total"], f["percents"], f["esperado"], f["obtenido"])
            for f in r["fallos"]
        )
        raise RuntimeError(
            "El reparto de Mage ya no coincide con el del ERP: %d de %d casos.\n%s"
            % (r["n_fallos"], r["n_casos"], lineas)
        )
    return r


def repartir_con_signo(base: float, percents: Iterable[float], signo: int) -> List[int]:
    """Reparte el valor absoluto y aplica el signo despues.

    El orden importa: en JavaScript Math.round(-0.5) devuelve -0, asi que
    repartir un monto negativo no es el espejo exacto de repartir su positivo.
    Una nota de credito dejaria de cuadrar contra su factura.
    """
    montos = repartir_monto(abs(float(base)), list(percents))
    return [signo * m for m in montos]
