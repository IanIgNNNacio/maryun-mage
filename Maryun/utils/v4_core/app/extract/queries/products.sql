-- Catálogo de productos activos (sale_ok = 1).
-- Se usa p.sku (código de barra / código operacional) como identificador,
-- no el sku_id interno de la BD.
SELECT
    p.sku                                        AS sku_id,
    p.sku                                        AS sku,
    p.nombre                                     AS nombre,
    p.descripcion                                AS variante,
    p.color                                      AS color,
    p.talla                                      AS talla,
    f.familia_descripcion                        AS familia,
    m.marca_descripcion                          AS marca,
    t.tipo_descripcion                           AS tipo,
    COALESCE(p.costo, 0)                         AS costo,
    LOWER(COALESCE(p.procedencia, ''))           AS procedencia,
    CASE WHEN p.critico     = 1 THEN 1 ELSE 0 END AS critico,
    CASE WHEN p.sale_ok     = 1 THEN 1 ELSE 0 END AS sale_ok,
    CASE WHEN p.purchase_ok = 1 THEN 1 ELSE 0 END AS purchase_ok
FROM tab_sku p
LEFT JOIN tab_familias f ON p.familia_id = f.familia_id
LEFT JOIN tab_marcas   m ON p.marca_id   = m.marca_id
LEFT JOIN tab_tipos    t ON p.tipo_id    = t.tipo_id
WHERE p.sale_ok = 1;
