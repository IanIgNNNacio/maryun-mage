SELECT
  toString(p.sku) AS sku_id,
  upper(trimBoth(toString(b.bodega_desc))) AS ubicacion,
  sum(toFloat64(a.qty)) AS qty
FROM mysis_almacenaje a
INNER JOIN mysis_tab_bodegas b ON a.bodega_id = b.bodega_id
INNER JOIN mysis_tab_sku p ON a.sku = p.sku
WHERE toFloat64(a.qty) > 0
  AND b.bodega_desc NOT IN ('CONSUMOS INTERNOS','PENDIENTES','ADMINISTRACION',
    'MUESTRA SIN RETORNO','INVENTARIO STGO','BORDADOS','CARDONAL')
GROUP BY toString(p.sku), upper(trimBoth(toString(b.bodega_desc)))
SETTINGS final = 1