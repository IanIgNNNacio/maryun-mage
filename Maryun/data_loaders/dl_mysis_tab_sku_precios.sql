-- Full load: la tabla completa se vuelca en cada corrida.
-- tab_sku_precios no tiene PK utilizable (hid se repite por sku/precio_id),
-- asi que el modo incremental no permitiria deduplicar ni propagar bajas:
-- las filas eliminadas o corregidas en MySis quedarian vivas en ClickHouse.
-- El exporter carga en staging y hace el reemplazo atomico con EXCHANGE TABLES.
SELECT
*
FROM tab_sku_precios
