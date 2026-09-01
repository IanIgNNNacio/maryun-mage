"""Baseline ABC/XYZ classification.

Computed per ``(sku_id, ubicacion)``:

* **ABC** — cumulative revenue contribution ranked descending, cuts via
  ``params.classification.abc_thresholds``.
* **XYZ** — coefficient of variation (std/mean) of monthly demand, cuts via
  ``params.classification.xyz_cv_thresholds``.

Returns ``[sku_id, ubicacion, abc_modelo, xyz_modelo, clase_modelo]``.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.config.engine_params import EngineParams
from app.utils.logging import get_logger


def classify(
    demand: pd.DataFrame,
    products: pd.DataFrame,
    params: EngineParams,
) -> pd.DataFrame:
    log = get_logger("classification.baseline")
    if demand.empty:
        return pd.DataFrame(columns=[
            "sku_id", "ubicacion", "abc_modelo", "xyz_modelo", "clase_modelo",
        ])

    cp = params.classification

    # Join cost for revenue approximation (costo × cantidad).
    cost_map = products.set_index("sku_id")["costo"].to_dict() if "costo" in products.columns else {}
    grouped = demand.groupby(["sku_id", "ubicacion"], sort=False)["demanda"]

    stats = grouped.agg(["sum", "mean", "std"]).rename(
        columns={"sum": "total_qty", "mean": "mean_qty", "std": "std_qty"}
    ).reset_index()
    stats["std_qty"] = stats["std_qty"].fillna(0.0)
    stats["cv"] = np.where(stats["mean_qty"] > 0, stats["std_qty"] / stats["mean_qty"], np.inf)
    stats["costo"] = stats["sku_id"].map(cost_map).fillna(1.0)
    stats["revenue"] = stats["total_qty"] * stats["costo"]

    # ABC per ubicacion (so each sucursal gets its own ABC ranking).
    parts: list[pd.DataFrame] = []
    for ubicacion, g in stats.groupby("ubicacion", sort=False):
        parts.append(_abc_for_group(g.copy(), cp.abc_thresholds))
    abc_df = pd.concat(parts, ignore_index=True)

    abc_df["xyz_modelo"] = abc_df["cv"].apply(lambda v: _xyz_cat(v, cp.xyz_cv_thresholds))
    abc_df["clase_modelo"] = abc_df["abc_modelo"] + abc_df["xyz_modelo"]

    log.info("classification.baseline.done", rows=len(abc_df))
    return abc_df[["sku_id", "ubicacion", "abc_modelo", "xyz_modelo", "clase_modelo"]]


def _abc_for_group(g: pd.DataFrame, thresholds: list[float]) -> pd.DataFrame:
    g = g.sort_values("revenue", ascending=False).reset_index(drop=True)
    total = g["revenue"].sum()
    if total <= 0:
        g["abc_modelo"] = "C"
        return g
    g["cum_share"] = g["revenue"].cumsum() / total
    a_cut, b_cut = thresholds
    g["abc_modelo"] = np.where(
        g["cum_share"] <= a_cut, "A",
        np.where(g["cum_share"] <= b_cut, "B", "C"),
    )
    return g


def _xyz_cat(cv: float, thresholds: list[float]) -> str:
    x_cut, y_cut = thresholds
    if cv <= x_cut:
        return "X"
    if cv <= y_cut:
        return "Y"
    return "Z"
