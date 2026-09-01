-- Full load: la tabla completa se vuelca en cada corrida.
-- Antes era incremental (20 dias sobre dt_in mas las conciliaciones
-- recientes), pero ese modo no propagaba las bajas: las filas anuladas en
-- MySis quedaban vivas en ClickHouse. Con 117 mil filas el volcado completo
-- es barato, deja la tabla identica al origen y ademas elimina la necesidad
-- del OR sobre mstr_concilia (la conciliacion tardaba hasta 553 dias en
-- llegar y se perdia fuera de la ventana). El exporter hace el reemplazo
-- atomico.
SELECT
*
FROM mstr_pedidos_creditos
