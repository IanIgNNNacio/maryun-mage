CREATE TABLE IF NOT EXISTS dwh.rcv_incidencia (
  id            bigserial PRIMARY KEY,
  documento_id  text NOT NULL,
  dimension     text NOT NULL,
  regla         text NOT NULL,
  detalle       text,
  monto         bigint NOT NULL DEFAULT 0,
  periodo       text,
  detectado_en  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS rcv_incidencia_periodo ON dwh.rcv_incidencia (periodo);
CREATE INDEX IF NOT EXISTS rcv_incidencia_regla   ON dwh.rcv_incidencia (regla);
COMMENT ON TABLE dwh.rcv_incidencia IS
  'Documentos en cuarentena: no se publican en las tablas del DWH y se ven como indicador. Detener la carga entera por un documento raro es peor que apartarlo.';
GRANT SELECT ON dwh.rcv_incidencia TO dwh_lector;
