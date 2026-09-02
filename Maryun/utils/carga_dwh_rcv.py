"""Carga del DWH de facturas RCV desde maryun-erp.

Lee `SiiDocument` y `DocumentSplit` de la base del ERP y escribe las tres
tablas del esquema `dwh`. La aritmetica del reparto viene de `reparto.py`, que
es el port verificado de la funcion del ERP.

Tres cosas que no son obvias y que estan aqui a proposito:

1. La politica de impuestos se LEE DE LA BASE. `getAccountingConfig()` del ERP
   la saca de `IntegrationSetting` con la clave 'accounting' y solo cae a
   {28, 35} por defecto. Fijarla en el codigo haria que el DWH divergiera en
   silencio el dia que alguien la cambie por pantalla.

2. La base que se reparte NO es `montoTotal`: es neto + exento + impuestos a
   mayor costo + IVA no recuperable. Prorratear el total carga el IVA credito
   fiscal a las cuentas de gasto e infla el gasto un 18,5 %.

3. Recarga completa por periodo, nunca upsert por fila. `DocumentSplit` no
   tiene `updatedAt` ni tumba, y `applyClassification` hace deleteMany +
   createMany: un incremental que solo inserte deja filas huerfanas y el
   documento pasa a sumar mas que su base.
"""

from __future__ import annotations

import json
import os
import sys
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reparto import repartir_con_signo, repartir_monto, verificar_o_fallar  # noqa: E402

CLAVE_CONFIG = "accounting"
POLITICA_POR_DEFECTO = {"28", "35"}

# 61 es la nota de credito electronica, 60 la de papel. La 56 es nota de
# DEBITO y suma, asi que no va aqui.
TIPOS_NOTA_CREDITO = {60, 61}

# La clave del JSON del SII lleva 'Iva' en minusculas mientras la de al lado
# es 'Monto IVA Recuperable'. No es un descuido de nadie: viene asi del SII.
CLAVE_IVA_NO_RECUPERABLE = "Monto Iva No Recuperable"

SIN_CUENTA = "SIN_CUENTA"
SIN_CENTRO = "SIN_CENTRO"

# Umbrales de la seccion "Que hacer cuando fallan" del diseno.
TOLERANCIA_RESIDUO_PESOS = 2
UMBRAL_CUARENTENA_FRACCION = 0.005      # 0,5 % de los documentos del periodo
UMBRAL_DESVIO_TOTAL_PESOS = 10_000


class CargaDetenida(RuntimeError):
    """La carga no puede continuar. No es una rareza del SII: es un error."""


# ── lectura ───────────────────────────────────────────────────────────────

def leer_codigos_mayor_costo(cur) -> set:
    """Codigos de impuesto que van a mayor costo del gasto, segun el ERP."""
    cur.execute('SELECT config FROM "IntegrationSetting" WHERE key = %s', (CLAVE_CONFIG,))
    fila = cur.fetchone()
    guardado = (fila[0] or {}) if fila else {}
    politica = guardado.get("otherTaxPolicy")
    if not politica:
        return set(POLITICA_POR_DEFECTO)
    return {str(c) for c, p in politica.items() if isinstance(p, dict) and "mayorCosto" in p}


SQL_DOCUMENTOS = """
SELECT d.id, d.operacion::text, d."tipoDoc", d."rutNorm", d."folioNorm", d.folio,
       d."rutContraparte", d."nombreContraparte",
       d."fechaEmision"::date, d."fechaRecepcion"::date, d.periodo,
       coalesce(d."montoNeto",0)::bigint, coalesce(d."montoExento",0)::bigint,
       coalesce(d.iva,0)::bigint, coalesce(d."otrosImpuestos",0)::bigint,
       coalesce(d."montoTotal",0)::bigint,
       d."codigoOtroImpuesto",
       coalesce(nullif(d.fields->>%s, '')::numeric, 0)::bigint,
       d.status::text, d."statusSII"::text, d."isManual",
       d."accountId", d."costCenterId",
       d."accountSource"::text, d."costCenterSource"::text,
       a.code, a.name, a.type::text,
       c.code, c.name, c.kind::text
FROM "SiiDocument" d
LEFT JOIN "Account" a    ON a.id = d."accountId"
LEFT JOIN "CostCenter" c ON c.id = d."costCenterId"
WHERE (%s IS NULL OR d.periodo = %s)
ORDER BY d.id
"""

