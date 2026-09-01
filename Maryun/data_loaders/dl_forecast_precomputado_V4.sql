SELECT
  sku_id,
  ubicacion,
  mes,
  forecast_modelo,
  forecast_override,
  forecast_final,
  forecast_fue_forzado,
  motivo_override,
  responsable_override
FROM logistica_v2.logistica_forecast_precomputado FINAL
WHERE activo = 1
  AND (vigente_desde IS NULL OR vigente_desde <= toDate(now('America/Santiago')))
  AND (vigente_hasta IS NULL OR vigente_hasta >= toDate(now('America/Santiago')))





