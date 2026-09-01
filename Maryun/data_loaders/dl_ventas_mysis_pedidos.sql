-- Parámetros esperados (YYYY-MM-DD):
-- {{ start_date }}  -- fecha inicio inclusive
-- {{ end_date }}    -- fecha fin inclusive

SELECT 
  o.pid,
  o.padre,
  o.voucher AS shopify,
  b.bodega_desc AS sucursal,
  LTRIM(RTRIM(p.rso)) AS rso,
  p.rut,
  DATE_FORMAT(o.dt_in, '%Y-%m-%d') AS creado,
  DATE_FORMAT(o.dt_picking, '%Y-%m-%d %H:%i') AS dt_picking,
  DATE_FORMAT(o.dt_pk_out, '%Y-%m-%d %H:%i') AS facturar,
  DATE_FORMAT(o.dt_out, '%Y-%m-%d') AS facturado,
  DATE_FORMAT(o.dt_cierre, '%Y-%m-%d') AS confirmado,
  DATE_FORMAT(o.entregado, '%Y-%m-%d') AS entregado,
  DATE_FORMAT(o.dt_vencimiento, '%Y-%m-%d') AS vencimiento,
  o.guia,
  o.factura,
  ROUND(o.neto) AS neto,
  ROUND(o.iva) AS iva,
  ROUND(o.total) AS total,
  ROUND(o.deuda) AS deuda,
  a.sku,
  s.nombre,
  s.descripcion,
  FORMAT(a.qty, 0, 'es_CL') AS qty,
  FORMAT((a.entrega + a.picking), 0, 'es_CL') AS picking,
  FORMAT(a.pu, 2, 'es_CL') AS pu,
  a.tramo,
  FORMAT(a.pmp, 2, 'es_CL') AS pmp,
  FORMAT((a.entrega + a.picking) * a.pmp, 2, 'es_CL') AS totaliza_pmp,
  FORMAT((a.entrega + a.picking) * a.pu, 2, 'es_CL') AS totaliza_vta,
  FORMAT(((a.entrega + a.picking) * a.pu) - ((a.entrega + a.picking) * a.pmp), 2, 'es_CL') AS margen,
  (SELECT m.tipo
     FROM mstr_matrices m
    WHERE m.rut = p.rut
      AND m.sku = a.sku
      AND DATE(o.dt_out) >= m.desde
    ORDER BY m.desde DESC
    LIMIT 1) AS tipo_convenio,
  CASE
    WHEN (
      (SELECT IFNULL(a.pmp - m.convenio, 0)
         FROM mstr_matrices m
        WHERE m.rut = p.rut
          AND m.sku = a.sku
          AND DATE(o.dt_out) >= m.desde
        ORDER BY m.desde DESC
        LIMIT 1) >= 0
    ) THEN FORMAT(
      (SELECT IFNULL(a.pmp - m.convenio, 0)
         FROM mstr_matrices m
        WHERE m.rut = p.rut
          AND m.sku = a.sku
          AND DATE(o.dt_out) >= m.desde
        ORDER BY m.desde DESC
        LIMIT 1),
      2, 'es_CL'
    )
    ELSE FORMAT(
      (SELECT IFNULL(m.distribuidor - m.convenio, 0)
         FROM mstr_matrices m
        WHERE m.rut = p.rut
          AND m.sku = a.sku
          AND DATE(o.dt_out) >= m.desde
        ORDER BY m.desde DESC
        LIMIT 1),
      2, 'es_CL'
    )
  END AS diferencia,
  CASE
    WHEN (
      (SELECT IFNULL(a.pmp - m.convenio, 0)
         FROM mstr_matrices m
        WHERE m.rut = p.rut
          AND m.sku = a.sku
          AND DATE(o.dt_out) >= m.desde
        ORDER BY m.desde DESC
        LIMIT 1) >= 0
    ) THEN FORMAT(
      (SELECT IFNULL(a.pmp - m.convenio, 0)
         FROM mstr_matrices m
        WHERE m.rut = p.rut
          AND m.sku = a.sku
          AND DATE(o.dt_out) >= m.desde
        ORDER BY m.desde DESC
        LIMIT 1) * (a.entrega + a.picking),
      2, 'es_CL'
    )
    ELSE FORMAT(
      IFNULL(
        (SELECT IFNULL(m.distribuidor - m.convenio, 0)
           FROM mstr_matrices m
          WHERE m.rut = p.rut
            AND m.sku = a.sku
            AND DATE(o.dt_out) >= m.desde
          ORDER BY m.desde DESC
          LIMIT 1), 0
      ) * (a.entrega + a.picking),
      2, 'es_CL'
    )
  END AS totaliza_diferencia,
  CASE
    WHEN (
      (SELECT IFNULL(a.pmp - m.convenio, 0)
         FROM mstr_matrices m
        WHERE m.rut = p.rut
          AND m.sku = a.sku
          AND DATE(o.dt_out) >= m.desde
        ORDER BY m.desde DESC
        LIMIT 1) >= 0
    ) THEN FORMAT(
      (SELECT IFNULL(a.pmp - m.convenio, 0)
         FROM mstr_matrices m
        WHERE m.rut = p.rut
          AND m.sku = a.sku
          AND DATE(o.dt_out) >= m.desde
        ORDER BY m.desde DESC
        LIMIT 1) * (a.entrega + a.picking)
      + ((a.entrega + a.picking) * a.pu)
      - ((a.entrega + a.picking) * a.pmp),
      2, 'es_CL'
    )
    ELSE FORMAT(0, 2, 'es_CL')
  END AS margen_diferencia,
  FORMAT(
    (
      ((a.entrega + a.picking) * a.pu) - ((a.entrega + a.picking) * a.pmp)
      +
      CASE
        WHEN (
          (SELECT IFNULL(a.pmp - m.convenio, 0)
             FROM mstr_matrices m
            WHERE m.rut = p.rut
              AND m.sku = a.sku
              AND DATE(o.dt_out) >= m.desde
            ORDER BY m.desde DESC
            LIMIT 1) >= 0
        )
        THEN (SELECT IFNULL(a.pmp - m.convenio, 0)
                FROM mstr_matrices m
               WHERE m.rut = p.rut
                 AND m.sku = a.sku
                 AND DATE(o.dt_out) >= m.desde
               ORDER BY m.desde DESC
               LIMIT 1) * (a.entrega + a.picking)
        ELSE IFNULL(
               (SELECT IFNULL(m.distribuidor - m.convenio, 0)
                  FROM mstr_matrices m
                 WHERE m.rut = p.rut
                   AND m.sku = a.sku
                   AND DATE(o.dt_out) >= m.desde
                 ORDER BY m.desde DESC
                 LIMIT 1), 0
             ) * (a.entrega + a.picking)
      END
    ),
    2, 'es_CL'
  ) AS margen_final,
  CASE
    WHEN u.tcomision = 'TV' THEN FORMAT((a.entrega + a.picking) * a.pu, 2, 'es_CL')
    WHEN u.tcomision = 'DM'
     AND (SELECT IFNULL(a.pmp - m.convenio, 0) FROM mstr_matrices m
           WHERE m.rut = p.rut AND m.sku = a.sku AND DATE(o.dt_out) >= m.desde
           ORDER BY m.desde DESC LIMIT 1) <> 0
    THEN FORMAT(
      (SELECT IFNULL(a.pmp - m.convenio, 0) FROM mstr_matrices m
        WHERE m.rut = p.rut AND m.sku = a.sku AND DATE(o.dt_out) >= m.desde
        ORDER BY m.desde DESC LIMIT 1) * (a.entrega + a.picking)
      + ((a.entrega + a.picking) * a.pu) - ((a.entrega + a.picking) * a.pmp),
      2, 'es_CL'
    )
    WHEN u.tcomision = 'DM'
     AND (SELECT IFNULL(a.pmp - m.convenio, 0) FROM mstr_matrices m
           WHERE m.rut = p.rut AND m.sku = a.sku AND DATE(o.dt_out) >= m.desde
           ORDER BY m.desde DESC LIMIT 1) IS NULL
    THEN FORMAT(((a.entrega + a.picking) * a.pu) - ((a.entrega + a.picking) * a.pmp), 2, 'es_CL')
    ELSE 0
  END AS tipo_comision,
  u.tcomision,
  o.observacion,
  CONCAT(u.user_name, ' ', u.user_apellido) AS vendedor,
  u.user_rut AS rut_vendedor,
  '' AS remunera,
  p.comuna,
  p.direccion,
  s.area,
  s.procedencia,
  (SELECT marca_descripcion FROM tab_marcas WHERE marca_id = s.marca_id) AS marca,
  (SELECT familia_descripcion FROM tab_familias WHERE familia_id = s.familia_id) AS familia,
  (SELECT tipo_descripcion FROM tab_tipos WHERE tipo_id = s.tipo_id) AS tipo
