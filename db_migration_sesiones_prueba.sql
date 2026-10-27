-- ============================================================================
-- db_migration_sesiones_prueba.sql
-- Marca las conversaciones de PRUEBA (botón "Probar chat" del docente) para
-- no mezclarlas con las de alumnos ni contarlas en estadísticas.
-- Aplicar con psql (respaldar antes asistente_sesiones). Idempotente.
-- ============================================================================

BEGIN;

ALTER TABLE asistente_sesiones ADD COLUMN IF NOT EXISTS es_prueba   BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE asistente_sesiones ADD COLUMN IF NOT EXISTS probado_por INTEGER REFERENCES usuarios(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_asistente_sesiones_prueba ON asistente_sesiones(es_prueba);

COMMIT;
