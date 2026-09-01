SELECT
  sku_3_0,
  sku_id,
  ubicacion,
  abc_modelo,
  xyz_modelo,
  clase_final,
  score_automatizacion,
  clase_automatizacion
FROM logistica_v2.logistica_clasificacion_precomputada FINAL
WHERE activo = 1
  AND (vigente_desde IS NULL OR vigente_desde <= toDate(now('America/Santiago')))
  AND (vigente_hasta IS NULL OR vigente_hasta >= toDate(now('America/Santiago')))


