SELECT
  toString(p.sku) AS sku_id,
  toString(p.sku) AS sku,
  toString(p.nombre) AS nombre,
  toString(ifNull(p.descripcion, '')) AS variante,
  toString(ifNull(p.color, '')) AS color,
  toString(ifNull(p.talla, '')) AS talla,
  toString(ifNull(f.familia_descripcion, '')) AS familia,
  toString(ifNull(m.marca_descripcion, '')) AS marca,
  toString(ifNull(t.tipo_descripcion, '')) AS tipo,
  toFloat64(ifNull(p.costo, 0)) AS costo,
  lower(toString(ifNull(p.procedencia, ''))) AS procedencia,
  if(p.critico = 1, 1, 0) AS critico,
  if(p.sale_ok = 1, 1, 0) AS sale_ok,
  if(p.purchase_ok = 1, 1, 0) AS purchase_ok
-- FINAL: dedup de ReplacingMergeTree (catalogo re-ingestado no debe multiplicar filas).
FROM mysis_tab_sku p FINAL
LEFT JOIN mysis_tab_familias f FINAL ON p.familia_id = f.familia_id
LEFT JOIN mysis_tab_marcas m FINAL ON p.marca_id = m.marca_id
LEFT JOIN mysis_tab_tipos t FINAL ON p.tipo_id = t.tipo_id
WHERE p.sale_ok = 1




