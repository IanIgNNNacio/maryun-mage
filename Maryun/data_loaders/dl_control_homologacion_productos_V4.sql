SELECT
  sku_id_importado,
  sku_id_nacional,
  factor_conversion,
  usar_analitico,
  usar_operacional,
  vigente_desde,
  vigente_hasta,
  responsable
FROM logistica_v2.logistica_homologacion_productos FINAL
WHERE (vigente_desde IS NULL OR vigente_desde <= toDate(now('America/Santiago')))
  AND (vigente_hasta IS NULL OR vigente_hasta >= toDate(now('America/Santiago')))




