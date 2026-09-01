-- Demanda mensual por (sku, sucursal). Ventana: SOLO meses cerrados, dia-independiente
-- (igual cualquier dia que se ejecute):
--   start = 2024-01-01 (temporal.history_start), inclusivo.
--   end   = inicio del mes actual, EXCLUSIVO -> el mes en curso NUNCA entra.
-- Mismo criterio que demanda_mensual (sin tolerancia de dias).
SELECT
  toString(v.sku) AS sku_id,
  upper(trimBoth(toString(v.sucursal))) AS ubicacion,
  toStartOfMonth(toDate(v.entregado)) AS mes,
  sum(toFloat64(v.qty)) AS demanda
FROM ventas_mysis_2 v
WHERE v.entregado >= toDate('2024-01-01')
  AND v.entregado < toStartOfMonth(toDate(now('America/Santiago')))
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
  upper(trimBoth(toString(v.sucursal))),
  toStartOfMonth(toDate(v.entregado))
SETTINGS final = 1
