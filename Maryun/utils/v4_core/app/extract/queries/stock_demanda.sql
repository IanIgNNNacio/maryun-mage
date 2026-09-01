-- Último ingreso a bodega por SKU × sucursal y última compra global.
-- Usado por el módulo de Ofertas & Remates para calcular días en bodega.
SELECT
  b.bodega_desc                                          AS sucursal,
  a.sku                                                  AS sku_id,
  DATE_FORMAT(uis.ultimo_ingreso_sucursal, '%Y-%m-%d')   AS ultimo_ingreso_sucursal,
  DATE_FORMAT(ucg.ultima_compra_global,    '%Y-%m-%d')   AS ultima_compra_global
FROM almacenaje a
JOIN tab_bodegas b
  ON b.bodega_id = a.bodega_id
JOIN tab_sku s
  ON s.sku = a.sku
 AND s.oa = 1
LEFT JOIN (
  SELECT
    i.sucursal_id,
    h.sku,
    MAX(i.dt_cierre) AS ultimo_ingreso_sucursal
  FROM mstr_ingresos i
  JOIN mstr_ingresos_aux h
    ON h.hid = i.hid
  WHERE i.dt_cierre IS NOT NULL
  GROUP BY i.sucursal_id, h.sku
) uis
  ON uis.sucursal_id = a.bodega_id
 AND uis.sku         = a.sku
LEFT JOIN (
  SELECT
    h.sku,
    MAX(i.dt_valida) AS ultima_compra_global
  FROM mstr_oc i
  JOIN mstr_oc_aux h
    ON h.oc_id = i.oc_id
  WHERE i.dt_valida IS NOT NULL
  GROUP BY h.sku
) ucg
  ON ucg.sku = a.sku
ORDER BY sucursal, a.sku;
