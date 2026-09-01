"""Operational homologation — substitute nacional for Maryun when stock=0.

Trigger
-------
For each ``(importado → nacional)`` pair flagged ``usar_operacional``, a
candidate substitution fires when **all** of the following are true for the
target sucursal:

1. Maryun imported stock is 0 at the sucursal.
2. Maryun imported stock is 0 at every location listed in
   ``params.homologation.substitution_trigger_zero_at`` that is a CD
   (by default ``CD SUR`` and ``CD SANTIAGO``).
3. There IS demand/necesidad for the imported SKU at the sucursal.
4. Nacional stock is available somewhere (sucursal or CD) to fulfil.

Output
------
``compute_substitution_candidates`` returns a DataFrame of candidate
substitutions with the *transferred demand* (need × factor_conversion) —
the replenishment engine will consume this as additional need on the
nacional SKU.

``apply_operational_substitution`` mutates the ``needs`` frame: it zero-outs
the imported need at those sucursales and appends equivalent nacional
need, returning the updated needs + a substitution-audit DataFrame.
"""
from __future__ import annotations

import pandas as pd

from app.config.engine_params import EngineParams
from app.normalize.canonical import canonical_location, is_cd
from app.utils.logging import get_logger


def compute_substitution_candidates(
    needs: pd.DataFrame,
    stock: pd.DataFrame,
    homologacion,
    params: EngineParams,
) -> pd.DataFrame:
    """Return a DataFrame of triggered substitutions (pre-mutation)."""
    log = get_logger("homologation.operational")

    if (
        not params.homologation.enabled
        or not params.homologation.operational_substitution_enabled
        or homologacion.is_empty()
        or needs.empty
    ):
        return _empty_candidates()

    # Stock pivot: (sku, ubicacion) -> qty
    stock_map: dict[tuple[str, str], float] = (
        stock.groupby(["sku_id", "ubicacion"])["qty"].sum().to_dict()
    )

    # Canonical CD list from the config trigger.
    trigger_cds = [canonical_location(x) for x in params.homologation.substitution_trigger_zero_at if is_cd(x)]

    candidates: list[dict] = []
    for (imp_sku, nac_sku) in homologacion.operacional:
        factor = homologacion.factor[(imp_sku, nac_sku)]
        rows = needs[needs["sku_id"] == imp_sku]
        if rows.empty:
            continue
        for _, row in rows.iterrows():
            ubicacion = row["ubicacion"]
            if is_cd(ubicacion):
                continue  # only sucursales trigger operational substitution
            if not bool(row.get("dispara", False)):
                continue

            imp_stock_local = stock_map.get((imp_sku, ubicacion), 0.0)
            cd_stocks = {cd: stock_map.get((imp_sku, cd), 0.0) for cd in trigger_cds}
            if imp_stock_local > 0 or any(v > 0 for v in cd_stocks.values()):
                continue  # trigger does not fire

            need_imp = float(row["necesidad"])
            if need_imp <= 0:
                continue

            # Nacional must have stock somewhere. If not, we still record the
            # candidate but the replenishment engine will handle shortage.
            nac_total = sum(
                q for (s, _u), q in stock_map.items() if s == nac_sku
            )

            candidates.append({
                "sku_id_importado": imp_sku,
                "sku_id_nacional": nac_sku,
                "ubicacion": ubicacion,
                "necesidad_importado": need_imp,
                "factor_conversion": factor,
                "demanda_transferida_a_nacional": need_imp * factor,
                "stock_nacional_total": nac_total,
                "trigger_cumple": True,
                "trigger_cds_evaluados": ",".join(trigger_cds),
            })

    log.info("homologation.operational.candidates", n=len(candidates))
    if not candidates:
        return _empty_candidates()
    return pd.DataFrame(candidates)


def apply_operational_substitution(
    needs: pd.DataFrame,
    candidates: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Apply operational substitution to the needs frame.

    Returns ``(needs_ajustado, transferencias_demanda_audit)``.

    Behaviour:
      * For each candidate row, zero out the imported SKU's need at that
        sucursal (``necesidad=0``, ``dispara=False``).
      * Add an equivalent nacional need row (merging with any pre-existing
        row for that nacional SKU at the same sucursal).
      * Emit an audit row with both sides.
    """
    log = get_logger("homologation.operational")
    if candidates.empty:
        return needs, pd.DataFrame()

    out = needs.copy()
    audit_rows: list[dict] = []

    # Index needs by (sku_id, ubicacion)
    key_cols = ["sku_id", "ubicacion"]
    out_idx = out.set_index(key_cols)

    for _, c in candidates.iterrows():
        imp_key = (c["sku_id_importado"], c["ubicacion"])
        nac_key = (c["sku_id_nacional"], c["ubicacion"])

        # Zero imp
        if imp_key in out_idx.index:
            prev_imp = float(out_idx.at[imp_key, "necesidad"])
            out_idx.at[imp_key, "necesidad"] = 0.0
            out_idx.at[imp_key, "dispara"] = False
        else:
            prev_imp = 0.0

        # Add nac
        transferred = float(c["demanda_transferida_a_nacional"])
        if nac_key in out_idx.index:
            prev_nac = float(out_idx.at[nac_key, "necesidad"])
            new_nac = prev_nac + transferred
            out_idx.at[nac_key, "necesidad"] = new_nac
            out_idx.at[nac_key, "dispara"] = new_nac > 0
        else:
            # Construct a minimal new row preserving schema columns.
            new_row = {col: 0.0 for col in out.columns if col not in key_cols}
            new_row["necesidad"] = transferred
            new_row["dispara"] = transferred > 0
            new_row_df = pd.DataFrame([{**dict(zip(key_cols, nac_key)), **new_row}])
            new_row_df = new_row_df.set_index(key_cols)
            out_idx = pd.concat([out_idx, new_row_df])
            prev_nac = 0.0

        audit_rows.append({
            "sku_id_importado": c["sku_id_importado"],
            "sku_id_nacional": c["sku_id_nacional"],
            "ubicacion": c["ubicacion"],
            "necesidad_original_importado": prev_imp,
            "factor_conversion": c["factor_conversion"],
            "demanda_transferida": transferred,
            "necesidad_nacional_previa": prev_nac,
            "necesidad_nacional_post": prev_nac + transferred,
        })

    out = out_idx.reset_index()
    audit = pd.DataFrame(audit_rows)
    log.info("homologation.operational.applied", rows=len(audit))
    return out, audit


def _empty_candidates() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        "sku_id_importado", "sku_id_nacional", "ubicacion",
        "necesidad_importado", "factor_conversion",
        "demanda_transferida_a_nacional", "stock_nacional_total",
        "trigger_cumple", "trigger_cds_evaluados",
    ])
