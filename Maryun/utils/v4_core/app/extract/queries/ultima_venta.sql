-- Última fecha de venta por SKU × sucursal.
-- Usado por el dashboard para clasificar inmovilizado/sobrestock según
-- días transcurridos desde la última venta registrada.
SELECT
    v.sku                            AS sku_id,
    UPPER(TRIM(v.sucursal))          AS ubicacion,
    MAX(v.entregado)                 AS ultima_venta
FROM reporte_ventas_completo v
WHERE v.entregado IS NOT NULL
  AND v.qty       > 0
  AND v.sku       IS NOT NULL
  AND TRIM(v.sku) != ''
  AND v.sucursal NOT IN ('CONSUMOS INTERNOS', 'PENDIENTES', 'ADMINISTRACION',
                         'MUESTRA SIN RETORNO', 'INVENTARIO STGO', 'BORDADOS',
                         'CARDONAL')
GROUP BY v.sku, UPPER(TRIM(v.sucursal));
