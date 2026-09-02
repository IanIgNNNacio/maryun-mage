"""Carga el DWH de facturas RCV desde el Postgres del ERP.

Recarga completa: `DocumentSplit` no tiene `updatedAt` ni tumba y
`applyClassification` borra y recrea, asi que un incremental que solo inserte
deja filas huerfanas y el documento pasa a sumar mas que su base.

La variable `periodo` del pipeline acota la recarga a un mes (formato
2025-07). Vacia, recarga todo: a 21.900 filas son segundos.
"""
import os
import sys

import yaml

sys.path.insert(0, "/home/src/Maryun/utils")
from carga_dwh_rcv import CargaDetenida, cargar_periodo

if "custom" not in globals():
    from mage_ai.data_preparation.decorators import custom


def _cfg():
    # La ruta del proyecto, no la del bloque: los bloques viven en
    # /home/src/Maryun/custom y el io_config esta un nivel arriba.
    ruta = "/home/src/Maryun/io_config.yaml"
    return yaml.safe_load(open(ruta, encoding="utf-8"))["maryun"]


@custom
def cargar(*args, **kwargs):
    import psycopg2

    c = _cfg()
    periodo = (kwargs.get("periodo") or "").strip() or None

    cn = psycopg2.connect(
        host=c["ERP_PG_HOST"], port=int(c["ERP_PG_PORT"]), dbname=c["ERP_PG_DB"],
        user=c["ERP_PG_USER"], password=c["ERP_PG_PASSWORD"], connect_timeout=15,
    )
    try:
        cur = cn.cursor()
        try:
            r = cargar_periodo(cur, periodo, dry_run=False)
            cn.commit()
        except CargaDetenida:
            cn.rollback()
            raise
    finally:
        cn.close()

    for k in ("documentos_leidos", "documentos_publicados", "documentos_en_cuarentena",
              "filas_cuenta", "filas_centro", "desvio_total_pesos"):
        print("%-26s %s" % (k, r[k]))
    print("%-26s $%s" % ("base publicada", r["base_publicada"]))
    return r
