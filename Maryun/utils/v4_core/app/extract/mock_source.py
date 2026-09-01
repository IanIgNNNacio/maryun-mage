"""In-memory mock source — used in tests and for ``--source mock`` runs.

Generates a small but realistic dataset: ~12 SKUs (mix of nacional/importado),
11 sucursales + 2 CDs, 18 months of history. Deterministic with a seed.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from dateutil.relativedelta import relativedelta

from app.normalize.canonical import CDS_CANONICOS, SUCURSALES_CANONICAS
from app.temporal.gate import TemporalGate


@dataclass
class MockDataset:
    products: pd.DataFrame
    stock: pd.DataFrame
    demand: pd.DataFrame


class MockSource:
    """Deterministic mock data generator."""

    def __init__(self, seed: int = 42) -> None:
        self._rng = np.random.default_rng(seed)

    # A small SKU catalogue with nacional/importado pairs so homologation has
    # something to match. ``pareja`` links an importado to its nacional.
    _CATALOG: list[dict] = [
        {"sku_id": "1001", "sku": "TUE-001", "nombre": "Tuerca M10 Nac",  "procedencia": "nacional",  "costo": 120.0,  "critico": False, "familia": "Fijaciones", "marca": "Generico",  "tipo": "Tuerca",  "sale_ok": True, "purchase_ok": True},
        {"sku_id": "1002", "sku": "TUE-001I","nombre": "Tuerca M10 Imp",  "procedencia": "importado", "costo": 95.0,   "critico": False, "familia": "Fijaciones", "marca": "AsiaCo",    "tipo": "Tuerca",  "sale_ok": True, "purchase_ok": True},
        {"sku_id": "1003", "sku": "PER-020", "nombre": "Perno 3/8 Nac",   "procedencia": "nacional",  "costo": 250.0,  "critico": True,  "familia": "Fijaciones", "marca": "Generico",  "tipo": "Perno",   "sale_ok": True, "purchase_ok": True},
        {"sku_id": "1004", "sku": "PER-020I","nombre": "Perno 3/8 Imp",   "procedencia": "importado", "costo": 210.0,  "critico": True,  "familia": "Fijaciones", "marca": "AsiaCo",    "tipo": "Perno",   "sale_ok": True, "purchase_ok": True},
        {"sku_id": "1005", "sku": "GRA-500", "nombre": "Grasa 500g Nac",  "procedencia": "nacional",  "costo": 4500.0, "critico": False, "familia": "Lubricantes","marca": "Shell",     "tipo": "Grasa",   "sale_ok": True, "purchase_ok": True},
        {"sku_id": "1006", "sku": "GRA-500I","nombre": "Grasa 500g Imp",  "procedencia": "importado", "costo": 3900.0, "critico": False, "familia": "Lubricantes","marca": "Mobil",     "tipo": "Grasa",   "sale_ok": True, "purchase_ok": True},
        {"sku_id": "1007", "sku": "GUA-XL",  "nombre": "Guantes XL Nac",  "procedencia": "nacional",  "costo": 1800.0, "critico": False, "familia": "EPP",        "marca": "SafeIndustrial","tipo":"Guante","sale_ok": True, "purchase_ok": True},
        {"sku_id": "1008", "sku": "GUA-XLI", "nombre": "Guantes XL Imp",  "procedencia": "importado", "costo": 1450.0, "critico": False, "familia": "EPP",        "marca": "Honeywell","tipo": "Guante",  "sale_ok": True, "purchase_ok": True},
        {"sku_id": "1009", "sku": "CAS-AMA", "nombre": "Casco Amarillo",  "procedencia": "nacional",  "costo": 8200.0, "critico": True,  "familia": "EPP",        "marca": "SafeIndustrial","tipo":"Casco","sale_ok": True, "purchase_ok": True},
        {"sku_id": "1010", "sku": "CAS-BLA", "nombre": "Casco Blanco Imp","procedencia": "importado", "costo": 7500.0, "critico": True,  "familia": "EPP",        "marca": "3M",        "tipo": "Casco",   "sale_ok": True, "purchase_ok": True},
        {"sku_id": "1011", "sku": "LUB-1L",  "nombre": "Lubricante 1L",   "procedencia": "nacional",  "costo": 6500.0, "critico": False, "familia": "Lubricantes","marca": "Copec",     "tipo": "Aceite",  "sale_ok": True, "purchase_ok": True},
        {"sku_id": "1012", "sku": "HER-LLV", "nombre": "Llave Francesa",  "procedencia": "nacional",  "costo": 12000.0,"critico": False, "familia": "Herramientas","marca": "Stanley",  "tipo": "Llave",   "sale_ok": True, "purchase_ok": True},
    ]

    HOMOLOGATIONS: list[tuple[str, str]] = [
        ("1002", "1001"),
        ("1004", "1003"),
        ("1006", "1005"),
        ("1008", "1007"),
        ("1010", "1009"),
    ]

    def build(self, gate: TemporalGate) -> MockDataset:
        return MockDataset(
            products=self._products(),
            stock=self._stock(),
            demand=self._demand(gate),
        )

    # ------------------------------------------------------------------
    def _products(self) -> pd.DataFrame:
        df = pd.DataFrame(self._CATALOG)
        df["sku_id"] = df["sku_id"].astype(str)
        return df

    def _stock(self) -> pd.DataFrame:
        locations = list(SUCURSALES_CANONICAS) + list(CDS_CANONICOS)
        rows = []
        for prod in self._CATALOG:
            for loc in locations:
                base = 50 if loc.startswith("CD") else 15
                qty = float(max(0, self._rng.poisson(base)))
                rows.append({"sku_id": prod["sku_id"], "ubicacion": loc, "qty": qty})
        return pd.DataFrame(rows)

    def _demand(self, gate: TemporalGate) -> pd.DataFrame:
        locations = list(SUCURSALES_CANONICAS)  # demand lives at sucursales
        rows = []
        months = gate.window.months
        for prod in self._CATALOG:
            # Base demand per SKU (so ranks differ for ABC testing)
            base_sku = float(self._rng.integers(5, 40))
            for loc in locations:
                loc_factor = float(self._rng.uniform(0.5, 1.5))
                for m in months:
                    noise = float(self._rng.normal(1.0, 0.15))
                    seasonal = 1.0 + 0.20 * np.sin(2 * np.pi * (m.month / 12.0))
                    demand = max(0.0, base_sku * loc_factor * seasonal * noise)
                    rows.append({
                        "sku_id": prod["sku_id"],
                        "ubicacion": loc,
                        "mes": pd.Timestamp(m),
                        "demanda": round(demand, 2),
                    })
        return pd.DataFrame(rows)
