-- Demanda mensual por (sku, sucursal).
-- Usa `entregado` como fecha de demanda (día real de despacho).
-- Usa v.sku directamente como identificador (código operacional).
-- `:history_start` inclusivo, `:history_end` exclusivo (primer día del mes abierto).
SELECT
    v.sku                                    AS sku_id,
    UPPER(TRIM(v.sucursal))                  AS ubicacion,
    DATE_FORMAT(v.entregado, '%Y-%m-01')     AS mes,
    SUM(v.qty)                               AS demanda
FROM reporte_ventas_completo v
WHERE v.entregado >= :history_start
  AND v.entregado <  :history_end
  AND v.qty        > 0
  AND v.sku        IS NOT NULL
  AND TRIM(v.sku)  != ''
  AND v.sucursal NOT IN ('CONSUMOS INTERNOS', 'PENDIENTES', 'ADMINISTRACION',
                         'MUESTRA SIN RETORNO', 'INVENTARIO STGO', 'BORDADOS',
                         'CARDONAL')
GROUP BY v.sku,
         UPPER(TRIM(v.sucursal)),
         DATE_FORMAT(v.entregado, '%Y-%m-01');
