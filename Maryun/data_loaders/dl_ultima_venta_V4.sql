SELECT
  toString(v.sku) AS sku_id,
  upper(trimBoth(toString(v.sucursal))) AS ubicacion,
  max(toDateTime(v.entregado)) AS ultima_venta
FROM ventas_mysis_2 v
WHERE v.entregado IS NOT NULL
  AND toFloat64(v.qty) > 0
  AND v.sku IS NOT NULL
  AND trimBoth(toString(v.sku)) != ''
  AND v.sucursal NOT IN (
    'CONSUMOS INTERNOS', 'PENDIENTES', 'ADMINISTRACION',
    'MUESTRA SIN RETORNO', 'INVENTARIO STGO', 'BORDADOS',
    'CARDONAL'
  )
GROUP BY
  toString(v.sku),
  upper(trimBoth(toString(v.sucursal)))
SETTINGS final = 1