CAMPOS = [
    "id", "operacion", "tipo_doc", "rut_norm", "folio_norm", "folio",
    "rut_contraparte", "nombre_contraparte", "fecha_emision", "fecha_recepcion",
    "periodo", "neto", "exento", "iva", "otros_impuestos", "total",
    "codigo_otro_impuesto", "iva_no_recuperable", "status", "status_sii",
    "is_manual", "account_id", "cost_center_id", "account_source",
    "cost_center_source", "cuenta_code", "cuenta_nombre", "cuenta_tipo",
    "centro_code", "centro_nombre", "centro_kind",
]


def leer_documentos(cur, periodo: str | None) -> List[Dict[str, Any]]:
    cur.execute(SQL_DOCUMENTOS, (CLAVE_IVA_NO_RECUPERABLE, periodo, periodo))
    return [dict(zip(CAMPOS, f)) for f in cur.fetchall()]


SQL_SPLITS = """
SELECT s."documentId", s.kind::text, s.percent,
       coalesce(a.code, c.code, s."targetId") AS destino_code,
       coalesce(a.name, c.name)               AS destino_nombre,
       coalesce(a.type::text, c.kind::text)   AS destino_tipo
FROM "DocumentSplit" s
LEFT JOIN "Account" a    ON a.id = s."targetId" AND s.kind = 'ACCOUNT'
LEFT JOIN "CostCenter" c ON c.id = s."targetId" AND s.kind = 'COST_CENTER'
WHERE (%s IS NULL OR s."documentId" IN (
        SELECT id FROM "SiiDocument" WHERE periodo = %s))
-- El desempate por targetId no es cosmetico: los splits de un documento se
-- crean en la misma transaccion y comparten createdAt. Sin el, el tramo que
-- absorbe el residuo cambia entre dos corridas y el DWH deja de ser
-- reproducible.
ORDER BY s."documentId", s.kind, s."createdAt", s."targetId"
"""


def leer_splits(cur, periodo: str | None) -> Dict[Tuple[str, str], List[dict]]:
    cur.execute(SQL_SPLITS, (periodo, periodo))
    agrupados: Dict[Tuple[str, str], List[dict]] = {}
    for doc_id, kind, percent, code, nombre, tipo in cur.fetchall():
        agrupados.setdefault((doc_id, kind), []).append({
            "percent": float(percent), "code": code, "nombre": nombre, "tipo": tipo,
        })
    return agrupados


# ── construccion ──────────────────────────────────────────────────────────

def signo_de(tipo_doc: int) -> int:
    return -1 if tipo_doc in TIPOS_NOTA_CREDITO else 1


def partir_impuestos(doc: dict, codigos_mayor_costo: set) -> Tuple[int, int]:
    """Separa 'otros impuestos' en la parte repartible y la que no lo es.

    El ERP devuelve IMPUESTO_SIN_POLITICA y NO contabiliza el documento cuando
    el codigo no esta en la politica. El DWH hace lo mismo: no suma a la base
    lo que el ERP se niega a clasificar, y lo deja visible en su columna.
    """
    oi = int(doc["otros_impuestos"] or 0)
    if oi == 0:
        return 0, 0
    codigo = (doc["codigo_otro_impuesto"] or "").strip()
    if codigo and codigo in codigos_mayor_costo:
        return oi, 0
    return 0, oi


