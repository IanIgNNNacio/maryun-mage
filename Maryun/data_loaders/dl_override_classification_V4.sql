SELECT
  sku_id,
  ubicacion,
  abc_override,
  xyz_override,
  motivo,
  responsable
FROM logistica_v2.logistica_override_clasificacion FINAL
WHERE activo = 1
 AND vigente_desde <= toDate(now('America/Santiago'))
 AND vigente_hasta >= toDate(now('America/Santiago'))
--   AND vigente_desde <= toDate('{process_date}')
--   AND vigente_hasta >= toDate('{process_date}')




