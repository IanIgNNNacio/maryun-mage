-- Full load: la tabla completa se vuelca en cada corrida.
-- Antes era incremental (ventana de 20 dias sobre dt_in), pero ese modo no
-- propagaba las bajas: las filas anuladas en MySis quedaban vivas en
-- ClickHouse. Con 40 mil filas el volcado completo es barato y deja la
-- tabla identica al origen. El exporter hace el reemplazo atomico.
SELECT
*
FROM mstr_pagos