def filas_de_dimension(doc: dict, splits: List[dict], base_bruta: int, signo: int,
                       centinela: str, code_cabecera: str | None,
                       nombre_cabecera: str | None, tipo_cabecera: str | None):
    """Devuelve (filas, incidencias) para una dimension de un documento.

    Todo documento produce al menos una fila. `repartirMatriz` devuelve [] sin
    cuentas y la cabecera de un documento repartido esta en null: sin la fila
    centinela la tabla salia vacia, y una invariante de suma lo aprobaba porque
    cero filas son cero comparaciones.
    """
    incidencias = []

    if splits:
        percents = [s["percent"] for s in splits]
        destinos = [(s["code"], s["nombre"], s["tipo"], False) for s in splits]
    else:
        percents = [100.0]
        destinos = [(code_cabecera or centinela, nombre_cabecera, tipo_cabecera,
                     code_cabecera is None)]

    # I6, con la tolerancia del ERP: dos decimales, no cuatro. sumaPorcentajes
    # redondea antes de comparar, asi que 33,3333 x 3 = 99,9999 -> 100,00 es
    # valido para el servidor y tiene que serlo aqui.
    suma = round(sum(percents), 2)
    if suma != 100.0:
        incidencias.append({
            "regla": "I6_PORCENTAJES", "detalle": "suma %.4f" % sum(percents),
            "monto": 0,
        })

    montos = repartir_con_signo(base_bruta, percents, signo)

    # I2: cuanto absorbe el ultimo tramo frente a su tramo natural. Aqui esta
    # el dinero que ninguna comprobacion de porcentaje ve.
    if len(montos) > 1:
        tramo_natural = int(round(abs(base_bruta) * percents[-1] / 100))
        desvio = abs(abs(montos[-1]) - tramo_natural)
        if desvio > TOLERANCIA_RESIDUO_PESOS:
            incidencias.append({
                "regla": "I2_RESIDUO", "detalle": "el ultimo tramo absorbe %d pesos de mas" % desvio,
                "monto": desvio,
            })

    # Un tramo con el signo contrario al del documento. Pasa cuando redondear
    # hacia arriba los primeros tramos se pasa del total: repartirMonto(2,
    # [25,25,25,25]) da [1, 1, 1, -1]. Es comportamiento real del ERP, no un
    # defecto del port, pero es plata en la direccion equivocada.
    for m in montos:
        if m != 0 and (m > 0) != (signo > 0):
            incidencias.append({
                "regla": "SIGNO_TRAMO", "detalle": "tramo %d con signo contrario" % m,
                "monto": abs(m),
            })
            break

    filas = []
    for (code, nombre, tipo, es_cent), p, m in zip(destinos, percents, montos):
        filas.append({
            "documento_id": doc["id"], "key": code or centinela, "nombre": nombre,
            "tipo": tipo, "percent": p, "monto_base": m,
            "periodo": doc["periodo"], "tipo_doc": doc["tipo_doc"],
            "rut_norm": doc["rut_norm"], "fecha_emision": doc["fecha_emision"],
            "es_centinela": bool(es_cent),
        })
    return filas, incidencias


def construir(doc: dict, splits_por_clave: Dict[Tuple[str, str], List[dict]],
              codigos_mayor_costo: set):
    """Convierte un documento del ERP en sus filas del DWH."""
    signo = signo_de(doc["tipo_doc"])
    mayor_costo, sin_politica = partir_impuestos(doc, codigos_mayor_costo)
    inr = int(doc["iva_no_recuperable"] or 0)

    base_bruta = int(doc["neto"]) + int(doc["exento"]) + mayor_costo + inr

    fila_doc = {
        "documento_id": doc["id"], "operacion": doc["operacion"],
        "tipo_doc": doc["tipo_doc"], "rut_norm": doc["rut_norm"],
        "folio_norm": doc["folio_norm"], "folio": doc["folio"],
        "rut_contraparte": doc["rut_contraparte"],
        "nombre_contraparte": doc["nombre_contraparte"],
        "fecha_emision": doc["fecha_emision"], "fecha_recepcion": doc["fecha_recepcion"],
        "periodo": doc["periodo"], "signo": signo,
        "neto_bruto": int(doc["neto"]), "exento_bruto": int(doc["exento"]),
        "iva_bruto": int(doc["iva"]), "otros_impuestos_bruto": int(doc["otros_impuestos"]),
        "total_bruto": int(doc["total"]),
        "impuesto_mayor_costo": mayor_costo, "impuesto_sin_politica": sin_politica,
        "iva_no_recuperable": inr,
        "base_reparto": signo * base_bruta,
        "monto_con_signo": signo * int(doc["total"]),
        "base_en_revision": sin_politica != 0,
        "status": doc["status"], "status_sii": doc["status_sii"],
        "is_manual": bool(doc["is_manual"]),
        # 'Sin clasificar' se pregunta por el SOURCE, nunca por el id: un
        # documento repartido tiene la cabecera en null a proposito.
        "cuenta_sin_clasificar": doc["account_source"] is None,
        "centro_sin_clasificar": doc["cost_center_source"] is None,
    }

    fc, ic = filas_de_dimension(
        doc, splits_por_clave.get((doc["id"], "ACCOUNT"), []), base_bruta, signo,
        SIN_CUENTA, doc["cuenta_code"], doc["cuenta_nombre"], doc["cuenta_tipo"])
    fk, ik = filas_de_dimension(
        doc, splits_por_clave.get((doc["id"], "COST_CENTER"), []), base_bruta, signo,
        SIN_CENTRO, doc["centro_code"], doc["centro_nombre"], doc["centro_kind"])

    incidencias = [dict(i, documento_id=doc["id"], dimension="ACCOUNT") for i in ic]
    incidencias += [dict(i, documento_id=doc["id"], dimension="COST_CENTER") for i in ik]
    return fila_doc, fc, fk, incidencias


