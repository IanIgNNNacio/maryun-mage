"""Comprueba que el reparto de Mage sigue coincidiendo con el del ERP.

Es el primer bloque a proposito. El DWH reimplementa `repartirMonto` en Python
para no depender del repo del ERP, y el precio de esa independencia es que las
dos versiones pueden separarse sin que nadie lo note. Aqui se contrasta contra
1.286 casos generados desde la funcion real de TypeScript.

Si falla, el pipeline se detiene y el bloque de carga no llega a correr.
"""
import sys

sys.path.insert(0, "/home/src/Maryun/utils")
from reparto import verificar_o_fallar

if "custom" not in globals():
    from mage_ai.data_preparation.decorators import custom


@custom
def verificar(*args, **kwargs):
    r = verificar_o_fallar()
    print("reparto: %d casos, %d fallos" % (r["n_casos"], r["n_fallos"]))
    print("regla del ERP: %s" % r["hash_regla"][:16])
    return r