FROM
  mstr_pedidos o
  JOIN mstr_pedidos_aux a ON o.pid = a.pid
  JOIN tab_sku s ON a.sku = s.sku
  JOIN tab_clientes p ON o.cliente_id = p.cliente_id
  JOIN tab_users u ON u.user_id = o.usr_in
  JOIN tab_bodegas b ON o.sucursal_id = b.bodega_id
WHERE
  o.factura IS NOT NULL
  AND o.direccion_id <> 0
  AND o.dt_out >= '{{ start_date }}'
  AND o.dt_out <  DATE_ADD('{{ end_date }}', INTERVAL 1 DAY)

UNION ALL

SELECT 
  o1.pid,
  o1.padre,
  '' AS shopify,
  b1.bodega_desc AS sucursal,
  LTRIM(RTRIM(p1.rso)) AS rso,
  p1.rut,
  DATE_FORMAT(o1.dt_in, '%Y-%m-%d') AS creado,
  DATE_FORMAT(o1.dt_picking, '%Y-%m-%d %H:%i') AS dt_picking,
  '' AS facturar,
  DATE_FORMAT(o1.dt_out, '%Y-%m-%d') AS facturado,
  DATE_FORMAT(o1.dt_cierre, '%Y-%m-%d') AS confirmado,
  DATE_FORMAT(o1.entregado, '%Y-%m-%d') AS entregado,
  DATE_FORMAT(o1.dt_vencimiento, '%Y-%m-%d') AS vencimiento,
  o1.guia,
  o1.factura,
  ROUND(o1.neto) * -1 AS neto,
  ROUND(o1.iva) * -1 AS iva,
  ROUND(o1.total) * -1 AS total,
  0 AS deuda,
  a1.sku,
  s1.nombre,
  s1.descripcion,
  FORMAT(a1.entrega, 0, 'es_CL') AS qty,
  FORMAT(a1.entrega, 0, 'es_CL') AS picking,
  FORMAT(a1.pu * -1, 2, 'es_CL') AS pu,
  a1.tramo,
  (FORMAT(a1.pmp, 2, 'es_CL') * -1) AS pmp,
  FORMAT((a1.entrega) * a1.pmp * -1, 2, 'es_CL') AS totaliza_pmp,
  FORMAT((a1.entrega) * a1.pu * -1, 2, 'es_CL') AS totaliza_vta,
  FORMAT(((a1.entrega) * a1.pu * -1) - ((a1.entrega) * a1.pmp * -1), 2, 'es_CL') AS margen,
  (SELECT m.tipo
     FROM mstr_matrices m
    WHERE m.rut = p1.rut
      AND m.sku = a1.sku
      AND DATE(o1.dt_out) >= m.desde
    ORDER BY m.desde DESC
    LIMIT 1) AS tipo_convenio,
  CASE
    WHEN (
      (SELECT IFNULL(a1.pmp - m.convenio, 0)
         FROM mstr_matrices m
        WHERE m.rut = p1.rut
          AND m.sku = a1.sku
          AND DATE(o1.dt_out) >= m.desde
        ORDER BY m.desde DESC
        LIMIT 1) >= 0
    ) THEN FORMAT(
      (SELECT IFNULL(a1.pmp - m.convenio * -1, 0)
         FROM mstr_matrices m
        WHERE m.rut = p1.rut
          AND m.sku = a1.sku
          AND DATE(o1.dt_out) >= m.desde
        ORDER BY m.desde DESC
        LIMIT 1),
      2, 'es_CL'
    )
    ELSE FORMAT(
      (SELECT IFNULL(m.distribuidor - m.convenio * -1, 0)
         FROM mstr_matrices m
        WHERE m.rut = p1.rut
          AND m.sku = a1.sku
          AND DATE(o1.dt_out) >= m.desde
        ORDER BY m.desde DESC
        LIMIT 1),
      2, 'es_CL'
    )
  END AS diferencia,
  CASE
    WHEN (
      (SELECT IFNULL(a1.pmp - m.convenio, 0)
         FROM mstr_matrices m
        WHERE m.rut = p1.rut
          AND m.sku = a1.sku
          AND DATE(o1.dt_out) >= m.desde
        ORDER BY m.desde DESC
        LIMIT 1) >= 0
    ) THEN FORMAT(
      (SELECT IFNULL((a1.pmp - m.convenio) * -1, 0)
         FROM mstr_matrices m
        WHERE m.rut = p1.rut
          AND m.sku = a1.sku
          AND DATE(o1.dt_out) >= m.desde
        ORDER BY m.desde DESC
        LIMIT 1) * (a1.entrega),
      2, 'es_CL'
    )
    ELSE FORMAT(
      IFNULL(
        (SELECT IFNULL((m.distribuidor - m.convenio) * -1, 0)
           FROM mstr_matrices m
          WHERE m.rut = p1.rut
            AND m.sku = a1.sku
            AND DATE(o1.dt_out) >= m.desde
          ORDER BY m.desde DESC
          LIMIT 1), 0
      ) * (a1.entrega),
      2, 'es_CL'
    )
  END AS totaliza_diferencia,
  CASE
    WHEN (
      (SELECT IFNULL(a1.pmp - m.convenio, 0)
         FROM mstr_matrices m
        WHERE m.rut = p1.rut
          AND m.sku = a1.sku
          AND DATE(o1.dt_out) >= m.desde
        ORDER BY m.desde DESC
        LIMIT 1) >= 0
    ) THEN FORMAT(
      (SELECT IFNULL((a1.pmp - m.convenio) * -1, 0)
         FROM mstr_matrices m
        WHERE m.rut = p1.rut
          AND m.sku = a1.sku
          AND DATE(o1.dt_out) >= m.desde
        ORDER BY m.desde DESC
        LIMIT 1) * (a1.entrega)
      + ((a1.entrega) * a1.pu * -1)
      - ((a1.entrega) * a1.pmp * -1),
      2, 'es_CL'
    )
    ELSE FORMAT(0, 2, 'es_CL')
  END AS margen_diferencia,
  FORMAT(
    (
      ((a1.entrega) * a1.pu * -1) - ((a1.entrega) * a1.pmp * -1)
      +
      CASE
        WHEN (
          (SELECT IFNULL(a1.pmp - m.convenio, 0)
             FROM mstr_matrices m
            WHERE m.rut = p1.rut
              AND m.sku = a1.sku
              AND DATE(o1.dt_out) >= m.desde
            ORDER BY m.desde DESC
            LIMIT 1) >= 0
        )
        THEN (SELECT IFNULL((a1.pmp - m.convenio) * -1, 0)
                FROM mstr_matrices m
               WHERE m.rut = p1.rut
                 AND m.sku = a1.sku
                 AND DATE(o1.dt_out) >= m.desde
               ORDER BY m.desde DESC
               LIMIT 1) * (a1.entrega)
        ELSE IFNULL(
               (SELECT IFNULL((m.distribuidor - m.convenio) * -1, 0)
                  FROM mstr_matrices m
                 WHERE m.rut = p1.rut
                   AND m.sku = a1.sku
                   AND DATE(o1.dt_out) >= m.desde
                 ORDER BY m.desde DESC
                 LIMIT 1), 0
             ) * (a1.entrega)
      END
    ),
    2, 'es_CL'
  ) AS margen_final,
  CASE
    WHEN u1.tcomision = 'TV' THEN FORMAT((a1.entrega) * a1.pu * -1, 2, 'es_CL')
    WHEN u1.tcomision = 'DM'
     AND (SELECT IFNULL(a1.pmp - m.convenio * -1, 0) FROM mstr_matrices m
           WHERE m.rut = p1.rut AND m.sku = a1.sku AND DATE(o1.dt_out) >= m.desde
           ORDER BY m.desde DESC LIMIT 1) <> 0
    THEN FORMAT(
      (SELECT IFNULL(a1.pmp - m.convenio * -1, 0) FROM mstr_matrices m
        WHERE m.rut = p1.rut AND m.sku = a1.sku AND DATE(o1.dt_out) >= m.desde
        ORDER BY m.desde DESC LIMIT 1) * (a1.entrega)
      + ((a1.entrega) * a1.pu * -1)
      - ((a1.entrega) * a1.pmp * -1),
      2, 'es_CL'
    )
    WHEN u1.tcomision = 'DM'
     AND (SELECT IFNULL(a1.pmp - m.convenio * -1, 0) FROM mstr_matrices m
           WHERE m.rut = p1.rut AND m.sku = a1.sku AND DATE(o1.dt_out) >= m.desde
           ORDER BY m.desde DESC LIMIT 1) IS NULL
    THEN FORMAT(((a1.entrega) * a1.pu * -1) - ((a1.entrega) * a1.pmp * -1), 2, 'es_CL')
    ELSE 0
  END AS tipo_comision,
  u1.tcomision,
  o1.observacion,
  CONCAT(u1.user_name, ' ', u1.user_apellido) AS vendedor,
  u1.user_rut AS rut_vendedor,
  '' AS remunera,
  p1.comuna,
  p1.direccion,
  s1.area,
  s1.procedencia,
  (SELECT marca_descripcion FROM tab_marcas WHERE marca_id = s1.marca_id) AS marca,
  (SELECT familia_descripcion FROM tab_familias WHERE familia_id = s1.familia_id) AS familia,
  (SELECT tipo_descripcion FROM tab_tipos WHERE tipo_id = s1.tipo_id) AS tipo