# ── escritura ─────────────────────────────────────────────────────────────

COLS_DOC = [
    "documento_id", "operacion", "tipo_doc", "rut_norm", "folio_norm", "folio",
    "rut_contraparte", "nombre_contraparte", "fecha_emision", "fecha_recepcion",
    "periodo", "signo", "neto_bruto", "exento_bruto", "iva_bruto",
    "otros_impuestos_bruto", "total_bruto", "impuesto_mayor_costo",
    "impuesto_sin_politica", "iva_no_recuperable", "base_reparto",
    "monto_con_signo", "base_en_revision", "status", "status_sii", "is_manual",
    "cuenta_sin_clasificar", "centro_sin_clasificar",
]
COLS_REPARTO = ["documento_id", "key", "nombre", "tipo", "percent", "monto_base",
                "periodo", "tipo_doc", "rut_norm", "fecha_emision", "es_centinela"]


def _insertar(cur, tabla: str, columnas: Sequence[str], filas: Iterable[dict],
              renombres: Dict[str, str] | None = None) -> int:
    filas = list(filas)
    if not filas:
        return 0
    destino = [renombres.get(c, c) if renombres else c for c in columnas]
    plantilla = "(" + ",".join(["%s"] * len(columnas)) + ")"
    valores = [tuple(f[c] for c in columnas) for f in filas]
    from psycopg2.extras import execute_values
    execute_values(
        cur,
        "INSERT INTO %s (%s) VALUES %%s" % (tabla, ",".join('"%s"' % c for c in destino)),
        valores, template=plantilla, page_size=1000,
    )
    return len(filas)


