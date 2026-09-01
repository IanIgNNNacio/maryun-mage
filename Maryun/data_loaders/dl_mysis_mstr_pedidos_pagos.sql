-- Incremental: filas nuevas de los ultimos 20 dias
-- + filas antiguas cuya conciliacion es reciente (la conciliacion ocurre
--   en promedio 172 dias despues de dt_in, hasta 556 dias).
SELECT *
FROM mstr_pedidos_pagos
WHERE dt_in >= NOW() - INTERVAL 20 DAY
   OR concilia_id IN (
        SELECT id FROM mstr_concilia WHERE dt_in >= NOW() - INTERVAL 20 DAY
      )
