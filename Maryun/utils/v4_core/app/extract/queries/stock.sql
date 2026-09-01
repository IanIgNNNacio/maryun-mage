-- Stock físico por (sku, ubicacion) desde tabla de almacenaje.
-- `almacenaje` representa unidades realmente en estante (no incluye compras en tránsito).
-- La tabla `stock` original usa `qty_available` que mezcla stock físico con OC pendientes
-- y produce falsos positivos en los movimientos de inmovilizado.
-- Excluye ubicaciones internas que no son sucursales operativas.
SELECT
    p.sku                           AS sku_id,
    UPPER(TRIM(b.bodega_desc))      AS ubicacion,
    SUM(a.qty)                      AS qty
FROM almacenaje a
JOIN tab_bodegas b ON a.bodega_id  = b.bodega_id
JOIN tab_sku     p ON a.sku        = p.sku
WHERE a.qty > 0
  AND b.bodega_desc NOT IN (
      'CONSUMOS INTERNOS', 'PENDIENTES', 'ADMINISTRACION',
      'MUESTRA SIN RETORNO', 'INVENTARIO STGO', 'BORDADOS',
      'CARDONAL'
  )
GROUP BY p.sku, UPPER(TRIM(b.bodega_desc));