FROM
  mstr_nc o1
  JOIN mstr_nc_aux a1 ON o1.pid = a1.pid
  JOIN tab_sku s1 ON a1.sku = s1.sku
  JOIN tab_clientes p1 ON o1.cliente_id = p1.cliente_id
  JOIN tab_users u1 ON u1.user_id = o1.usr_in
  JOIN tab_bodegas b1 ON o1.sucursal_id = b1.bodega_id
WHERE
  o1.direccion_id <> 0
  AND o1.dt_out >= '{{ start_date }}'
  AND o1.dt_out <  DATE_ADD('{{ end_date }}', INTERVAL 1 DAY)

UNION ALL

SELECT 
  a.id AS pid,
  0 AS padre,
  '' AS shopify,
  b.bodega_desc AS sucursal,
  '' AS rso,
  '' AS rut,
  a.desde AS creado,
  a.desde AS dt_picking,
  a.desde AS facturar,
  a.desde AS facturado,
  a.desde AS confirmado,
  a.desde AS entregado,
  a.desde AS vencimiento,
  '' AS guia,
  '' AS factura,
  0 AS neto,
  0 AS iva,
  0 AS total,
  0 AS deuda,
  '' AS sku,
  a.descripcion AS nombre,
  '' AS descripcion,
  1 AS qty,
  1 AS picking,
  FORMAT(ROUND(a.diferencia), 2, 'es_CL') AS pu,
  '' AS tramo,
  0 AS pmp,
  0 AS totaliza_pmp,
  0 AS totaliza_vta,
  FORMAT(ROUND(a.diferencia), 2, 'es_CL') AS margen,
  '' AS tipo_convenio,
  0 AS diferencia,
  FORMAT(0, 2, 'es_CL') AS totaliza_diferencia,
  0 AS margen_diferencia,
  FORMAT(ROUND(a.diferencia), 2, 'es_CL') AS margen_final,
  FORMAT(ROUND(a.diferencia), 2, 'es_CL') AS tipo_comision,
  u.tcomision,
  '' AS observacion,
  CONCAT(u.user_name,' ',u.user_apellido) AS vendedor,
  a.rut AS rut_vendedor,
  a.remuneracion AS remunera,
  '' AS comuna,
  '' AS direccion,
  '' AS area,
  '' AS procedencia,
  '' AS marca,
  '' AS familia,
  '' AS tipo
FROM
  mstr_anexo a
  JOIN tab_bodegas b ON a.sucursal_id = b.bodega_id
  JOIN tab_users u ON a.rut = u.user_rut
WHERE
  a.desde >= '{{ start_date }}'
  AND a.desde <  DATE_ADD('{{ end_date }}', INTERVAL 1 DAY)
ORDER BY 4 DESC, 1;