def cargar_periodo(cur, periodo: str | None, dry_run: bool = False) -> dict:
    """Recarga completa de un periodo. `periodo=None` carga todo.

    Completa y no incremental: DocumentSplit no tiene updatedAt ni tumba, y
    applyClassification borra y recrea. Un incremental que solo inserte deja
    filas huerfanas y el documento pasa a sumar mas que su base.
    """
    verificar_o_fallar()   # el port del reparto sigue coincidiendo con el ERP

    codigos = leer_codigos_mayor_costo(cur)
    documentos = leer_documentos(cur, periodo)
    splits = leer_splits(cur, periodo)

    filas_doc, filas_cta, filas_cc, incidencias = [], [], [], []
    for d in documentos:
        fd, fc, fk, inc = construir(d, splits, codigos)
        filas_doc.append(fd)
        filas_cta.extend(fc)
        filas_cc.extend(fk)
        incidencias.extend(inc)

    # ── los que detienen la carga, sin excepcion ──────────────────────────
    # No son rarezas del SII: son errores de la ETL o perdida de datos.
    en_cuarentena = {i["documento_id"] for i in incidencias}

    # I1 cobertura: todo documento produce al menos una fila en CADA dimension.
    con_cta = {f["documento_id"] for f in filas_cta}
    con_cc = {f["documento_id"] for f in filas_cc}
    ids = {d["id"] for d in documentos}
    if con_cta != ids or con_cc != ids:
        raise CargaDetenida(
            "I1 cobertura: %d documentos, %d con fila de cuenta, %d con fila de centro"
            % (len(ids), len(con_cta), len(con_cc)))

    # I1b recuento: la expansion produce exactamente las filas esperadas.
    for nombre, filas, kind in (("cuenta", filas_cta, "ACCOUNT"),
                                ("centro", filas_cc, "COST_CENTER")):
        sin_split = sum(1 for d in documentos if not splits.get((d["id"], kind)))
        de_split = sum(len(v) for (doc_id, k), v in splits.items()
                       if k == kind and doc_id in ids)
        esperadas = sin_split + de_split
        if len(filas) != esperadas:
            raise CargaDetenida(
                "I1b recuento en %s: %d filas, se esperaban %d (%d sin reparto + %d de split)"
                % (nombre, len(filas), esperadas, sin_split, de_split))

    # Clave natural duplicada.
    claves = [(f["operacion"], f["tipo_doc"], f["rut_norm"], f["folio_norm"]) for f in filas_doc]
    if len(set(claves)) != len(claves):
        from collections import Counter
        rep = [k for k, n in Counter(claves).items() if n > 1][:5]
        raise CargaDetenida("clave natural duplicada: %s" % rep)

    # Signo del documento coherente con su tipo.
    malos = [f for f in filas_doc
             if f["total_bruto"] and (f["monto_con_signo"] > 0) != (f["signo"] > 0)]
    if malos:
        raise CargaDetenida("signo incoherente en %d documentos" % len(malos))

    # ── umbral global de cuarentena ───────────────────────────────────────
    desvio_total = sum(i["monto"] for i in incidencias)
    fraccion = len(en_cuarentena) / len(documentos) if documentos else 0
    if fraccion > UMBRAL_CUARENTENA_FRACCION:
        raise CargaDetenida(
            "cuarentena %.2f %% de los documentos (%d de %d), sobre el umbral de %.1f %%"
            % (fraccion * 100, len(en_cuarentena), len(documentos),
               UMBRAL_CUARENTENA_FRACCION * 100))
    if desvio_total > UMBRAL_DESVIO_TOTAL_PESOS:
        raise CargaDetenida(
            "los desvios suman $%d, sobre el umbral de $%d"
            % (desvio_total, UMBRAL_DESVIO_TOTAL_PESOS))

    # El documento en cuarentena NO se publica: se ve como indicador, no como
    # cifra. Publicarlo mezclado seria peor que perderlo.
    filas_doc_pub = [f for f in filas_doc if f["documento_id"] not in en_cuarentena]
    filas_cta_pub = [f for f in filas_cta if f["documento_id"] not in en_cuarentena]
    filas_cc_pub = [f for f in filas_cc if f["documento_id"] not in en_cuarentena]

    resumen = {
        "periodo": periodo or "(todos)",
        "documentos_leidos": len(documentos),
        "documentos_publicados": len(filas_doc_pub),
        "documentos_en_cuarentena": len(en_cuarentena),
        "filas_cuenta": len(filas_cta_pub),
        "filas_centro": len(filas_cc_pub),
        "incidencias": len(incidencias),
        "desvio_total_pesos": desvio_total,
        "codigos_mayor_costo": sorted(codigos),
        "base_publicada": sum(f["base_reparto"] for f in filas_doc_pub),
        "total_publicado": sum(f["monto_con_signo"] for f in filas_doc_pub),
    }
    if dry_run:
        resumen["escrito"] = False
        resumen["detalle_incidencias"] = incidencias[:20]
        return resumen

    # ── borrado y reinsercion del periodo ─────────────────────────────────
    if periodo:
        cur.execute("DELETE FROM dwh.rcv_incidencia WHERE periodo = %s", (periodo,))
        cur.execute("DELETE FROM dwh.rcv_documento WHERE periodo = %s", (periodo,))
    else:
        cur.execute("DELETE FROM dwh.rcv_incidencia")
        cur.execute("DELETE FROM dwh.rcv_documento")
    # Las dos tablas de reparto caen por ON DELETE CASCADE.

    _insertar(cur, "dwh.rcv_documento", COLS_DOC, filas_doc_pub)
    _insertar(cur, "dwh.rcv_reparto_cuenta", COLS_REPARTO, filas_cta_pub,
              {"key": "cuenta_key", "nombre": "cuenta_nombre", "tipo": "cuenta_tipo"})
    _insertar(cur, "dwh.rcv_reparto_centro_costo", COLS_REPARTO, filas_cc_pub,
              {"key": "centro_key", "nombre": "centro_nombre", "tipo": "centro_kind"})
    _insertar(cur, "dwh.rcv_incidencia",
              ["documento_id", "dimension", "regla", "detalle", "monto", "periodo"],
              [dict(i, periodo=next((d["periodo"] for d in documentos
                                     if d["id"] == i["documento_id"]), None))
               for i in incidencias])

    cur.execute("""
        INSERT INTO dwh.rcv_proveedor (rut_norm, rut, nombre, giro, es_party, activo)
        SELECT DISTINCT ON (d.rut_norm) d.rut_norm, d.rut_contraparte,
               coalesce(p."businessName", d.nombre_contraparte), p.giro,
               p.id IS NOT NULL, p.active
        FROM dwh.rcv_documento d
        LEFT JOIN public."Party" p ON p."rutNorm" = d.rut_norm
        ORDER BY d.rut_norm, p.id NULLS LAST
        ON CONFLICT (rut_norm) DO UPDATE
          SET nombre = excluded.nombre, giro = excluded.giro,
              es_party = excluded.es_party, activo = excluded.activo
    """)

    resumen["escrito"] = True
    return resumen
